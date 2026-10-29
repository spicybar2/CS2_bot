"""严格复现「建仓监测」的生产条件，看它到底有没有预测力。

生产条件（bot/anomaly/cycle.py + config.yaml）：
  求购窗口净增 ≥ accum_buy_pct_min(12%)  且绝对增量 ≥ 3 单
  在售净增 < accum_sell_max(5%)
  价格 |chg| < accum_price_quiet(3%)（或绝对变动 ≤ ¥0.5）
  窗口内求购**逐段不降**
  假买墙过滤：求购增 + 价格跌 + 在售暴增 → 直接丢弃

本脚本按上面每一条逐项复现，并用「该品自身同视界基准」配对对照。
再拆几个子集，看有没有哪个版本真有效：
  - 求购数涨 but 求购价也涨（真出价 vs 只堆量）
  - 高价品 / 低价品
  - 求购/在售 比值处于极端

    .venv\\Scripts\\python.exe tools\\probe_accum_exact.py
"""
from __future__ import annotations

import bisect
import sqlite3
import statistics as st
from collections import defaultdict
from datetime import datetime

# 生产阈值
BUY_PCT_MIN = 12.0
BUY_ABS_MIN = 3
SELL_MAX = 5.0
PRICE_QUIET = 3.0
PRICE_QUIET_ABS = 0.5
WINDOW_MIN = 60
HORIZONS = {"24h": 86400, "72h": 259200, "7d": 604800}


def build(c):
    rows = c.execute("""select good_id, ts, buff_sell_price, buff_buy_price,
                               buff_sell_num, buff_buy_num
                        from item_listing_history where ts >= '2026-08-20'
                        and buff_sell_price > 0 order by good_id, ts""").fetchall()
    seq = defaultdict(list)
    for r in rows:
        seq[r["good_id"]].append((
            datetime.strptime(r["ts"], "%Y-%m-%d %H:%M:%S").timestamp(),
            r["buff_sell_price"], r["buff_buy_price"],
            r["buff_sell_num"], r["buff_buy_num"]))
    return seq


def main() -> None:
    c = sqlite3.connect("file:D:/cs2-qq-bot/data/csqaq.db?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    seq = build(c)
    c.close()
    print(f"{len(seq)} 个品")

    groups = defaultdict(lambda: defaultdict(list))
    base = defaultdict(list)

    for gid, items in seq.items():
        if len(items) < 10:
            continue
        ts = [x[0] for x in items]
        for i in range(1, len(items)):
            now = items[i]
            # 找 ~WINDOW_MIN 分钟前的锚点
            target = ts[i] - WINDOW_MIN * 60
            j = bisect.bisect_left(ts, target)
            cand = [k for k in (j - 1, j) if 0 <= k < i]
            if not cand:
                continue
            j = min(cand, key=lambda k: abs(ts[k] - target))
            if abs(ts[j] - target) > 30 * 60:
                continue

            p0, bp0, s0, b0 = items[j][1], items[j][2], items[j][3], items[j][4]
            p1, bp1, s1, b1 = now[1], now[2], now[3], now[4]
            if not s0 or not b0 or not p0 or not p1:
                continue
            buy_chg = (b1 - b0) / b0 * 100
            sell_chg = (s1 - s0) / s0 * 100
            price_chg = (p1 - p0) / p0 * 100
            buy_abs = b1 - b0
            bp_chg = ((bp1 - bp0) / bp0 * 100) if (bp0 and bp1) else 0.0
            # 窗口内求购逐段不降
            seg = items[j:i + 1]
            monotonic = all(x[4] <= y[4] for x, y in zip(seg, seg[1:]))

            # ---- 假买墙过滤 ----
            if (buy_chg >= BUY_PCT_MIN and price_chg <= -1.0 and sell_chg >= 5.0):
                continue

            # ---- 生产条件 ----
            is_accum = (buy_chg >= BUY_PCT_MIN and buy_abs >= BUY_ABS_MIN
                        and sell_chg < SELL_MAX
                        and (abs(price_chg) < PRICE_QUIET or abs(p1 - p0) <= PRICE_QUIET_ABS)
                        and monotonic)

            for h, sec in HORIZONS.items():
                k = bisect.bisect_left(ts, ts[i] + sec)
                if k >= len(items) or abs(ts[k] - (ts[i] + sec)) > sec * 0.5:
                    continue
                fwd = (items[k][1] / p1 - 1) * 100
                base[h].append((gid, fwd))
                if is_accum:
                    groups["生产条件(完整)"][h].append((gid, fwd))
                    if bp_chg >= 3:
                        groups["+求购价也涨3%"][h].append((gid, fwd))
                    if p1 >= 100:
                        groups["+高价品(≥¥100)"][h].append((gid, fwd))
                    if p1 >= 500:
                        groups["+高价品(≥¥500)"][h].append((gid, fwd))
                    if b0 and s0 and b1 / max(s1, 1) >= 0.5:
                        groups["+求购/在售 ≥0.5"][h].append((gid, fwd))

    # 每品自身基准
    item_base = defaultdict(lambda: defaultdict(float))
    for h in HORIZONS:
        per = defaultdict(list)
        for gid, fwd in base[h]:
            per[gid].append(1 if fwd > 0 else 0)
        for gid, v in per.items():
            if v:
                item_base[h][gid] = 100.0 * sum(v) / len(v)

    def show(label, d):
        v7 = d.get("7d") or []
        if not v7:
            print(f"  {label:<22} 无样本")
            return
        line = [f"  {label:<22}"]
        for h in HORIZONS:
            v = d.get(h) or []
            if not v:
                continue
            rets = [x[1] for x in v]
            ups = 100.0 * sum(1 for x in rets if x > 0) / len(rets)
            ex = [ups - item_base[h].get(g, ups) for g, _ in v]
            line.append(f"{h}: n={len(v):<5} 中位 {st.median(rets):+6.2f}% "
                        f"上涨 {ups:>5.1f}% 超额 {st.median(ex):+5.1f}pp")
        print("\n".join(line) if len(line) > 2 else line[0])
        for seg in line[1:]:
            print(f"      {seg}")

    print("\n=== 基准 ===")
    show("全体采样点", base)
    print("\n=== 生产条件及子集 ===")
    for k in ("生产条件(完整)", "+求购价也涨3%", "+高价品(≥¥100)", "+高价品(≥¥500)", "+求购/在售 ≥0.5"):
        if k in groups:
            show(k, groups[k])


if __name__ == "__main__":
    main()
