"""老鼠仓的关键区分：「悄悄建仓」还是「追已经涨的」。

求购上升可能只是价格已经在涨的机械结果（庄家抬价 → 跟风挂求购）。
真正的建仓应该是「求购涨、价格还没动」。

本脚本在深度够的品上，把「求购一周 +30%」拆成两种：
  A 悄悄建仓：求购 +30%，但这一周价格变动在 ±2% 以内（还没拉）
  B 追涨：求购 +30%，且这一周价格已经涨 >5%（已经在拉）
看各自的 7 天前瞻（含均值、中位、上涨率、暴涨率）。

    .venv\\Scripts\\python.exe tools\\probe_quiet_accum.py
"""
from __future__ import annotations

import sqlite3
import statistics as st
from collections import defaultdict

BUY_UP_TH = 30.0
HOLD = 7
MIN_DEPTH = 300


def main() -> None:
    c = sqlite3.connect("file:D:/cs2-qq-bot/data/csqaq.db?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    rows = c.execute("""select good_id, ts, buff_sell_price, buff_sell_num, buff_buy_num
                        from item_listing_history where ts >= '2026-08-20'
                        and buff_sell_price > 0 order by good_id, ts""").fetchall()
    c.close()

    days = defaultdict(dict)
    for r in rows:
        days[r["good_id"]][r["ts"][:10]] = (
            r["buff_sell_price"], r["buff_sell_num"], r["buff_buy_num"])

    groups = defaultdict(list)
    base = []

    for gid, daily in days.items():
        dts = sorted(daily)
        if len(dts) < 20:
            continue
        for i in range(14, len(dts) - HOLD):
            p, sell, _buy = daily[dts[i]]
            p_fwd = daily[dts[i + HOLD]][0]
            if not p or not p_fwd or not sell or sell < MIN_DEPTH:
                continue
            fwd = (p_fwd / p - 1) * 100
            base.append(fwd)

            win = dts[i - 14:i - 6]
            if len(win) < 5:
                continue
            buys = [daily[d][2] for d in win if daily[d][2]]
            prices = [daily[d][0] for d in win if daily[d][0]]
            if len(buys) < 3 or not min(buys) or len(prices) < 3:
                continue
            buy_up = (max(buys) / min(buys) - 1) * 100
            if buy_up < BUY_UP_TH:
                continue
            week_move = (prices[-1] / prices[0] - 1) * 100
            if abs(week_move) <= 2:
                groups["A 悄悄建仓（求购+30%，价±2%）"].append(fwd)
            elif week_move > 5:
                groups["B 追涨（求购+30%，价已+5%）"].append(fwd)
            else:
                groups["C 其它（价 -2%~+5%）"].append(fwd)

    def show(label, rets):
        if not rets:
            print(f"  {label:<32} 无样本")
            return
        ups = 100.0 * sum(1 for x in rets if x > 0) / len(rets)
        big = 100.0 * sum(1 for x in rets if x >= 15) / len(rets)
        rough = 100.0 * sum(1 for x in rets if x <= -15) / len(rets)
        print(f"  {label:<32} n={len(rets):<6} 均值 {st.mean(rets):+6.2f}%  "
              f"中位 {st.median(rets):+6.2f}%  上涨 {ups:>5.1f}%  "
              f"≥15% {big:>4.1f}%  ≤-15% {rough:>4.1f}%")

    print(f"只看 BUFF 在售 ≥{MIN_DEPTH} 的品，持有 {HOLD} 天\n")
    show("基准（全体深度品任意日）", base)
    print()
    for k in sorted(groups):
        show(k, groups[k])


if __name__ == "__main__":
    main()
