# -*- coding: utf-8 -*-
"""回测(严格版):买入价与卖出价都必须落在目标时点附近,避免用陈旧快照冒充。

买入:事件时点前 60 分钟内最近一条快照
卖出:+24h/+72h/+7d 前后 24 小时内最近一条快照
不满足则丢弃该样本。
"""
import sqlite3
import statistics
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "data" / "csqaq.db"
COST = (1 - 0.025) * (1 - 0.01)


def parse(ts):
    for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(ts, fmt)
        except (ValueError, TypeError):
            continue
    return None


def near(c, gid, when, tol_min, side):
    """取 when 附近 tol_min 分钟内的快照。side='before' 取 <= when 的最近一条。"""
    if side == "before":
        r = c.execute(
            "select buff_sell_price p, created_at t from price_snapshots "
            "where good_id=? and buff_sell_price > 0 and created_at <= ? "
            "order by created_at desc limit 1",
            (gid, when.strftime("%Y-%m-%d %H:%M:%S.%f"))).fetchone()
        if not r:
            return None, None
        t = parse(r["t"])
        if not t or (when - t).total_seconds() > tol_min * 60:
            return None, None
        return float(r["p"]), t
    # around: 在 [when-tol, when+tol] 内取最接近的
    lo = (when - timedelta(minutes=tol_min)).strftime("%Y-%m-%d %H:%M:%S.%f")
    hi = (when + timedelta(minutes=tol_min)).strftime("%Y-%m-%d %H:%M:%S.%f")
    r = c.execute(
        "select buff_sell_price p, created_at t from price_snapshots "
        "where good_id=? and buff_sell_price > 0 and created_at between ? and ? "
        "order by abs(julianday(created_at) - julianday(?)) limit 1",
        (gid, lo, hi, when.strftime("%Y-%m-%d %H:%M:%S.%f"))).fetchone()
    if not r:
        return None, None
    return float(r["p"]), parse(r["t"])


def main():
    c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    events = c.execute(
        "select good_id, ts, score from anomaly_events where good_id is not null "
        "and score >= 52 and ts >= datetime('now','-60 day') order by ts"
    ).fetchall()
    print(f"=== 严格口径回测(score>=52,候选 {len(events)} 条) ===")
    print("  买入:事件前 60 分钟内快照 / 卖出:目标时点 ±24h 内快照 / 扣 2.5%+1%")

    for label, hours, tol in (("+24h", 24, 360), ("+72h", 72, 720), ("+7d", 24 * 7, 1440)):
        rets = []
        used = 0
        for e in events:
            t = parse(e["ts"])
            if not t:
                continue
            p0, _ = near(c, e["good_id"], t, 60, "before")
            if not p0:
                continue
            p1, _ = near(c, e["good_id"], t + timedelta(hours=hours), tol, "around")
            if not p1:
                continue
            used += 1
            rets.append((p1 * COST / p0 - 1) * 100)
        if not rets:
            print(f"  {label}: 无有效样本")
            continue
        rets.sort()
        n = len(rets)
        win = sum(1 for x in rets if x > 0) / n * 100
        print(f"  {label:<5} n={n:>4}  中位 {statistics.median(rets):+6.2f}%  "
              f"均值 {statistics.mean(rets):+6.2f}%  盈利占比 {win:5.1f}%  "
              f"P10 {rets[int(n*0.1)]:+6.2f}%  P90 {rets[int(n*0.9)]:+6.2f}%")

    # 随机对照(同样严格口径)
    print("\n=== 对照:随机商品、同样严格口径(+7d) ===")
    sample = c.execute(
        "select good_id, created_at from price_snapshots where buff_sell_price > 0 "
        "and created_at >= datetime('now','-30 day') order by random() limit 2000"
    ).fetchall()
    rets = []
    for r in sample:
        t = parse(r["created_at"])
        if not t:
            continue
        p0, _ = near(c, r["good_id"], t, 60, "before")
        p1, _ = near(c, r["good_id"], t + timedelta(days=7), 1440, "around")
        if p0 and p1:
            rets.append((p1 * COST / p0 - 1) * 100)
    if rets:
        rets.sort()
        n = len(rets)
        win = sum(1 for x in rets if x > 0) / n * 100
        print(f"  n={n}  中位 {statistics.median(rets):+6.2f}%  盈利占比 {win:.1f}%")
    c.close()


if __name__ == "__main__":
    main()
