# -*- coding: utf-8 -*-
"""回测:异动事件在不同持有期的「扣费后收益」,用于确定成本门槛。

口径:事件发生时买入(BUFF 最低要价),持有到 +24h / +72h / +7d 以最低要价卖出,
扣 2.5% 卖方手续费 + 1% 提现,统计净收益分布与盈利占比。
"""
import sqlite3
import statistics
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "data" / "csqaq.db"
SELL_FEE = 0.025
WITHDRAW = 0.01
COST = (1 - SELL_FEE) * (1 - WITHDRAW)      # 卖出到手系数 0.96525


def parse(ts):
    for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(ts, fmt)
        except (ValueError, TypeError):
            continue
    return None


def price_at(c, gid, when):
    r = c.execute(
        "select buff_sell_price from price_snapshots where good_id=? and buff_sell_price > 0 "
        "and created_at <= ? order by created_at desc limit 1",
        (gid, when.strftime("%Y-%m-%d %H:%M:%S.%f"))).fetchone()
    return float(r["buff_sell_price"]) if r else None


def main():
    c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    events = c.execute(
        "select good_id, ts, score from anomaly_events "
        "where good_id is not null and score >= 38 and ts >= datetime('now','-45 day') "
        "order by ts"
    ).fetchall()
    print(f"=== 事件回测(score>=38,共 {len(events)} 条) ===")
    print("  净收益 = 卖出价 × 0.96525 / 买入价 - 1   (2.5% 卖方费 + 1% 提现)")

    for label, hours in (("+24h", 24), ("+72h", 72), ("+7d", 24 * 7)):
        rets = []
        for e in events:
            t = parse(e["ts"])
            if not t:
                continue
            p0 = price_at(c, e["good_id"], t)
            p1 = price_at(c, e["good_id"], t + timedelta(hours=hours))
            if not p0 or not p1 or p0 <= 0:
                continue
            rets.append((p1 * COST / p0 - 1) * 100)
        if not rets:
            print(f"  {label}: 无样本")
            continue
        rets.sort()
        n = len(rets)
        win = sum(1 for x in rets if x > 0) / n * 100
        print(f"  {label:<5} n={n:>4}  中位 {statistics.median(rets):+6.2f}%  "
              f"均值 {statistics.mean(rets):+6.2f}%  盈利占比 {win:5.1f}%  "
              f"P10 {rets[int(n*0.1)]:+6.2f}%  P90 {rets[int(n*0.9)]:+6.2f}%")

    # 分价格带
    print("\n=== 分价格带(+7d,扣费后) ===")
    bands = [(0, 50), (50, 200), (200, 1000), (1000, 5000), (5000, 10**9)]
    for lo, hi in bands:
        rets = []
        for e in events:
            t = parse(e["ts"])
            if not t:
                continue
            p0 = price_at(c, e["good_id"], t)
            if not p0 or not (lo <= p0 < hi):
                continue
            p1 = price_at(c, e["good_id"], t + timedelta(days=7))
            if not p1:
                continue
            rets.append((p1 * COST / p0 - 1) * 100)
        if len(rets) >= 10:
            rets.sort()
            win = sum(1 for x in rets if x > 0) / len(rets) * 100
            label = f"¥{lo}-{hi if hi < 10**9 else '+'}"
            print(f"  {label:<12} n={len(rets):>4}  中位 {statistics.median(rets):+6.2f}%  "
                  f"盈利占比 {win:5.1f}%")

    # 对照:随机买入
    print("\n=== 对照:随机商品持有 7 天(同样扣费) ===")
    sample = c.execute(
        "select good_id, created_at, buff_sell_price from price_snapshots "
        "where buff_sell_price > 0 and created_at >= datetime('now','-14 day') "
        "order by random() limit 1500"
    ).fetchall()
    rets = []
    for r in sample:
        t = parse(r["created_at"])
        if not t:
            continue
        p0 = float(r["buff_sell_price"])
        p1 = price_at(c, r["good_id"], t + timedelta(days=7))
        if p1 and p0 > 0:
            rets.append((p1 * COST / p0 - 1) * 100)
    if rets:
        rets.sort()
        n = len(rets)
        win = sum(1 for x in rets if x > 0) / n * 100
        print(f"  n={n}  中位 {statistics.median(rets):+6.2f}%  "
              f"盈利占比 {win:.1f}%")
    c.close()


if __name__ == "__main__":
    main()
