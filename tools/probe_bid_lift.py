"""「抬价收货」子形态:最高求购价明显上移、但最低售价没跟涨,到底有没有预测力。

为什么单独测
------------
CS 市场里,庄家真收货的动作是**串价**:把最高求购价往上挪(愿意多付),而卖家还在往下让价。
表现为 求购价 ↑ + 卖价 ↓/平 —— 两边价差收窄,货从卖家流向这个买家。

这和「求购量涨、求购价不动」完全不同:后者的求购单不锁资金,零成本就能堆(假墙/散户跟风)。
线上实盘已经出现一例(AWP 鬼退治:求购价 +4.1%、卖价 -1.9%、求购量 +14%、位置 22% 分位),
但卡片判读只会说「价格在低位、不能说明有资金进场」——如果这个子形态真有效,判读写错了。

分组
----
A 抬价收货: 求购价 ≥+3% 且 卖价 <+1%(没跟涨)
B 量增价不动: 求购量 ≥+12% 且 求购价 <+1%(现有建仓指纹的典型样子)
C 两者同时: 求购价 ≥+3% 且 求购量 ≥+12%

口径:现价 ≥¥100(廉价品一个报价台阶就是 ±25%)、相邻采样 ≤120 分钟、
前瞻容差 = 前瞻时长的一半。

    .venv\\Scripts\\python.exe tools\\probe_bid_lift.py
"""
from __future__ import annotations

import bisect
import sqlite3
import statistics as st
from collections import defaultdict
from datetime import datetime

MIN_PRICE = 100.0
BID_UP = 3.0           # 求购价上移阈值
PRICE_FLAT = 1.0       # 卖价「没跟涨」的上限
QTY_UP = 12.0          # 求购量门槛(与线上 accum_buy_pct_min 一致)
HORIZONS = {"24h": 86400, "7d": 604800}


def main() -> None:
    c = sqlite3.connect("file:D:/cs2-qq-bot/data/csqaq.db?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    rows = c.execute("""select good_id, ts, buff_sell_price, buff_sell_num,
                               buff_buy_num, buff_buy_price
                        from item_listing_history
                        where ts >= '2026-08-01' and buff_sell_price >= ?
                          and buff_buy_price > 0
                        order by good_id, ts""", (MIN_PRICE,)).fetchall()
    c.close()

    seq = defaultdict(list)
    for r in rows:
        seq[r["good_id"]].append((datetime.strptime(r["ts"], "%Y-%m-%d %H:%M:%S").timestamp(),
                                  r["buff_sell_price"], r["buff_sell_num"],
                                  r["buff_buy_num"], r["buff_buy_price"]))
    print(f"{len(seq)} 个品、{len(rows)} 行（现价 ≥¥{MIN_PRICE:g}）")

    groups = {k: defaultdict(list) for k in
              ("A 抬价收货", "B 量增价不动", "C 两者同时",
               "D 线上建仓+抬价", "E 线上建仓+未抬价")}
    by_pos = {"低位": {k: defaultdict(list) for k in groups},
              "中位": {k: defaultdict(list) for k in groups},
              "高位": {k: defaultdict(list) for k in groups}}
    base = defaultdict(list)
    base_pos = {"低位": defaultdict(list), "中位": defaultdict(list), "高位": defaultdict(list)}
    only_a_low = defaultdict(list)

    for gid, items in seq.items():
        if len(items) < 30:
            continue
        ts = [x[0] for x in items]
        for i in range(len(items) - 1):
            a, b = items[i], items[i + 1]
            dt = b[0] - a[0]
            if dt <= 0 or dt > 120 * 60:
                continue
            p0, s0, b0, bp0 = a[1], a[2], a[3], a[4]
            p1, s1, b1, bp1 = b[1], b[2], b[3], b[4]
            if not s0 or not p0 or not bp0:
                continue
            price_chg = (p1 - p0) / p0 * 100
            qty_chg = ((b1 - b0) / b0 * 100) if b0 else 0.0
            bid_chg = (bp1 - bp0) / bp0 * 100
            sell_chg = ((s1 - s0) / s0 * 100) if s0 else 0.0

            lo = bisect.bisect_left(ts, ts[i] - 30 * 86400)
            window = items[lo:i + 1]
            lo_px = min(x[1] for x in window)
            hi_px = max(x[1] for x in window)
            pos = None
            if hi_px > lo_px:
                pct = (p1 - lo_px) / (hi_px - lo_px) * 100
                pos = "低位" if pct <= 30 else ("高位" if pct >= 70 else "中位")

            bucket_a = bid_chg >= BID_UP and price_chg < PRICE_FLAT
            bucket_b = qty_chg >= QTY_UP and bid_chg < PRICE_FLAT
            bucket_c = bid_chg >= BID_UP and qty_chg >= QTY_UP
            # D/E = 线上建仓的完整条件(量 +12%、价格横盘、在售不增),再按求购价是否上移拆开
            online_accum = (qty_chg >= QTY_UP and abs(price_chg) < 3.0 and sell_chg <= 5.0)
            bucket_d = online_accum and bid_chg >= BID_UP
            bucket_e = online_accum and bid_chg < BID_UP

            for h, sec in HORIZONS.items():
                j = bisect.bisect_left(ts, ts[i + 1] + sec)
                if j >= len(items) or abs(ts[j] - (ts[i + 1] + sec)) > sec * 0.5:
                    continue
                fwd = (items[j][1] / p1 - 1) * 100
                base[h].append(fwd)
                if pos:
                    base_pos[pos][h].append(fwd)
                if bucket_a:
                    groups["A 抬价收货"][h].append(fwd)
                    if pos:
                        by_pos[pos]["A 抬价收货"][h].append(fwd)
                    if pos == "低位":
                        only_a_low[h].append(fwd)
                if bucket_b:
                    groups["B 量增价不动"][h].append(fwd)
                    if pos:
                        by_pos[pos]["B 量增价不动"][h].append(fwd)
                if bucket_c:
                    groups["C 两者同时"][h].append(fwd)
                    if pos:
                        by_pos[pos]["C 两者同时"][h].append(fwd)
                for name, hit in (("D 线上建仓+抬价", bucket_d), ("E 线上建仓+未抬价", bucket_e)):
                    if hit:
                        groups[name][h].append(fwd)
                        if pos:
                            by_pos[pos][name][h].append(fwd)

    def show(label, d, indent="  "):
        if not d.get("7d"):
            print(f"{indent}{label:<16} 无样本")
            return
        bits = []
        for h in HORIZONS:
            v = d.get(h) or []
            if v:
                up = 100.0 * sum(1 for x in v if x > 0) / len(v)
                bits.append(f"{h}: n={len(v):<4} 中位 {st.median(v):+6.2f}% 上涨 {up:>4.1f}%")
        print(f"{indent}{label:<16} " + " | ".join(bits))

    print("\n=== 基准 ===")
    show("全体", base)
    for p in base_pos:
        show(p, base_pos[p])

    print("\n=== 三种子形态（全体） ===")
    for k in groups:
        show(k, groups[k])

    print("\n=== 按区间位置拆（关键：低位才有意义） ===")
    for p in ("低位", "中位", "高位"):
        print(f"  [{p}]  基准:", end="")
        v = base_pos[p].get("7d") or []
        if v:
            print(f" 7d n={len(v)} 中位 {st.median(v):+.2f}% 上涨 {100.0*sum(1 for x in v if x>0)/len(v):.1f}%")
        else:
            print(" 无样本")
        for k in groups:
            show(k, by_pos[p][k], indent="    ")

    print("\n判据：若「A 抬价收货」在低位的中位与上涨率明显好于同位置基准，"
          "说明判读该按求购价方向分叉，而不是一律说「不能说明有资金进场」。")


if __name__ == "__main__":
    main()
