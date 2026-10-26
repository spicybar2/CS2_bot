# -*- coding: utf-8 -*-
"""自查修正 2:按价格带分层,检验「高折扣组是否真的涨得更多」。

理由:折扣与收益都以 p0 为分母,价格跨度从 ¥0.02 到 ¥50000,任何全样本相关系数
都会被廉价品放大。只有在同一价格带内比较,才有可比性。
"""
import sqlite3
import statistics
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "data" / "csqaq.db"
COST = (1 - 0.025) * (1 - 0.01)
BANDS = [(0, 50), (50, 200), (200, 1000), (1000, 5000), (5000, 10**9)]


def parse(ts):
    for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(ts, fmt)
        except (ValueError, TypeError):
            continue
    return None


def band_of(p):
    for lo, hi in BANDS:
        if lo <= p < hi:
            return f"¥{lo}-{hi if hi < 10**9 else '+'}"
    return "?"


def main():
    c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    events = c.execute(
        "select good_id, ts, price from anomaly_events where good_id is not null "
        "and price > 0 and score >= 52 "
        "and ts between datetime('now','-60 day') and datetime('now','-8 day')"
    ).fetchall()

    rows = []
    for e in events:
        t = parse(e["ts"])
        if not t:
            continue
        p0 = float(e["price"])
        lo = (t - timedelta(days=30)).strftime("%Y-%m-%d %H:%M:%S.%f")
        base = c.execute(
            "select avg(buff_sell_price) av, count(*) n, count(distinct date(created_at)) d "
            "from price_snapshots where good_id=? and buff_sell_price > 0 "
            "and created_at between ? and ?", (e["good_id"], lo, t.strftime("%Y-%m-%d %H:%M:%S.%f"))).fetchone()
        if not base or not base["av"] or base["n"] < 20 or base["d"] < 5:
            continue
        av = float(base["av"])
        target = t + timedelta(days=7)
        fwd = c.execute(
            "select buff_sell_price p from price_snapshots where good_id=? and buff_sell_price > 0 "
            "and created_at between ? and ? order by created_at limit 1",
            (e["good_id"], (target - timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S.%f"),
             (target + timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S.%f"))).fetchone()
        if not fwd:
            continue
        p1 = float(fwd["p"])
        rows.append({
            "band": band_of(p0),
            "disc": (av - p0) / av * 100,
            "ret": (p1 * COST / p0 - 1) * 100,
        })

    print(f"=== 有效配对 {len(rows)} 条(基准点数≥20 且覆盖≥5 天) ===")
    bands = [f"¥{lo}-{hi if hi < 10**9 else '+'}" for lo, hi in BANDS]
    for b in bands:
        sub = [r for r in rows if r["band"] == b]
        if len(sub) < 12:
            if sub:
                print(f"  {b:<12} n={len(sub):>3}  样本不足,跳过")
            continue
        sub.sort(key=lambda r: r["disc"])
        k = max(3, len(sub) // 3)
        lo_g, hi_g = sub[:k], sub[-k:]
        print(f"  {b:<12} n={len(sub):>3}  "
              f"低折扣组(中位{statistics.median([r['disc'] for r in lo_g]):+5.1f}%) 收益中位 "
              f"{statistics.median([r['ret'] for r in lo_g]):+6.2f}%  |  "
              f"高折扣组(中位{statistics.median([r['disc'] for r in hi_g]):+5.1f}%) 收益中位 "
              f"{statistics.median([r['ret'] for r in hi_g]):+6.2f}%")

    # 只在可交易价位(≥¥50)做一次合并检验
    tradeable = [r for r in rows if float(r["band"].split("-")[0].replace("¥", "")) >= 50]
    if len(tradeable) >= 20:
        tradeable.sort(key=lambda r: r["disc"])
        k = max(3, len(tradeable) // 3)
        lo_g, hi_g = tradeable[:k], tradeable[-k:]
        print(f"\n  【≥¥50 合并】n={len(tradeable)}  "
              f"低折扣组收益中位 {statistics.median([r['ret'] for r in lo_g]):+6.2f}%  vs  "
              f"高折扣组 {statistics.median([r['ret'] for r in hi_g]):+6.2f}%")
        hi_win = sum(1 for r in hi_g if r["ret"] > 0) / len(hi_g) * 100
        lo_win = sum(1 for r in lo_g if r["ret"] > 0) / len(lo_g) * 100
        print(f"  盈利占比: 高折扣 {hi_win:.0f}%  低折扣 {lo_win:.0f}%")
    print("\n  (只有当高折扣组在带内仍明显更好,「折扣」才配当门槛)")
    c.close()


if __name__ == "__main__":
    main()
