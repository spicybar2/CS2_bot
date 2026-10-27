# -*- coding: utf-8 -*-
"""V0.3.28 池卫生 —— 回归用例:反抖冷却、enter_time 语义、回扫队列、沉底。

起因(2026-10-27 线上实测):
  · 一枚印花 1 小时 45 分内 A→S 升级 10 次,每次升上去几十秒就被 TTL 打回来。
    根因:UPSERT 的 DO UPDATE 漏了 enter_time,它永远是「首次发现时间」,
    S 池 TTL 一旦过期就永久过期 → 每轮 S→A(ttl)、下一轮 A→S,无限抖。
  · B 池 1116 件里 952 件(85%)超过 3 天没被任何扫描碰过 —— 只靠 10 个 sort
    轮转(180 分钟一圈)永远轮不到长尾。

本用例锁死:
  [1] enter_time = 进入当前池的时刻(换池才重置,不换池保留)
  [2] S 降级写冷却;s_rearm_at 冷却期内 A→S 被拦住,冷却过后才放行
  [3] A→S 需连续 confirm 轮,单轮尖峰不升级
  [4] best_score 只涨不跌(沉底判定的依据)
  [5] 回扫队列:按 last_update 最旧优先、限流、可关闭
  [6] 沉底:进池够久 + 历史最高分不达标才删;没到天数的、达过标的不动
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

from bot.anomaly.pool import POOL_A, POOL_B, POOL_S, WatchPoolManager   # noqa: E402
from bot.anomaly.runner import _fetch_stale_pool_rows                   # noqa: E402
from bot.config import AnomalyConfig                                    # noqa: E402
from bot.storage.db import SnapshotStore                                # noqa: E402

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


def fmt(dt):
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def res(gid, name, score, details=None):
    return SimpleNamespace(good_id=gid, name=name, score=score, level="L2",
                           reasons=[], details=details or {})


def fresh_store():
    tmp = tempfile.mktemp(suffix=".db")
    return SnapshotStore(tmp), tmp


def row_of(tmp, gid):
    con = sqlite3.connect(tmp)
    con.row_factory = sqlite3.Row
    r = con.execute("SELECT * FROM watch_pool WHERE good_id=?", (gid,)).fetchone()
    con.close()
    return dict(r) if r else None


def put(tmp, **kw):
    """直接插一行池成员(绕过 manager,用于造场景)。"""
    base = dict(good_id=1, name="测试品", market_hash_name="mhn", pool=POOL_B,
                score=0.0, enter_time=fmt(datetime.now()), last_update=fmt(datetime.now()),
                last_push_at=None, cooldown_until=None, hit_count=0, quiet_count=0,
                last_score=0.0, quality_reject_count=0, anomaly_buy_low_since=None,
                s_rearm_at=None, best_score=0.0)
    base.update(kw)
    con = sqlite3.connect(tmp)
    cols = ",".join(base.keys())
    ph = ",".join("?" * len(base))
    con.execute(f"INSERT OR REPLACE INTO watch_pool ({cols}) VALUES ({ph})",
                list(base.values()))
    con.commit()
    con.close()


print("=" * 80)
print("V0.3.28 池卫生 —— 回归")
print("=" * 80)

# ------------------------------------------------ [1] enter_time 语义
print("\n[1] enter_time = 进入当前池的时刻")
st, tmp = fresh_store()
cfg = AnomalyConfig()
mgr = WatchPoolManager(cfg, st)
old = fmt(datetime.now() - timedelta(hours=6))
put(tmp, good_id=101, name="A池成员", pool=POOL_A, enter_time=old, score=80.0,
    last_score=80.0, best_score=80.0, hit_count=5)
# A→S 升级(连着两轮高分)
run(mgr.sync([res(101, "A池成员", 90.0)]))
run(mgr.sync([res(101, "A池成员", 90.0)]))
r = row_of(tmp, 101)
ok("A→S 升级成功", r["pool"] == POOL_S, r["pool"])
ok("enter_time 已重置为本次进池时刻(不是 6h 前)",
   (datetime.now() - datetime.strptime(r["enter_time"], "%Y-%m-%d %H:%M:%S")
    ).total_seconds() < 120, r["enter_time"])
ok("进 S 时 s_rearm_at 清空", not r["s_rearm_at"], r["s_rearm_at"])

# 同池再更新一次:enter_time 必须保留
prev_enter = r["enter_time"]
run(mgr.sync([res(101, "A池成员", 90.0)]))
r = row_of(tmp, 101)
ok("同池更新不动 enter_time", r["enter_time"] == prev_enter, (prev_enter, r["enter_time"]))

# ------------------------------------------------ [2] S 降级冷却
print("\n[2] S 降级写冷却,冷却期内 A→S 被拦")
for _ in range(int(cfg.s_to_a_rounds) + 1):
    run(mgr.sync([res(101, "A池成员", 30.0)]))
r = row_of(tmp, 101)
ok("连续低分后 S→A", r["pool"] == POOL_A, r["pool"])
ok("降级写了 s_rearm_at", bool(r["s_rearm_at"]), r["s_rearm_at"])
ok("冷却时间在将来", r["s_rearm_at"] > fmt(datetime.now()), r["s_rearm_at"])
# 冷却期内高分:连续两轮也不该升回 S
run(mgr.sync([res(101, "A池成员", 100.0)]))
run(mgr.sync([res(101, "A池成员", 100.0)]))
r = row_of(tmp, 101)
ok("冷却期内即使连轮 100 分也不升 S", r["pool"] == POOL_A, r["pool"])
# 人为把冷却推到过去 → 应放行
con = sqlite3.connect(tmp)
con.execute("UPDATE watch_pool SET s_rearm_at=? WHERE good_id=101",
            (fmt(datetime.now() - timedelta(minutes=1)),))
con.commit()
con.close()
run(mgr.sync([res(101, "A池成员", 100.0)]))
run(mgr.sync([res(101, "A池成员", 100.0)]))
r = row_of(tmp, 101)
ok("冷却过后 A→S 放行", r["pool"] == POOL_S, r["pool"])
ok("回到 S 后 s_rearm_at 清空", not r["s_rearm_at"], r["s_rearm_at"])

# ------------------------------------------------ [3] A→S 需连续确认
print("\n[3] A→S 单轮尖峰不升级(需 confirm 轮)")
st2, tmp2 = fresh_store()
mgr2 = WatchPoolManager(AnomalyConfig(), st2)
put(tmp2, good_id=202, name="尖峰品", pool=POOL_A, score=80.0, last_score=80.0,
    best_score=80.0, hit_count=0)
run(mgr2.sync([res(202, "尖峰品", 95.0)]))
r = row_of(tmp2, 202)
ok("第一轮 95 分仍在 A(需两轮确认)", r["pool"] == POOL_A, r["pool"])
run(mgr2.sync([res(202, "尖峰品", 95.0)]))
r = row_of(tmp2, 202)
ok("第二轮 95 分升 S", r["pool"] == POOL_S, r["pool"])

# ------------------------------------------------ [4] best_score
print("\n[4] best_score 只涨不跌")
st3, tmp3 = fresh_store()
mgr3 = WatchPoolManager(AnomalyConfig(), st3)
put(tmp3, good_id=303, name="记录品", pool=POOL_B, score=10.0, last_score=10.0,
    best_score=10.0)
run(mgr3.sync([res(303, "记录品", 60.0)]))
r = row_of(tmp3, 303)
ok("升到 60 记录最高分", abs(float(r["best_score"]) - 60.0) < 1e-6, r["best_score"])
for _ in range(3):
    run(mgr3.sync([res(303, "记录品", 5.0)]))
r = row_of(tmp3, 303)
ok("跌到 5 分后 best_score 仍是 60", abs(float(r["best_score"]) - 60.0) < 1e-6,
   (r["score"], r["best_score"]))

# ------------------------------------------------ [5] 回扫队列
print("\n[5] 回扫队列:最旧优先 + 限流 + 可关闭")
st4, tmp4 = fresh_store()
cfg4 = AnomalyConfig()
now = datetime.now()
for i in range(5):
    put(tmp4, good_id=400 + i, name=f"陈旧{i}", pool=POOL_B,
        last_update=fmt(now - timedelta(hours=48 - i * 4)), score=float(i))
put(tmp4, good_id=499, name="新鲜品", pool=POOL_B, last_update=fmt(now), score=99.0)
rows = st4.list_stale_watch_pool(24, 10, pool=POOL_B)
ok("只取超过 24h 没更新的", all("陈旧" in r["name"] for r in rows), [r["name"] for r in rows])
ok("最旧排最前", rows[0]["name"] == "陈旧0", rows[0]["name"])
rows3 = st4.list_stale_watch_pool(24, 3, pool=POOL_B)
ok("limit 生效", len(rows3) == 3, len(rows3))
cfg4.stale_rescan_hours = 24
cfg4.stale_rescan_per_cycle = 2
rows_cfg, ids = run(_fetch_stale_pool_rows(cfg4, st4))
ok("按配置取 2 件", len(rows_cfg) == 2, len(rows_cfg))
ok("返回 good_id 集合", ids == {400, 401}, ids)
cfg4.stale_rescan_per_cycle = 0
rows_off, ids_off = run(_fetch_stale_pool_rows(cfg4, st4))
ok("配置为 0 时关闭回扫", rows_off == [] and ids_off == set())

# ------------------------------------------------ [6] 沉底
print("\n[6] 沉底:够久 + 历史最高分没达标才删")
st5, tmp5 = fresh_store()
old7 = fmt(datetime.now() - timedelta(days=8))
put(tmp5, good_id=501, name="老冷门", pool=POOL_B, enter_time=old7, best_score=20.0)
put(tmp5, good_id=502, name="老达标", pool=POOL_B, enter_time=old7, best_score=70.0)
put(tmp5, good_id=503, name="新冷门", pool=POOL_B,
    enter_time=fmt(datetime.now() - timedelta(days=1)), best_score=5.0)
put(tmp5, good_id=504, name="老冷门A", pool=POOL_A, enter_time=old7, best_score=5.0)
removed = st5.purge_stale_watch_pool(7, 52.0, 50, POOL_B)
names = [n for _g, n in removed]
ok("只会删『老 + 从没达标』的 B 成员", names == ["老冷门"], names)
ok("老达标的不删", row_of(tmp5, 502) is not None)
ok("没到天数的不删", row_of(tmp5, 503) is not None)
ok("别的池不动", row_of(tmp5, 504) is not None)
ok("删掉的行确实没了", row_of(tmp5, 501) is None)
removed2 = st5.purge_stale_watch_pool(7, 52.0, 50, POOL_B)
ok("重复执行幂等(第二次没有可删的)", removed2 == [], removed2)

print("\n" + "=" * 80)
print(f"结果: 通过 {PASS} / 失败 {FAIL}")
print("=" * 80)
sys.exit(1 if FAIL else 0)
