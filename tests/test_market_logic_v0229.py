# -*- coding: utf-8 -*-
"""V0.2.29 口径修正回归:价格绝对下限 / 有符号价差 / 中期基线闸 / 1d-7d 列语义 /
信号结果闭环 / 推荐策略口径。

全部离线(不依赖网络、不碰运行中 csqaq.db)。运行:
    .venv\\Scripts\\python.exe test_market_logic_v0229.py
"""
import pathlib as _pl, sys as _sys          # tests/_bootstrap:保证能 import bot
_sys.path.insert(0, str(_pl.Path(__file__).resolve().parent.parent))

import asyncio
import os
import sys
import tempfile
from datetime import datetime, timedelta

from bot.analyzer.outcome import (
    format_summary, pick_forward_price, settle_event, summarize)
from bot.analyzer.recommender import Recommender
from bot.anomaly.engine import AnomalyEngine, HistoryPoint, MetricSnapshot
from bot.config import RecommendConfig, load_config
from bot.storage.db import SnapshotStore

CFG = load_config("config.yaml")
CFG.anomaly.min_anomaly_buy_num = 0
CFG.anomaly.min_price = 0
ENGINE = AnomalyEngine(CFG.anomaly)
NOW = datetime(2026, 9, 24, 12, 0, 0)

_PASS = 0
_FAIL = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global _PASS, _FAIL
    if cond:
        _PASS += 1
        print(f"  OK {name}")
    else:
        _FAIL += 1
        print(f"  FAIL {name}  {detail}")


def hist(price, sell_num=80, buy_num=60, buy_price=0.0, minutes_back=30, n=12) -> list:
    """构造 minutes_back 分钟前的平盘历史(最后一个点落在窗口边界内)。"""
    out = []
    for i in range(n):
        t = NOW - timedelta(minutes=minutes_back) + timedelta(minutes=i * 2.5)
        out.append(HistoryPoint(ts=t.strftime("%Y-%m-%d %H:%M:%S"), price=price,
                                sell_num=sell_num, buy_num=buy_num, buy_price=buy_price))
    return out


def snap(price, sell_num=80, buy_num=60, buy_price=0.0, **kw) -> MetricSnapshot:
    return MetricSnapshot(good_id=kw.pop("good_id", 1), name=kw.pop("name", "t"),
                          price=price, sell_price=price, buy_price=buy_price,
                          sell_num=sell_num, buy_num=buy_num,
                          buff_yyyp_complete=True, yyyp_sell_num=0, **kw)


# ============================================================
# 1. 价格绝对变动下限(V0.2.29)
# ============================================================

def test_price_abs_floor() -> None:
    print("[1] 价格绝对变动下限(V0.2.29)")
    # 低价品:百分比可观但金额过小 → 不计分(权重仍占位)
    r = ENGINE.evaluate(snap(9.0), hist(8.55), now=NOW)   # +5.3% 但只有 ¥0.45
    check("¥9 品 +5.3%(+¥0.45) 不给价格分", "price_30m" not in r.parts,
          f"parts={r.parts}")
    check("¥9 品 金额过小标注", r.details.get("price_abs_floor_30m") is True,
          f"details={r.details.get('price_abs_floor_30m')}")
    check("¥9 品 原因文案含金额", any("金额过小" in x for x in r.reasons),
          f"reasons={r.reasons}")
    check("¥9 品 绝对变动已白盒", r.details.get("price_delta_abs_30m") == 0.45,
          f"abs={r.details.get('price_delta_abs_30m')}")

    # 金额达标 → 正常计分(注意 ±10% 以上且盘口不动会被 thin_price_noise 闸门拦,属既有设计)
    r2 = ENGINE.evaluate(snap(27.4), hist(25.0), now=NOW)  # +9.6%,+¥2.40
    check("¥27 品 +9.6%(+¥2.40) 正常计分", r2.parts.get("price_30m", 0) > 0,
          f"parts={r2.parts} rejected={r2.details.get('rejected')}")

    # 高价品不受影响(百分比主导;盘口需同步变化,否则被高价品 stale 闸门拦)
    r3 = ENGINE.evaluate(snap(5000.0), hist(4800.0, sell_num=100, buy_num=50), now=NOW)
    check("¥5000 品 +4.2%(+¥200) 正常计分", r3.parts.get("price_30m", 0) > 0,
          f"parts={r3.parts} rejected={r3.details.get('rejected')}")

    # 边界:恰好 ¥2.00 计入;¥1.99 不计
    check("恰好 ¥2 变动计入", ENGINE.evaluate(snap(101.0), hist(99.0), now=NOW)
          .details.get("price_abs_floor_30m") is None)
    check("¥1.99 变动不计分", ENGINE.evaluate(snap(100.99), hist(99.0), now=NOW)
          .details.get("price_abs_floor_30m") is True)


# ============================================================
# 2. 买卖价差:有符号 + 百分点口径(V0.2.29)
# ============================================================

def test_spread_signed() -> None:
    print("[2] 买卖价差有符号 + 百分点口径(V0.2.29)")
    # 榜单源:价差收窄 25% → 计分
    s = snap(100.0, buy_price=95.0, spread_chg=-25.0)
    r = ENGINE.evaluate(s, hist(100.0, buy_price=95.0), now=NOW)
    check("价差收窄 25% 计分", r.parts.get("spread", 0) > 0, f"parts={r.parts}")
    check("价差收窄原因文案", any("价差收窄" in x for x in r.reasons), f"{r.reasons}")
    check("榜单源口径标记 pct", r.details.get("spread_basis") == "pct")

    # 榜单源:价差扩大 25% → 不计分,但留原因(旧实现同分)
    s2 = snap(100.0, buy_price=95.0, spread_chg=25.0)
    r2 = ENGINE.evaluate(s2, hist(100.0, buy_price=95.0), now=NOW)
    check("价差扩大 25% 不计分", "spread" not in r2.parts, f"parts={r2.parts}")
    check("价差扩大原因标注不计分", any("扩大" in x and "不计分" in x for x in r2.reasons),
          f"{r2.reasons}")
    check("价差扩大不再当利好(方向不含价差)", r2.details.get("spread_basis") == "pct")

    # 兜底路径:批量源自算 → 百分点差(旧版是百分比之百分比,会出 340% 级假值)
    s3 = snap(9.0, buy_price=8.9, sell_num=80)            # 当前价差 1.12%
    h3 = hist(9.3, buy_price=8.6, sell_num=80)            # 30m 前价差 8.14%
    r3 = ENGINE.evaluate(s3, h3, now=NOW)
    check("兜底路径口径标记 pp", r3.details.get("spread_basis") == "pp",
          f"{r3.details.get('spread_basis')}")
    check("兜底路径取百分点差(-7.0pp 级)",
          r3.details.get("spread_chg") is not None and abs(r3.details["spread_chg"]) < 30,
          f"chg={r3.details.get('spread_chg')}")
    check("价差收窄(pp)计分", r3.parts.get("spread", 0) > 0, f"parts={r3.parts}")

    # 价差扩大(pp)不计分
    s4 = snap(9.3, buy_price=8.6)
    r4 = ENGINE.evaluate(s4, hist(9.0, buy_price=8.9), now=NOW)
    check("价差扩大(pp)不计分", "spread" not in r4.parts, f"parts={r4.parts}")


# ============================================================
# 3. 中期基线合理性闸(V0.2.29)
# ============================================================

def test_trend_baseline_gate() -> None:
    print("[3] 7日/30日基线合理性闸(V0.2.29)")
    s = snap(20.0, rate_7=665.0, rate_30=3000.0, price_7=2.6, price_30=0.6)
    r = ENGINE.evaluate(s, hist(20.0), now=NOW)
    check("7日异常基线被记录", r.details.get("baseline_reject_7d") == 665.0,
          f"{r.details.get('baseline_reject_7d')}")
    check("30日异常基线被记录", r.details.get("baseline_reject_30d") == 3000.0,
          f"{r.details.get('baseline_reject_30d')}")
    check("原因文案含基线异常",
          sum(1 for x in r.reasons if "基线异常" in x) == 2, f"{r.reasons}")
    check("趋势不含被污染的方向票(0/0 不成立)",
          r.details.get("trend_aligned") is None, f"{r.details.get('trend_aligned')}")

    # 正常基线仍参与趋势
    s2 = snap(20.0, rate_7=-12.0, rate_30=-15.0, price_7=22.7, price_30=23.5)
    r2 = ENGINE.evaluate(s2, hist(21.0), now=NOW)
    check("正常 7日/30日 仍计入趋势", r2.details.get("trend_aligned") is not None,
          f"{r2.details.get('trend_aligned')}")

    # 边界:恰好 +100% 计入,超过则剔除
    check("+100.0% 不剔除", ENGINE.evaluate(
        snap(20.0, rate_7=100.0), hist(20.0), now=NOW).details.get("baseline_reject_7d") is None)
    check("+100.1% 剔除", ENGINE.evaluate(
        snap(20.0, rate_7=100.1), hist(20.0), now=NOW).details.get("baseline_reject_7d") == 100.1)


# ============================================================
# 4. price_change_1d / rate_7 列语义(V0.2.29)
# ============================================================

def test_event_columns() -> None:
    print("[4] 事件表 1d/7d 列语义(V0.2.29)")
    tmp = tempfile.mkdtemp()
    store = SnapshotStore(os.path.join(tmp, "t.db"))
    cols = {r[1] for r in store._connect().execute("PRAGMA table_info(anomaly_events)")}
    check("anomaly_events 新增 rate_7 列", "rate_7" in cols, f"{sorted(cols)}")

    store.insert_anomaly_events([
        {"good_id": 1, "name": "新品", "anomaly_type": "normal", "drop_pct": 0,
         "abs_drop": 0, "window_min": 30, "sell_prev": 10, "sell_cur": 10,
         "level": "L2", "detail": "", "score": 60.0, "price": 100.0,
         "price_change_1d": 3.5, "rate_7": -12.0},
        {"good_id": 2, "name": "无1日数据品", "anomaly_type": "normal", "drop_pct": 0,
         "abs_drop": 0, "window_min": 30, "sell_prev": 10, "sell_cur": 10,
         "level": "L2", "detail": "", "score": 60.0, "price": 100.0,
         "price_change_1d": None, "rate_7": 4.0},
    ])
    from bot.anomaly.alert import format_row_alert
    rows = {r["good_id"]: r for r in store.list_active_anomalies(10)}
    check("1日/7日 分别落库", rows[1]["price_change_1d"] == 3.5 and rows[1]["rate_7"] == -12.0,
          f"{rows[1].get('price_change_1d')},{rows[1].get('rate_7')}")
    check("1日缺失写 NULL(不冒充 0)", rows[2]["price_change_1d"] is None,
          f"{rows[2].get('price_change_1d')}")
    txt = format_row_alert(rows[1])
    check("推送 7d 取 rate_7 列", "7d：-12.0%" in txt, txt.splitlines()[4] if len(txt.splitlines()) > 4 else txt)
    check("推送不再把 7 日当 1 日(无 近1日 -12.0)", "近1日 -12.0%" not in txt)


# ============================================================
# 5. 信号结果闭环(V0.2.29)
# ============================================================

def test_outcome_loop() -> None:
    print("[5] 信号结果闭环(V0.2.29)")
    base = datetime(2026, 9, 1, 12, 0, 0)
    series = [
        ((base + timedelta(hours=24)).strftime("%Y-%m-%d %H:%M:%S"), 110.0),
        ((base + timedelta(hours=72)).strftime("%Y-%m-%d %H:%M:%S"), 90.0),
        ((base + timedelta(hours=168)).strftime("%Y-%m-%d %H:%M:%S"), 120.0),
    ]
    ev = {"id": 7, "good_id": 3, "name": "x", "anomaly_type": "accum", "level": "L2",
          "score": 58.0, "ts": base.strftime("%Y-%m-%d %H:%M:%S"), "price": 100.0}
    row = settle_event(ev, series)
    check("T+1d 收益 +10%", row["fwd_24h"] == 10.0, f"{row['fwd_24h']}")
    check("T+3d 收益 -10%", row["fwd_72h"] == -10.0, f"{row['fwd_72h']}")
    check("T+7d 收益 +20%", row["fwd_168h"] == 20.0, f"{row['fwd_168h']}")
    check("事件元信息随行落库", row["event_id"] == 7 and row["anomaly_type"] == "accum")

    # 无前瞻价 → 留空,不插值
    row2 = settle_event(ev, [])
    check("无后续快照时收益留空", row2["fwd_24h"] is None and row2["fwd_168h"] is None)
    # 容差外不取价(±6h)
    far = [((base + timedelta(hours=40)).strftime("%Y-%m-%d %H:%M:%S"), 200.0)]
    check("超出容差不取价", pick_forward_price(far, base + timedelta(hours=24)) is None)
    # 容差内取最近
    near = [((base + timedelta(hours=22)).strftime("%Y-%m-%d %H:%M:%S"), 101.0),
            ((base + timedelta(hours=27)).strftime("%Y-%m-%d %H:%M:%S"), 105.0)]
    check("容差内取最近点", pick_forward_price(near, base + timedelta(hours=24)) == 101.0)

    # 汇总:样本 <5 不报胜率(避免 1/1=100% 误导)
    rows = [{"anomaly_type": "accum", "fwd_24h": 5.0, "fwd_72h": -1.0, "fwd_168h": None}]
    st = summarize(rows)["accum"]
    check("样本<5 不报胜率", st["fwd_24h"]["win_rate"] is None and st["fwd_24h"]["median"] == 5.0,
          f"{st['fwd_24h']}")
    check("缺失窗口样本数为 0", st["fwd_168h"]["n"] == 0 and st["fwd_168h"]["median"] is None)
    rows5 = [{"anomaly_type": "surge", "fwd_24h": v, "fwd_72h": None, "fwd_168h": None}
             for v in (1, -2, 3, -4, 5)]
    st5 = summarize(rows5)["surge"]["fwd_24h"]
    check("样本≥5 报胜率 60%", st5["win_rate"] == 60.0, f"{st5}")
    check("汇总文案含口径声明", "未扣手续费" in format_summary(summarize(rows5)))

    # 落库幂等(可反复重算)
    tmp = tempfile.mkdtemp()
    store = SnapshotStore(os.path.join(tmp, "o.db"))
    store.upsert_signal_outcomes([dict(row)])
    store.upsert_signal_outcomes([dict(row, fwd_24h=99.0)])
    saved = store.list_signal_outcomes()
    check("结果行事件唯一(幂等重算)", len(saved) == 1 and saved[0]["fwd_24h"] == 99.0,
          f"{saved}")


# ============================================================
# 6. 推荐策略口径(V0.2.29)
# ============================================================

class _FakeApi:
    def __init__(self, items):
        self._items = items
        self.calls = []

    async def get_rank_items_all(self, page_size=100, max_pages=10, sorts=None, extra_filter=None):
        self.calls.append((tuple(sorts or ()), extra_filter))
        return list(self._items)


def _rank_item(gid, name, rate7, sell=1000.0, buff_sell=40, buff_buy=40,
               steam_sell=40, steam_buy=999999, yyyp_sell=10, yyyp_buy=10):
    from bot.csqaq.client import RankItem
    return RankItem(good_id=gid, name=name, exterior="崭新出厂",
                    buff_sell_price=sell, buff_buy_price=sell * 0.95,
                    sell_price_rate_7=rate7, sell_price_7=sell * 1.1,
                    buff_price_chg=1.0, buff_sell_num=buff_sell, buff_buy_num=buff_buy,
                    steam_sell_num=steam_sell, steam_buy_num=steam_buy,
                    yyyp_sell_num=yyyp_sell, yyyp_buy_num=yyyp_buy)


def test_recommend_scope() -> None:
    print("[6] 推荐策略口径(V0.2.29)")
    cfg = RecommendConfig(min_price=50, max_price=20000, min_liquidity=50, top_n=8)
    rec = Recommender(cfg)

    # Steam 脏求购不再计入流动性:40+40+40+10+10=140(含脏值则百万级)
    liq = rec._liquidity({"buff_sell_num": 40, "buff_buy_num": 40, "steam_sell_num": 40,
                          "steam_buy_num": 999999, "yyyp_sell_num": 10, "yyyp_buy_num": 10})
    check("_liquidity 已剔除 steam_buy_num", liq == 140, f"liq={liq}")
    # 仅靠脏求购凑门槛的品必须被拦
    poor = _rank_item(1, "冷门品", -5.0, buff_sell=1, buff_buy=1, steam_sell=1,
                      steam_buy=500000, yyyp_sell=0, yyyp_buy=0)
    api = _FakeApi([poor])
    res = asyncio.run(rec.recommend_realtime(api, "dip_7d"))
    check("仅靠脏求购的品不进入超跌榜", not res.items, f"{[i.name for i in res.items]}")

    # low_7d 与 dip_7d 实时分支不再同源:区间与流动性闸均不同
    items = [
        _rank_item(2, "温和下跌品", -12.0),       # 只在 low_7d
        _rank_item(3, "深度超跌品", -45.0),       # 只在 dip_7d
        _rank_item(4, "上涨品", 9.0),             # 两者都不该有
    ]
    api2 = _FakeApi(items)
    low = asyncio.run(rec.recommend_realtime(api2, "low_7d"))
    api3 = _FakeApi(items)
    dip = asyncio.run(rec.recommend_realtime(api3, "dip_7d"))
    low_names = [i.name for i in low.items]
    dip_names = [i.name for i in dip.items]
    check("low_7d 只含 -30%~0 区间", low_names == ["温和下跌品"], f"{low_names}")
    # dip_7d 口径 = 全部下跌品 + 流动性闸(与本地 dip_7d 一致),跌幅最大者在前
    check("dip_7d 含全部下跌品且按跌幅升序",
          dip_names == ["深度超跌品", "温和下跌品"], f"{dip_names}")
    check("两个策略结果不再相同", low_names != dip_names)

    # 本地 liquid:排序键改为数值直传(不再从文案反解)
    rows = [
        {"good_id": 11, "name": "窄价差品", "main_price": 1000.0, "buff_sell_price": 1000.0,
         "buff_buy_price": 990.0, "sell_price_7": 1100.0, "sell_price_rate_7": -5.0,
         "buff_price_chg": 0.0},
        {"good_id": 12, "name": "宽价差品", "main_price": 1000.0, "buff_sell_price": 1000.0,
         "buff_buy_price": 975.0, "sell_price_7": 1100.0, "sell_price_rate_7": -5.0,
         "buff_price_chg": 0.0},
    ]
    out = rec.liquid(rows)
    check("liquid 按价差升序(数值键)", [i.name for i in out] == ["窄价差品", "宽价差品"],
          f"{[(i.name, i.sort_value) for i in out]}")


def main() -> None:
    print("=" * 60)
    print("V0.2.29 口径修正回归")
    print("=" * 60)
    test_price_abs_floor()
    test_spread_signed()
    test_trend_baseline_gate()
    test_event_columns()
    test_outcome_loop()
    test_recommend_scope()
    print()
    print(f"结果: 通过 {_PASS} / 失败 {_FAIL}")
    sys.exit(1 if _FAIL else 0)


if __name__ == "__main__":
    main()
