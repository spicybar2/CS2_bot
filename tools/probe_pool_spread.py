# -*- coding: utf-8 -*-
"""只测「池内/活跃商品」的买卖价差,并给出分价格带的成本门槛建议。"""
import sqlite3
import statistics
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "data" / "csqaq.db"


def main():
    c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    c.row_factory = sqlite3.Row

    # 只取「池内商品」最近的快照
    rows = c.execute(
        "select p.buff_sell_price s, p.buff_buy_price b, p.name, p.good_id "
        "from price_snapshots p "
        "where p.buff_sell_price > 0 and p.buff_buy_price > 0 "
        "and p.created_at >= datetime('now','-7 day') "
        "and p.good_id in (select good_id from watch_pool)"
    ).fetchall()
    print(f"=== 池内商品价差(样本 {len(rows)} 条) ===")
    vals = []
    for r in rows:
        s, b = float(r["s"]), float(r["b"])
        if b < s:
            vals.append((s - b) / s * 100)
    if vals:
        vals.sort()
        n = len(vals)
        print(f"  中位 {statistics.median(vals):.2f}%  P25 {vals[n//4]:.2f}%  "
              f"P75 {vals[n*3//4]:.2f}%  P90 {vals[int(n*0.9)]:.2f}%")

    # 分价格带(池内)
    bands = [(0, 50), (50, 200), (200, 1000), (1000, 5000), (5000, 10**9)]
    print("\n  按价格带(池内):")
    for lo, hi in bands:
        sub = [(float(r["s"]) - float(r["b"])) / float(r["s"]) * 100
               for r in rows if lo <= float(r["s"]) < hi and float(r["b"]) < float(r["s"])]
        if len(sub) >= 15:
            sub.sort()
            label = f"¥{lo}-{hi if hi < 10**9 else '+'}"
            print(f"    {label:<12} n={len(sub):>5}  中位 {statistics.median(sub):5.2f}%")

    # 基准价折扣:现价相对近 30 日区间中点的位置
    print("\n=== 参考:池内商品现价相对 30 日区间的折扣分布 ===")
    disc = []
    for r in c.execute(
        "select good_id, buff_sell_price from price_snapshots "
        "where buff_sell_price > 0 and created_at >= datetime('now','-30 minute') "
        "and good_id in (select good_id from watch_pool) group by good_id"
    ):
        gid, price = r["good_id"], float(r["buff_sell_price"])
        st = c.execute(
            "select min(buff_sell_price) mn, max(buff_sell_price) mx, avg(buff_sell_price) av "
            "from price_snapshots where good_id=? and buff_sell_price > 0 "
            "and created_at >= datetime('now','-30 day')", (gid,)).fetchone()
        if not st or not st["av"]:
            continue
        disc.append((float(st["av"]) - price) / float(st["av"]) * 100)
    if disc:
        disc.sort()
        n = len(disc)
        print(f"  n={n}  中位 {statistics.median(disc):+.2f}%  "
              f"P10 {disc[int(n*0.1)]:+.2f}%  P90 {disc[int(n*0.9)]:+.2f}%")
        print(f"  折扣 > 5% 的占比: {sum(1 for x in disc if x > 5)/n*100:.0f}%  "
              f"> 8%: {sum(1 for x in disc if x > 8)/n*100:.0f}%")
    c.close()


if __name__ == "__main__":
    main()
