# -*- coding: utf-8 -*-
"""验证方案 B 的市场有效性:rate_7/rate_30 是否可得、是否有判别力。

1) 本地可得性:price_snapshots 里 rate 字段的覆盖率(能否免 API 补齐)
2) 判别力:发生异动的商品,其事发前后的 rate_7/30 是否显著高于基准
3) 7 天锁定约束下,跟踪窗口(36 分钟)与持有窗口(7 天)的量级对比
"""
import sqlite3
import statistics
from collections import defaultdict
from datetime import datetime
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


def main():
    c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    c.row_factory = sqlite3.Row

    print("=== 1) rate 字段在本地库的可得性(近 7 天快照) ===")
    r = c.execute(
        "select count(*) n, sum(sell_price_rate_7 is not null and sell_price_rate_7 <> 0) r7, "
        "sum(sell_price_rate_30 is not null and sell_price_rate_30 <> 0) r30, "
        "count(distinct good_id) gids "
        "from price_snapshots where created_at >= datetime('now','-7 day')"
    ).fetchone()
    print(f"  近 7 天快照 {r['n']} 条, 覆盖 {r['gids']} 件商品")
    print(f"  带非零 rate_7: {r['r7']} 条 ({r['r7']/max(1,r['n'])*100:.1f}%)")
    print(f"  带非零 rate_30: {r['r30']} 条 ({r['r30']/max(1,r['n'])*100:.1f}%)")

    print("\n=== 2) rate 数值分布(非零样本) ===")
    for col in ("sell_price_rate_7", "sell_price_rate_30"):
        vals = [x[0] for x in c.execute(
            f"select {col} from price_snapshots where {col} is not null and {col} <> 0 "
            "and created_at >= datetime('now','-7 day') limit 200000")]
        if vals:
            vals.sort()
            n = len(vals)
            print(f"  {col}: n={n}  P10={vals[int(n*0.1)]:.1f}%  P50={vals[n//2]:.1f}%  "
                  f"P90={vals[int(n*0.9)]:.1f}%  |值|中位={statistics.median(abs(v) for v in vals):.1f}%")

    print("\n=== 3) 判别力:异动商品 vs 全体商品的 rate_30 ===")
    # 异动事件前的最近一条快照 rate_30
    events = c.execute(
        "select good_id, ts from anomaly_events where good_id is not null "
        "and score >= 52 order by ts desc limit 200"
    ).fetchall()
    ev_rates, base_rates = [], []
    for e in events:
        t = parse(e["ts"])
        if not t:
            continue
        row = c.execute(
            "select sell_price_rate_30 from price_snapshots where good_id=? "
            "and created_at <= ? order by created_at desc limit 1",
            (e["good_id"], e["ts"])).fetchone()
        if row and row["sell_price_rate_30"] not in (None, 0):
            ev_rates.append(row["sell_price_rate_30"])
    for row in c.execute(
        "select sell_price_rate_30 from price_snapshots where sell_price_rate_30 <> 0 "
        "and created_at >= datetime('now','-7 day') order by random() limit 3000"):
        base_rates.append(row["sell_price_rate_30"])

    def describe(xs, label):
        if not xs:
            print(f"  {label}: 无样本")
            return
        xs = sorted(xs)
        n = len(xs)
        print(f"  {label}: n={n}  均值 {statistics.mean(xs):+.1f}%  中位 {statistics.median(xs):+.1f}%  "
              f"正占比 {sum(1 for x in xs if x > 0)/n*100:.0f}%")

    describe(ev_rates, "异动前 rate_30")
    describe(base_rates, "全库基准 rate_30")

    print("\n=== 4) 时间尺度对比 ===")
    print("  跟踪窗口(A 池): 每轮 3 分钟,12 轮 quiet = 36 分钟")
    print("  S 池 TTL: 120 分钟; A 池僵尸线: 180 分钟")
    print("  交易锁定期: 7 天 = 10080 分钟")
    print("  单次异动到复发的实测中位: 30.7 小时")

    print("\n=== 5) 谁在动:近 3 天盘口变化幅度(判断 30 分钟窗口能否承载信号) ===")
    rows = c.execute(
        "select good_id, count(*) n, avg(abs(d)) avg_abs_d, max(abs(d)) max_abs_d from ("
        "  select good_id, total_sell_num - lag(total_sell_num) over (partition by good_id order by ts) d "
        "  from item_listing_history where ts >= datetime('now','-3 day')) "
        "where d is not null group by good_id having n >= 20 order by n desc limit 200"
    ).fetchall()
    if rows:
        avgs = sorted(r["avg_abs_d"] for r in rows)
        print(f"  样本 {len(avgs)} 件: 每次观测在售变化绝对值 中位 {statistics.median(avgs):.1f} 件, "
              f"P90 {avgs[int(len(avgs)*0.9)]:.1f} 件")
    c.close()


if __name__ == "__main__":
    main()
