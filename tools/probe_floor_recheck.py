"""按价格下限重验：派发 / 拉升 这两个关键结论，剔掉廉价品后还成立吗。

背景：建仓信号的「上涨率 +7pp」在加了 ¥10 价格下限后缩到 +2.2pp，
「≥+15% 暴涨率 22.1%」在 ≥¥50 时只剩 0.3% ——
说明之前的市场图景被几毛钱廉价品的「一个价格台阶 +50%」严重污染。

本脚本对两个最重要的结论做同样的净化：
  派发（在售+6% + 高位 + 求购停滞）
  拉升（在售-10% + 价格+1%）
按 MIN_PRICE = 0 / 10 / 50 分别看上涨率与中位。

    .venv\\Scripts\\python.exe tools\\probe_floor_recheck.py
"""
from __future__ import annotations

import bisect
import sqlite3
import statistics as st
from collections import defaultdict
from datetime import datetime

PRICE_FLOORS = (0.0, 10.0, 50.0)
HORIZONS = {"24h": 1, "7d": 7}


def main() -> None:
    c = sqlite3.connect("file:D:/cs2-qq-bot/data/csqaq.db?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    rows = c.execute("""select good_id, ts, buff_sell_price, buff_sell_num, buff_buy_num
                        from item_listing_history where ts >= '2026-08-20'
                        and buff_sell_price > 0 order by good_id, ts""").fetchall()
    c.close()

    seq = defaultdict(list)
    for r in rows:
        seq[r["good_id"]].append((
            datetime.strptime(r["ts"], "%Y-%m-%d %H:%M:%S").timestamp(),
            r["buff_sell_price"], r["buff_sell_num"], r["buff_buy_num"]))
    print(f"{len(seq)} 个品\n")

    base = defaultdict(lambda: defaultdict(list))
    distrib = defaultdict(lambda: defaultdict(list))
    launch = defaultdict(lambda: defaultdict(list))

    for gid, items in seq.items():
        if len(items) < 30:
            continue
        ts = [x[0] for x in items]
        for i in range(1, len(items) - 1):
            a, b = items[i - 1], items[i]
            if b[0] - a[0] <= 0 or b[0] - a[0] > 120 * 60:
                continue
            p0, s0, b0 = a[1], a[2], a[3]
            p1, s1, b1 = b[1], b[2], b[3]
            if not (p0 and s0 and b0 and p1):
                continue
            sell_chg = (s1 - s0) / s0 * 100
            buy_chg = ((b1 - b0) / b0 * 100) if b1 else 0.0
            price_chg = (p1 - p0) / p0 * 100

            lo = bisect.bisect_left(ts, b[0] - 30 * 86400)
            win = [x[1] for x in items[lo:i + 1]]
            hi = max(win) if win else p1
            high_ok = hi > 0 and p1 / hi >= 0.90

            is_distrib = (sell_chg >= 6.0 and high_ok and abs(buy_chg) < 2.0)
            is_launch = (sell_chg <= -10.0 and price_chg >= 1.0)

            for hname, days in HORIZONS.items():
                k = bisect.bisect_left(ts, b[0] + days * 86400)
                if k >= len(items) or abs(ts[k] - (b[0] + days * 86400)) > days * 86400 * 0.5:
                    continue
                fwd = (items[k][1] / p1 - 1) * 100
                for floor in PRICE_FLOORS:
                    if p1 < floor:
                        continue
                    base[floor][hname].append(fwd)
                    if is_distrib:
                        distrib[floor][hname].append(fwd)
                    if is_launch:
                        launch[floor][hname].append(fwd)

    def stat(rets):
        if not rets:
            return None
        ups = 100.0 * sum(1 for x in rets if x > 0) / len(rets)
        return len(rets), ups, st.median(rets)

    for kind, data, note in (("派发（在售+6% + 高位 + 求购停滞）", distrib, "预警方向应「下跌更多」"),
                             ("拉升（在售-10% + 价格+1%）", launch, "追涨方向应「下跌更多」")):
        print(f"\n{'='*78}\n{kind}  —— {note}\n{'='*78}")
        for floor in PRICE_FLOORS:
            tag = "含廉价品" if floor == 0 else f"价格 ≥¥{floor:g}"
            print(f"\n  【{tag}】")
            for hname in HORIZONS:
                bs = stat(base[floor][hname])
                sg = stat(data[floor][hname])
                if not bs:
                    continue
                b_line = f"    基准  {hname:<4} n={bs[0]:<7} 上涨 {bs[1]:>5.1f}%  中位 {bs[2]:+7.2f}%"
                print(b_line)
                if sg:
                    diff = sg[1] - bs[1]
                    print(f"    信号  {hname:<4} n={sg[0]:<7} 上涨 {sg[1]:>5.1f}%  中位 {sg[2]:+7.2f}%"
                          f"   → 上涨率差 {diff:+.1f}pp")
                else:
                    print(f"    信号  {hname:<4} 无样本")


if __name__ == "__main__":
    main()
