# -*- coding: utf-8 -*-
"""量出「一笔倒货的真实往返成本」:价差 + 手续费 + 提现(含最低额)。

只读本地库。BUFF 用 buff_sell_price(最低要价) / buff_buy_price(最高求购),
悠悠用 yyyp_sell_price,求购用现价的镜像估算(库内无 yyyp 求购价时标注)。
"""
import sqlite3
import statistics
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "data" / "csqaq.db"

BANDS = [(0, 20), (20, 50), (50, 200), (200, 1000), (1000, 5000), (5000, 10**9)]


def band_of(p):
    for lo, hi in BANDS:
        if lo <= p < hi:
            return f"¥{lo}-{hi if hi < 10**9 else '+'}"
    return "?"


def main():
    c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    c.row_factory = sqlite3.Row

    print("=== 1) BUFF 买卖价差(最低要价 vs 最高求购) ===")
    rows = c.execute(
        "select buff_sell_price s, buff_buy_price b from price_snapshots "
        "where buff_sell_price > 0 and buff_buy_price > 0 "
        "and created_at >= datetime('now','-7 day') limit 300000"
    ).fetchall()
    spreads = []
    by_band = {}
    for r in rows:
        s, b = float(r["s"]), float(r["b"])
        if b >= s:
            continue
        pct = (s - b) / s * 100
        spreads.append(pct)
        by_band.setdefault(band_of(s), []).append(pct)
    spreads.sort()
    n = len(spreads)
    print(f"  样本 {n} 条")
    print(f"  价差中位 {statistics.median(spreads):.2f}%  "
          f"P25 {spreads[n//4]:.2f}%  P75 {spreads[n*3//4]:.2f}%  P90 {spreads[int(n*0.9)]:.2f}%")
    print("\n  按价格带:")
    for key in [f"¥{lo}-{hi if hi < 10**9 else '+'}" for lo, hi in BANDS]:
        v = by_band.get(key)
        if v and len(v) >= 20:
            v.sort()
            print(f"    {key:<12} n={len(v):>6}  中位 {statistics.median(v):5.2f}%  P75 {v[len(v)*3//4]:5.2f}%")

    print("\n=== 2) 提现最低额对小额品的影响(银行卡 ¥2 起、四舍五入) ===")
    print(f"  {'单价':>8} {'2.5%手续费':>11} {'提现¥2':>9} {'提现占比':>9} {'合计成本':>9}")
    for price in (10, 20, 50, 100, 200, 1000, 5000):
        fee = price * 0.025
        wd = max(2.0, round(price * 0.01))
        wd_pct = wd / price * 100
        total = 2.5 + wd_pct
        print(f"  ¥{price:>7} {fee:>10.2f} {wd:>8.2f} {wd_pct:>8.1f}% {total:>8.1f}%")

    print("\n=== 3) 盈亏平衡需要的涨幅(往返) ===")
    print("  公式: 卖出所得 = 售价 × (1 - 卖方费率) - 提现;要求 卖出所得 > 买入价")
    med_spread = statistics.median(spreads) if spreads else 0
    for label, sell_fee in (("悠悠 1.0%(低)", 0.010), ("悠悠 2.5%/BUFF 2.5%", 0.025)):
        for price in (50, 200, 1000):
            wd = max(2.0, round(price * 0.01))
            # 需要的新涨幅 x: price*(1+x)*(1-fee) - wd_withdraw_effect > price
            # 简化: 售价 P2, 到手 = P2*(1-fee) - max(2, P2*0.01)
            lo, hi = 0.0, 1.0
            for _ in range(60):
                mid = (lo + hi) / 2
                p2 = price * (1 + mid)
                net = p2 * (1 - sell_fee) - max(2.0, p2 * 0.01)
                if net > price:
                    hi = mid
                else:
                    lo = mid
            print(f"  {label:<20} 单价 ¥{price:<5} 打平需涨 {hi*100:5.1f}%"
                  f"  (若按要价卖出;若须让利半个价差 {med_spread/2:.1f}% → "
                  f"{hi*100 + med_spread/2:5.1f}%)")
    c.close()


if __name__ == "__main__":
    main()
