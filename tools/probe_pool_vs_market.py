# -*- coding: utf-8 -*-
"""池子 vs 全市场：选品质量对照。

只读 data/csqaq.db。

    .venv\\Scripts\\python.exe tools\\probe_pool_vs_market.py

回答的问题：池子里的品，涨跌表现比「随便一个品」更好还是更差？
"""

from __future__ import annotations

import sqlite3
import statistics as st
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "data" / "csqaq.db"
SINCE = "2026-09-25"
H = 7


def category(name: str) -> str:
    n = name or ""
    if "★" in n and ("刀" in n or "匕首" in n):
        return "刀具"
    if "手套" in n:
        return "手套"
    if "印花" in n or "贴纸" in n:
        return "印花/贴纸"
    if "挂件" in n:
        return "挂件"
    if "音乐盒" in n:
        return "音乐盒"
    if "收藏品" in n:
        return "收藏品"
    if "探员" in n:
        return "探员"
    return "武器皮肤"


def main() -> None:
    c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    c.row_factory = sqlite3.Row

    print("=" * 78)
    print("池子 vs 全市场")
    print("=" * 78)

    rows = c.execute("""
        select good_id, d, p from (
            select good_id, substr(ts,1,10) d, buff_sell_price p, ts,
                   row_number() over (partition by good_id, substr(ts,1,10) order by ts desc) rn
            from item_listing_history
            where buff_sell_price > 0 and ts >= ?)
        where rn=1 order by good_id, d""", (SINCE,)).fetchall()

    by_gid: dict[int, list[float]] = defaultdict(list)
    for r in rows:
        by_gid[r["good_id"]].append(r["p"])

    per_item = {}
    for gid, px in by_gid.items():
        if len(px) < 8:
            continue
        rets = [(px[i + H] / px[i] - 1) * 100 for i in range(len(px) - H) if px[i] > 0]
        if rets:
            per_item[gid] = st.mean(rets)

    universe = list(per_item.values())
    print(f"\n[1] 全市场逐品 {H} 日收益（{len(universe)} 个品，{SINCE} 起）")
    print(f"   中位 {st.median(universe):+6.2f}%  均值 {st.mean(universe):+6.2f}%  "
          f"上涨占比 {100.0 * sum(1 for x in universe if x > 0) / len(universe):>5.1f}%")

    pool = c.execute("select good_id, name from watch_pool").fetchall()
    pool_ids = {p["good_id"] for p in pool}
    inside = [v for gid, v in per_item.items() if gid in pool_ids]
    print(f"\n[2] 池内 {len(inside)} 个品（池子共 {len(pool)} 件，其余无足够日线）")
    if inside:
        print(f"   中位 {st.median(inside):+6.2f}%  均值 {st.mean(inside):+6.2f}%  "
              f"上涨占比 {100.0 * sum(1 for x in inside if x > 0) / len(inside):>5.1f}%")
        print(f"   → 与全市场中位差 {st.median(inside) - st.median(universe):+.2f}pp")

    print("\n[3] 池子品类结构")
    cnt = Counter(category(p["name"]) for p in pool)
    for k, v in cnt.most_common():
        print(f"   {k:<10} {v:>4} 件  ({100.0 * v / len(pool):.0f}%)")

    print("\n[4] 按品类看池内表现")
    cat_ret: dict[str, list[float]] = defaultdict(list)
    names = {p["good_id"]: p["name"] for p in pool}
    for gid, v in per_item.items():
        if gid in names:
            cat_ret[category(names[gid])].append(v)
    for k, v in sorted(cat_ret.items(), key=lambda kv: -len(kv[1])):
        print(f"   {k:<10} n={len(v):<4} 中位 {st.median(v):+6.2f}%  "
              f"上涨占比 {100.0 * sum(1 for x in v if x > 0) / len(v):>5.1f}%")

    c.close()


if __name__ == "__main__":
    main()
