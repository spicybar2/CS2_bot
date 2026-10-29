# -*- coding: utf-8 -*-
"""V0.2.0 资金周期四阶段检测回归测试:建仓/启动/派发/消化 + 假买墙过滤 + 引擎集成。"""
import pathlib as _pl, sys as _sys          # tests/_bootstrap:保证能 import bot
_sys.path.insert(0, str(_pl.Path(__file__).resolve().parent.parent))
import os
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from bot.anomaly.engine import AnomalyEngine, HistoryPoint, MetricSnapshot
from bot.anomaly.cycle import detect_phase, digest_label
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


def make_history(buy_seq, sell_seq, price_seq, buy_price_seq=None,
                 start=None, step_min=5):
    """构造正序时序:各序列长度即点数,末点 ≈ now(锚点窗口才有效)。"""
    n = len(buy_seq)
    if start is None:
        start = BASE - timedelta(minutes=step_min * (n - 1))
    pts = []
    for i in range(n):
        ts = (start + timedelta(minutes=step_min * i)).strftime("%Y-%m-%d %H:%M:%S")
        pts.append(HistoryPoint(
            ts=ts, price=float(price_seq[i]),
            sell_num=int(sell_seq[i]), buy_num=int(buy_seq[i]),
            buy_price=float((buy_price_seq or price_seq)[i]),
        ))
    return pts


def flat(seq_len, v):
    return [v] * seq_len


def snap(buy=1060, sell=3000, price=100.0, buy_price=95.0, price_30=95.0,
         sell_price=100.0, rate_30=0.0):
    return MetricSnapshot(
        good_id=1, name="测试品", price=price, sell_price=sell_price,
        buy_price=buy_price, sell_num=sell, buy_num=buy,
        rate_7=0.0, rate_30=rate_30, price_7=price * 0.98, price_30=price_30,
    )


def test_accum():
    print("== 建仓监测(①: 求购持续净增+价格横盘) ==")
    # V0.3.0:门槛 6%→12%,绝对量 = max(3, 基线×12%)。
    # 求购 1000→1140(+14%,+140 单 ≥ 120),价格 100 横盘,在售平稳
    h = make_history(
        buy_seq=[1000, 1005, 1010, 1020, 1030, 1040, 1045, 1050, 1060, 1080, 1100, 1120, 1140],
        sell_seq=flat(13, 3000),
        price_seq=flat(13, 100.0),
        buy_price_seq=flat(13, 95.0),
    )
    sig = detect_phase(snap(buy=1140), h, BASE, CFG)
    check("触发 accum", sig is not None and sig.phase == "accum",
          str(sig.phase if sig else None))
    if sig:
        check("下限分数 = L2(55)", sig.floor_score == CFG.phase_score_accum)
        check("带盘口 1 小时块", any("求购" in ln for ln in sig.lines))

    # 反例:价格已涨 5%(>3% 且金额 >¥0.5) → 不再是建仓(进入启动/派发阶段)
    h2 = make_history(
        buy_seq=[1000, 1005, 1010, 1020, 1030, 1040, 1045, 1050, 1060, 1080, 1100, 1120, 1140],
        sell_seq=flat(13, 3000),
        price_seq=[100.0, 100.1, 100.2, 100.3, 100.5, 100.8, 101.2, 101.8, 102.5, 103.2, 103.8, 104.5, 105.0],
        buy_price_seq=flat(13, 95.0),
    )
    sig2 = detect_phase(snap(buy=1140, price=105.0), h2, BASE, CFG)
    check("价格已涨 → 不判建仓", sig2 is None or sig2.phase != "accum")

    # 反例:小基数百分比达标但绝对增量低于地板(基线 10 单 +20%(+2 单) < 地板 3 单)
    h3 = make_history(
        buy_seq=[10, 10, 10, 11, 11, 11, 11, 12, 12, 12, 12, 12, 12],
        sell_seq=flat(13, 3000),
        price_seq=flat(13, 100.0),
        buy_price_seq=flat(13, 95.0),
    )
    sig3 = detect_phase(snap(buy=12), h3, BASE, CFG)
    check("小基数 +20% 但仅 +2 单(< 地板 3) → 不判建仓",
          sig3 is None or sig3.phase != "accum")

    # 正例:同样小基数但 +3 单(≥ 地板) → 建仓成立(V0.3.0 地板放行)
    h4 = make_history(
        buy_seq=[10, 10, 10, 10, 11, 11, 11, 11, 12, 12, 12, 13, 13],
        sell_seq=flat(13, 3000),
        price_seq=flat(13, 100.0),
        buy_price_seq=flat(13, 95.0),
    )
    sig4 = detect_phase(snap(buy=13), h4, BASE, CFG)
    check("小基数 +3 单(≥ 地板) → 判建仓", sig4 is not None and sig4.phase == "accum",
          str(sig4.phase if sig4 else None))


def test_launch():
    print("== 启动确认(②: 求购价上移+价格跟涨+四道锁) ==")
    # V0.3.0:求购价 ≥3%、价格 ≥1% **且金额 ≥¥2**、逐段走高、在售不增、7 日涨幅 <15%
    # 求购价 95→98.5(+3.7%),价格 100→102.5(+2.5% = +¥2.5 ≥2),在售递减,单调上行
    h = make_history(
        buy_seq=flat(13, 1060),
        sell_seq=[3000, 2995, 2990, 2985, 2980, 2970, 2960, 2955, 2950, 2945, 2940, 2930, 2920],
        price_seq=[100.0, 100.2, 100.4, 100.6, 100.8, 101.0, 101.2, 101.5, 101.8, 102.0, 102.1, 102.3, 102.5],
        buy_price_seq=[95.0, 95.3, 95.6, 95.9, 96.2, 96.5, 96.8, 97.1, 97.4, 97.7, 98.0, 98.2, 98.5],
    )
    sig = detect_phase(snap(price=102.5, buy_price=98.5, price_30=97.0), h, BASE, CFG)
    check("触发 launch", sig is not None and sig.phase == "launch",
          str(sig.phase if sig else None))


def test_distribute():
    print("== 派发预警(③: 高位+在售暴增+求购停滞) ==")
    # pos_30 = +11%(高位),在售 1000→1200(+20% ≥10%,+200 ≥ req30),求购 +0.5%(停滞)
    h = make_history(
        buy_seq=[1000, 1000, 1001, 1001, 1002, 1002, 1003, 1003, 1004, 1004, 1005, 1005, 1005],
        sell_seq=[1000, 1015, 1030, 1045, 1060, 1080, 1100, 1120, 1140, 1160, 1180, 1190, 1200],
        price_seq=[100.0] * 13,
        buy_price_seq=flat(13, 95.0),
    )
    s = snap(buy=1005, sell=1200, price=100.0, buy_price=95.0, price_30=90.0)
    sig = detect_phase(s, h, BASE, CFG)
    check("触发 distribute", sig is not None and sig.phase == "distribute",
          str(sig.phase if sig else None))
    if sig:
        check("派发下限 = L3(70)", sig.floor_score == CFG.phase_score_distrib)


def test_fake_wall():
    print("== 假买墙过滤(求购增+价格跌+在售暴增=诱多) ==")
    h = make_history(
        buy_seq=[1000, 1020, 1040, 1060, 1080, 1100, 1120, 1140, 1160, 1180, 1190, 1195, 1200],
        sell_seq=[1000, 1020, 1040, 1060, 1090, 1120, 1150, 1180, 1200, 1220, 1235, 1245, 1250],
        price_seq=[100.0, 99.6, 99.2, 98.8, 98.5, 98.2, 97.9, 97.6, 97.4, 97.2, 97.1, 97.0, 97.0],
        buy_price_seq=flat(13, 95.0),
    )
    sig = detect_phase(snap(buy=1200, sell=1250, price=97.0), h, BASE, CFG)
    check("假买墙 → 不触发任何周期信号", sig is None, str(sig.phase if sig else "None"))


def test_engine_integration():
    print("== 引擎集成(加分制/类型覆写/高价闸门豁免) ==")
    eng = AnomalyEngine(CFG)
    h = make_history(
        buy_seq=[1000, 1005, 1010, 1020, 1030, 1040, 1045, 1050, 1060, 1080, 1100, 1120, 1140],
        sell_seq=flat(13, 3000),
        price_seq=flat(13, 100.0),
        buy_price_seq=flat(13, 95.0),
    )
    r = eng.evaluate(snap(buy=1140), h, now=BASE)
    check("建仓: anomaly_type=accum", r.anomaly_type == "accum", r.anomaly_type)
    check("建仓: 加分制后到 L2(≥55)", r.level in ("L2", "L3", "L4") and r.score >= 55,
          f"score={r.score} level={r.level}")
    check("建仓: details 带基础分/加分", bool((r.details or {}).get("phase_bonus")),
          str((r.details or {}).get("phase_bonus")))
    check("建仓: details 带盘口块", bool((r.details.get("phase") or {}).get("lines"))

          )
    # 高价品横盘吸筹:无周期信号时会被 high_price_weak 拦;有信号必须豁免
    h2 = make_history(
        buy_seq=[1000, 1005, 1010, 1020, 1030, 1040, 1045, 1050, 1060, 1080, 1100, 1120, 1140],
        sell_seq=flat(13, 3000),
        price_seq=flat(13, 10000.0),
        buy_price_seq=flat(13, 9500.0),
    )
    s2 = snap(buy=1140, sell=3000, price=10000.0, buy_price=9500.0, price_30=9500.0)
    r2 = eng.evaluate(s2, h2, now=BASE)
    check("高价品建仓不被 high_price_weak 拦",
          (r2.details or {}).get("rejected") is None,
          str((r2.details or {}).get("rejected")))
    check("高价品建仓: type=accum 且 ≥L2", r2.anomaly_type == "accum" and r2.score >= 55,
          f"{r2.anomaly_type}/{r2.score}")

    # 派发事件: 评分下限到 L3(70)
    h3 = make_history(
        buy_seq=[1000, 1000, 1001, 1001, 1002, 1002, 1003, 1003, 1004, 1004, 1005, 1005, 1005],
        sell_seq=[1000, 1015, 1030, 1045, 1060, 1080, 1100, 1120, 1140, 1160, 1180, 1190, 1200],
        price_seq=[100.0] * 13,
        buy_price_seq=flat(13, 95.0),
    )
    r3 = eng.evaluate(snap(buy=1005, sell=1200, price=100.0, price_30=90.0), h3, now=BASE)
    check("派发: 评分下限到 L3(≥70)", r3.score >= 70 and r3.level in ("L3", "L4"),
          f"score={r3.score} level={r3.level}")


def test_digest():
    print("== 消化标签(④: /分析 展示) ==")
    h = make_history(
        buy_seq=[1000, 1005, 1010, 1015, 1020, 1025, 1030, 1035, 1040, 1045, 1050, 1055, 1060],
        sell_seq=[3000, 2995, 2990, 2985, 2980, 2970, 2960, 2955, 2950, 2945, 2930, 2915, 2900],
        price_seq=flat(13, 100.0),
        buy_price_seq=flat(13, 95.0),
    )
    lbl = digest_label(h, BASE, CFG)
    check("触发消化标签", lbl is not None, str(lbl))
    check("标签含「供给消化」", lbl is not None and "供给消化" in lbl)
    # 反例:在售暴增 → 无标签
    h2 = make_history(
        buy_seq=flat(13, 1000),
        sell_seq=[3000, 3050, 3100, 3150, 3200, 3250, 3300, 3350, 3400, 3450, 3500, 3550, 3600],
        price_seq=flat(13, 100.0),
        buy_price_seq=flat(13, 95.0),
    )
    check("在售暴增 → 无消化标签", digest_label(h2, BASE, CFG) is None)


def test_watch_cycle() -> None:
    """V0.2.3 关注品周期监测: parse/锚点放宽/推送格式/冷却防重推。"""
    from bot.qqbot.commands import parse_watch_arg, _watch_cond_text
    # 关注类型解析
    name, _th, _tp, at, _p = parse_watch_arg("M4A4 狮鹫 周期监测")
    check("parse: 周期监测 -> cycle", at == "cycle" and "狮鹫" in name, f"{name}/{at}")
    name2, _t, _t2, at2, _p2 = parse_watch_arg("蝴蝶刀 4500")
    check("parse: 价格关注不误伤", at2 == "price", at2)
    check("cond 文案含周期监测", "周期" in _watch_cond_text("cycle", 0, None, None))

    # 15 分钟采样(5 点)在 min_points=4 下可判,默认 6 点判不了
    # V0.3.0:建仓门槛 12%,基线 1000 → 绝对量要求 max(3, 120)=120 单
    from bot.anomaly.cycle import detect_phase
    buy_seq = [1000, 1030, 1060, 1090, 1140]
    pts = []
    for i, b in enumerate(buy_seq):
        ts = (BASE - timedelta(minutes=60 - 15 * i)).strftime("%Y-%m-%d %H:%M:%S")
        pts.append(HistoryPoint(ts=ts, price=100.0, sell_num=3000, buy_num=b, buy_price=95.0))
    s = MetricSnapshot(good_id=9, name="关注品", price=100.0, sell_price=100.0,
                       buy_price=95.0, sell_num=3000, buy_num=1140,
                       price_7=98.0, price_30=95.0)
    sig4 = detect_phase(s, pts, BASE, CFG, min_points=4)
    sig6 = detect_phase(s, pts, BASE, CFG)
    check("关注品 4 点可判 accum", sig4 is not None and sig4.phase == "accum",
          str(sig4.phase if sig4 else None))
    check("默认 6 点仍判不足(不误判)", sig6 is None)

    # 端到端: _run_cycle_watch 推送 + 冷却防重推
    import asyncio, json as _json, tempfile, os as _os
    from bot.storage.db import SnapshotStore

    tmp = tempfile.mktemp(suffix=".db")
    store = SnapshotStore(tmp)

    class FakeClient:
        def __init__(self):
            self.pushed = []
        async def push_to_group_markdown(self, gid, text):
            self.pushed.append((gid, text))
        async def push_to_user(self, uid, text):
            self.pushed.append((uid, text))

    async def run():
        now = datetime.now()
        # 预置 5 个 15 分钟间隔快照(递增求购,横盘价)进临时库
        rows = []
        for i, b in enumerate(buy_seq):
            rows.append({"good_id": 9, "name": "关注品",
                         "ts": (BASE - timedelta(minutes=60 - 15 * i)).strftime("%Y-%m-%d %H:%M:%S"),
                         "buff_sell_num": 3000, "buff_buy_num": b,
                         "steam_sell_num": 0, "steam_buy_num": 0,
                         "buff_sell_price": 100.0, "buff_buy_price": 95.0,
                         "total_sell_num": 3000, "total_buy_num": b,
                         "total_sell_scope": "buff_yyyp", "total_buy_scope": "buff_yyyp"})
        # 快照时间用真实"过去"以便锚点对齐 now
        for i, r in enumerate(rows):
            r["ts"] = (datetime.now() - timedelta(minutes=60 - 15 * i)).strftime("%Y-%m-%d %H:%M:%S")
        await store.async_save_listing_history_batch(rows)
        from bot.anomaly.cycle import run_cycle_watch as _run_cycle_watch
        client = FakeClient()
        wrows = [{"good_id": 9, "zh_name": "关注品", "market_hash_name": "Fake Case",
                  "user_openid": "UID1", "group_openid": "G1", "alert_type": "cycle"}]
        prices = {"Fake Case": {"buffSellPrice": 100.0, "buffBuyPrice": 95.0,
                                "buffSellNum": 3000, "buffBuyNum": 1140,
                                "steamSellNum": 0, "steamBuyNum": 0,
                                "yyypSellNum": 0, "yyypBuyNum": 0,
                                "c5SellNum": 0, "c5BuyNum": 0,
                                "igxeSellNum": 0, "igxeBuyNum": 0,
                                "ecoSellNum": 0, "ecoBuyNum": 0,
                                "sell_price_rate_7": 0, "sell_price_30": 95,
                                "sell_price_rate_30": 0, "sell_price_7": 98, "sell_price_30d": 0}}
        prices["Fake Case"]["sell_price_30"] = 95.0
        await _run_cycle_watch(CFG, store, client, wrows, prices, datetime.now())
        first = len(client.pushed)
        text = client.pushed[0][1] if client.pushed else ""
        # 第二轮: 冷却期内同阶段 → 不重推
        await _run_cycle_watch(CFG, store, client, wrows, prices, datetime.now())
        second = len(client.pushed)
        return first, second, text

    first, second, text = asyncio.run(run())
    check("周期命中推送 1 条", first == 1, f"pushed={first}")
    # V0.3.29:标题按「阶段 + 位置」决定(建仓迹象/低位吸筹迹象/高位诱多预警),
    # 盘口块标题统一为「▍盘口变动（近 X 小时）」。
    check("推送含建仓标题", "迹象" in text and "▍盘口变动" in text, text[:40].replace("\n", "|"))
    # V0.3.12: 盘口块改为**分平台**展示(BUFF / 悠悠有品 各自的变化量与当前值);
    # 只有历史或快照缺分平台字段时才回退到 V0.2.24 的合计标注。
    check("推送盘口块为分平台格式",
          "求购：" in text and "在售：" in text
          and ("BUFF +" in text or "BUFF -" in text or "BUFF 0" in text)
          and "悠悠有品" in text, text[:0])
    check("分平台行含变化量与当前值", "→" in text and "个(" in text, text[:0])
    check("冷却期内不重推", second == first, f"{first}->{second}")
    try:
        _os.remove(tmp)
    except OSError:
        pass


def test_no_volume_words():
    print("== 口径红线: 输出文案无成交量词 ==")
    h = make_history(
        buy_seq=[1000, 1005, 1010, 1020, 1030, 1040, 1045, 1050, 1060, 1080, 1100, 1120, 1140],
        sell_seq=flat(13, 3000),
        price_seq=flat(13, 100.0),
        buy_price_seq=flat(13, 95.0),
    )
    sig = detect_phase(snap(buy=1140), h, BASE, CFG)
    check("建仓文案样本已触发(否则本用例空跑)", sig is not None and sig.phase == "accum")
    texts = list(sig.lines) + list(sig.reasons) if sig else []
    from bot.anomaly.alert import _format_phase_alert
    row = {"anomaly_type": "accum", "level": "L2", "name": "测试品", "price": 100,
           "score": 55, "position": "中位", "reason": "\n".join(texts),
           "detail": "求购持续净增"}
    out = _format_phase_alert(row)
    texts.append(out)
    joined = "\n".join(texts)
    for w in ("成交量", "放量", "缩量", "天量", "无量"):
        check(f"输出无「{w}」", w not in joined)


def main():
    test_accum()
    test_launch()
    test_distribute()
    test_fake_wall()
    test_engine_integration()
    test_digest()
    test_watch_cycle()
    test_no_volume_words()
    print(f"\n结果: {PASS} 通过 / {FAIL} 失败")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
