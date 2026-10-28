"""派发预警的真实预测力：加上「高位」条件后，还准不准。

代码里的派发条件（config.yaml）：
  在售窗口净增 ≥6%  +  现价 ≥ 近 30 日最高价 ×90%  +  求购 |chg| <2%（停滞）

之前只测了「在售 +10%」，没加高位。本脚本对比：
  A 裸在售暴增（不加高位）
  B 在售暴增 + 高位（代码真实条件）
  C 在售暴增 + 高位 + 求购停滞（完整条件）

    .venv\\Scripts\\python.exe tools\\probe_distribute.py
"""
from __future__ import annotations

import bisect
import sqlite3
import statistics as st
from collections import defaultdict
from datetime import datetime

SELL_UP = 6.0          # distrib_sell_pct
HIGH_RATIO = 0.90      # distrib_range_pct
BUY_STALL = 2.0        # distrib_buy_stall
HORIZONS = {"24h": 86400, "72h": 259200, "7d": 604800}


def main() -> None:
    c = sqlite3.connect("file:D:/cs2-qq-bot/data/csqaq.db?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    rows = c.execute("""select good_id, ts, buff_sell_price, buff_sell_num, buff_buy_num
                        from item_listing_history where ts >= '2026-08-20'
                        and buff_sell_price > 0 order by good_id, ts""").fetchall()
    c.close()

    seq = defaultdict(list)
    for r in rows:
        seq[r["good_id"]].append((datetime.strptime(r["ts"], "%Y-%m-%d %H:%M:%S").timestamp(),
                                  r["buff_sell_price"], r["buff_sell_num"], r["buff_buy_num"]))
    print(f"{len(seq)} 个品、{len(rows)} 行")

    groups = {k: defaultdict(list) for k in ("A 裸在售暴增", "B +高位", "C +高位+求购停滞")}
    base = defaultdict(list)

    for gid, items in seq.items():
        if len(items) < 30:
            continue
        ts = [x[0] for x in items]
        # 逐点算「近 30 日最高价」（用日内最后一点近似，简化：滚动窗口 30 天）
        for i in range(len(items) - 1):
            a, b = items[i], items[i + 1]
            dt = b[0] - a[0]
            if dt <= 0 or dt > 120 * 60:
                continue
            p0, s0, b0 = a[1], a[2], a[3]
            p1, s1, b1 = b[1], b[2], b[3]
            if not s0 or not p0:
                continue
            sell_chg = (s1 - s0) / s0 * 100
            buy_chg = ((b1 - b0) / b0 * 100) if (b0 and b1) else 0.0

            # 前 30 天最高价（用 i 之前 30 天的采样点近似）
            lo = bisect.bisect_left(ts, ts[i] - 30 * 86400)
            window = items[lo:i + 1]
            hi = max(x[1] for x in window) if window else p1
            high_ok = hi > 0 and p1 / hi >= HIGH_RATIO

            for h, sec in HORIZONS.items():
                j = bisect.bisect_left(ts, ts[i + 1] + sec)
                if j >= len(items) or abs(ts[j] - (ts[i + 1] + sec)) > sec * 0.5:
                    continue
                fwd = (items[j][1] / p1 - 1) * 100
                base[h].append(fwd)
                if sell_chg >= SELL_UP:
                    groups["A 裸在售暴增"][h].append(fwd)
                    if high_ok:
                        groups["B +高位"][h].append(fwd)
                        if abs(buy_chg) < BUY_STALL:
                            groups["C +高位+求购停滞"][h].append(fwd)

    def show(label, d):
        if not d.get("7d"):
            print(f"  {label:<22} 无样本")
            return
        parts = []
        for h in HORIZONS:
            v = d.get(h) or []
            if v:
                parts.append(f"{h}: n={len(v):<5} 中位 {st.median(v):+6.2f}% 上涨 {100.0*sum(1 for x in v if x>0)/len(v):>5.1f}%")
        print(f"  {label}")
        for p_ in parts:
            print(f"      {p_}")

    print("\n=== 基准（所有品的所有步进） ===")
    show("基准", base)
    print("\n=== 派发三类条件对比 ===")
    for k in groups:
        show(k, groups[k])
    print("\n判据：派发要成立，需要「相对基准更负」且 7 日不衰减。")


if __name__ == "__main__":
    main()
