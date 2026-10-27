# -*- coding: utf-8 -*-
"""异动雷达回归测试(2026-09-03):覆盖用户验收清单 —— 低价死盘/高价微动/薄盘噪声/
冷却重复推送/池升降级/401·429 分类/推送前复核价格带。

全部离线(不依赖网络、不碰运行中 csqaq.db)。运行:
    .venv\\Scripts\\python.exe test_anomaly_regression.py
"""
import pathlib as _pl, sys as _sys          # tests/_bootstrap:保证能 import bot
_sys.path.insert(0, str(_pl.Path(__file__).resolve().parent.parent))

import asyncio
import os
import tempfile
import sys
from datetime import datetime, timedelta

from bot.anomaly.engine import AnomalyEngine, HistoryPoint, MetricSnapshot
from bot.anomaly.pool import POOL_A, POOL_B, POOL_S, WatchPoolManager
from bot.config import load_config
from bot.storage.db import SnapshotStore

CFG = load_config("config.yaml")
# Existing fixtures use small synthetic order counts; exercise the production
# 50-order gate explicitly while keeping these historical scoring cases focused.
CFG.anomaly.min_anomaly_buy_num = 0
ENGINE = AnomalyEngine(CFG.anomaly)
NOW = datetime(2026, 9, 3, 12, 0, 0)

_PASS = 0
_FAIL = 0
_CASES = []


def check(name: str, cond: bool, detail: str = "") -> None:
    """断言式用例登记:cond True 为通过。"""
    global _PASS, _FAIL
    if cond:
        _PASS += 1
        print(f"  OK {name}")
    else:
        _FAIL += 1
        print(f"  FAIL {name}  {detail}")


def mk_snap(price, sell_num, buy_num, buy_price=0.0, yyyp=None, good_id=1, name="t") -> MetricSnapshot:
    return MetricSnapshot(good_id=good_id, name=name, price=price, sell_price=price,
                          buy_price=buy_price, sell_num=sell_num + int(yyyp or 0), buy_num=buy_num,
                          yyyp_sell_num=int(yyyp or 0),
                          buff_yyyp_complete=yyyp is not None)


def flat_hist(price, sell_num=50, buy_num=10, minutes_back=30, n=12) -> list:
    """历史:前 minutes_back 分钟价格恒定(构造平盘),最后一个点落在 minutes_back 分钟前。"""
    out = []
    for i in range(n):
        t = NOW - timedelta(minutes=minutes_back) + timedelta(minutes=i * 2.5)
        out.append(HistoryPoint(ts=t.strftime("%Y-%m-%d %H:%M:%S"), price=price,
                                sell_num=sell_num, buy_num=buy_num, buy_price=0))
    return out


def jump_hist(prev_price, prev_sell, prev_buy, minutes_back=30, n=12) -> list:
    """历史:前 minutes_back 分钟价格=prev(突变基线),当前价由 snap 给。用于触发价动。"""
    out = []
    for i in range(n):
        t = NOW - timedelta(minutes=minutes_back) + timedelta(minutes=i * 2.5)
        out.append(HistoryPoint(ts=t.strftime("%Y-%m-%d %H:%M:%S"), price=prev_price,
                                sell_num=prev_sell, buy_num=prev_buy, buy_price=0))
    return out


def eval_reject(snap, hist) -> str:
    """返回 rejected reason 或空串。"""
    r = ENGINE.evaluate(snap, hist, now=NOW)
    return r.details.get("rejected") or ""


# ============================================================
# 1. 引擎闸门矩阵(低价死盘/高价微动/薄盘噪声/基础门槛/不误伤真异动)
# ============================================================

def test_engine_gates() -> None:
    print("\n[1] 引擎闸门矩阵")
    # 低价(¥1) → min_price
    check("低价品 ¥1 拦 min_price", eval_reject(mk_snap(1.0, 500, 100), flat_hist(1.0)) == "min_price")
    # 死盘(求购 0) → min_buy_num
    check("死盘求购0 拦 min_buy_num", eval_reject(mk_snap(100, 50, 0), flat_hist(100)) == "min_buy_num")
    # 薄盘(在售 2 个) → min_sell_num
    check("薄盘在售2 拦 min_sell_num", eval_reject(mk_snap(100, 2, 1), flat_hist(100)) == "min_sell_num")
    # 金额深度不足 → min_depth (sell3×price10=30 < 600)
    check("深度不足 拦 min_depth", eval_reject(mk_snap(10.0, 3, 1), flat_hist(10.0)) == "min_depth")
    # 挂单总数少 → min_listing
    check("挂单总数少 拦 min_listing", eval_reject(mk_snap(100, 6, 3), flat_hist(100)) == "min_listing")
    # S 池 yyyp 口径:全平台在售 20 < min_platform_sell(30) → 拦(需先过 min_listing=10)
    check("全平台在售20 拦 min_platform_sell",
          eval_reject(mk_snap(200, 12, 5, buy_price=190, yyyp=8), flat_hist(200)) == "min_platform_sell")
    # 价差死盘:售价/求购价 > 3(卖 100 / 求 20) → spread_too_wide
    check("价差死盘 拦 spread_too_wide",
          eval_reject(mk_snap(100, 30, 5, buy_price=20), flat_hist(100)) == "spread_too_wide")
    # ---- 高价品三道闸门 ----
    # 高价买盘稀薄(¥16800 求购 2 <3) → high_price_thin_buy
    check("高价买盘稀薄 拦 high_price_thin_buy",
          eval_reject(mk_snap(16800, 33, 2, buy_price=15000), flat_hist(16800)) == "high_price_thin_buy")
    # 高价微动(渐变之色类:¥14799, 5m/30m 均 <2%) → high_price_weak
    hist = jump_hist(14961, 85, 29)
    check("高价微动(-1.1%) 拦 high_price_weak",
          eval_reject(mk_snap(14799, 85, 29, buy_price=13520), hist) == "high_price_weak")
    # 高价价动盘口不动(+5% 但盘口 0 变化) → high_price_stale
    hist2 = jump_hist(14000, 86, 26)
    check("高价价动盘口不动(+5%) 拦 high_price_stale",
          eval_reject(mk_snap(14700, 86, 26, buy_price=14000), hist2) == "high_price_stale")
    # ---- 低价薄盘噪声(价格大幅波动盘口不动) ----
    # ¥300 品 30m -42% 但盘口几乎不动(±2%) → thin_price_noise
    hist3 = jump_hist(517, 40, 12)
    check("薄盘价动量不动(-42%) 拦 thin_price_noise",
          eval_reject(mk_snap(300, 40, 12, buy_price=280), hist3) == "thin_price_noise")
    # ---- 不误伤:真异动保留(高价品盘口「真配合」须绝对量变化 ≥2 单) ----
    # 渐变之色类真扫货:¥14000 30m -4.1%,求购 16→26(+10 单,+58.8%)→ 真配合,不拦
    hist6 = jump_hist(14600, 86, 16)
    r6 = ENGINE.evaluate(mk_snap(14000, 86, 26, buy_price=13300), hist6, now=NOW)
    check("高价真配合(求购+10单) 不误拦", not r6.details.get("rejected") and r6.score >= 55,
          f"rejected={r6.details.get('rejected')} score={r6.score}")
    # 高价绝对量变化小(求购 5→6,+20% 但仅 1 单)=噪声 → 拦 high_price_stale
    hist7 = jump_hist(17670, 20, 5)
    check("高价绝对量1单(+20%) 拦 high_price_stale",
          eval_reject(mk_snap(18222, 19, 6, buy_price=17000), hist7) == "high_price_stale")
    # 手套武器箱类低价健康品(+3.4% 盘口配合) → 不拦
    hist5 = jump_hist(81.0, 3216, 1099)
    check("低价健康品(+3.4%) 不误拦",
          eval_reject(mk_snap(83.8, 3216, 1099, buy_price=81.0), hist5) == "")


def test_anomaly_buy_depth_gate() -> None:
    """生产配置启用求购 50 门槛，并验证榜单 BUFF+悠悠合计口径。"""
    print("\n[1b] 异动求购深度门槛(生产配置=50)")
    import copy
    cfg = copy.deepcopy(CFG.anomaly)
    cfg.min_anomaly_buy_num = 50
    engine = AnomalyEngine(cfg)
    low = engine.evaluate(mk_snap(100, 80, 49, buy_price=95), flat_hist(100, 80, 49), now=NOW)
    ok = engine.evaluate(mk_snap(100, 80, 50, buy_price=95), flat_hist(100, 80, 50), now=NOW)
    zero = engine.evaluate(mk_snap(100, 80, 0, buy_price=95), flat_hist(100, 80, 0), now=NOW)
    check("求购49拦截", low.details.get("rejected") == "min_anomaly_buy_num",
          str(low.details))
    check("求购50通过该门槛", ok.details.get("rejected") != "min_anomaly_buy_num",
          str(ok.details))
    check("生产配置求购0仍命中基础死盘门槛",
          zero.details.get("rejected") == "min_buy_num", str(zero.details))

    from types import SimpleNamespace
    from bot.anomaly.runner import _snapshot_from_rank

    def rank_snap(buff_buy, yyyp_buy):
        return _snapshot_from_rank(SimpleNamespace(
            good_id=99100, name="双平台门槛品", main_price=100,
            buff_sell_price=100, buff_buy_price=95,
            buff_sell_num=40, yyyp_sell_num=40,
            sell_price_rate_7=0, sell_price_rate_30=0,
            sell_price_7=100, sell_price_30=100, sell_buy_diff_chg_buff=0,
            raw={"buff_buy_num": buff_buy, "yyyp_buy_num": yyyp_buy,
                 "buff_sell_num": 40, "yyyp_sell_num": 40},
        ))

    sum49 = rank_snap(30, 19)
    sum50 = rank_snap(30, 20)
    r49 = engine.evaluate(sum49, flat_hist(100, 80, 49), now=NOW)
    r50 = engine.evaluate(sum50, flat_hist(100, 80, 50), now=NOW)
    check("榜单 BUFF30+悠悠19=49 被拦",
          sum49.buy_num == 49 and r49.details.get("rejected") == "min_anomaly_buy_num",
          f"buy={sum49.buy_num} details={r49.details}")
    check("榜单 BUFF30+悠悠20=50 通过深度门槛",
          sum50.buy_num == 50 and r50.details.get("rejected") != "min_anomaly_buy_num",
          f"buy={sum50.buy_num} details={r50.details}")


# ============================================================
# 2. 池升降级(decide 纯逻辑)
# ============================================================

def test_pool_decide() -> None:
    print("\n[2] 三层池升降级(decide)")
    # 临时 store(不碰运行中库)
    tmp = tempfile.mktemp(suffix=".db")
    store = SnapshotStore(tmp)
    mgr = WatchPoolManager(CFG.anomaly, store)

    def _row(pool, score, quiet=0, enter=None):
        return {"good_id": 1, "name": "t", "pool": pool, "score": score,
                "quiet_count": quiet,
                "enter_time": enter or datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "last_update": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}

    # 新商品
    check("新商品≥70 → S", mgr.decide(None, 80.0)[0] == POOL_S)
    check("新商品≥55 → A", mgr.decide(None, 60.0)[0] == POOL_A)
    check("新商品<55 → B", mgr.decide(None, 30.0)[0] == POOL_B)
    # A→S 升级 / A 连续低分降级
    # V0.3.28:A→S 改为「连续 confirm 轮」确认(单轮尖峰不升级,防 100/14 抖动来回)
    check("A 高分单轮(hit=1) → 仍留 A", mgr.decide(_row(POOL_A, 60.0), 75.0, hit_rounds=1)[0] == POOL_A)
    check("A 高分连轮(hit=2) → S", mgr.decide(_row(POOL_A, 60.0), 75.0, hit_rounds=2)[0] == POOL_S)
    check("A 连续低分 → B", mgr.decide(_row(POOL_A, 60.0, quiet=CFG.anomaly.a_to_b_rounds - 1), 30.0)[0] == POOL_B)
    check("A 低分未满轮 → 留 A", mgr.decide(_row(POOL_A, 60.0, quiet=0), 30.0)[0] == POOL_A)
    # S TTL 过期降级 / 连续低分降级
    old_enter = (datetime.now() - timedelta(minutes=CFG.anomaly.s_max_minutes + 1)).strftime("%Y-%m-%d %H:%M:%S")
    check("S TTL 过期 → A", mgr.decide(_row(POOL_S, 60.0, enter=old_enter), 60.0)[0] == POOL_A)
    check("S 连续低分 → A", mgr.decide(_row(POOL_S, 60.0, quiet=CFG.anomaly.s_to_a_rounds - 1), 40.0)[0] == POOL_A)
    # B 扫描 allow_downgrade=False:只升不降(不干预 A/S 归属)
    check("B扫描 A 低分不降级(allow_downgrade=False)",
          mgr.decide(_row(POOL_A, 60.0), 30.0, allow_downgrade=False)[0] == POOL_A)
    check("B扫描 S 高分仍升(allow_downgrade=False)",
          mgr.decide(_row(POOL_S, 60.0), 75.0, allow_downgrade=False)[0] == POOL_S)

    try:
        os.remove(tmp)
    except OSError:
        pass


def test_b_quality_preserves_high_pools() -> None:
    """B 深翻的质量失败不能清掉 A/S 高频跟踪成员。"""
    print("\n[2b] B 深翻质量失败不清理 A/S")
    tmp = tempfile.mktemp(suffix=".db")
    store = SnapshotStore(tmp)
    mgr = WatchPoolManager(CFG.anomaly, store)

    async def run():
        await store.async_upsert_watch_pool([{
            "good_id": 90001, "name": "A保留品", "pool": POOL_A,
            "score": 60, "last_score": 60,
        }, {
            "good_id": 90002, "name": "S保留品", "pool": POOL_S,
            "score": 80, "last_score": 80,
        }])

        def rejected(gid, name):
            return type("R", (), {
                "good_id": gid, "name": name, "score": 0,
                "level": "L0", "anomaly_type": "normal", "reasons": [],
                "parts": [], "details": {"rejected": "min_listing"},
            })()

        await mgr.sync([
            rejected(90001, "A保留品"), rejected(90002, "S保留品")
        ], allow_downgrade=False)
        rows = await store.async_list_watch_pool(limit=20)
        return {int(r["good_id"]): r.get("pool") for r in rows}

    pools = asyncio.run(run())
    check("B 质量失败保留 A", pools.get(90001) == POOL_A, str(pools))
    check("B 质量失败保留 S", pools.get(90002) == POOL_S, str(pools))
    try:
        os.remove(tmp)
    except OSError:
        pass


def test_quality_reject_grace() -> None:
    """质量闸门连续失败才移除;批量字段缺失始终保留。"""
    print("\n[2c] 质量失败确认与字段缺失保护")
    tmp = tempfile.mktemp(suffix=".db")
    store = SnapshotStore(tmp)
    mgr = WatchPoolManager(CFG.anomaly, store)

    def result(gid, name, incomplete=False):
        details = {"rejected": "min_buy_num"}
        if incomplete:
            details["data_incomplete"] = "buy_num_missing"
        return type("R", (), {
            "good_id": gid, "name": name, "score": 0,
            "level": "L0", "anomaly_type": "normal", "reasons": [],
            "parts": [], "details": details,
        })()

    async def run():
        await store.async_upsert_watch_pool([{
            "good_id": 90003, "name": "瞬态品", "pool": POOL_A,
            "score": 60, "last_score": 60,
        }, {
            "good_id": 90004, "name": "缺字段品", "pool": POOL_S,
            "score": 80, "last_score": 80,
        }])
        await mgr.sync([result(90003, "瞬态品")], allow_downgrade=True)
        first = {int(r["good_id"]): r for r in await store.async_list_watch_pool(limit=20)}
        await mgr.sync([result(90003, "瞬态品")], allow_downgrade=True)
        await mgr.sync([result(90003, "瞬态品")], allow_downgrade=True)
        await mgr.sync([result(90004, "缺字段品", incomplete=True)], allow_downgrade=True)
        final = {int(r["good_id"]): r for r in await store.async_list_watch_pool(limit=20)}
        return first, final

    first, final = asyncio.run(run())
    check("单次质量失败保留", first.get(90003, {}).get("pool") == POOL_A
          and int(first.get(90003, {}).get("quality_reject_count") or 0) == 1,
          str(first.get(90003)))
    check("连续质量失败后移除", 90003 not in final, str(final))
    check("字段缺失不触发移除", final.get(90004, {}).get("pool") == POOL_S,
          str(final.get(90004)))
    try:
        os.remove(tmp)
    except OSError:
        pass


def test_anomaly_buy_depth_never_removes_pool_member() -> None:
    """真实 evaluate→sync：深度不足只拦评分，不能掩盖基础质量失败。"""
    print("\n[2d] 异动资格与基础质量闸门分层")
    import copy
    tmp = tempfile.mktemp(suffix=".db")
    store = SnapshotStore(tmp)
    cfg = copy.deepcopy(CFG.anomaly)
    cfg.min_anomaly_buy_num = 50
    cfg.quality_reject_confirm_rounds = 3
    engine = AnomalyEngine(cfg)
    mgr = WatchPoolManager(cfg, store)

    def evaluate(gid, name, *, buy_num=10, buy_price=95, price=100,
                 sell_num=500, carry=False, history=None):
        snap = MetricSnapshot(
            good_id=gid, name=name, price=price, sell_price=price,
            buy_price=buy_price, sell_num=sell_num, buy_num=buy_num,
            buff_yyyp_complete=True,
        )
        if carry:
            snap.buy_num_observed = False
        result = engine.evaluate(
            snap, history if history is not None
            else flat_hist(price, sell_num, buy_num), now=NOW)
        if carry:
            result.details["data_incomplete"] = "buy_carried_forward"
        return result

    async def run():
        await store.async_upsert_watch_pool([{
            "good_id": 90005, "name": "健康低求购品", "pool": POOL_A,
            "score": 60, "last_score": 60, "quality_reject_count": 2,
        }, {
            "good_id": 90006, "name": "低求购宽价差品", "pool": POOL_A,
            "score": 60, "last_score": 60,
        }, {
            "good_id": 90007, "name": "沿用低求购品", "pool": POOL_S,
            "score": 80, "last_score": 80, "quality_reject_count": 2,
        }, {
            "good_id": 90008, "name": "沿用高价微动品", "pool": POOL_A,
            "score": 60, "last_score": 60,
        }])

        healthy_low = evaluate(90005, "健康低求购品")
        bad_spread = evaluate(90006, "低求购宽价差品", buy_price=10)
        carried_low = evaluate(90007, "沿用低求购品", carry=True)
        high_hist = flat_hist(10000, 500, 60)
        carried_weak = evaluate(
            90008, "沿用高价微动品", buy_num=60, buy_price=9000,
            price=10000, carry=True, history=high_hist)

        # carry-forward 低求购不应触发 min_anomaly，也不应让 S 池立刻降级。
        await mgr.sync([carried_low], allow_downgrade=True)
        for _ in range(5):
            await mgr.sync([
                healthy_low, bad_spread, carried_weak,
            ], allow_downgrade=True)
        rows = await store.async_list_watch_pool(limit=20)
        return ({int(r["good_id"]): r for r in rows},
                healthy_low.details.get("rejected"),
                bad_spread.details.get("rejected"),
                carried_low.details.get("rejected"),
                carried_weak.details.get("rejected"),
                carried_low.details.get("buy_change"),
                carried_low.details.get("order_ratio"))

    (rows, healthy_reason, spread_reason, carry_reason, weak_reason,
     carried_buy_change, carried_order_ratio) = asyncio.run(run())
    check("健康低求购只命中异动资格门槛", healthy_reason == "min_anomaly_buy_num",
          str(healthy_reason))
    check("低求购不再掩盖宽价差质量失败", spread_reason == "spread_too_wide",
          str(spread_reason))
    check("健康求购<50 连续五轮仍保留 A",
          rows.get(90005, {}).get("pool") == POOL_A, str(rows.get(90005)))
    check("真实宽价差连续三轮后移除", 90006 not in rows, str(rows.get(90006)))
    check("carry-forward 不触发异动深度门槛且保留 S",
          carry_reason is None and rows.get(90007, {}).get("pool") == POOL_S,
          str(rows.get(90007)))
    check("carry-forward 不参与求购变化/买卖盘比计算",
          carried_buy_change is None and carried_order_ratio is None,
          f"buy_change={carried_buy_change} ratio={carried_order_ratio}")
    check("high_price_weak 不再被 carry-forward 豁免",
          weak_reason == "high_price_weak" and 90008 not in rows,
          f"reason={weak_reason} row={rows.get(90008)}")
    check("健康/携带深度拦截会清零旧质量拒绝计数",
          int(rows.get(90005, {}).get("quality_reject_count") or 0) == 0
          and int(rows.get(90007, {}).get("quality_reject_count") or 0) == 0,
          str(rows))
    try:
        os.remove(tmp)
    except OSError:
        pass


def test_b_pool_capacity() -> None:
    """B 池必须有数据库全池硬上限，而非只限制本次 upsert。"""
    print("\n[2e] B 池容量上限")
    import copy
    tmp = tempfile.mktemp(suffix=".db")
    store = SnapshotStore(tmp)
    cfg = copy.deepcopy(CFG.anomaly)
    cfg.b_size = 3
    mgr = WatchPoolManager(cfg, store)

    async def run():
        await store.async_upsert_watch_pool([{
            "good_id": 90100 + i, "name": f"B容量{i}", "pool": POOL_B,
            "score": float(i), "last_score": float(i),
        } for i in range(6)])
        await mgr.sync([])
        return await store.async_list_watch_pool(pool=POOL_B, limit=20)

    rows = asyncio.run(run())
    check("B 池超过上限后只留 3 件", len(rows) == 3, str(rows))
    check("B 池按分数保留最高成员",
          [float(r.get("score") or 0) for r in rows] == [5.0, 4.0, 3.0], str(rows))
    try:
        os.remove(tmp)
    except OSError:
        pass


def test_anomaly_buy_grace_timeout() -> None:
    """真实求购长期低于 50 时，有限观察期后可退出，避免永久僵尸。"""
    print("\n[2f] 低求购有限观察期")
    import copy
    tmp = tempfile.mktemp(suffix=".db")
    store = SnapshotStore(tmp)
    cfg = copy.deepcopy(CFG.anomaly)
    cfg.anomaly_buy_grace_minutes = 60
    mgr = WatchPoolManager(cfg, store)
    old = (datetime.now() - timedelta(minutes=61)).strftime("%Y-%m-%d %H:%M:%S")

    async def run():
        await store.async_upsert_watch_pool([{
            "good_id": 90110, "name": "低求购到期品", "pool": POOL_B,
            "score": 30, "last_score": 30,
            "anomaly_buy_low_since": old,
        }])
        result = type("R", (), {
            "good_id": 90110, "name": "低求购到期品", "score": 0,
            "level": "L0", "anomaly_type": "normal", "reasons": [],
            "parts": [], "details": {"rejected": "min_anomaly_buy_num"},
        })()
        await mgr.sync([result])
        return await store.async_list_watch_pool(pool=POOL_B, limit=20)

    rows = asyncio.run(run())
    check("真实低求购超过观察期后移除", not rows, str(rows))
    try:
        os.remove(tmp)
    except OSError:
        pass


# ============================================================
# 3. 冷却:不重复推送 / 评分升级突破冷却
# ============================================================

def _mk_result(good_id, name, score, level="L2", rejected=None, atype="surge"):
    details = {"rejected": rejected} if rejected else {}
    return type("R", (), {"good_id": good_id, "name": name, "score": score, "level": level,
                          "anomaly_type": atype, "details": details,
                          "direction": "UP", "reasons": ["测试"], "parts": {}})()


def test_cooldown() -> None:
    print("\n[3] 推送冷却(不重复报 / 升级突破)")
    import sqlite3
    from bot.anomaly.runner import _persist_events

    tmp = tempfile.mktemp(suffix=".db")
    store = SnapshotStore(tmp)

    def count_events():
        c = sqlite3.connect(tmp)
        n = c.execute("SELECT COUNT(*) FROM anomaly_events").fetchone()[0]
        c.close()
        return n

    async def run():
        now = datetime.now()
        # 冷却期内:cooldown_until 在未来 30 分钟,last_score=60,本次评分 62(升级<10) → 不落事件
        await store.async_set_anomaly_cooldown(
            1, "冷却品A", (now + timedelta(minutes=30)).strftime("%Y-%m-%d %H:%M:%S"), 60.0)
        n1 = count_events()
        await _persist_events(CFG.anomaly, store, [_mk_result(1, "冷却品A", 62.0)], POOL_A)
        n2 = count_events()
        # 评分升级 ≥10 突破冷却:last_score=50,本次 70 → 落事件
        await store.async_set_anomaly_cooldown(
            2, "冷却品B", (now + timedelta(minutes=30)).strftime("%Y-%m-%d %H:%M:%S"), 50.0)
        await _persist_events(CFG.anomaly, store, [_mk_result(2, "冷却品B", 70.0, "L3")], POOL_A)
        n3 = count_events()
        return n1, n2, n3

    n1, n2, n3 = asyncio.run(run())
    check("冷却期内(未升级) 不重复落事件", n2 == n1, f"{n1}->{n2}")
    check("评分升级≥10 突破冷却 落事件", n3 == n1 + 1, f"{n1}->{n3}")


def test_same_wave_skip() -> None:
    """[3b] 冷却过期后同一波异动延续(同分同级) → 顺延冷却不重推(V0.0.13,X 射线重推事故)。"""
    import sqlite3
    from bot.anomaly.runner import _persist_events

    tmp = tempfile.mktemp(suffix=".db")
    store = SnapshotStore(tmp)

    def count_events():
        c = sqlite3.connect(tmp)
        n = c.execute("SELECT COUNT(*) FROM anomaly_events").fetchone()[0]
        cd = c.execute("SELECT cooldown_until FROM anomaly_push_cooldown WHERE good_id=? AND name=?",
                       (7, "同波品G")).fetchone()
        c.close()
        return n, (cd[0] if cd else None)

    async def run():
        now = datetime.now()
        # 冷却已过期 5 分钟,last_score=71.3,本次评分仍 71.3(L3 同级,分差 0)
        past = (now - timedelta(minutes=5)).strftime("%Y-%m-%d %H:%M:%S")
        await store.async_set_anomaly_cooldown(7, "同波品G", past, 71.3)
        n1, cd1 = count_events()
        await _persist_events(CFG.anomaly, store, [_mk_result(7, "同波品G", 71.3, "L3")], POOL_A)
        n2, cd2 = count_events()
        # 对照:分差 ≥5(71.3 → 78,L3 同级) → 视为新异动,落事件
        await store.async_set_anomaly_cooldown(
            8, "变化品H", past, 71.3)
        n3, _ = count_events()
        await _persist_events(CFG.anomaly, store, [_mk_result(8, "变化品H", 78.0, "L3")], POOL_A)
        n4, _ = count_events()
        return n1, n2, cd1, cd2, n3, n4

    n1, n2, cd1, cd2, n3, n4 = asyncio.run(run())
    check("同波延续(同分同级,冷却过期) 不重复落事件", n2 == n1, f"{n1}->{n2}")
    check("同波延续 冷却被顺延到未来", bool(cd2) and cd2 > cd1, f"{cd1} -> {cd2}")
    check("分差≥5 视为新异动 落事件", n4 == n3 + 1, f"{n3}->{n4}")

    try:
        os.remove(tmp)
    except OSError:
        pass


def test_phase_progression() -> None:
    """[3c] 周期形态升级放行(V0.2.0):建仓→启动→派发是递进信息,同分也必须推;
    同类型延续仍走同波顺延。依赖 anomaly_push_cooldown.last_type 列。"""
    import sqlite3
    from bot.anomaly.runner import _persist_events

    tmp = tempfile.mktemp(suffix=".db")
    store = SnapshotStore(tmp)

    def count_events():
        c = sqlite3.connect(tmp)
        n = c.execute("SELECT COUNT(*) FROM anomaly_events").fetchone()[0]
        c.close()
        return n

    async def run():
        now = datetime.now()
        past = (now - timedelta(minutes=5)).strftime("%Y-%m-%d %H:%M:%S")
        # 预置:上次推送为 accum(L2, 55 分),冷却已过期
        await store.async_set_anomaly_cooldown(9, "周期品I", past, 55.0, last_type="accum")
        n1 = count_events()
        # 形态升级 accum→launch(同分同级) → 必须放行落库
        await _persist_events(CFG.anomaly, store,
                              [_mk_result(9, "周期品I", 55.0, "L2", atype="launch")], POOL_A)
        n2 = count_events()
        # 同类型 launch→launch(同分同级) → 同波顺延,不落库
        await _persist_events(CFG.anomaly, store,
                              [_mk_result(9, "周期品I", 55.0, "L2", atype="launch")], POOL_A)
        n3 = count_events()
        return n1, n2, n3

    n1, n2, n3 = asyncio.run(run())
    check("形态升级 accum→launch 同分也放行", n2 == n1 + 1, f"{n1}->{n2}")
    check("同类型 launch 延续 不重复落事件", n3 == n2, f"{n2}->{n3}")

    try:
        os.remove(tmp)
    except OSError:
        pass


# ============================================================
# 4. 401/429 状态码分类(重试/不重试)
# ============================================================

def test_http_status_class() -> None:
    print("\n[4] HTTP 状态码分类(401 直接失败不重试 / 429 退避重试)")
    from bot.csqaq.client import CSQAQClient
    check("401(IP白名单) 不重试", not CSQAQClient._is_retryable_status(401))
    check("429(限频) 重试", CSQAQClient._is_retryable_status(429))
    check("403/503 重试", CSQAQClient._is_retryable_status(403) and CSQAQClient._is_retryable_status(503))
    check("500(服务错) 不重试", not CSQAQClient._is_retryable_status(500))


def test_ip_rebind_single_flight() -> None:
    print("\n[4b] CSQAQ 漂移重绑单飞(同一时刻只重绑一次)")
    import bot.csqaq.client as cmod

    cmod._IP_REBIND_LOCK = None
    cmod._IP_REBIND_UNTIL = 0.0
    cmod._IP_DRIFT_UNTIL = 0.0
    cmod._IP_LAST_DRIFT_IP = None

    calls = {"bind": 0, "probe": 0}

    class FakeClient:
        def __init__(self, cfg):
            self.cfg = cfg

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def bind_local_ip(self):
            calls["bind"] += 1
            await asyncio.sleep(0.05)
            return True, "绑定IP更新成功"

        async def probe_ip_status(self, *, allow_drift_skip=True, allow_rebind=True):
            calls["probe"] += 1
            return True, None

    orig = cmod.CSQAQClient
    cmod.CSQAQClient = FakeClient

    async def run():
        return await asyncio.gather(
            cmod.try_rebind_local_ip(CFG.csqaq, ip_hint="1.1.1.1", source="test", cooldown_seconds=1),
            cmod.try_rebind_local_ip(CFG.csqaq, ip_hint="1.1.1.1", source="test", cooldown_seconds=1),
        )

    try:
        res = asyncio.run(run())
        check("并发两次重绑仅成功一次", sum(1 for ok, _ in res if ok) == 1, f"res={res}")
        check("绑定接口仅调用一次", calls["bind"] == 1, f"bind={calls['bind']}")
        check("复测接口仅调用一次", calls["probe"] == 1, f"probe={calls['probe']}")
    finally:
        cmod.CSQAQClient = orig

    # 重绑冷却可从 config.yaml 配置(改 300 无需动代码)
    check("rebind_cooldown_seconds 可配且>0",
          getattr(CFG.csqaq, "rebind_cooldown_seconds", 0) > 0,
          f"val={getattr(CFG.csqaq, 'rebind_cooldown_seconds', 0)}")


# ============================================================
# 5. 推送前复核价格带(全平台在售)
# ============================================================

def test_push_review_tiers() -> None:
    print("\n[5] 推送前复核价格带(BUFF+悠悠在售阈值)")
    a = CFG.anomaly
    low = a.platform_sell_low
    high = a.platform_sell_high
    tier = a.platform_sell_tier

    def need(price):
        return low if price < tier else high

    # 灼木类:¥19 全平台 95 < 200 → 拦
    check("¥19 全平台95 需≥200 → 拦", 95 < need(19.0))
    # 手套武器箱:¥84 全平台 8万 ≥ 200 → 放
    check("¥84 全平台8万 ≥200 → 放", 80941 >= need(83.8))
    # 高价且有足够全平台供给:559 ≥ 200 → 放
    check("¥14000 全平台559 ≥200 → 放", 559 >= need(14089))
    # 咆哮/潘多拉破损类高价薄盘:130/82 < 200 → 拦
    check("咆哮全平台130 <200 → 拦", 130 < need(27790))
    check("潘多拉破损全平台82 <200 → 拦", 82 < need(20500))
    # 0 件也必须拦，不能被 truthy 判断绕过
    check("全平台在售0 <200 → 拦", 0 < need(8000))


def test_push_review_resolution() -> None:
    """低流动性是终态；同品积压只复核/发送一次；瞬态失败仍可重试。"""
    print("\n[5b] 推送复核终态归档与同品合并")
    import copy
    import sqlite3
    from bot.anomaly.runner import anomaly_push_loop

    tmp = tempfile.mktemp(suffix=".db")
    store = SnapshotStore(tmp)
    cfg = copy.deepcopy(CFG.anomaly)
    cfg.push_top_n = 10

    def event(gid, name, atype="surge", score=60.0):
        return {
            "good_id": gid, "name": name, "anomaly_type": atype,
            "drop_pct": 0, "abs_drop": 0, "window_min": 0,
            "sell_prev": 0, "sell_cur": 0, "level": "L2",
            "detail": "测试", "score": score, "pool": POOL_A,
            "price": 100.0,
        }

    store.insert_anomaly_events([
        event(91001, "低流动品", "surge", 60),
        event(91001, "低流动品", "accum", 55),
        event(91002, "健康品", "surge", 60),
        event(91002, "健康品", "launch", 65),
        event(91003, "接口失败品", "surge", 60),
    ])

    calls = {91001: 0, 91002: 0, 91003: 0}

    class FakeApi:
        async def get_good_info(self, gid):
            calls[gid] += 1
            if gid == 91003:
                raise RuntimeError("temporary 429")
            total = 100 if gid == 91001 else 300
            return {
                "name": "低流动品" if gid == 91001 else "健康品",
                "buff_sell_num": total, "steam_sell_num": 0,
                "yyyp_sell_num": 0, "c5_sell_num": 0,
                "igxe_sell_num": 0, "eco_sell_num": 0,
                "buff_sell_price": 100.0, "buff_buy_price": 95.0,
            }

    class FakeClient:
        _cfg = None
        _anomaly_targets = ["test-group"]

        def __init__(self):
            self.messages = []

        async def push_to_group(self, gid, text):
            self.messages.append((gid, text))
            return True

    client = FakeClient()

    async def run_once():
        task = asyncio.create_task(anomaly_push_loop(cfg, store, client, FakeApi()))
        try:
            # V0.3.23:轮询要显式 close 连接 —— `with sqlite3.connect()` 只提交不关闭,
            # 每 10ms 泄漏一个句柄;WAL 下句柄抖动会触发瞬时 "readonly database"。
            # 同时把轮询间隔放到 0.1s、窗口放到 30s,减少与写入线程抢文件句柄。
            for _ in range(300):
                await asyncio.sleep(0.1)
                conn = sqlite3.connect(tmp, timeout=10)
                try:
                    done = conn.execute(
                        "SELECT COUNT(*) FROM anomaly_events "
                        "WHERE good_id IN (91001,91002) AND push_status='pending'"
                    ).fetchone()[0] == 0
                finally:
                    conn.close()
                if done:
                    break
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    asyncio.run(run_once())

    with sqlite3.connect(tmp) as conn:
        low = conn.execute(
            "SELECT push_status,last_push_at,push_detail FROM anomaly_events "
            "WHERE good_id=91001 ORDER BY id"
        ).fetchall()
        healthy = conn.execute(
            "SELECT push_status,last_push_at FROM anomaly_events "
            "WHERE good_id=91002 ORDER BY id"
        ).fetchall()
        transient = conn.execute(
            "SELECT push_status,last_push_at FROM anomaly_events WHERE good_id=91003"
        ).fetchone()

    check("同品两事件只复核一次", calls[91001] == 1 and calls[91002] == 1,
          str(calls))
    check("低流动性事件归档且不伪装已发送",
          len(low) == 2 and all(r[0] == "rejected_liquidity" and r[1] is None for r in low),
          str(low))
    check("积压同品只发最新一条并归档旧事件",
          len(client.messages) == 1
          and sorted(r[0] for r in healthy) == ["sent", "superseded"],
          f"messages={len(client.messages)} rows={healthy}")
    check("瞬态接口失败保留待重试",
          transient is not None and transient[0] == "pending" and transient[1] is None,
          str(transient))
    multi = store.get_multi_snapshot([91001]).get(91001)
    check("低流动性实时复核也写缓存",
          bool(multi) and int(multi.get("buff_sell_num") or 0) == 100, str(multi))
    remaining = store.list_pending_anomalies(top_n=10, min_score=cfg.level_warning)
    check("已拒绝/已发送事件不再占待推队列",
          [int(r["good_id"]) for r in remaining] == [91003], str(remaining))

    try:
        os.remove(tmp)
    except OSError:
        pass


def test_scan_sell_filter() -> None:
    """扫描前置过滤:全平台在售<阈值的品不落历史、不评分、不留监控。"""
    print("\n[5c] 扫描前置在售过滤")
    import copy
    from bot.anomaly.runner import _TokenBucket, _scan_pool
    from bot.csqaq.client import RankItem

    tmp = tempfile.mktemp(suffix=".db")
    store = SnapshotStore(tmp)
    cfg = copy.deepcopy(CFG.anomaly)
    cfg.min_scan_sell_num = 400
    engine = AnomalyEngine(cfg)
    pool_mgr = WatchPoolManager(cfg, store)

    def rank(gid, name, counts):
        keys = ("buff", "yyyp")
        raw = {"%sSellNum" % k: v for k, v in zip(keys, counts)}
        raw.update({"buffBuyNum": 20, "yyypBuyNum": 0})
        return RankItem(good_id=gid, name=name, buff_sell_price=100,
                        buff_buy_price=95, buff_sell_num=counts[0], yyyp_sell_num=counts[1],
                        buff_buy_num=20,
                        raw=raw)

    low = rank(92001, "低在售品", (100, 299))       # BUFF+悠悠399
    healthy = rank(92002, "达标品", (100, 300))    # BUFF+悠悠400
    buff_low_but_total_ok = rank(92003, "BUFF少但悠悠补足", (1, 399))
    incomplete = RankItem(good_id=92004, name="字段不完整品", buff_sell_price=100,
                          buff_buy_price=95, buff_sell_num=199, buff_buy_num=20,
                          raw={"buffSellNum": 199})

    class FakeApi:
        async def get_rank_list(self, **kwargs):
            return [low, healthy, buff_low_but_total_ok, incomplete]

    async def run():
        return await _scan_pool(cfg, FakeApi(), store, engine, pool_mgr,
                                _TokenBucket(1000), ["test"], 1, POOL_B)

    results = asyncio.run(run())
    history = store.get_listing_series_batch([92001, 92002, 92003, 92004], hours=24)
    watch = {int(r["good_id"]): r for r in store.list_watch_pool(limit=20)}
    check("低在售品不进入评分结果",
          all(int(r.good_id) != 92001 for r in results), str(results))
    check("低在售品不写历史且不留监控",
          92001 not in history and 92001 not in watch,
          f"history={history.keys()} watch={watch.keys()}")
    check("在售恰好400的品保留",
          92002 in history and 92002 in watch,
          f"history={history.keys()} watch={watch.keys()}")
    check("BUFF少但悠悠补足达标的品保留",
          92003 in history and 92003 in watch,
          f"history={history.keys()} watch={watch.keys()}")
    check("两平台字段不完整不进入扫描",
          92004 not in history and 92004 not in watch,
          f"history={history.keys()} watch={watch.keys()}")

    store.upsert_multi_snapshot([
        {"good_id": 92002, "name": "达标品", "buff_sell_num": 100,
         "yyyp_sell_num": 300, "sell_scope": "buff_yyyp"},
        {"good_id": 92003, "name": "BUFF少但悠悠补足", "buff_sell_num": 1,
         "yyyp_sell_num": 399, "sell_scope": "buff_yyyp"},
        {"good_id": 92006, "name": "存量低在售", "buff_sell_num": 100,
         "yyyp_sell_num": 299, "sell_scope": "buff_yyyp"},
    ])
    store.upsert_watch_pool([
        {"good_id": 92005, "name": "存量字段未知", "pool": POOL_B},
        {"good_id": 92006, "name": "存量低在售", "pool": POOL_B},
    ])
    removed = store.purge_watch_pool_low_sell(400)
    after_purge = {int(r["good_id"]): r for r in store.list_watch_pool(limit=20)}
    check("存量B池未知/低在售清理，完整达标保留",
          removed == 2 and 92005 not in after_purge and 92006 not in after_purge
          and 92002 in after_purge and 92003 in after_purge,
          f"removed={removed} watch={after_purge.keys()}")
    try:
        os.remove(tmp)
    except OSError:
        pass


# ============================================================
# 7. 盘口信号三重判定(百分比+绝对量+基线):500→550 强 / 5→6 噪声
# ============================================================

def _signal_case(price, sell, buy, prev_sell, prev_buy, buy_price=None):
    """跑 evaluate 返回 (buy_signal, parts, reasons)。"""
    hist = jump_hist(price, prev_sell, prev_buy)
    snap = mk_snap(price, sell, buy, buy_price or price * 0.95)
    r = ENGINE.evaluate(snap, hist, now=NOW)
    d = r.details or {}
    return d.get("buy_signal"), d.get("buy_delta_abs"), d.get("buy_base"), r.parts, r.reasons, r.score


def test_book_signal_triple() -> None:
    print("\n[7] 盘口信号三重判定(500→550 强 / 5→6 噪声)")
    # 500→550: 高基线 abs_req=15,+50 ✓ 且 +10%>8% → strong 且 buy_change 计分
    sig, delta, base, parts, reasons, score = _signal_case(500, 300, 550, 300, 500)
    check("500→550 判 strong", sig == "strong", f"sig={sig} delta={delta} base={base}")
    check("500→550 buy_change 计分", parts.get("buy_change", 0) > 0, f"parts={parts.get('buy_change')}")
    check("500→550 原因白盒(含+50单)", any("+50单" in x or "强信号" in x for x in reasons), f"{reasons[:2]}")
    # 5→6: 低基线 abs_req=3,+1 ✗ → weak(噪声),buy_change 不计分
    sig2, delta2, base2, parts2, reasons2, score2 = _signal_case(100, 50, 6, 50, 5)
    check("5→6 判 weak(噪声)", sig2 == "weak", f"sig={sig2}")
    check("5→6 buy_change 不计分", parts2.get("buy_change", 0) == 0, f"{parts2.get('buy_change')}")
    check("5→6 原因标注噪声", any("噪声" in x for x in reasons2), f"{reasons2[:2]}")
    # 中基线 20→28(+8,+40%): abs_req=5 ✓ → strong
    sig3, delta3, base3, *_ = _signal_case(150, 100, 28, 100, 20)
    check("20→28(+8单,+40%) 判 strong", sig3 == "strong", f"sig={sig3}")
    # 中基线 20→21(+1,+5%): pct 5%<8 ✗ → None(温和波动,非信号)
    sig4, *_ = _signal_case(150, 100, 21, 100, 20)
    check("20→21(+1,+5%) 温和 非信号", sig4 in (None, "weak"), f"sig={sig4}")


# ============================================================
# 8. 池新阈值 + 连续两轮确认 + A 僵尸回落
# ============================================================

def test_pool_confirm_and_new_thresholds() -> None:
    print("\n[8] 池新阈值(52/38/12)+两轮确认+僵尸回落")
    tmp = tempfile.mktemp(suffix=".db")
    store = SnapshotStore(tmp)
    mgr = WatchPoolManager(CFG.anomaly, store)
    a = CFG.anomaly

    def _row(pool, score, hit=0, quiet=0, last_update=None, enter=None):
        return {"good_id": 1, "name": "t", "pool": pool, "score": score, "last_score": score,
                "hit_count": hit, "quiet_count": quiet,
                "last_update": last_update or datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "enter_time": enter or datetime.now().strftime("%Y-%m-%d %H:%M:%S")}

    # 新阈值:b_to_a=52 / a_to_b=38 / rounds=12
    check("新商品≥52 → A", mgr.decide(None, 52.0)[0] == POOL_A)
    check("新商品51 → B", mgr.decide(None, 51.0)[0] == POOL_B)
    check("A 低分37未满12轮 留 A", mgr.decide(_row(POOL_A, 60, quiet=10), 37.0)[0] == POOL_A)
    check("A 低分37已满12轮 → B", mgr.decide(_row(POOL_A, 60, quiet=11), 37.0)[0] == POOL_B)
    # 存量 B→A 两轮确认:普通高分第一轮不升(hit=1),第二轮(hit=2)升 A
    b1 = _row(POOL_B, 45, hit=1)
    check("B 高分第1轮(hit=1) 留 B 观察",
          mgr.decide(b1, 60.0, hit_rounds=1)[0] == POOL_B)
    b2 = _row(POOL_B, 45, hit=2)
    check("B 高分第2轮(hit=2) → A", mgr.decide(b2, 60.0, hit_rounds=2)[0] == POOL_A)
    # 强信号单轮直升(500→550 类):hit=0 但 strong=True → A
    b3 = _row(POOL_B, 45, hit=0)
    check("B 强信号单轮(strong) → A", mgr.decide(b3, 60.0, strong=True)[0] == POOL_A)
    async def run_sync_confirm():
        await store.async_upsert_watch_pool([_row(POOL_B, 45, hit=1)])
        class R:
            good_id = 1
            name = "t"
            score = 52.0
            level = "L2"
            anomaly_type = "normal"
            reasons = []
            parts = {}
            details = {}
        await mgr.sync([R()], allow_downgrade=True)
        rows = await store.async_list_watch_pool(limit=10)
        return rows[0]

    synced = asyncio.run(run_sync_confirm())
    check("B 52 两轮确认后升A", synced["pool"] == POOL_A and int(synced["hit_count"]) >= 2,
          f"pool={synced['pool']} hit={synced['hit_count']}")
    # A 僵尸回落:超 a_stale_minutes 未更新且 last_score<b_to_a → 降 B
    old_ts = (datetime.now() - timedelta(minutes=a.a_stale_minutes + 10)).strftime("%Y-%m-%d %H:%M:%S")
    az = _row(POOL_A, 30, last_update=old_ts)
    tgt, why = mgr.decide(az, 30.0)
    check("A 僵尸(3h+ 未更新且分低) → B", tgt == POOL_B and why == "stale_off_list",
          f"->{tgt}/{why}")
    # 非僵尸(近期更新)不受 stale 影响
    af = _row(POOL_A, 30, last_update=datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    check("A 近期更新 非僵尸 留 A", mgr.decide(af, 30.0)[0] == POOL_A)

    try:
        os.remove(tmp)
    except OSError:
        pass

def test_steamdt_observability() -> None:
    print("\n[6] SteamDT 可观测性(缓存命中不打真实成功标记 / 限频可单独捕获)")
    import time as _time
    from bot.steamdt.client import SteamDTClient, SteamDTRateLimit, _BROAD_INDEX_PATH, _CachedResp
    sdt = SteamDTClient(CFG.steamdt)
    # 限频异常是 RuntimeError 子类:调用方用 except SteamDTRateLimit 精确接住,不落 generic 故障
    check("限频异常可单独捕获", issubclass(SteamDTRateLimit, RuntimeError))

    async def cache_hit():
        # 预置未过期缓存 → get_broad_index 走缓存路径,不打网络 → _last_network_ok 必须保持 0
        # (V0.0.1 起缓存改为 _CachedResp 结构,非老 (expire, data) tuple)
        data = {"broadMarketIndex": 100.0, "diffYesterday": 1.0,
                "diffYesterdayRatio": 0.5, "updateTime": 1700000000,
                "historyMarketIndexList": []}
        sdt._cache[_BROAD_INDEX_PATH] = _CachedResp(_time.time() + 300, data)
        r, network_ok = await sdt.get_broad_index_ex()
        return r, network_ok

    try:
        r, network_ok = asyncio.run(cache_hit())
        check("缓存命中返回数据", r["index"] == 100.0)
        check("缓存命中不置真实网络成功标记", network_ok is False, f"network_ok={network_ok}")
    finally:
        asyncio.run(sdt.close())  # 关会话,避免测试残留警告


def test_steamdt_backoff_escalation() -> None:
    """[9c] V0.2.4/V0.3.14 退避:零网络+锁不清+静默续锁+连续失败逐级加长到 3600s。

    V0.3.14 变更(2026-10-12 实测):broad 限的是**请求间隔(约 30 秒)**,不是配额总量
    (间隔 1.5s/15s 必回 4005,30s/60s 必成功)。所以旧断言"首轮探测真打 3 次(换口)"
    已作废 —— 那 3 次重试正是"自己制造 4005 再把接口族锁 1800 秒"的元凶,现在首轮只打 1 次。
    本用例改用小间隔/小退避的客户端来验证**逐级加长**这条独立逻辑。
    """
    import time as _time
    import json as _json
    from bot.config import SteamDTConfig
    from bot.steamdt.client import SteamDTClient, SteamDTRateLimit

    calls = {"n": 0}

    async def fake_curl(method, path, payload):
        calls["n"] += 1
        return 200, _json.dumps({"success": False, "errorCode": 4005,
                                 "errorMsg": "当前接口请求已达到上限"})

    # ---- ① 真实配置:首轮只打 1 次 + 退避期内零网络 ----
    cfg = load_config("config.yaml")
    sdt = SteamDTClient(cfg.steamdt)
    sdt._curl_request = fake_curl

    async def probe(c):
        try:
            await c.get_broad_index_ex(force=True)
            return "ok"
        except SteamDTRateLimit:
            return "rl"

    async def run():
        r1 = await probe(sdt)
        n1 = calls["n"]
        r2 = await probe(sdt)                  # 退避期内:0 网络、立即抛
        n2 = calls["n"]
        return r1, r2, n1, n2, sdt.broad_backoff_active()

    r1, r2, n1, n2, lock_kept = asyncio.run(run())
    check("首轮探测只打 1 次网络(V0.3.14:不再 1.5 秒连试 3 次)", n1 == 1, str(n1))
    check("退避期内探测零网络", n2 == n1 and r2 == "rl", f"{n1}->{n2}")
    check("退避锁未被清", lock_kept)

    # ---- ② 逐级加长:用短间隔客户端连续真失败,streak 8 时应到 3600s ----
    calls2 = {"n": 0}

    async def fake_curl2(method, path, payload):
        calls2["n"] += 1
        return 200, _json.dumps({"success": False, "errorCode": 4005,
                                 "errorMsg": "当前接口请求已达到上限"})

    cfg2 = SteamDTConfig(api_key="k" * 32, request_interval=0.0,
                         broad_min_gap_seconds=0.0, broad_cooldown_seconds=60.0,
                         broad_retry_delay_seconds=999.0)
    sdt2 = SteamDTClient(cfg2)
    sdt2._curl_request = fake_curl2

    async def run2():
        ladder = []
        for _ in range(9):
            sdt2._rate_limited["broad"] = 0.0     # 模拟退避到点(不睡觉)
            await probe(sdt2)
            ladder.append(round(sdt2._rate_limited["broad"] - _time.time()))
        return ladder

    ladder = asyncio.run(run2())
    check("首次失败退避 = 60s(基础值,实测 30s 就能恢复)", ladder[0] <= 60, str(ladder))
    check("连续失败逐级加长(≥300 → ≥900 → ≥3600)",
          any(200 <= x <= 400 for x in ladder)
          and any(800 <= x <= 1000 for x in ladder)
          and max(ladder) >= 3500, str(ladder))


def test_steamdt_family_backoff() -> None:
    import time as _time
    """[9b] V0.2.1 SteamDT 限频退避按接口族隔离:大盘 4005 不得阻塞 K 线(配额独立)。
    事故: 2026-09-06 17:57 /分析 狮鹫无走势图——大盘配额耗尽的全局退避锁误杀 K 线。"""
    import json as _json
    from bot.steamdt.client import (
        SteamDTClient, SteamDTRateLimit,
    )
    cfg = load_config("config.yaml")
    sdt = SteamDTClient(cfg.steamdt)
    kline_body = _json.dumps(
        {"success": True, "data": [[1693795200, 93.02, 79.50, 94.10, 78.80]]})

    async def fake_curl(method, path, payload):
        return 200, kline_body

    sdt._curl_request = fake_curl

    # 场景1: 大盘族退避中 → K 线仍正常(配额独立,不误杀)
    sdt._rate_limited["broad"] = _time.time() + 1800

    async def run1():
        k = await sdt.get_item_kline("M4A4 | Griffin (Factory New)", periods=2)
        return len(k.get("points") or [])

    n = asyncio.run(run1())
    check("大盘退避不阻塞 K 线(V0.2.1 修复)", n == 1, f"points={n}")

    # 场景2: kline 族退避中且无缓存 → 正确抛限频(退避本身仍有效)
    sdt._rate_limited["kline"] = _time.time() + 1800

    async def run2():
        try:
            await sdt.get_item_kline("XX", periods=2, force=True)
            return "no-exc"
        except SteamDTRateLimit:
            return "rate-limit"

    r = asyncio.run(run2())
    check("kline 族退避正确生效", r == "rate-limit", r)

    # 场景3: 族隔离对称性——kline 退避不影响 broad 缓存命中路径
    sdt._rate_limited["broad"] = 0.0
    check("族隔离: broad/kline 退避互不干扰",
          sdt._rate_limited["kline"] > _time.time() and sdt._rate_limited["broad"] == 0.0)


def main() -> None:
    print("=" * 60)
    print(f"异动雷达回归测试  配置: config.yaml")
    print("=" * 60)
    test_engine_gates()
    test_anomaly_buy_depth_gate()
    test_pool_decide()
    test_b_quality_preserves_high_pools()
    test_quality_reject_grace()
    test_anomaly_buy_depth_never_removes_pool_member()
    test_b_pool_capacity()
    test_anomaly_buy_grace_timeout()
    test_cooldown()
    test_same_wave_skip()
    test_phase_progression()
    test_http_status_class()
    test_ip_rebind_single_flight()
    test_push_review_tiers()
    test_push_review_resolution()
    test_scan_sell_filter()
    test_steamdt_observability()
    test_steamdt_family_backoff()
    test_steamdt_backoff_escalation()
    test_book_signal_triple()
    test_pool_confirm_and_new_thresholds()
    print()
    print(f"结果: 通过 {_PASS} / 失败 {_FAIL}")
    sys.exit(1 if _FAIL else 0)


if __name__ == "__main__":
    main()
