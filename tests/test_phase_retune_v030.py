# -*- coding: utf-8 -*-
"""V0.3.0 周期阶段阈值重构回归测试(2026-09-24)。

覆盖四组改动,每条都对应实测依据(见 更新日志.md V0.3.0):
1. 建仓绝对量改连续式 `max(地板, 基线×百分比)`,取消「基线分档×2」的 3 倍跳变;
2. 建仓门槛 6%→12% + 低价品横盘「金额豁免」(百分比在低价段没有分辨力);
3. 启动四道锁(逐段走高 / 在售不增 / 不追高 / 金额下限);
4. 派发「高位」改真实区间位置(近 N 日最高价 ×90%),区间缺失回退旧口径;在售门槛 10%→6%;
5. 保底分改「加分制」:最终分 = min(100, 基础分 + 加分),评分重新参与取舍。
"""
import pathlib as _pl, sys as _sys          # tests/_bootstrap:保证能 import bot
_sys.path.insert(0, str(_pl.Path(__file__).resolve().parent.parent))
import os
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from bot.anomaly.cycle import _abs_req, detect_phase
from bot.anomaly.engine import AnomalyEngine, HistoryPoint, MetricSnapshot
from bot.config import AnomalyConfig

CFG = AnomalyConfig()
BASE = datetime(2026, 9, 6, 12, 0, 0)

PASS = 0
FAIL = 0


def check(name: str, cond: bool, extra: str = ""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [PASS] {name}" + (f"  ({extra})" if extra else ""))
    else:
        FAIL += 1
        print(f"  [FAIL] {name}  {extra}")


def hist(buy_seq, sell_seq, price_seq, buy_price_seq=None, step_min=5):
    n = len(buy_seq)
    start = BASE - timedelta(minutes=step_min * (n - 1))
    pts = []
    for i in range(n):
        ts = (start + timedelta(minutes=step_min * i)).strftime("%Y-%m-%d %H:%M:%S")
        pts.append(HistoryPoint(ts=ts, price=float(price_seq[i]), sell_num=int(sell_seq[i]),
                                buy_num=int(buy_seq[i]),
                                buy_price=float((buy_price_seq or price_seq)[i])))
    return pts


def snap(price=100.0, buy=1140, sell=3000, buy_price=95.0, price_30=95.0, rate_7=0.0):
    return MetricSnapshot(good_id=1, name="测试品", price=price, sell_price=price,
                          buy_price=buy_price, sell_num=sell, buy_num=buy,
                          rate_7=rate_7, rate_30=0.0, price_7=price * 0.98, price_30=price_30)


def flat(n, v):
    return [v] * n


# ---------------------------------------------------------------- 1. 连续式绝对量

def test_abs_req_formula():
    print("== ① 建仓绝对量:连续式 max(地板, 基线×百分比) ==")
    check("基线 10 单 × 12% = 1.2 → 取地板 3 单", _abs_req(CFG, 10, 12.0) == 3,
          str(_abs_req(CFG, 10, 12.0)))
    check("基线 25 单 × 12% = 3 → 3 单", _abs_req(CFG, 25, 12.0) == 3)
    check("基线 1000 单 × 12% = 120 单", _abs_req(CFG, 1000, 12.0) == 120)
    check("基线 3000 单 × 6%(派发) = 180 单", _abs_req(CFG, 3000, 6.0) == 180)
    # 旧口径在 49→50 单跨越分档时绝对量要求从 10 跳到 30(3 倍);连续式必须平滑
    check("旧「分档×2」跳变已消除(49 单与 50 单要求差 ≤1)",
          abs(_abs_req(CFG, 49, 12.0) - _abs_req(CFG, 50, 12.0)) <= 1,
          f"{_abs_req(CFG, 49, 12.0)} vs {_abs_req(CFG, 50, 12.0)}")


# ---------------------------------------------------------------- 2. 建仓

def test_accum_threshold():
    print("== ② 建仓:门槛 12% + 横盘金额豁免 ==")
    # 旧 6% 档:1000→1060 不再算建仓
    h6 = hist([1000, 1010, 1020, 1030, 1040, 1045, 1050, 1050, 1055, 1055, 1060, 1060, 1060],
              flat(13, 3000), flat(13, 100.0))
    sig6 = detect_phase(snap(buy=1060), h6, BASE, CFG)
    check("求购 +6%(旧门槛) → 不再判建仓", sig6 is None or sig6.phase != "accum",
          str(sig6.phase if sig6 else None))

    h12 = hist([1000, 1005, 1010, 1020, 1030, 1040, 1045, 1050, 1060, 1080, 1100, 1120, 1140],
               flat(13, 3000), flat(13, 100.0))
    sig12 = detect_phase(snap(buy=1140), h12, BASE, CFG)
    check("求购 +14%(≥12% 且 ≥120 单) → 判建仓",
          sig12 is not None and sig12.phase == "accum",
          str(sig12.phase if sig12 else None))
    check("门槛写进原因文案", sig12 is not None and any("12%" in r for r in sig12.reasons))
    check("建仓文案带短线提示(V0.3.0 新增)",
          sig12 is not None and any("短线提示" in ln for ln in sig12.lines),
          str(sig12.lines[-1] if sig12 and sig12.lines else None))

    # 低价品:¥6 品涨 ¥0.24(+4%)—— 百分比拆穿不了报价台阶,靠金额豁免仍判横盘
    h_low = hist([100, 103, 106, 109, 112, 115, 118, 121, 124, 127, 130, 133, 136],
                 flat(13, 800),
                 [6.0, 6.02, 6.04, 6.06, 6.08, 6.10, 6.12, 6.14, 6.16, 6.18, 6.20, 6.22, 6.24],
                 flat(13, 5.7))
    s_low = snap(price=6.24, buy=136, sell=800, buy_price=5.7, price_30=5.9)
    sig_low = detect_phase(s_low, h_low, BASE, CFG)
    check("¥6 品 +4%(=¥0.24 ≤ 豁免额 0.5) → 仍判建仓横盘",
          sig_low is not None and sig_low.phase == "accum",
          str(sig_low.phase if sig_low else None))
    check("豁免在原因里说明", sig_low is not None and any("金额豁免" in r for r in sig_low.reasons))

    # 反例:高价品 +5%(¥5 > 0.5) 不算横盘
    h_up = hist([1000, 1005, 1010, 1020, 1030, 1040, 1045, 1050, 1060, 1080, 1100, 1120, 1140],
                flat(13, 3000),
                [100.0, 100.1, 100.2, 100.3, 100.5, 100.8, 101.2, 101.8, 102.5, 103.2, 103.8, 104.5, 105.0])
    sig_up = detect_phase(snap(price=105.0, buy=1140), h_up, BASE, CFG)
    check("¥100 品 +5% → 不判建仓横盘", sig_up is None or sig_up.phase != "accum")


# ---------------------------------------------------------------- 3. 启动四道锁

def _launch_hist(bp_seq, price_seq, sell_seq, buy_seq=None):
    return hist(buy_seq or flat(13, 1060), sell_seq, price_seq, bp_seq)


def test_launch_locks():
    print("== ③ 启动:四道锁(逐段走高/在售不增/不追高/金额下限) ==")
    ok_price = [100.0, 100.2, 100.4, 100.6, 100.8, 101.0, 101.2, 101.5, 101.8, 102.0, 102.1, 102.3, 102.5]
    ok_bp = [95.0, 95.3, 95.6, 95.9, 96.2, 96.5, 96.8, 97.1, 97.4, 97.7, 98.0, 98.2, 98.5]
    ok_sell = [3000, 2995, 2990, 2985, 2980, 2970, 2960, 2955, 2950, 2945, 2940, 2930, 2920]
    h = _launch_hist(ok_bp, ok_price, ok_sell)
    sig = detect_phase(snap(price=102.5, buy_price=98.5, price_30=97.0), h, BASE, CFG)
    check("基线正例(求购价 +3.7%/价 +2.5% = ¥2.5) → 判启动",
          sig is not None and sig.phase == "launch", str(sig.phase if sig else None))
    check("原因含逐段走高确认", sig is not None and any("逐段走高" in r for r in sig.reasons))

    # 锁①:窗口内单点跳变后回落(中间点高于现价)= 不是持续走高
    spike_bp = [95.0, 95.5, 96.0, 96.5, 97.0, 98.0, 101.0, 100.0, 99.8, 99.6, 99.6, 99.5, 99.5]
    h1 = _launch_hist(spike_bp, ok_price, ok_sell)
    sig1 = detect_phase(snap(price=102.5, buy_price=99.5, price_30=97.0), h1, BASE, CFG)
    check("锁①求购价中间点冲高后回落 → 不判启动",
          sig1 is None or sig1.phase != "launch", str(sig1.phase if sig1 else None))

    spike_price = [100.0, 100.2, 100.4, 100.6, 100.8, 101.0, 103.5, 102.0, 101.8, 101.6, 101.6, 101.5, 101.5]
    h1b = _launch_hist(ok_bp, spike_price, ok_sell)
    sig1b = detect_phase(snap(price=101.5, buy_price=98.5, price_30=97.0), h1b, BASE, CFG)
    check("锁①价格中间点冲高后回落 → 不判启动",
          sig1b is None or sig1b.phase != "launch", str(sig1b.phase if sig1b else None))

    # 锁②:价格在涨但在售同步暴增(=派发指纹,不是启动)
    h2 = _launch_hist(ok_bp, ok_price,
                      [3000, 3050, 3100, 3150, 3200, 3250, 3300, 3350, 3400, 3450, 3500, 3550, 3600])
    s2 = snap(price=102.5, buy_price=98.5, buy=1060, sell=3600, price_30=100.0)
    sig2 = detect_phase(s2, h2, BASE, CFG)
    check("锁②在售同步暴增(+20% > 5%) → 不判启动",
          sig2 is None or sig2.phase != "launch", str(sig2.phase if sig2 else None))

    # 锁③:7 日已涨 20%(> 15%)= 追高,不再喊启动
    sig3 = detect_phase(snap(price=102.5, buy_price=98.5, price_30=97.0, rate_7=20.0),
                        h, BASE, CFG)
    check("锁③7 日涨幅 +20% → 不判启动", sig3 is None or sig3.phase != "launch",
          str(sig3.phase if sig3 else None))
    sig3b = detect_phase(snap(price=102.5, buy_price=98.5, price_30=97.0, rate_7=8.0),
                         h, BASE, CFG)
    check("锁③7 日涨幅 +8%(<15%) → 仍判启动",
          sig3b is not None and sig3b.phase == "launch")

    # 锁④:价格只涨 ¥1.2(+1.2% 过百分比线,但金额 < ¥2)
    small_price = [100.0, 100.1, 100.2, 100.3, 100.4, 100.5, 100.6, 100.8, 100.9, 101.0, 101.0, 101.1, 101.2]
    h4 = _launch_hist(ok_bp, small_price, ok_sell)
    sig4 = detect_phase(snap(price=101.2, buy_price=98.5, price_30=97.0), h4, BASE, CFG)
    check("锁④跟涨金额 ¥1.2 < ¥2 → 不判启动",
          sig4 is None or sig4.phase != "launch", str(sig4.phase if sig4 else None))


# ---------------------------------------------------------------- 4. 派发

def test_distribute_range():
    print("== ④ 派发:真实区间位置(近 N 日最高价 ×90%) ==")
    sell_surge = [1000, 1010, 1020, 1030, 1040, 1045, 1050, 1055, 1060, 1062, 1063, 1064, 1065]
    buy_stall = [1000, 1000, 1001, 1001, 1002, 1002, 1003, 1003, 1004, 1004, 1005, 1005, 1005]
    h = hist(buy_stall, sell_surge, flat(13, 100.0))

    s_hi = snap(price=100.0, buy=1005, sell=1065, price_30=90.0)
    sig = detect_phase(s_hi, h, BASE, CFG, range_30d=(70.0, 100.0))
    check("现价 = 区间最高价(100%)+ 在售 +6.5% → 判派发",
          sig is not None and sig.phase == "distribute", str(sig.phase if sig else None))
    check("原因用真实区间口径",
          sig is not None and any("近 30 日高位" in r and "区间最高" in r for r in sig.reasons),
          str(sig.reasons[0] if sig else None))

    # 区间位置不足 90%:即使旧口径 pos_30 = +11%(≥8%)成立,也不再判派发
    sig2 = detect_phase(s_hi, h, BASE, CFG, range_30d=(70.0, 200.0))
    check("区间位置仅 50%(旧口径 pos_30=+11% 仍成立) → 不判派发",
          sig2 is None or sig2.phase != "distribute", str(sig2.phase if sig2 else None))

    # 区间数据缺失 → 回退旧口径,行为与 V0.2.x 一致
    sig3 = detect_phase(s_hi, h, BASE, CFG, range_30d=None)
    check("区间缺失 → 回退 pos_30 旧口径并标注",
          sig3 is not None and sig3.phase == "distribute"
          and any("回退口径" in r for r in sig3.reasons),
          str(sig3.phase if sig3 else None))

    # 门槛放宽:在售 +7%(旧门槛 10% 不触发,新门槛 6% 触发)
    sell7 = [1000, 1005, 1010, 1015, 1020, 1025, 1030, 1035, 1040, 1045, 1050, 1060, 1070]
    h7 = hist(buy_stall, sell7, flat(13, 100.0))
    sig7 = detect_phase(snap(price=100.0, buy=1005, sell=1070, price_30=90.0), h7, BASE, CFG,
                        range_30d=(70.0, 100.0))
    check("在售 +7%(旧 10% 门槛下不触发) → 判派发",
          sig7 is not None and sig7.phase == "distribute", str(sig7.phase if sig7 else None))


# ---------------------------------------------------------------- 5. 加分制

def test_phase_bonus():
    print("== ⑤ 保底分改加分制(评分重新参与取舍) ==")
    h = hist([1000, 1005, 1010, 1020, 1030, 1040, 1045, 1050, 1060, 1080, 1100, 1120, 1140],
             flat(13, 3000), flat(13, 100.0))

    cfg = AnomalyConfig()
    eng = AnomalyEngine(cfg)
    r = eng.evaluate(snap(buy=1140), h, now=BASE)
    pb = (r.details or {}).get("phase_bonus") or {}
    check("最终分 = 基础分 + 加分", pb and r.score == round(min(100.0, pb["base"] + pb["bonus"]), 1),
          f"base={pb.get('base')} bonus={pb.get('bonus')} score={r.score}")
    check("阶段信号基础分确实很低(<25,证明评分体系对横盘指纹天然不给分)",
          bool(pb) and pb["base"] < 25, str(pb.get("base")))
    check("加分后过 L2 推送线", r.score >= 55 and r.level in ("L2", "L3", "L4"),
          f"{r.score}/{r.level}")

    # 把加分调 0:评分重新说话 —— 低基础分不再自动到推送线
    cfg0 = AnomalyConfig()
    cfg0.phase_bonus_accum = 0.0
    r0 = AnomalyEngine(cfg0).evaluate(snap(buy=1140), h, now=BASE)
    check("加分=0 时评分不再被架空(score == 基础分)", r0.score == pb["base"],
          f"{r0.score} vs base {pb.get('base')}")
    check("加分=0 时该样本到不了 L2(即:低基础分噪声不再自动推送)",
          r0.score < 55, f"score={r0.score} level={r0.level}")

    # 派发加分 65:基础分 6.4 → 71.4 过 L3
    sell_surge = [1000, 1015, 1030, 1045, 1060, 1080, 1100, 1120, 1140, 1160, 1180, 1190, 1200]
    buy_stall = [1000, 1000, 1001, 1001, 1002, 1002, 1003, 1003, 1004, 1004, 1005, 1005, 1005]
    hd = hist(buy_stall, sell_surge, flat(13, 100.0))
    rd = AnomalyEngine(AnomalyConfig()).evaluate(
        snap(price=100.0, buy=1005, sell=1200, price_30=90.0), hd, now=BASE)
    check("派发: 6.4 + 65 → 过 L3(≥70) 且 type=distribute",
          rd.score >= 70 and rd.anomaly_type == "distribute" and rd.level in ("L3", "L4"),
          f"{rd.score}/{rd.level}/{rd.anomaly_type}")


# ---------------------------------------------------------------- 6. 区间缓存链路

def test_range_cache():
    print("== ⑥ 近 30 日区间缓存(_ensure_price_ranges,派发数据的来源) ==")
    import asyncio
    import time as _time
    from bot.anomaly.runner import _ensure_price_ranges

    cfg = AnomalyConfig()
    eng = AnomalyEngine(cfg)

    class FakeStore:
        def __init__(self):
            self.calls = []

        async def async_get_price_range_map(self, days=30, min_days=5, good_ids=None):
            self.calls.append(list(good_ids or []))
            return {int(g): (100.0, 200.0) for g in (good_ids or [])}

    st = FakeStore()
    # 首次:只拉本轮要评估的品
    asyncio.run(_ensure_price_ranges(eng, st, cfg, [1, 2, 3]))
    check("首次按本轮品拉取", st.calls and sorted(st.calls[0]) == [1, 2, 3], str(st.calls))
    check("区间写入缓存", eng.range_map.get(1) == (100.0, 200.0), str(eng.range_map))
    # 二次:TTL 内不再查
    asyncio.run(_ensure_price_ranges(eng, st, cfg, [1, 2, 3]))
    check("TTL 内不重复查询", len(st.calls) == 1, f"calls={len(st.calls)}")
    # 新增品:只拉缺的那件
    asyncio.run(_ensure_price_ranges(eng, st, cfg, [1, 2, 3, 9]))
    check("只补新出现的品", sorted(st.calls[-1]) == [9], str(st.calls[-1]))
    # 过期:全部重拉
    eng.range_map_ts = {g: 0.0 for g in eng.range_map_ts}
    asyncio.run(_ensure_price_ranges(eng, st, cfg, [1, 2]))
    check("TTL 过期后重新查询", sorted(st.calls[-1]) == [1, 2], str(st.calls[-1]))
    # 空集合:不查(避免无意义的整表扫描)
    n0 = len(st.calls)
    asyncio.run(_ensure_price_ranges(eng, st, cfg, []))
    check("空集合不查询", len(st.calls) == n0)
    # 查询抛异常:不冒泡、扫描不中断、旧缓存保留
    eng2 = AnomalyEngine(cfg)
    asyncio.run(_ensure_price_ranges(eng2, _FailStore(), cfg, [77]))
    check("查询失败不上抛异常且不污染缓存", eng2.range_map == {}, str(eng2.range_map))
    # 引擎侧查表:命中/未命中
    check("引擎查表命中", eng._range_for(1) == (100.0, 200.0))
    check("引擎查表未命中返回 None", eng._range_for(424242) is None)


def test_range_cache_v031():
    """V0.3.1:重聚合改只读连接 + 3 小时 TTL + 未命中短 TTL。"""
    print("\n== ⑦ 区间查询性能与 TTL(V0.3.1 上线事故复盘) ==")
    import asyncio
    import os
    import tempfile
    import threading
    import time as _time
    from bot.anomaly.runner import (_RANGE_MISS_TTL_SEC, _RANGE_TTL_SEC,
                                    _ensure_price_ranges)
    from bot.storage.db import SnapshotStore

    cfg = AnomalyConfig()
    # 长 TTL:区间由日收盘定义,一天最多变一次(B 池一圈 180 分钟)
    check("区间 TTL ≥ 3 小时", _RANGE_TTL_SEC >= 10800, f"{_RANGE_TTL_SEC}s")
    check("未命中短 TTL 为 15 分钟", _RANGE_MISS_TTL_SEC == 900.0, f"{_RANGE_MISS_TTL_SEC}s")

    class PartialStore:
        """只返回 gid=1 的区间,gid=2 视作样本不足。"""

        async def async_get_price_range_map(self, days=30, min_days=5, good_ids=None):
            return {1: (100.0, 200.0)} if 1 in (good_ids or []) else {}

    eng = AnomalyEngine(cfg)
    asyncio.run(_ensure_price_ranges(eng, PartialStore(), cfg, [1, 2]))
    now = _time.time()
    got_age = now - eng.range_map_ts.get(1, 0.0)
    miss_age = now - eng.range_map_ts.get(2, 0.0)
    check("命中的品按长 TTL 缓存", got_age < 60, f"age={got_age:.1f}s")
    check("未命中的品 15 分钟后就重试",
          abs(miss_age - (_RANGE_TTL_SEC - _RANGE_MISS_TTL_SEC)) < 60,
          f"age={miss_age:.1f}s (应≈{_RANGE_TTL_SEC - _RANGE_MISS_TTL_SEC:.0f}s)")

    # 只读连接:文件库给 RO 连接(WAL 下不阻塞写、不占 self._lock),文件缺失回退共享锁
    d = tempfile.mkdtemp()
    store = SnapshotStore(os.path.join(d, "t.db"))
    ro = store._connect_ro()
    check("文件库提供只读连接(不占 store 写锁)", ro is not None)
    if ro is not None:
        ro.close()
    ghost = SnapshotStore.__new__(SnapshotStore)
    ghost._db_path = os.path.join(d, "missing.db")
    ghost._lock = threading.Lock()
    check("文件缺失时回退共享锁路径", ghost._connect_ro() is None)
    # 新 SQL(GROUP BY + MAX(ts) 裸列)在空库上不该抛错
    check("空库查询返回空 dict", store.get_price_range_map(30, 5, None, [1, 2]) == {})


class _FailStore:
    async def async_get_price_range_map(self, days=30, min_days=5, good_ids=None):
        raise RuntimeError("boom")


def main():
    test_abs_req_formula()
    test_accum_threshold()
    test_launch_locks()
    test_distribute_range()
    test_phase_bonus()
    test_range_cache()
    test_range_cache_v031()
    print(f"\n结果: {PASS} 通过 / {FAIL} 失败")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
