"""派发口径复核（CS 市场）：求购「停滞」是否该只认 ±2%，还是也该认「求购被撤」。

问题背景
--------
现有派发条件（config.yaml distrib_buy_stall=2.0）：
    在售窗口净增 ≥6%  +  现价 ≥ 近 30 日最高 ×90%  +  |求购变化| < 2%
最后一项用的是**绝对值**，于是「求购量 -20%（庄家撤掉求购墙）」会被判成
「不满足停滞」→ 不算派发。

这在 CS 市场上是反常识的：庄家出货时会**先撤掉自己的求购单**再往上挂卖，
求购量骤降本身就是派发指纹的一部分，而不是「没有派发」。

本脚本把「在售暴增 + 高位」的三类求购方向拆开对比，看：
    停滞（|chg|<2） / 下降（chg≤-2） / 上升（chg≥+2）
各自的 T+24h / T+7d 前瞻是否都为负。若「下降」同样甚至更负，
说明现有 `abs()` 写法漏掉了最典型的撤单派发，应当改成单边条件（只要求不增）。

口径
----
- 现价 ≥¥10（低价品一个报价台阶就是 ±25%，会把涨跌率全冲掉，见 2026-10-28 结论）；
- 相邻采样间隔 ≤120 分钟（与线上「近 2 小时窗口」一致）；
- 高位用滚动 30 日最高价（日内最后一点近似）；
- 前瞻点容差 = 前瞻时长的一半。

    .venv\\Scripts\\python.exe tools\\probe_distribute_buy.py
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
MIN_PRICE = 10.0
HORIZONS = {"24h": 86400, "7d": 604800}


def main() -> None:
    c = sqlite3.connect("file:D:/cs2-qq-bot/data/csqaq.db?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    rows = c.execute("""select good_id, ts, buff_sell_price, buff_sell_num, buff_buy_num
                        from item_listing_history
                        where ts >= '2026-08-20' and buff_sell_price >= ?
                        order by good_id, ts""", (MIN_PRICE,)).fetchall()
    c.close()

    seq = defaultdict(list)
    for r in rows:
        seq[r["good_id"]].append((datetime.strptime(r["ts"], "%Y-%m-%d %H:%M:%S").timestamp(),
                                  r["buff_sell_price"], r["buff_sell_num"], r["buff_buy_num"]))
    print(f"{len(seq)} 个品（现价 ≥¥{MIN_PRICE:g}）、{len(rows)} 行")

    labels = {
        "停滞 |chg|<2（现规则）": lambda x: abs(x) < BUY_STALL,
        "下降 chg≤-2（被漏掉）": lambda x: x <= -BUY_STALL,
        "上升 chg≥+2（现规则排除）": lambda x: x >= BUY_STALL,
        "单边 chg<+2（建议新规则）": lambda x: x < BUY_STALL,
    }
    groups = {k: defaultdict(list) for k in labels}
    base = defaultdict(list)

    for gid, items in seq.items():
        if len(items) < 30:
            continue
        ts = [x[0] for x in items]
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
                if sell_chg >= SELL_UP and high_ok:
                    for name, test in labels.items():
                        if test(buy_chg):
                            groups[name][h].append(fwd)

    def show(label, d):
        if not d.get("7d"):
            print(f"  {label}: 无样本")
            return
        print(f"  {label}")
        for h in HORIZONS:
            v = d.get(h) or []
            if not v:
                continue
            up = 100.0 * sum(1 for x in v if x > 0) / len(v)
            print(f"      {h}: n={len(v):<5} 中位 {st.median(v):+6.2f}%  上涨 {up:>5.1f}%")

    print("\n=== 基准（现价 ≥¥10 的全部步进） ===")
    show("基准", base)
    print("\n=== 在售 +6% 且 高位：按求购方向拆开 ===")
    for k in labels:
        show(k, groups[k])
    print("\n判据：若「下降」组的 7 日中位同样为负（甚至更负），"
          "说明 abs() 写法漏掉了撤单派发，应改为单边（只要求不增）。")


if __name__ == "__main__":
    main()
