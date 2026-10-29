"""建仓 → 拉升的周期有多长？「等方向确认再提示」符不符合 CS2 市场规律？

两个问题：
  A 建仓信号在 7/14/21/28 天视界下的表现 —— 周期可能比 7 天长
  B 「建仓 + 方向确认（随后在售下降 / 求购价上移）」是否比「建仓」本身更好

B 就是拟议中的新流程：建仓只进观察池，等方向确认才提示。
如果 B 优于 A，说明这个设计符合市场规律；否则说明确认也是噪声。

    .venv\\Scripts\\python.exe tools\\probe_cycle_length.py
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
CONFIRM_DAYS = 7          # 建仓后几天内出现方向确认
HORIZONS = {"7d": 7, "14d": 14, "21d": 21, "28d": 28}


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
    print(f"{len(seq)} 个品、{len(rows)} 行")

    base = defaultdict(list)       # 视界 -> [(gid, fwd)]
    accum = defaultdict(list)      # 建仓点起算
    confirmed = defaultdict(list)  # 确认点起算

    for gid, items in seq.items():
        if len(items) < 10:
            continue
        ts = [x[0] for x in items]
        for i in range(1, len(items)):
            p1 = items[i][1]
            for h, days in HORIZONS.items():
                k = bisect.bisect_left(ts, ts[i] + days * 86400)
                if k < len(items) and abs(ts[k] - (ts[i] + days * 86400)) <= days * 86400 * 0.5:
                    base[h].append((gid, (items[k][1] / p1 - 1) * 100))

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
            if not ok:
                continue

            for h, days in HORIZONS.items():
                k = bisect.bisect_left(ts, ts[i] + days * 86400)
                if k < len(items) and abs(ts[k] - (ts[i] + days * 86400)) <= days * 86400 * 0.5:
                    accum[h].append((gid, (items[k][1] / p1 - 1) * 100))

            # ---- 方向确认：建仓后 CONFIRM_DAYS 天内，在售下降 ≥5% 或 求购价上移 ≥3% ----
            end = bisect.bisect_right(ts, ts[i] + CONFIRM_DAYS * 86400)
            ck = None
            for m in range(i + 1, end):
                sell_dn = (items[m][3] - s1) / s1 * 100 if s1 else 0
                bp_up = ((items[m][2] - bp1) / bp1 * 100) if (bp1 and items[m][2]) else 0
                if sell_dn <= -5.0 or bp_up >= 3.0:
                    ck = m
                    break
            if ck is None:
                continue
            pc = items[ck][1]
            for h, days in HORIZONS.items():
                k2 = bisect.bisect_left(ts, ts[ck] + days * 86400)
                if k2 < len(items) and abs(ts[k2] - (ts[ck] + days * 86400)) <= days * 86400 * 0.5:
                    confirmed[h].append((gid, (items[k2][1] / pc - 1) * 100))

    def show(label, d, horizons=HORIZONS):
        print(f"\n  {label}")
        for h in horizons:
            v = d.get(h) or []
            if not v:
                continue
            rets = [x[1] for x in v]
            ups = 100.0 * sum(1 for x in rets if x > 0) / len(rets)
            big = 100.0 * sum(1 for x in rets if x >= 15) / len(rets)
            print(f"    {h:<4} n={len(v):<6} 上涨 {ups:>5.1f}%  中位 {st.median(rets):+6.2f}%  "
                  f"≥+15% {big:>5.1f}%")

    print("\n=== A 基准 ===")
    show("全体采样点", base)
    print("\n=== B 建仓信号（从建仓点起算） ===")
    show("建仓（生产条件）", accum)
    print(f"\n=== C 建仓 + {CONFIRM_DAYS} 天内方向确认（从确认点起算） ===")
    show("建仓→确认", confirmed)


if __name__ == "__main__":
    main()
