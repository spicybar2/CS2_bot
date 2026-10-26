# -*- coding: utf-8 -*-
"""用真实历史事件回放「成本门槛观察」的判定,看线上会是什么分布。

口径与线上一致:事件时点 T 之前**已完结交易日**的日收盘作基准,事件时点的报价作买入价。
只读库,不写任何东西。
"""
import sqlite3
import statistics
import sys
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from bot.anomaly.cost import (CostGateConfig, PlatformQuote, VERDICT_BLOCK,  # noqa: E402
                              VERDICT_PASS, VERDICT_SKIP, build_observation,
                              rank_observations, summarize)

DB = ROOT / "data" / "csqaq.db"
CFG = CostGateConfig()      # 与 config.yaml 默认一致


def parse(ts):
    for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(ts, fmt)
        except (ValueError, TypeError):
            continue
    return None


def quotes_at(c, gid, t, days):
    """复刻 SnapshotStore.get_price_quotes,但只取 T 之前的数据。"""
    cutoff = (t - timedelta(days=days + 1)).strftime("%Y-%m-%d %H:%M:%S")
    tstr = t.strftime("%Y-%m-%d %H:%M:%S")
    today = t.strftime("%Y-%m-%d")
    out = {}
    latest = c.execute(
        "SELECT ts, buff_sell_price, buff_buy_price, buff_sell_num, "
        "       yyyp_sell_price, yyyp_buy_price FROM item_listing_history "
        "WHERE good_id=? AND ts <= ? ORDER BY ts DESC LIMIT 1", (gid, tstr)).fetchone()
    if not latest:
        return {}
    closes = c.execute(
        "SELECT MAX(ts) mts, buff_sell_price bp, yyyp_sell_price yp "
        "FROM item_listing_history WHERE good_id=? AND ts >= ? AND ts <= ? "
        "AND substr(ts,1,10) < ? GROUP BY substr(ts,1,10) ORDER BY mts",
        (gid, cutoff, tstr, today)).fetchall()
    for name, ask, bid, num in (("buff", latest["buff_sell_price"], latest["buff_buy_price"],
                                 latest["buff_sell_num"]),
                                ("yyyp", latest["yyyp_sell_price"], latest["yyyp_buy_price"], 0)):
        cl = [(float(r["bp"]), float(r["yp"])) for r in closes]
        seq = [(a if name == "buff" else b) for a, b in cl]
        seq = [x for x in seq if x > 0][-days:]
        ts0 = parse(latest["ts"])
        age = (t - ts0).total_seconds() / 60.0 if ts0 else 1e9
        out[name] = PlatformQuote(platform=name, ask=float(ask or 0), bid=float(bid or 0),
                                  sell_num=int(num or 0), ask_age_min=max(0.0, age),
                                  closes=seq)
    return out


def main():
    c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    events = c.execute(
        "SELECT good_id, name, ts, score, level, pool, push_status FROM anomaly_events "
        "WHERE score >= 40 AND good_id > 0 AND ts >= datetime('now','-10 day') "
        "ORDER BY ts DESC LIMIT 400").fetchall()
    print(f"=== 回放 {len(events)} 条真实异动事件(近 10 天,score>=40) ===\n")

    obs_list = []
    for e in events:
        t = parse(e["ts"])
        if not t:
            continue
        qq = quotes_at(c, e["good_id"], t, CFG.baseline_days)
        if not qq:
            continue
        obs_list.append(build_observation(
            e["good_id"], e["ts"], qq, CFG, name=e["name"], pool=e["pool"] or "",
            level=e["level"] or "", score=float(e["score"] or 0),
            push_status=e["push_status"] or ""))
    if not obs_list:
        print("  无有效样本")
        return

    passed = [o for o in obs_list if o.verdict == VERDICT_PASS]
    blocked = [o for o in obs_list if o.verdict == VERDICT_BLOCK]
    skipped = [o for o in obs_list if o.verdict == VERDICT_SKIP]
    n = len(obs_list)
    print(summarize(obs_list))
    print(f"  达标 {len(passed)/n*100:.1f}%  未达标 {len(blocked)/n*100:.1f}%  "
          f"跳过 {len(skipped)/n*100:.1f}%\n")

    if skipped:
        reasons = {}
        for o in skipped:
            key = o.reason.split("(")[0]
            reasons[key] = reasons.get(key, 0) + 1
        print("  跳过原因:")
        for k, v in sorted(reasons.items(), key=lambda x: -x[1]):
            print(f"    {k:<22} {v}")
        print()

    edges = sorted(o.edge_pct for o in blocked + passed)
    if edges:
        k = len(edges)
        print(f"  边际分布(n={k}): 中位 {statistics.median(edges):+.2f}%  "
              f"P10 {edges[int(k*0.1)]:+.2f}%  P90 {edges[int(k*0.9)]:+.2f}%")
        print(f"  边际 >0 的占比 {sum(1 for x in edges if x > 0)/k*100:.1f}%")

    print("\n  买入平台分布:")
    plat = {}
    for o in passed + blocked:
        plat[o.buy_platform] = plat.get(o.buy_platform, 0) + 1
    for k, v in sorted(plat.items(), key=lambda x: -x[1]):
        print(f"    {k:<6} {v}")

    print("\n  样例(边际最高 8 条):")
    rank_observations(obs_list)
    shown = sorted([o for o in passed + blocked], key=lambda o: -o.edge_pct)[:8]
    for o in shown:
        print(f"    {o.log_line()}")
    print("\n  样例(边际最低 5 条):")
    for o in sorted([o for o in passed + blocked], key=lambda o: o.edge_pct)[:5]:
        print(f"    {o.log_line()}")
    c.close()


if __name__ == "__main__":
    main()
