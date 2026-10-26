# -*- coding: utf-8 -*-
"""修正版:盘口/价格变化的持续性,只统计非零变化;并测「异动后 6 小时内是否再动」。"""
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

    picks = c.execute(
        "select good_id, name, count(*) n from item_listing_history "
        "where ts >= datetime('now','-3 day') group by good_id "
        "having n >= 60 order by n desc limit 40"
    ).fetchall()

    print("=== 盘口/价格变化持续性(仅非零变化) ===")
    stats = {"sell": [], "buy": [], "price": []}
    zero_ratio = {"sell": [], "buy": [], "price": []}
    for p in picks:
        hist = c.execute(
            "select ts, total_sell_num, total_buy_num, buff_sell_num, buff_buy_num, buff_sell_price "
            "from item_listing_history where good_id=? order by ts", (p["good_id"],)
        ).fetchall()
        series = {"sell": [], "buy": [], "price": []}
        for a, b in zip(hist, hist[1:]):
            ta, tb = parse(a["ts"]), parse(b["ts"])
            if not ta or not tb or (tb - ta).total_seconds() > 3600:
                continue
            sa = a["total_sell_num"] if a["total_sell_num"] is not None else a["buff_sell_num"]
            sb = b["total_sell_num"] if b["total_sell_num"] is not None else b["buff_sell_num"]
            ba = a["total_buy_num"] if a["total_buy_num"] is not None else a["buff_buy_num"]
            bb = b["total_buy_num"] if b["total_buy_num"] is not None else b["buff_buy_num"]
            pa, pb = a["buff_sell_price"], b["buff_sell_price"]
            if None not in (sa, sb):
                series["sell"].append(sb - sa)
            if None not in (ba, bb):
                series["buy"].append(bb - ba)
            if None not in (pa, pb):
                series["price"].append(round(pb - pa, 4))
        for k, deltas in series.items():
            nz = [d for d in deltas if d != 0]
            if len(nz) >= 10:
                same = sum(1 for x, y in zip(nz, nz[1:]) if x * y > 0) / (len(nz) - 1)
                stats[k].append(same)
                zero_ratio[k].append(1 - len(nz) / len(deltas))

    for k, label in (("sell", "在售数"), ("buy", "求购数"), ("price", "BUFF 售价")):
        if stats[k]:
            s = sorted(stats[k])
            print(f"  {label:<10} 相邻窗口同向比例 中位 {statistics.median(s)*100:4.0f}%  "
                  f"(样本 {len(s)} 件; 零变化占比中位 {statistics.median(zero_ratio[k])*100:3.0f}%)")

    print("\n=== 一次异动之后,6 小时内同商品是否再次异动 ===")
    rows = c.execute(
        "select good_id, ts from anomaly_events where good_id is not null and ts is not null "
        "order by good_id, ts"
    ).fetchall()
    by_gid = defaultdict(list)
    for r in rows:
        by_gid[r["good_id"]].append(r["ts"])
    n_first = 0
    again = defaultdict(int)
    for gid, tss in by_gid.items():
        times = [t for t in (parse(x) for x in tss) if t]
        if not times:
            continue
        n_first += 1
        first = times[0]
        for t in times[1:]:
            h = (t - first).total_seconds() / 3600
            for bucket in (1, 6, 24):
                if h <= bucket:
                    again[bucket] += 1
            break
    print(f"  首次异动后复发: 1h 内 {again[1]}/{n_first} ({again[1]/max(1,n_first)*100:.0f}%), "
          f"6h 内 {again[6]}/{n_first} ({again[6]/max(1,n_first)*100:.0f}%), "
          f"24h 内 {again[24]}/{n_first} ({again[24]/max(1,n_first)*100:.0f}%)")

    print("\n=== 价格快照的观测密度(判断跟踪粒度是否有意义) ===")
    for r in c.execute(
        "select round(avg(n),1) avg_n, round(min(n),1) min_n, round(max(n),1) max_n from ("
        "  select good_id, count(*) n from price_snapshots "
        "  where created_at >= datetime('now','-3 hour') group by good_id)"
    ):
        print(f"  近 3 小时: 每商品平均 {r['avg_n']} 次观测 (最少 {r['min_n']}, 最多 {r['max_n']})")
    c.close()


if __name__ == "__main__":
    main()
