"""扣费之后的真实胜率：卡片上该印的「胜率」到底是哪个数。

为什么不能直接印「上涨率」
--------------------------
卡片里现在的「上涨 39.7%」是 P(7 天后最低售价 > 今天最低售价)，**没扣手续费**。
但真实交易是：今天按最低售价买入，7 天后按最低售价挂出，平台抽 2.5%（BUFF）。
所以涨 0% 是亏的 —— 盈亏平衡点是 +2.564%（= 1/0.975 - 1）。

一个「上涨率 39.7%」的形态，扣费后可能只剩 20% 出头，这才是可交易的数字。

本脚本对每个信号组同时给出四个口径
--------------------------------
  1. 上涨率        P(fwd > 0)              —— 现在卡片印的那个
  2. 扣费胜率      P(fwd > 2.564%)         —— 买卖都走最低售价、扣 2.5%
  3. 秒出胜率      P(bid_{t+h} 扣费 > ask_t) —— 直接砸给最高求购价（最坏情况）
  4. 中位净收益    median(net)             —— 扣费后的中位，正负一目了然

    .venv\\Scripts\\python.exe tools\\probe_net_winrate.py
"""
from __future__ import annotations

import bisect
import sqlite3
import statistics as st
from collections import defaultdict
from datetime import datetime

FEE = 0.025                     # BUFF 卖出手续费 2.5%
BREAKEVEN = (1.0 / (1.0 - FEE) - 1.0) * 100.0   # +2.564%
MIN_PRICE = 100.0
HORIZONS = {"24h": 86400, "7d": 604800}


def main() -> None:
    c = sqlite3.connect("file:D:/cs2-qq-bot/data/csqaq.db?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    rows = c.execute("""select good_id, ts, buff_sell_price, buff_sell_num,
                               buff_buy_num, buff_buy_price
                        from item_listing_history
                        where ts >= datetime('now','localtime','-45 days') and buff_sell_price >= ?
                          and buff_buy_price > 0
                        order by good_id, ts""", (MIN_PRICE,)).fetchall()
    c.close()

    seq = defaultdict(list)
    for r in rows:
        seq[r["good_id"]].append((datetime.strptime(r["ts"], "%Y-%m-%d %H:%M:%S").timestamp(),
                                  r["buff_sell_price"], r["buff_sell_num"],
                                  r["buff_buy_num"], r["buff_buy_price"]))
    print(f"{len(seq)} 个品、{len(rows)} 行（现价 ≥¥{MIN_PRICE:g}，卖方费 {FEE*100:.1f}%）")
    print(f"盈亏平衡点 = +{BREAKEVEN:.2f}%（涨不到这里就是亏）")

    groups = defaultdict(lambda: defaultdict(list))   # group -> horizon -> [(fwd, bid_ratio)]
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
            if not s0 or not p0 or not bp0:
                continue
            price_chg = (b[1] - p0) / p0 * 100
            qty_chg = ((b[3] - b0) / b0 * 100) if b0 else 0.0
            bid_chg = (b[4] - bp0) / bp0 * 100
            sell_chg = ((b[2] - s0) / s0 * 100) if s0 else 0.0

            lo = bisect.bisect_left(ts, ts[i] - 30 * 86400)
            window = items[lo:i + 1]
            lo_px, hi_px = min(x[1] for x in window), max(x[1] for x in window)
            if hi_px <= lo_px:
                continue
            pct = (b[1] - lo_px) / (hi_px - lo_px) * 100
            pos = "低位" if pct <= 30 else ("高位" if pct >= 70 else "中位")

            online_accum = (qty_chg >= 12.0 and abs(price_chg) < 3.0 and sell_chg <= 5.0)
            lift = bid_chg >= 3.0
            high_ok = b[1] / hi_px >= 0.90
            distribute = (sell_chg >= 6.0 and high_ok and qty_chg < 2.0)
            launch = (bid_chg >= 3.0 and price_chg >= 1.0 and sell_chg <= 5.0)

            tags = [f"基准·{pos}", "基准·全体"]
            if online_accum:
                tags.append(f"建仓+抬价·{pos}" if lift else f"建仓+堆量·{pos}")
            if distribute:
                tags.append("派发")
            if launch and lift:
                tags.append("启动")

            for h, sec in HORIZONS.items():
                j = bisect.bisect_left(ts, ts[i + 1] + sec)
                if j >= len(items) or abs(ts[j] - (ts[i + 1] + sec)) > sec * 0.5:
                    continue
                fwd = (items[j][1] / b[1] - 1) * 100
                bid_ratio = (items[j][4] / b[1] - 1) * 100 if items[j][4] else None
                for t in tags:
                    groups[t][h].append((fwd, bid_ratio))

    order = ["基准·全体", "基准·低位", "基准·中位", "基准·高位",
             "建仓+堆量·低位", "建仓+堆量·中位", "建仓+堆量·高位",
             "建仓+抬价·低位", "建仓+抬价·中位", "建仓+抬价·高位",
             "派发", "启动"]

    for h in HORIZONS:
        print(f"\n=== T+{h}（买入=当时最低售价，卖出=当时最低售价 − {FEE*100:.1f}%） ===")
        print(f"  {'组合':<16}{'n':>6}{'上涨率':>9}{'扣费胜率':>10}{'中位涨跌':>10}{'中位净':>10}")
        for g in order:
            v = groups.get(g, {}).get(h) or []
            if len(v) < 10:
                continue
            n = len(v)
            up = 100.0 * sum(1 for x, _ in v if x > 0) / n
            net_up = 100.0 * sum(1 for x, _ in v if x > BREAKEVEN) / n
            raw_med = st.median([x for x, _ in v])
            net_med = st.median([(1.0 + x / 100.0) * (1.0 - FEE) - 1.0 for x, _ in v]) * 100
            print(f"  {g:<16}{n:>6}{up:>8.1f}%{net_up:>9.1f}%{raw_med:>9.2f}%{net_med:>11.2f}%")

    print("\n说明：")
    print("  · 上涨率 = 现在卡片印的数；扣费胜率 = 真正能赚钱的比例；")
    print("  · 中位涨跌 = 未扣费的中位涨幅（卡片现在印的那个）；")
    print("  · 中位净收益为负时，说明这个形态即便胜率不低，也是「小赚大亏」结构。")


if __name__ == "__main__":
    main()
