"""把建仓信号按状态维度分档，测出每一档的真实前瞻表现。

目的：卡片上「历史参照」不能写死一个常量，要按当时的状态取对应统计。
本脚本测量各维度分档的 T+7 表现，供落表 phase_stats 使用。

维度：
  求购强度  强(≥30%) / 中(15-30%) / 弱(12-15%)
  供给方向  收缩(≤-3%) / 持平(-3~3%) / 增加(≥3%)
  价格      横盘(|chg|<1%) / 微跌(-3~-1%) / 微涨(1~3%)
  位置      低位(≤30%) / 中位(30-70%) / 高位(≥70%)
  供需比    紧张(<3) / 偏紧(3-10) / 宽松(10-20) / 过剩(≥20)

全部限定 现价 ≥¥100（可交易品），避免廉价品伪影。

    .venv\\Scripts\\python.exe tools\\probe_accum_states.py
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
MIN_PRICE = 100.0


def main() -> None:
    c = sqlite3.connect("file:D:/cs2-qq-bot/data/csqaq.db?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    rows = c.execute("""select good_id, ts, buff_sell_price, buff_sell_num, buff_buy_num
                        from item_listing_history where ts >= '2026-08-20'
                        and buff_sell_price >= ? order by good_id, ts""",
                     (MIN_PRICE,)).fetchall()
    c.close()

    seq = defaultdict(list)
    for r in rows:
        seq[r["good_id"]].append((
            datetime.strptime(r["ts"], "%Y-%m-%d %H:%M:%S").timestamp(),
            r["buff_sell_price"], r["buff_sell_num"], r["buff_buy_num"]))
    print(f"现价 ≥¥{MIN_PRICE:g}：{len(seq)} 个品、{len(rows)} 行\n")

    dims = {
        "求购强度": lambda b, s, p, pos, ratio: (
            "强 ≥30%" if b >= 30 else "中 15-30%" if b >= 15 else "弱 12-15%"),
        "供给方向": lambda b, s, p, pos, ratio: (
            "收缩 ≤-3%" if s <= -3 else "持平" if s < 3 else "增加 ≥3%"),
        "价格": lambda b, s, p, pos, ratio: (
            "横盘" if abs(p) < 1 else "微涨" if p > 0 else "微跌"),
        "位置": lambda b, s, p, pos, ratio: (
            "低位 ≤30%" if pos <= 30 else "中位" if pos < 70 else "高位 ≥70%"),
        "供需比": lambda b, s, p, pos, ratio: (
            "紧张 <3" if ratio < 3 else "偏紧 3-10" if ratio < 10
            else "宽松 10-20" if ratio < 20 else "过剩 ≥20"),
    }
    groups = {k: defaultdict(list) for k in dims}
    overall = []

    for gid, items in seq.items():
        if len(items) < 10:
            continue
        ts = [x[0] for x in items]
        for i in range(1, len(items)):
            p1, s1, b1 = items[i][1], items[i][2], items[i][3]
            target = ts[i] - WINDOW_MIN * 60
            j = bisect.bisect_left(ts, target)
            cand = [x for x in (j - 1, j) if 0 <= x < i]
            if not cand:
                continue
            j = min(cand, key=lambda x: abs(ts[x] - target))
            if abs(ts[j] - target) > 30 * 60:
                continue
            p0, s0, b0 = items[j][1], items[j][2], items[j][3]
            if not (s0 and b0 and p0):
                continue
            buy_chg = (b1 - b0) / b0 * 100
            sell_chg = (s1 - s0) / s0 * 100
            price_chg = (p1 - p0) / p0 * 100
            seg = items[j:i + 1]
            monotonic = all(x[3] <= y[3] for x, y in zip(seg, seg[1:]))
            if buy_chg >= BUY_PCT_MIN and price_chg <= -1.0 and sell_chg >= 5.0:
                continue
            if not (buy_chg >= BUY_PCT_MIN and b1 - b0 >= BUY_ABS_MIN
                    and sell_chg < SELL_MAX and monotonic
                    and (abs(price_chg) < PRICE_QUIET or abs(p1 - p0) <= PRICE_QUIET_ABS)):
                continue

            lo = bisect.bisect_left(ts, ts[i] - 30 * 86400)
            win = [x[1] for x in items[lo:i + 1]]
            if len(win) < 20:
                continue
            hi, low = max(win), min(win)
            pos = (p1 - low) / (hi - low) * 100 if hi > low else 50.0
            ratio = s1 / b1 if b1 else 999

            k = bisect.bisect_left(ts, ts[i] + 7 * 86400)
            if k >= len(items) or abs(ts[k] - (ts[i] + 7 * 86400)) > 3.5 * 86400:
                continue
            fwd = (items[k][1] / p1 - 1) * 100
            overall.append(fwd)
            for name, fn in dims.items():
                groups[name][fn(buy_chg, sell_chg, price_chg, pos, ratio)].append(fwd)

    def line(label, rets, width=16):
        ups = 100.0 * sum(1 for x in rets if x > 0) / len(rets)
        big = 100.0 * sum(1 for x in rets if x >= 15) / len(rets)
        dn = 100.0 * sum(1 for x in rets if x <= -15) / len(rets)
        print(f"  {label:<{width}} n={len(rets):<6} 上涨 {ups:>5.1f}%  "
              f"中位 {st.median(rets):+7.2f}%  ≥+15% {big:>5.1f}%  ≤-15% {dn:>5.1f}%")

    print("=== 全部建仓信号（现价 ≥¥100） ===")
    line("合计", overall)
    for name in dims:
        print(f"\n=== {name} ===")
        for label, rets in sorted(groups[name].items(), key=lambda kv: -st.median(kv[1])):
            if len(rets) >= 30:
                line(label, rets)
            else:
                print(f"  {label:<16} n={len(rets):<6} (样本不足 30，不采用)")


if __name__ == "__main__":
    main()
