# -*- coding: utf-8 -*-
"""信号择时能力检验：绝对收益 / 同品基准 / 配对超额。

只读 data/csqaq.db，不影响运行中的实例。

    .venv\\Scripts\\python.exe tools\\probe_signal_edge.py

口径：
- 信号收益取 signal_outcomes.fwd_24h / fwd_72h / fwd_168h（已按 buff_sell_price 结算）
- 基准取同一批品、同一时间窗内「任意一天买入」的等视界收益（item_listing_history 日线）
- 配对超额 = 信号收益 − 该品自己的日均收益（消掉选品与大盘的影响，只留择时）
"""

from __future__ import annotations

import sqlite3
import statistics as st
from collections import defaultdict
from pathlib import Path

DB = Path(__file__).resolve().parent.parent / "data" / "csqaQ.db"
DB = DB if DB.exists() else Path(__file__).resolve().parent.parent / "data" / "csqaq.db"

COLS = {1: "fwd_24h", 3: "fwd_72h", 7: "fwd_168h"}
SINCE = "2026-08-31"


def fmt(n: int, mean: float, med: float, win: float) -> str:
    return f"n={n:<5} 均值 {mean:+6.2f}%  中位 {med:+6.2f}%  胜率 {win:>5.1f}%"


def main() -> None:
    c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    c.row_factory = sqlite3.Row

    print("=" * 78)
    print("信号择时能力检验")
    print("=" * 78)

    rows = c.execute("select fwd_24h,fwd_72h,fwd_168h from signal_outcomes").fetchall()
    print(f"\n[1] 信号绝对收益（已结算 n={len(rows)}）")
    for k, col in ((0, "24 小时"), (1, "72 小时"), (2, "7 天")):
        v = [r[k] for r in rows if r[k] is not None]
        if v:
            print("   " + col.ljust(8) + fmt(len(v), st.mean(v), st.median(v),
                                             100.0 * sum(1 for x in v if x > 0) / len(v)))

    sig = defaultdict(list)
    for r in c.execute("select good_id, fwd_24h, fwd_72h, fwd_168h from signal_outcomes"):
        sig[r["good_id"]].append(r)
    print(f"\n   信号涉及 {len(sig)} 个品")

    base = {1: [], 3: [], 7: []}
    paired = {1: [], 3: [], 7: []}
    used = 0
    for gid, evs in sig.items():
        seq = c.execute("""
            select d, p from (
                select substr(ts,1,10) d, buff_sell_price p, ts,
                       row_number() over (partition by substr(ts,1,10) order by ts desc) rn
                from item_listing_history
                where good_id=? and buff_sell_price > 0 and ts >= ?)
            where rn=1 order by d""", (gid, SINCE)).fetchall()
        if len(seq) < 8:
            continue
        used += 1
        px = [r["p"] for r in seq]
        for k in (1, 3, 7):
            rets = [(px[i + k] / px[i] - 1) * 100 for i in range(len(px) - k) if px[i] > 0]
            if not rets:
                continue
            base[k].extend(rets)
            own = [e[COLS[k]] for e in evs if e[COLS[k]] is not None]
            if own:
                m = st.mean(rets)
                paired[k].extend(v - m for v in own)

    print(f"\n[2] 同品基准收益（{used} 个品、{SINCE} 起、任意一天买入）")
    for k in (1, 3, 7):
        v = base[k]
        if v:
            print("   " + f"{k} 日".ljust(8) + fmt(len(v), st.mean(v), st.median(v),
                                                  100.0 * sum(1 for x in v if x > 0) / len(v)))

    print("\n[3] 配对超额（信号 − 同品日均）—— 择时能力")
    for k in (1, 3, 7):
        v = paired[k]
        if v:
            print("   " + f"{k} 日".ljust(8) +
                  f"n={len(v):<5} 超额均值 {st.mean(v):+6.2f}pp  中位 {st.median(v):+6.2f}pp  "
                  f"跑赢比例 {100.0 * sum(1 for x in v if x > 0) / len(v):>5.1f}%")

    print("\n[4] 7 日收益门槛（往返成本约 4~6%）")
    v = [r[2] for r in rows if r[2] is not None]
    for th in (0, 2, 4, 6, 10):
        print(f"   >{th:>2}%: {100.0 * sum(1 for x in v if x > th) / len(v):>5.1f}%"
              f"  ({sum(1 for x in v if x > th)} 条)")

    print("\n[5] 分维度")
    for label, sql in (
        ("按类型", "select anomaly_type k, count(*) n, avg(fwd_24h) a, avg(fwd_72h) b,"
                   " avg(fwd_168h) d from signal_outcomes where fwd_168h is not null"
                   " group by 1 order by n desc"),
        ("按分数", "select case when score>=80 then 'L4 80+' when score>=65 then 'L3 65-79'"
                   " else 'L2 55-64' end k, count(*) n, avg(fwd_24h) a, avg(fwd_72h) b,"
                   " avg(fwd_168h) d from signal_outcomes where fwd_168h is not null"
                   " group by 1"),
    ):
        print(f"\n   {label}")
        for r in c.execute(sql):
            print(f"     {r['k']:<12} n={r['n']:<4} 24h {r['a']:+6.2f}%  "
                  f"72h {r['b']:+6.2f}%  7d {r['d']:+6.2f}%")

    c.close()


if __name__ == "__main__":
    main()
