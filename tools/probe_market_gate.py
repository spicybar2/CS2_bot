# -*- coding: utf-8 -*-
"""大盘闸门回测：事件当日市场强弱，能否预测信号的前瞻收益？

只读 data/csqaq.db。

    .venv\\Scripts\\python.exe tools\\probe_market_gate.py

大盘代理用「当日横截面上涨品占比」自建（market_sentiment 只有 2026-10-14 起的 15 天，不够用）。
结论（2026-10-28 实测）：强市开火反而更差，相关系数 -0.382 —— 「只在上涨日推送」是反的。
"""

from __future__ import annotations

import math
import sqlite3
import statistics as st
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "data" / "csqaq.db"


def main() -> None:
    c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    c.row_factory = sqlite3.Row

    print("=" * 78)
    print("大盘闸门回测")
    print("=" * 78)

    rows = c.execute("""
        select good_id, d, p from (
            select good_id, substr(ts,1,10) d, buff_sell_price p, ts,
                   row_number() over (partition by good_id, substr(ts,1,10) order by ts desc) rn
            from item_listing_history
            where buff_sell_price > 0 and ts >= '2026-08-20')
        where rn=1 order by good_id, d""").fetchall()

    px: dict[int, dict[str, float]] = defaultdict(dict)
    for r in rows:
        px[r["good_id"]][r["d"]] = r["p"]
    days = sorted({d for g in px.values() for d in g})

    prev: dict[int, float] = {}
    breadth: dict[str, float] = {}
    mret: dict[str, float] = {}
    for d in days:
        rets = []
        for gid, seq in px.items():
            p0, p1 = prev.get(gid), seq.get(d)
            if p0 and p1:
                rets.append((p1 / p0 - 1) * 100)
        if len(rets) >= 300:
            breadth[d] = 100.0 * sum(1 for x in rets if x > 0) / len(rets)
            mret[d] = st.mean(rets)
        prev = {gid: seq[d] for gid, seq in px.items() if d in seq}

    print(f"\n[1] 自建大盘指标（{len(breadth)} 天有效，{len(px)} 个品）")
    print("   最近 10 天:")
    for d in sorted(breadth)[-10:]:
        print(f"     {d}  上涨占比 {breadth[d]:>5.1f}%  横截面均值 {mret[d]:+6.2f}%")

    sig = c.execute("""select event_ts, fwd_24h, fwd_72h, fwd_168h
                       from signal_outcomes where fwd_168h is not null""").fetchall()
    joined = [(breadth[r["event_ts"][:10]], r) for r in sig if r["event_ts"][:10] in breadth]
    if not joined:
        print("\n没有信号能对上大盘代理。")
        return
    print(f"\n[2] 可对上的信号 {len(joined)} / {len(sig)}")

    med = st.median([b for b, _ in joined])
    print(f"\n[3] 按事件当日「上涨品占比」分组（分界 {med:.0f}%）")
    for lab, cond in ((f"弱市 <{med:.0f}%", lambda b: b < med),
                      (f"强市 >={med:.0f}%", lambda b: b >= med)):
        sub = [r for b, r in joined if cond(b)]
        for k, nm in (("fwd_24h", "24h"), ("fwd_72h", "72h"), ("fwd_168h", "7d")):
            v = [r[k] for r in sub if r[k] is not None]
            if v:
                print(f"   {lab:<12} {nm:<4} n={len(v):<4} 中位 {st.median(v):+6.2f}%  "
                      f"胜率 {100.0 * sum(1 for x in v if x > 0) / len(v):>5.1f}%")
        print()

    xs = [b for b, _ in joined]
    ys = [r["fwd_168h"] for _, r in joined]
    n = len(xs)
    mx, my = st.mean(xs), st.mean(ys)
    cov = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / n
    sx = math.sqrt(sum((x - mx) ** 2 for x in xs) / n)
    sy = math.sqrt(sum((y - my) ** 2 for y in ys) / n)
    print(f"[4] 上涨占比 vs 信号 7 日收益：相关系数 {cov / (sx * sy):+.3f}（n={n}）")
    print("    注意：信号按日聚集，独立观测只有约 26 天，只能当方向性提示。")
    c.close()


if __name__ == "__main__":
    main()
