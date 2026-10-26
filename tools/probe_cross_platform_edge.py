# -*- coding: utf-8 -*-
"""跨平台可捕获价差:买便宜的、卖贵的,扣卖方费与提现后的真实边际。

口径:
  买入价   = min(BUFF 最低在售, 悠悠 最低在售)
  卖出毛价 = max(BUFF 最低在售, 悠悠 最低在售)
  卖出净额 = 卖出毛价 × (1 - 该平台卖方费) - 提现(1%, 最低 ¥2)
  可捕获%  = (卖出净额 - 买入价) / 买入价

流动性守卫:只统计 BUFF 在售数 >= 400 的品(与机器人现有 min_scan_sell_num 对齐),
并对平台间价差 > 15% 的样本单独标注(疑似单边无接盘)。
"""
import sqlite3
import statistics
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "data" / "csqaq.db"
BUFF_FEE = 0.025
YYYP_FEE = 0.020          # 悠悠 1.0%~2.5%,取中值
WITHDRAW_PCT = 0.01
WITHDRAW_MIN = 2.0
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
        "select name, buff_sell_price b, yyyp_sell_price y, buff_sell_num n "
        "from price_snapshots where buff_sell_price > 0 and yyyp_sell_price > 0 "
        "and buff_sell_num >= 400 and created_at >= datetime('now','-3 day')"
    ).fetchall()
    print(f"=== 流动性守卫后在售≥400 的样本: {len(rows)} 条 ===")

    edges, wide = [], 0
    by_band = {}
    for r in rows:
        b, y = float(r["b"]), float(r["y"])
        buy = min(b, y)
        if b >= y:
            sell_gross, fee = b, BUFF_FEE
        else:
            sell_gross, fee = y, YYYP_FEE
        net = sell_gross * (1 - fee) - max(WITHDRAW_MIN, sell_gross * WITHDRAW_PCT)
        edge = (net - buy) / buy * 100
        if abs(b - y) / min(b, y) > 0.15:
            wide += 1
        edges.append(edge)
        by_band.setdefault(band_of(buy), []).append(edge)

    if not edges:
        print("  无样本")
        return
    edges.sort()
    n = len(edges)
    print(f"  平台间价差 >15% 的样本: {wide} 条 ({wide/n*100:.0f}%)")
    print(f"\n  可捕获边际: 中位 {statistics.median(edges):+5.2f}%  "
          f"P25 {edges[n//4]:+5.2f}%  P75 {edges[n*3//4]:+5.2f}%  P90 {edges[int(n*0.9)]:+5.2f}%")
    for th in (0, 2, 5, 8):
        cnt = sum(1 for x in edges if x >= th)
        print(f"  边际 ≥{th}% 的占比: {cnt/n*100:5.1f}%  ({cnt} 条)")

    print("\n  按价格带:")
    for key in [f"¥{lo}-{hi if hi < 10**9 else '+'}" for lo, hi in BANDS]:
        v = by_band.get(key)
        if v and len(v) >= 20:
            v.sort()
            pos = sum(1 for x in v if x >= 2) / len(v) * 100
            print(f"    {key:<12} n={len(v):>5}  中位 {statistics.median(v):+5.2f}%  "
                  f"≥2% 占比 {pos:4.0f}%")
    c.close()


if __name__ == "__main__":
    main()
