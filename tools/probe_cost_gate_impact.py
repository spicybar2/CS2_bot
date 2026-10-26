# -*- coding: utf-8 -*-
"""成本门槛的拦截率估算:每条异动事件「相对 30 日均价的折扣」分布。

门槛规则(草案): 折扣% - 往返成本% >= 最小边际%
往返成本按价格带取值(含提现最低 ¥2 的影响)。
"""
import sqlite3
import statistics
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "data" / "csqaq.db"


def parse(ts):
    for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(ts, fmt)
        except (ValueError, TypeError):
            continue
    return None


def cost_pct(price, sell_fee=0.025):
    """往返成本% = 卖方费 + 提现(1%,最低 ¥2)。"""
    if price <= 0:
        return 99.0
    withdraw = max(2.0, price * 0.01) / price
    return (sell_fee) * 100 + withdraw * 100


def main():
    c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    events = c.execute(
        "select good_id, ts, price, score from anomaly_events "
        "where good_id is not null and score >= 52 and price > 0 "
        "and ts >= datetime('now','-60 day') order by ts"
    ).fetchall()

    rows = []
    for e in events:
        t = parse(e["ts"])
        if not t:
            continue
        p0 = float(e["price"])
        lo = (t - timedelta(days=30)).strftime("%Y-%m-%d %H:%M:%S.%f")
        hi = t.strftime("%Y-%m-%d %H:%M:%S.%f")
        st = c.execute(
            "select avg(buff_sell_price) av, count(*) n from price_snapshots "
            "where good_id=? and buff_sell_price > 0 and created_at between ? and ?",
            (e["good_id"], lo, hi)).fetchone()
        if not st or not st["av"] or st["n"] < 3:
            continue
        avg = float(st["av"])
        disc = (avg - p0) / avg * 100          # 相对 30 日均价的折扣
        rows.append((p0, disc, cost_pct(p0)))

    print(f"=== 事件相对 30 日均价的折扣(有效样本 {len(rows)} 条) ===")
    if not rows:
        return
    discs = sorted(r[1] for r in rows)
    n = len(discs)
    print(f"  折扣中位 {statistics.median(discs):+.2f}%  "
          f"P10 {discs[int(n*0.1)]:+.2f}%  P90 {discs[int(n*0.9)]:+.2f}%")
    print(f"  正折扣(低于均价的)占比: {sum(1 for x in discs if x > 0)/n*100:.0f}%")

    print("\n=== 不同门槛下的拦截率 ===")
    print(f"  {'最小边际':>8} {'放行':>6} {'拦截':>6} {'拦截率':>7}")
    for edge in (0, 1, 2, 3, 5):
        passed = sum(1 for _p, d, cp in rows if d - cp >= edge)
        print(f"  {edge:>7}% {passed:>6} {len(rows)-passed:>6} "
              f"{(len(rows)-passed)/len(rows)*100:>6.1f}%")

    print("\n=== 按价格带的成本(2.5% 卖方费 + 提现) ===")
    for p in (20, 50, 100, 200, 1000, 5000):
        print(f"  ¥{p:<6} 往返成本 {cost_pct(p):5.1f}%")

    print("\n=== 事件价格分布 ===")
    prices = sorted(r[0] for r in rows)
    for q in (0.1, 0.25, 0.5, 0.75, 0.9):
        print(f"  P{int(q*100):>2} = ¥{prices[int(n*q)]:.0f}", end="   ")
    print()
    c.close()


if __name__ == "__main__":
    main()
