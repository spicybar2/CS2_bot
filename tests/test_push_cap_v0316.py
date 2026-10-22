# -*- coding: utf-8 -*-
"""V0.3.16 每品每日推送上限 —— 回归用例。

起因(老大 2026-10-14 问「为什么一直弹渐变之色/潘多拉的建仓和异动」):
  · 6822 蝴蝶刀|渐变之色 累计推 **25 次**、7474 运动手套|潘多拉之盒 累计 **23 次**
  · 最近 3 天这两件占全部推送的 **29%**(28 条里 8 条)
  · 时间线证据:7474 在 10-12 16:07(L4 86.7)→16:21(L3 73.8) **14 分钟内连推两次**;
    6822 在 10-11 04:48→05:03→05:31→06:10 **82 分钟 4 次**
  · 根因是"同一波反复报":高价活跃品求购基数 30~60,±8 单的正常波动就反复命中建仓指纹,
    分数在 73↔96 振荡 → 三道放行口(Δ≥10 突破冷却 / 冷却到期后 Δ≥5 / 级别变化不算同波)
    几乎每轮命中一条;而冷却方向还是反的(L4 只有 12 分钟)

修法:`max_push_per_item_per_day`(默认 3),在 `_persist_events` 所有冷却判定**之后**兜底 ——
超限的**仍然落库**(push_status='capped',/分析 与 signal_outcomes 照常可用)但不推送。

本用例锁死:
  [1] 计数语义:只算 sent/pending;capped/failed 不吃额度;跨天不算
  [2] 额度用尽 → 第 4 件落库为 capped,且**不出现在待推列表**
  [3] capped 的事件仍能被 /分析 看到(list_active_anomalies)
  [4] 上限是**按品**的,别的品不受影响
  [5] 批量内也不会超发(同一轮的 pending 也计入额度)
  [6] 配 0 = 不限(老行为)
"""
import pathlib as _pl, sys as _sys          # tests/_bootstrap:保证能 import bot
_sys.path.insert(0, str(_pl.Path(__file__).resolve().parent.parent))
import asyncio
import sqlite3
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from bot.config import AnomalyConfig                      # noqa: E402
from bot.storage.db import SnapshotStore                  # noqa: E402
from bot.anomaly.runner import _persist_events            # noqa: E402

PASS = FAIL = 0
LOOP = asyncio.new_event_loop()
GID = 6822
NAME = "蝴蝶刀（★） | 渐变之色 (崭新出厂)"


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


def ev(gid, score, level, atype="accum", name=NAME):
    return SimpleNamespace(good_id=gid, name=name, score=score, level=level,
                           anomaly_type=atype, reasons=["测试原因"],
                           details={"sell_num": 100, "price": 12480.0})


def raw_row(gid, name, status, ts=None):
    row = {"good_id": gid, "name": name, "anomaly_type": "accum", "drop_pct": 0.0,
           "abs_drop": 0, "window_min": 0, "sell_prev": 0, "sell_cur": 100,
           "level": "L3", "detail": "测试", "score": 73.0,
           "push_status": status}
    return row


def count_by_status(tmp, gid):
    con = sqlite3.connect(tmp)
    cur = con.cursor()
    out = dict(cur.execute(
        "SELECT COALESCE(push_status,'pending'), COUNT(*) FROM anomaly_events "
        "WHERE good_id=? GROUP BY 1", (gid,)).fetchall())
    con.close()
    return out


print("=" * 80)
print("V0.3.16 每品每日推送上限 —— 回归")
print("=" * 80)

# ---------------------------------------------------------------- [1] 计数语义
print("\n[1] 计数语义:只算 sent/pending,capped/failed 不吃额度,跨天不算")
tmp1 = tempfile.mktemp(suffix=".db")
st1 = SnapshotStore(tmp1)
day = "2026-10-14 00:00:00"
run(st1.async_insert_anomaly_events([
    raw_row(111, "甲", "sent"), raw_row(111, "甲", "sent"), raw_row(111, "甲", "pending"),
    raw_row(111, "甲", "capped"), raw_row(111, "甲", "failed"),
    raw_row(222, "乙", "sent"), raw_row(333, "丙", "sent"),
]))
c = run(st1.async_count_anomaly_pushes_today(day))
ok("sent×2 + pending×1 = 3(capped/failed 不计)", c.get(111) == 3, c)
ok("别的品各自计数", c.get(222) == 1 and c.get(333) == 1, c)
# 把丙的一条改成"昨天",应不计入今天
con = sqlite3.connect(tmp1)
con.execute("UPDATE anomaly_events SET ts='2026-10-13 10:00:00' WHERE good_id=333")
con.commit()
con.close()
c2 = run(st1.async_count_anomaly_pushes_today(day))
ok("昨天的事件不算今天额度", c2.get(333) is None, c2)

# ---------------------------------------------------------------- [2][3][5] 上限生效
print("\n[2] 额度(3 条)用尽后:落库为 capped,但不进待推列表")
tmp2 = tempfile.mktemp(suffix=".db")
st2 = SnapshotStore(tmp2)
cfg = AnomalyConfig(max_push_per_item_per_day=3)
# 同一轮 3 件 -> 全部占额度(体现"批量内也不超发")
run(_persist_events(cfg, st2, [ev(GID, 55.0, "L2"),
                               ev(GID, 65.0, "L2"),
                               ev(GID, 75.0, "L3")], "A"))
st = count_by_status(tmp2, GID)
ok("一轮 3 件全部 pending", st.get("pending") == 3, st)
pend = run(st2.async_list_pending_anomalies(top_n=50))
ok("待推列表 3 条", len(pend) == 3, len(pend))
# 第 4 件:分数升级足够(95-75=20 >= 10)能过冷却,但额度已满
run(_persist_events(cfg, st2, [ev(GID, 95.0, "L4")], "A"))
st = count_by_status(tmp2, GID)
ok("第 4 件落库为 capped", st.get("capped") == 1, st)
ok("第 4 件没进 pending", st.get("pending") == 3, st)
pend = run(st2.async_list_pending_anomalies(top_n=50))
ok("待推列表仍然是 3 条(第 4 件不推)", len(pend) == 3, len(pend))
ok("push_detail 写明原因",
   any("上限" in str(r.get("push_detail") or "")
       for r in run(st2.async_list_active_anomalies(top_n=50))),
   [r.get("push_detail") for r in run(st2.async_list_active_anomalies(top_n=50))])

print("\n[3] capped 的事件仍然能被 /分析 看到(只是不推)")
act = run(st2.async_list_active_anomalies(top_n=50))
ok("capped 事件在 24h 活跃列表里", len(act) == 4, len(act))

print("\n[4] 上限是按品的:别的品不受影响")
run(_persist_events(cfg, st2, [ev(9999, 90.0, "L4", name="别的品")], "A"))
st_other = count_by_status(tmp2, 9999)
ok("别的品照常 pending", st_other.get("pending") == 1, st_other)
ok("原品的额度没被顶掉", count_by_status(tmp2, GID).get("pending") == 3)

print("\n[5] 第二天额度刷新(日期边界)")
con = sqlite3.connect(tmp2)
con.execute("UPDATE anomaly_events SET ts='2026-10-13 12:00:00' WHERE good_id=?", (GID,))
con.commit()
con.close()
run(_persist_events(cfg, st2, [ev(GID, 60.0, "L2")], "A"))
st = count_by_status(tmp2, GID)
ok("昨天用过不影响今天(新的 pending 出现)", st.get("pending", 0) >= 1, st)

# ---------------------------------------------------------------- [6] 0 = 不限
print("\n[6] max_push_per_item_per_day=0 → 不限(老行为)")
tmp3 = tempfile.mktemp(suffix=".db")
st3 = SnapshotStore(tmp3)
cfg0 = AnomalyConfig(max_push_per_item_per_day=0)
run(_persist_events(cfg0, st3, [ev(GID, 56.0 + i * 5, "L2") for i in range(6)], "A"))
st = count_by_status(tmp3, GID)
ok("6 件全部 pending(不设上限)", st.get("pending") == 6, st)

print("\n" + "=" * 80)
print(f"结果: 通过 {PASS} / 失败 {FAIL}")
print("=" * 80)
sys.exit(1 if FAIL else 0)
