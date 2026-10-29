"""把「上涨率 39.7%」拆开看：这个 39.7% 到底是什么、为什么它会和「典型跌 1.22%」同时成立。

要回答三个问题
--------------
1. 393 例是 393 个不同的品，还是同一个品被重复计算？
2. 39.7% 的涨幅分布长什么样（是小涨多、还是大涨少）？
3. 拿它跟「什么都不看」比，到底强多少？

    .venv\\Scripts\\python.exe tools\\probe_winrate_shape.py
"""
from __future__ import annotations

import bisect
import statistics as st
import sqlite3
from collections import defaultdict
from datetime import datetime

FEE = 0.025
BREAKEVEN = (1.0 / (1.0 - FEE) - 1.0) * 100.0
MIN_PRICE = 100.0
H = 604800

BUCKETS = [("涨超 +10%", 10.0, 1e9), ("涨 +2.56~10%", BREAKEVEN, 10.0),
           ("涨 0~2.56%（扣费仍亏）", 0.0, BREAKEVEN), ("跌 0~2.56%", -BREAKEVEN, 0.0),
           ("跌 2.56~10%", -10.0, -BREAKEVEN), ("跌超 10%", -1e9, -10.0)]


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

    lift, base = [], []
    lift_gids, base_gids = set(), set()
    groups = defaultdict(list)
    group_gids = defaultdict(set)

    for gid, items in seq.items():
        if len(items) < 30:
            continue
        ts = [x[0] for x in items]
        for i in range(len(items) - 1):
            a, b = items[i], items[i + 1]
            if not (0 < b[0] - a[0] <= 120 * 60):
                continue
            p0, s0, b0, bp0 = a[1], a[2], a[3], a[4]
            p1, b1, bp1 = b[1], b[3], b[4]
            if not s0 or not p0 or not bp0:
                continue
            price_chg = (p1 - p0) / p0 * 100
            qty_chg = ((b1 - b0) / b0 * 100) if b0 else 0.0
            bid_chg = (bp1 - bp0) / bp0 * 100
            sell_chg = ((b[2] - s0) / s0 * 100) if s0 else 0.0

            lo = bisect.bisect_left(ts, ts[i] - 30 * 86400)
            win = items[lo:i + 1]
            lo_px, hi_px = min(x[1] for x in win), max(x[1] for x in win)
            if hi_px <= lo_px:
                continue
            pct = (p1 - lo_px) / (hi_px - lo_px) * 100
            pos = "低位" if pct <= 30 else ("高位" if pct >= 70 else "中位")

            j = bisect.bisect_left(ts, ts[i + 1] + H)
            if j >= len(items) or abs(ts[j] - (ts[i + 1] + H)) > H * 0.5:
                continue
            fwd = (items[j][1] / p1 - 1) * 100
            base.append(fwd)
            base_gids.add(gid)

            online = qty_chg >= 12.0 and abs(price_chg) < 3.0 and sell_chg <= 5.0
            dist = sell_chg >= 6.0 and b[1] / hi_px >= 0.90 and qty_chg < 2.0
            tags = []
            if online:
                tags.append(f"建仓+{'抬价' if bid_chg >= 3.0 else '堆量'}·{pos}")
            if dist:
                tags.append("派发")
            if bid_chg >= 3.0 and price_chg >= 1.0 and sell_chg <= 5.0:
                tags.append("启动")
            for name in tags:
                groups[name].append(fwd)
                group_gids[name].add(gid)

            if pct <= 30 and online and bid_chg >= 3.0:
                lift.append(fwd)
                lift_gids.add(gid)

    def show(name, v, gids):
        n = len(v)
        if not n:
            return
        up = 100.0 * sum(1 for x in v if x > 0) / n
        net = 100.0 * sum(1 for x in v if x > BREAKEVEN) / n
        print(f"\n【{name}】事件 {n:,} 条，涉及 {len(gids):,} 个不同的品")
        print(f"  上涨率 {up:.1f}%   扣费后赚钱 {net:.1f}%   "
              f"平均 {st.mean(v):+.2f}%   中位 {st.median(v):+.2f}%")
        print(f"  分布：")
        for label, lo, hi in BUCKETS:
            k = sum(1 for x in v if lo <= x < hi)
            bar = "█" * max(1, round(k / n * 40))
            print(f"    {label:<20}{k:>5} 例 {k / n * 100:>5.1f}%  {bar}")

    show("低位 + 抬价收货（卡上那 393 例）", lift, lift_gids)
    show("低位任意时刻（基准）", base, base_gids)

    # 同一个品重复出现的程度：事件数远大于独立品数时，样本独立性差，结论要打折
    print("\n=== 各信号组「事件数 vs 独立品数」（判断样本独立性） ===")
    order = ["建仓+抬价·低位", "建仓+堆量·低位", "建仓+抬价·中位", "建仓+抬价·高位",
             "建仓+堆量·中位", "建仓+堆量·高位", "启动", "派发"]
    for name in order:
        v = groups.get(name) or []
        if len(v) < 10:
            continue
        k = len(group_gids[name])
        print(f"  {name:<14} 事件 {len(v):>6,} 条，独立品 {k:>4,} 个，"
              f"平均每品 {len(v) / max(1, k):>4.1f} 次")

    print("\n结论怎么读：")
    print("  · 39.7% 是「过去 393 次同形态里，7 天后价格更高的比例」，不是对某一件的预测；")
    print("  · 它把「涨 0.1%」也算赢，所以看着高；扣费后只剩 20.1%；")
    print("  · 分布图看的是「赢的时候赢多少、输的时候输多少」——中位为负说明一半以上在跌。")


if __name__ == "__main__":
    main()
