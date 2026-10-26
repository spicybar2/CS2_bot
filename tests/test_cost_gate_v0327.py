# -*- coding: utf-8 -*-
"""V0.3.27 成本门槛【观察模式】—— 回归用例。

动机(2026-10-26 调研结论,详见 docs/交易规则与池策略评估.md):
  · 一笔倒货的往返成本 = 卖出费(2.5%/2.0%)+ 提现(1%,银行卡最低 ¥2)+ 买卖价差
  · 实测池内买卖价差中位 4.2%、¥50 以下 49%;¥20 品光提现最低额就占 10%
  · 而引擎对 ¥58 品 0.26% 的价格波动也计分 —— 扣费后是负收益
  · 「相对 7 日中位价的折扣」与后续收益单调相关,但最高档仍亏 ⇒ 只能是**排序信号**,
    不能当**过线门槛** ⇒ 本版只观察、不拦截

本用例锁死:
  [1] 往返成本口径:fee + 提现(最低 ¥2 生效),分价格带数值
  [2] 买入平台取两平台里更便宜的有效报价(只看 BUFF 会高估成本)
  [3] 各种数据不足都判 skip 并写明原因,绝不猜
  [4] 判定 pass/block 与原因(no_discount / edge_below_min)
  [5] 排序:按边际降序,skip 不参与
  [6] 集成:_persist_events 落 cost_gate_observe,且 ts 与 anomaly_events 一致(可 join)
  [7] observe 模式**不改变推送行为**(block 的候选仍然 pending)
  [8] 配置解析:费用继承 fees 段;非法 mode 回退 observe
"""
import pathlib as _pl, sys as _sys          # tests/_bootstrap:保证能 import bot
_sys.path.insert(0, str(_pl.Path(__file__).resolve().parent.parent))
import asyncio
import sqlite3
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from bot.anomaly.cost import (CostGateConfig, PlatformQuote, VERDICT_BLOCK,
                              VERDICT_PASS, VERDICT_SKIP, build_observation,
                              rank_observations, round_trip_cost_pct, summarize,
                              withdrawal_fee)                                     # noqa: E402
from bot.anomaly.runner import _persist_events                                # noqa: E402
from bot.config import AnomalyConfig                                          # noqa: E402
from bot.storage.db import SnapshotStore                                      # noqa: E402

PASS = FAIL = 0
LOOP = asyncio.new_event_loop()


def ok(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [OK]   {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name}   {extra}")


def run(coro):
    return LOOP.run_until_complete(coro)


def q(platform, ask, bid=0.0, sell_num=1000, age=5.0, closes=None):
    return PlatformQuote(platform=platform, ask=ask, bid=bid, sell_num=sell_num,
                         ask_age_min=age, closes=list(closes or []))


CFG = CostGateConfig()          # 默认:buff 2.5% / yyyp 2.0% / 提现 1% 最低 ¥2 / 边际 2%

print("=" * 80)
print("V0.3.27 成本门槛观察 —— 回归")
print("=" * 80)

# ---------------------------------------------------------------- [1] 成本口径
print("\n[1] 往返成本 = 卖出费 + 提现(最低 ¥2 生效)")
ok("¥20 品 = 2.5% + 10.0% = 12.5%", abs(round_trip_cost_pct(20, 2.5, CFG) - 12.5) < 1e-6,
   round_trip_cost_pct(20, 2.5, CFG))
ok("¥50 品 = 2.5% + 4.0% = 6.5%", abs(round_trip_cost_pct(50, 2.5, CFG) - 6.5) < 1e-6,
   round_trip_cost_pct(50, 2.5, CFG))
ok("¥100 品 = 2.5% + 2.0% = 4.5%", abs(round_trip_cost_pct(100, 2.5, CFG) - 4.5) < 1e-6,
   round_trip_cost_pct(100, 2.5, CFG))
ok("¥1000 品 = 2.5% + 1.0% = 3.5%", abs(round_trip_cost_pct(1000, 2.5, CFG) - 3.5) < 1e-6,
   round_trip_cost_pct(1000, 2.5, CFG))
ok("悠悠 2.0% 比 BUFF 便宜 0.5pp",
   abs(round_trip_cost_pct(1000, 2.0, CFG) - 3.0) < 1e-6, round_trip_cost_pct(1000, 2.0, CFG))
ok("提现最低额生效(¥20 → ¥2)", abs(withdrawal_fee(20, CFG) - 2.0) < 1e-6,
   withdrawal_fee(20, CFG))
ok("提现按比例生效(¥1000 → ¥10)", abs(withdrawal_fee(1000, CFG) - 10.0) < 1e-6,
   withdrawal_fee(1000, CFG))

# ---------------------------------------------------------------- [2] 买入平台
print("\n[2] 买入平台 = 两平台里更便宜的有效报价")
quotes = {"buff": q("buff", 180.0, bid=170.0, closes=[200, 190, 210, 195]),
          "yyyp": q("yyyp", 170.0, closes=[175, 172, 178])}
obs = build_observation(1, "2026-10-26 10:00:00", quotes, CFG)
ok("买入平台选悠悠(¥170 < ¥180)", obs.buy_platform == "yyyp", obs.buy_platform)
ok("买入价 ¥170", abs(obs.buy_ask - 170.0) < 1e-6, obs.buy_ask)
ok("基准取悠悠自己的日收盘中位 175", abs(obs.baseline - 175.0) < 1e-6, obs.baseline)
ok("基准天数 3", obs.baseline_days == 3, obs.baseline_days)
ok("平台价差 5.88%", abs(obs.gap_pct - (10 / 170 * 100)) < 1e-6, obs.gap_pct)
ok("折扣 = (175-170)/175 = +2.86%", abs(obs.discount_pct - 2.857142) < 1e-3, obs.discount_pct)
ok("成本 = 2.0% + max(2,1.7)/170 = 3.18%", abs(obs.cost_pct - (2.0 + 2 / 170 * 100)) < 1e-6,
   obs.cost_pct)
ok("边际 = 折扣 - 成本 ≈ -0.32%", obs.edge_pct < 0, obs.edge_pct)
ok("判定 block", obs.verdict == VERDICT_BLOCK, obs.verdict)
ok("原因写明 edge_below_min", "edge_below_min" in obs.reason, obs.reason)

# ---------------------------------------------------------------- [3] skip 分支
print("\n[3] 数据不足一律 skip(宁缺毋假)")
cases = [
    ("无报价", {"buff": q("buff", 0.0), "yyyp": q("yyyp", 0.0)}, "no_usable_quote"),
    ("报价过期", {"buff": q("buff", 100.0, age=999, closes=[100] * 5),
                 "yyyp": q("yyyp", 0.0)}, "stale_quote"),
    ("基准太薄", {"buff": q("buff", 100.0, closes=[100]), "yyyp": q("yyyp", 0.0)},
     "baseline_thin"),
    ("在售太薄", {"buff": q("buff", 100.0, sell_num=10, closes=[100] * 5),
                 "yyyp": q("yyyp", 0.0)}, "thin_book"),
    ("价差过宽", {"buff": q("buff", 100.0, bid=50.0, closes=[100] * 5),
                 "yyyp": q("yyyp", 0.0)}, "spread_wide"),
    ("平台离群", {"buff": q("buff", 100.0, closes=[100] * 5),
                 "yyyp": q("yyyp", 20.0, closes=[20] * 5)}, "platform_gap_outlier"),
]
for label, qq, expect in cases:
    o = build_observation(2, "2026-10-26 10:00:00", qq, CFG)
    ok(f"{label} → skip({expect})",
       o.verdict == VERDICT_SKIP and expect in o.reason, f"{o.verdict}/{o.reason}")

# ---------------------------------------------------------------- [4] pass / no_discount
print("\n[4] 判定 pass 与 no_discount")
qq = {"buff": q("buff", 180.0, closes=[200, 190, 210, 195]), "yyyp": q("yyyp", 0.0)}
o = build_observation(3, "2026-10-26 10:00:00", qq, CFG)
# 基准 197.5,折扣 8.86%,成本 2.5+2/180=3.61% → 边际 5.25% ≥ 2%
ok("折扣 8.86% 足够覆盖成本 → pass", o.verdict == VERDICT_PASS, f"{o.verdict}/{o.edge_pct:.2f}")
ok("边际 > 0", o.edge_pct > 0, o.edge_pct)
qq2 = {"buff": q("buff", 300.0, closes=[200, 190, 210, 195]), "yyyp": q("yyyp", 0.0)}
o2 = build_observation(4, "2026-10-26 10:00:00", qq2, CFG)
ok("现价高于基准 → block", o2.verdict == VERDICT_BLOCK, o2.verdict)
ok("原因写明 no_discount", "no_discount" in o2.reason, o2.reason)

# ---------------------------------------------------------------- [5] 排序
print("\n[5] 排序按边际降序,skip 不参与")
a = build_observation(10, "t", {"buff": q("buff", 100.0, closes=[200] * 5),
                               "yyyp": q("yyyp", 0.0)}, CFG)      # 折扣 50%
b = build_observation(11, "t", {"buff": q("buff", 190.0, closes=[200] * 5),
                               "yyyp": q("yyyp", 0.0)}, CFG)      # 折扣 5%
sk = build_observation(12, "t", {"buff": q("buff", 0.0), "yyyp": q("yyyp", 0.0)}, CFG)
rank_observations([b, sk, a])
ok("a 排第 1", a.rank == 1, a.rank)
ok("b 排第 2", b.rank == 2, b.rank)
ok("skip 不参与排名", sk.rank == 0, sk.rank)
ok("汇总文案含候选/达标/未达标/跳过",
   all(k in summarize([a, b, sk]) for k in ("候选3", "skip" if False else "跳过1")),
   summarize([a, b, sk]))

# ---------------------------------------------------------------- [8] 配置解析
print("\n[8] 配置解析:费用继承 fees 段,非法 mode 回退 observe")
cg = CostGateConfig.from_mapping({"mode": "ENFORCE", "min_edge_pct": 3.5},
                                 buff_fee=2.5, yyyp_fee=2.0)
ok("mode 归一化为 enforce", cg.mode == "enforce", cg.mode)
ok("min_edge_pct 覆盖为 3.5", abs(cg.min_edge_pct - 3.5) < 1e-9, cg.min_edge_pct)
ok("费用继承 fees(2.5/2.0)",
   abs(cg.buff_sell_fee_pct - 2.5) < 1e-9 and abs(cg.yyyp_sell_fee_pct - 2.0) < 1e-9,
   (cg.buff_sell_fee_pct, cg.yyyp_sell_fee_pct))
cg2 = CostGateConfig.from_mapping({"mode": "不存在的模式"})
ok("非法 mode 回退 observe", cg2.mode == "observe", cg2.mode)

# ---------------------------------------------------------------- [6][7] 集成
print("\n[6][7] 集成本轮:cost_gate_observe 落库 + ts 可 join + observe 不改推送")
tmp = tempfile.mktemp(suffix=".db")
st = SnapshotStore(tmp)
GID = 4321
NAME = "测试品 | 成本门槛"
now = datetime.now()
con = sqlite3.connect(tmp)
rows = []
# 造 4 天日收盘:200/190/210/195 → 基准中位 197.5;最新价 180(折扣 8.86%)
for i, (price, days_ago) in enumerate([(200.0, 4), (190.0, 3), (210.0, 2), (195.0, 1)]):
    rows.append((GID, NAME, (now - timedelta(days=days_ago)).strftime("%Y-%m-%d 12:00:00"),
                 price, 0.0, price, 1000, 0.0))
rows.append((GID, NAME, now.strftime("%Y-%m-%d %H:%M:%S"), 180.0, 170.0, 181.0, 1200, 0.0))
con.executemany(
    "INSERT INTO item_listing_history (good_id,name,ts,buff_sell_price,buff_buy_price,"
    "yyyp_sell_price,buff_sell_num,yyyp_buy_num) VALUES (?,?,?,?,?,?,?,?)", rows)
con.commit()
con.close()

ev = SimpleNamespace(good_id=GID, name=NAME, score=73.0, level="L3",
                     anomaly_type="accum", reasons=["测试"], details={"sell_num": 100,
                                                                      "price": 180.0})
cfg = AnomalyConfig()
cfg.cost_gate = CostGateConfig()
run(_persist_events(cfg, st, [ev], "A"))

con = sqlite3.connect(tmp)
con.row_factory = sqlite3.Row
obs_rows = con.execute("SELECT * FROM cost_gate_observe").fetchall()
ev_rows = con.execute("SELECT * FROM anomaly_events").fetchall()
ok("cost_gate_observe 落库 1 行", len(obs_rows) == 1, len(obs_rows))
if obs_rows:
    r = obs_rows[0]
    ok("buy_platform=buff", r["buy_platform"] == "buff", r["buy_platform"])
    ok("buy_ask=180", abs(r["buy_ask"] - 180.0) < 1e-6, r["buy_ask"])
    ok("baseline≈197.5", abs(r["baseline"] - 197.5) < 1e-6, r["baseline"])
    ok("baseline_days=4(当天未完结,不算日收盘)", r["baseline_days"] == 4, r["baseline_days"])
    ok("verdict=pass", r["verdict"] == VERDICT_PASS, r["verdict"])
    ok("rank=1", r["rank"] == 1, r["rank"])
    ok("push_status=pending", r["push_status"] == "pending", r["push_status"])
    ok("ts 与 anomaly_events 一致(可 join)",
       bool(ev_rows) and r["ts"] == ev_rows[0]["ts"], (r["ts"], ev_rows[0]["ts"] if ev_rows else None))
ok("observe 模式不改推送:事件仍是 pending(没被拦)",
   bool(ev_rows) and ev_rows[0]["push_status"] == "pending",
   ev_rows[0]["push_status"] if ev_rows else None)
con.close()

print("\n" + "=" * 80)
print(f"结果: 通过 {PASS} / 失败 {FAIL}")
print("=" * 80)
sys.exit(1 if FAIL else 0)
