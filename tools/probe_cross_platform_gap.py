# -*- coding: utf-8 -*-
"""两个平台的「最低在售价」差多少:决定成本门槛该用哪个价。

1) 谁更便宜(BUFF vs 悠悠),各占多少
2) 价差幅度分布(按价格带)
3) 用 min(两平台) 相比只用 BUFF,折扣会抬高多少
"""
import sqlite3
import statistics
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "data" / "csqaq.db"
BANDS = [(0, 50), (50, 200), (200, 1000), (1000, 5000), (5000, 10**9)]


def band_of(p):
    for lo, hi in BANDS:
        if lo <= p < hi:
            return f"¥{lo}-{hi if hi < 10**9 else '+'}"
    return "?"


def main():
    c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    c.row_factory = sqlite3.Row

    rows = c.execute(
        "select buff_sell_price b, yyyp_sell_price y from price_snapshots "
        "where buff_sell_price > 0 and yyyp_sell_price > 0 "
        "and created_at >= datetime('now','-7 day') limit 400000"
    ).fetchall()
    print(f"=== 两平台最低在售价(样本 {len(rows)} 条) ===")

    yyyp_cheaper = buff_cheaper = equal = 0
    gaps, gaps_by_band = [], {}
    for r in rows:
        b, y = float(r["b"]), float(r["y"])
        lo = min(b, y)
        gap = abs(b - y) / lo * 100
        if y < b - 0.005:
            yyyp_cheaper += 1
            side = "悠悠更便宜"
        elif b < y - 0.005:
            buff_cheaper += 1
            side = "BUFF更便宜"
        else:
            equal += 1
            side = "持平"
        gaps.append(gap)
        gaps_by_band.setdefault(band_of(lo), []).append(gap)

    n = len(rows)
    print(f"  悠悠更便宜: {yyyp_cheaper:>7} ({yyyp_cheaper/n*100:5.1f}%)")
    print(f"  BUFF更便宜: {buff_cheaper:>7} ({buff_cheaper/n*100:5.1f}%)")
    print(f"  完全持平:   {equal:>7} ({equal/n*100:5.1f}%)")

    gaps.sort()
    print(f"\n  差价幅度: 中位 {statistics.median(gaps):.2f}%  "
          f"P25 {gaps[n//4]:.2f}%  P75 {gaps[n*3//4]:.2f}%  P90 {gaps[int(n*0.9)]:.2f}%")

    print("\n  按价格带:")
    for key in [f"¥{lo}-{hi if hi < 10**9 else '+'}" for lo, hi in BANDS]:
        v = gaps_by_band.get(key)
        if v and len(v) >= 30:
            v.sort()
            print(f"    {key:<12} n={len(v):>7}  中位 {statistics.median(v):5.2f}%  "
                  f"P75 {v[len(v)*3//4]:5.2f}%")

    # 只用 BUFF 会漏掉多少折扣
    print("\n=== 用 min(两平台) 相比只用 BUFF:折扣被低估多少 ===")
    deltas = [(b - min(b, y)) / b * 100 for b, y in
              ((float(r["b"]), float(r["y"])) for r in rows)]
    deltas.sort()
    nonzero = [d for d in deltas if d > 0.01]
    print(f"  全部样本中位提升: {statistics.median(deltas):.2f}%")
    if nonzero:
        print(f"  悠悠更便宜那部分样本(n={len(nonzero)}): 中位提升 "
              f"{statistics.median(nonzero):.2f}%  P75 {nonzero[len(nonzero)*3//4]:.2f}%")

    print("\n=== 参考:平台费率 ===")
    print("  BUFF 卖方 2.5% | 悠悠卖方 1.0%~2.5% | 提现 1%(银行卡,最低 ¥2) / 支付宝 0%")
    c.close()


if __name__ == "__main__":
    main()
