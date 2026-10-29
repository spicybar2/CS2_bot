"""建仓信号：上涨率高，为什么中位数还是负的？

上一轮确认「生产条件」的上涨率 34.5% vs 基准 25.5%，是真优势。
但 7 日中位 -1.49%。本脚本拆开收益分布，判断这份优势能不能变成钱：

  上涨时的平均涨幅  vs  下跌时的平均跌幅
  扣掉交易成本（BUFF 手续费 + 买卖价差）后的期望值

    .venv\\Scripts\\python.exe tools\\probe_accum_asym.py
"""
from __future__ import annotations

import bisect
import sqlite3
import statistics as st
from collections import defaultdict
from datetime import datetime

BUY_PCT_MIN, BUY_ABS_MIN = 12.0, 3
SELL_MAX, PRICE_QUIET, PRICE_QUIET_ABS = 5.0, 3.0, 0.5
WINDOW_MIN = 60
# BUFF 卖出费率按 2.5%，买入侧无手续费；再算上吃单滑点 1%，往返约 3.5%
ROUND_TRIP_COST = 3.5


def main() -> None:
    c = sqlite3.connect("file:D:/cs2-qq-bot/data/csqaq.db?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    rows = c.execute("""select good_id, ts, buff_sell_price, buff_buy_price,
                               buff_sell_num, buff_buy_num
                        from item_listing_history where ts >= '2026-08-20'
                        and buff_sell_price > 0 order by good_id, ts""").fetchall()
    c.close()

    seq = defaultdict(list)
    for r in rows:
        seq[r["good_id"]].append((
            datetime.strptime(r["ts"], "%Y-%m-%d %H:%M:%S").timestamp(),
            r["buff_sell_price"], r["buff_buy_price"],
            r["buff_sell_num"], r["buff_buy_num"]))
    print(f"{len(seq)} 个品")

    accums, accums_bp = [], []
    base7 = []

    for gid, items in seq.items():
        if len(items) < 10:
            continue
        ts = [x[0] for x in items]
        for i in range(1, len(items)):
            target = ts[i] - WINDOW_MIN * 60
            j = bisect.bisect_left(ts, target)
            cand = [k for k in (j - 1, j) if 0 <= k < i]
            if not cand:
                continue
            j = min(cand, key=lambda k: abs(ts[k] - target))
            if abs(ts[j] - target) > 30 * 60:
                continue
            p0, bp0, s0, b0 = items[j][1], items[j][2], items[j][3], items[j][4]
            p1, bp1, s1, b1 = items[i][1], items[i][2], items[i][3], items[i][4]
            if not (s0 and b0 and p0 and p1):
                continue
            buy_chg = (b1 - b0) / b0 * 100
            sell_chg = (s1 - s0) / s0 * 100
            price_chg = (p1 - p0) / p0 * 100
            seg = items[j:i + 1]
            monotonic = all(x[4] <= y[4] for x, y in zip(seg, seg[1:]))
            if buy_chg >= BUY_PCT_MIN and price_chg <= -1.0 and sell_chg >= 5.0:
                continue
            ok = (buy_chg >= BUY_PCT_MIN and b1 - b0 >= BUY_ABS_MIN
                  and sell_chg < SELL_MAX
                  and (abs(price_chg) < PRICE_QUIET or abs(p1 - p0) <= PRICE_QUIET_ABS)
                  and monotonic)
            k = bisect.bisect_left(ts, ts[i] + 7 * 86400)
            if k >= len(items) or abs(ts[k] - (ts[i] + 7 * 86400)) > 3.5 * 86400:
                continue
            fwd = (items[k][1] / p1 - 1) * 100
            base7.append(fwd)
            if not ok:
                continue
            accums.append(fwd)
            bp_chg = ((bp1 - bp0) / bp0 * 100) if (bp0 and bp1) else 0.0
            if bp_chg >= 3:
                accums_bp.append(fwd)

    def dist(label, rets):
        if not rets:
            print(f"  {label:<26} 无样本")
            return
        ups = [x for x in rets if x > 0]
        dns = [x for x in rets if x < 0]
        flat = 100.0 * sum(1 for x in rets if x == 0) / len(rets)
        print(f"\n  {label}  (n={len(rets)})")
        print(f"    上涨 {len(ups)/len(rets)*100:>5.1f}%  平均 {st.mean(ups) if ups else 0:+6.2f}%"
              f"   |  下跌 {len(dns)/len(rets)*100:>5.1f}%  平均 {st.mean(dns) if dns else 0:+6.2f}%"
              f"   |  持平 {flat:>4.1f}%")
        print(f"    中位 {st.median(rets):+6.2f}%   均值 {st.mean(rets):+6.2f}%")
        print(f"    ≥+15% {100.0*sum(1 for x in rets if x>=15)/len(rets):>5.1f}%   "
              f"≤-15% {100.0*sum(1 for x in rets if x<=-15)/len(rets):>5.1f}%")

    print("\n=== 7 日收益分布 ===")
    dist("基准（全体）", base7)
    dist("建仓（生产条件）", accums)
    dist("建仓 + 求购价涨3%", accums_bp)

    print("\n=== 扣交易成本后的期望 ===")
    for label, rets in (("基准", base7), ("建仓(生产条件)", accums), ("建仓+求购价涨", accums_bp)):
        if not rets:
            continue
        m = st.mean(rets)
        print(f"  {label:<20} 均值 {m:+6.2f}%  − 成本 {ROUND_TRIP_COST}%  =  {m-ROUND_TRIP_COST:+6.2f}%"
              f"   {'✅ 正' if m-ROUND_TRIP_COST > 0 else '❌ 负'}")


if __name__ == "__main__":
    main()
