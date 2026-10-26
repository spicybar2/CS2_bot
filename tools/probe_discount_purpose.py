# -*- coding: utf-8 -*-
"""折扣的职责检验:高折扣是否真的提高「回本概率」。

回本概率 = 持有 7 天后卖价能覆盖 2.5%手续费+1%提现 的比例。
对比 7 日基准 / 30 日基准 两种折扣口径。
"""
import sqlite3
import statistics
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "data" / "csqaq.db"
COST = (1 - 0.025) * (1 - 0.01)


def parse(ts):
    for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(ts, fmt)
        except (ValueError, TypeError):
            continue
    return None


def stock_stats(c, gid, t, days):
    lo = (t - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S.%f")
    hi = t.strftime("%Y-%m-%d %H:%M:%S.%f")
    r = c.execute(
        "select avg(buff_sell_price) av, count(*) n, count(distinct date(created_at)) d "
        "from price_snapshots where good_id=? and buff_sell_price > 0 "
        "and created_at between ? and ?", (gid, lo, hi)).fetchone()
    if not r or not r["av"]:
        return None
    return float(r["av"]), r["n"], r["d"]


def main():
    c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    events = c.execute(
        "select good_id, ts, price from anomaly_events where good_id is not null "
        "and price > 0 and score >= 52 "
        "and ts between datetime('now','-60 day') and datetime('now','-8 day')"
    ).fetchall()

    for label, days, min_n, min_d in (("7日基准", 7, 10, 3), ("30日基准", 30, 20, 5)):
        rows = []
        for e in events:
            t = parse(e["ts"])
            if not t:
                continue
            st = stock_stats(c, e["good_id"], t, days)
            if not st:
                continue
            av, n, d = st
            if n < min_n or d < min_d:
                continue
            p0 = float(e["price"])
            target = t + timedelta(days=7)
            fwd = c.execute(
                "select buff_sell_price p from price_snapshots where good_id=? "
                "and buff_sell_price > 0 and created_at between ? and ? "
                "order by created_at limit 1",
                (e["good_id"], (target - timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S.%f"),
                 (target + timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S.%f"))).fetchone()
            if not fwd:
                continue
            ret = (float(fwd["p"]) * COST / p0 - 1) * 100
            rows.append({"disc": (av - p0) / av * 100, "ret": ret})

        print(f"\n=== {label}（≥{min_n}点 且 ≥{min_d}天） 有效 {len(rows)} 条 ===")
        if len(rows) < 12:
            print("  样本不足")
            continue
        rows.sort(key=lambda r: r["disc"])
        k = max(4, len(rows) // 3)
        for name, g in (("低折扣 1/3", rows[:k]), ("中段 1/3", rows[k:len(rows) - k]), ("高折扣 1/3", rows[-k:])):
            if not g:
                continue
            med = statistics.median([r["ret"] for r in g])
            win = sum(1 for r in g if r["ret"] > 0) / len(g) * 100
            print(f"  {name:<10} n={len(g):>3}  折扣中位 {statistics.median([r['disc'] for r in g]):+6.1f}%  "
                  f"收益中位 {med:+6.2f}%  回本占比 {win:5.1f}%")

    # 全样本基线:不挑折扣,买入后回本概率
    print("\n=== 对照:不设任何门槛时,异动买入持有 7 天的回本概率 ===")
    rets = []
    for e in events:
        t = parse(e["ts"])
        if not t:
            continue
        p0 = float(e["price"])
        target = t + timedelta(days=7)
        fwd = c.execute(
            "select buff_sell_price p from price_snapshots where good_id=? "
            "and buff_sell_price > 0 and created_at between ? and ? order by created_at limit 1",
            (e["good_id"], (target - timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S.%f"),
             (target + timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S.%f"))).fetchone()
        if fwd:
            rets.append((float(fwd["p"]) * COST / p0 - 1) * 100)
    if rets:
        rets.sort()
        print(f"  n={len(rets)}  回本(>0)占比 {sum(1 for x in rets if x>0)/len(rets)*100:.1f}%  "
              f"中位 {statistics.median(rets):+.2f}%  P90 {rets[int(len(rets)*0.9)]:+.2f}%")
    c.close()


if __name__ == "__main__":
    main()
