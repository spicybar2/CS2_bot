"""建仓痕迹的收益，到底是不是来自「买不动的浅盘品」？

上一版发现：出现「求购一周 +30%」之后 7 天，上涨率 40.9%（基准 29.5%），
配对超额 +1.01pp。但中位数仍是 0.00% —— 大量品价格根本没动过。

本脚本按决策当日的盘口深度分层，检验这个优势是不是只存在于无法成交的浅盘品上。
如果深度品上优势消失，那「老鼠仓」就只是个统计幻觉。

    .venv\\Scripts\\python.exe tools\\probe_accum_payoff.py
"""
from __future__ import annotations

import sqlite3
import statistics as st
from collections import defaultdict

BUY_UP_TH = 30.0
HOLD = 7
DEPTH_CUTS = ((0, 100, "浅盘 <100"), (100, 300, "较浅 100-300"),
              (300, 800, "中等 300-800"), (800, 10**9, "深盘 ≥800"))


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

    def depth_bucket(n):
        if not n:
            return None
        for lo, hi, label in DEPTH_CUTS:
            if lo <= n < hi:
                return label
        return None

    sig = defaultdict(list)       # (深度档) -> [(gid, fwd)]
    base = defaultdict(list)      # (深度档) -> [fwd]
    item_fwd = defaultdict(list)

    for gid, daily in days.items():
        dts = sorted(daily)
        if len(dts) < 20:
            continue
        for i in range(0, len(dts) - HOLD):
            p, sell, _buy = daily[dts[i]]
            p_fwd = daily[dts[i + HOLD]][0]
            if not p or not p_fwd:
                continue
            fwd = (p_fwd / p - 1) * 100
            bk = depth_bucket(sell)
            if bk:
                base[bk].append(fwd)
                item_fwd[(gid, bk)].append(fwd)

            if i < 14:
                continue
            win = dts[i - 14:i - 6]
            if len(win) < 5:
                continue
            buys = [daily[d][2] for d in win if daily[d][2]]
            if len(buys) < 3 or not min(buys):
                continue
            if (max(buys) / min(buys) - 1) * 100 >= BUY_UP_TH and bk:
                sig[bk].append((gid, fwd))

    item_mean = {k: st.mean(v) for k, v in item_fwd.items() if v}

    def stats(rets):
        ups = 100.0 * sum(1 for x in rets if x > 0) / len(rets)
        flat = 100.0 * sum(1 for x in rets if x == 0) / len(rets)
        big = 100.0 * sum(1 for x in rets if x >= 15) / len(rets)
        return ups, flat, big

    print(f"持有 {HOLD} 天。『求购一周 +{BUY_UP_TH:.0f}%』之后买入，按盘口深度分层\n")
    print(f"{'深度档':<14}{'痕迹 n':>8}{'上涨%':>8}{'不动%':>8}{'暴涨≥15%':>10}"
          f"{'超额pp':>9}   |{'基准上涨%':>10}{'基准暴涨%':>10}")
    for _lo, _hi, label in DEPTH_CUTS:
        v = sig[label]
        b = base[label]
        if not v or not b:
            continue
        rets = [x[1] for x in v]
        u, f, big = stats(rets)
        bu, bf, bbig = stats(b)
        ex = [r - item_mean.get((g, label), 0) for g, r in v]
        print(f"  {label:<12}{len(rets):>8}{u:>8.1f}{f:>8.1f}{big:>10.1f}"
              f"{st.median(ex):>9.2f}   |{bu:>10.1f}{bbig:>10.1f}")

    print("\n判据：若优势随深度上升而消失，说明痕迹只出现在买不动的浅盘品上。")


if __name__ == "__main__":
    main()
