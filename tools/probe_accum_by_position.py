"""加价格下限，剔除「几毛钱品跳一个刻度就是 +50%」的伪信号。

上一版发现「价格低位档」上涨率 53%、中位 +25%，验证后确认是廉价品伪影：
gid=7 现价 ¥0.04、30 日最高 ¥0.11，涨到 ¥0.07 就是 +75% —— 一个报价台阶。

本脚本按 MIN_PRICE 分档重跑，看：
  1) 基准与建仓信号的上涨率/中位/暴涨率 在剔掉廉价品后是否还成立
  2) 建仓 + 价格位置 是否有真优势

    .venv\\Scripts\\python.exe tools\\probe_accum_by_position.py
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
PRICE_FLOORS = (0.0, 10.0, 50.0)
BUCKETS = ((0, 50, "低位 0-50%"), (50, 100, "中位 50-100%"), (100, 10**9, "新高 >100%"))


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
    print(f"{len(seq)} 个品\n")

    base = defaultdict(list)
    accum = defaultdict(list)
    accum_pos = defaultdict(lambda: defaultdict(list))

    for gid, items in seq.items():
        if len(items) < 10:
            continue
        ts = [x[0] for x in items]
        for i in range(1, len(items)):
            p1 = items[i][1]
            k = bisect.bisect_left(ts, ts[i] + 7 * 86400)
            if k >= len(items) or abs(ts[k] - (ts[i] + 7 * 86400)) > 3.5 * 86400:
                continue
            fwd = (items[k][1] / p1 - 1) * 100

            target = ts[i] - WINDOW_MIN * 60
            j = bisect.bisect_left(ts, target)
            cand = [x for x in (j - 1, j) if 0 <= x < i]
            if not cand:
                continue
            j = min(cand, key=lambda x: abs(ts[x] - target))
            if abs(ts[j] - target) > 30 * 60:
                continue
            p0, bp0, s0, b0 = items[j][1], items[j][2], items[j][3], items[j][4]
            bp1, s1, b1 = items[i][2], items[i][3], items[i][4]
            if not (s0 and b0 and p0):
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

            lo = bisect.bisect_left(ts, ts[i] - 30 * 86400)
            win = [x[1] for x in items[lo:i + 1]]
            pos = (p1 / max(win) * 100) if (len(win) >= 20 and max(win) > 0) else None
            bucket = None
            if pos is not None:
                bucket = next((lab for a, b, lab in BUCKETS if a <= pos < b), None)

            for floor in PRICE_FLOORS:
                if p1 < floor:
                    continue
                base[floor].append(fwd)
                if ok:
                    accum[floor].append(fwd)
                    if bucket:
                        accum_pos[floor][bucket].append(fwd)

    def line(label, rets):
        if not rets:
            return f"    {label:<20} 无样本"
        ups = 100.0 * sum(1 for x in rets if x > 0) / len(rets)
        big = 100.0 * sum(1 for x in rets if x >= 15) / len(rets)
        dn = 100.0 * sum(1 for x in rets if x <= -15) / len(rets)
        return (f"    {label:<20}n={len(rets):<7} 上涨 {ups:>5.1f}%  中位 {st.median(rets):+7.2f}%"
                f"  ≥+15% {big:>5.1f}%  ≤-15% {dn:>5.1f}%")

    for floor in PRICE_FLOORS:
        tag = "不设下限（含廉价品）" if floor == 0 else f"价格 ≥¥{floor:g}"
        print(f"\n=== {tag} ===")
        print(line("基准", base[floor]))
        print(line("建仓信号", accum[floor]))
        b = base[floor]
        a = accum[floor]
        if b and a:
            print(f"    → 上涨率差 {100.0*sum(1 for x in a if x>0)/len(a) - 100.0*sum(1 for x in b if x>0)/len(b):+.1f}pp")
        if accum_pos[floor]:
            print("    建仓 × 价格位置：")
            for _a, _b, lab in BUCKETS:
                print(line(f"   {lab}", accum_pos[floor].get(lab) or []))


if __name__ == "__main__":
    main()
