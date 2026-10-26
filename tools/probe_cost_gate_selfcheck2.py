# -*- coding: utf-8 -*-
"""自查修正:剔除机械相关,重测「折扣」的预测力;并找 30 日基准的正经数据源。"""
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


def corr(xs, ys):
    n = len(xs)
    mx, my = statistics.mean(xs), statistics.mean(ys)
    cov = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / n
    sx = (sum((x - mx) ** 2 for x in xs) / n) ** 0.5
    sy = (sum((y - my) ** 2 for y in ys) / n) ** 0.5
    return cov / (sx * sy) if sx and sy else 0.0


def main():
    c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    c.row_factory = sqlite3.Row

    print("=== 1) 30 日基准的数据源盘点 ===")
    for t in ("price_history", "goods_index", "item_multi_snapshot"):
        try:
            n = c.execute(f"select count(*) n from {t}").fetchone()["n"]
            cols = [r[1] for r in c.execute(f"pragma table_info({t})")]
            print(f"  {t}: {n} 行  列={cols[:8]}")
        except Exception as e:
            print(f"  {t}: {e}")

    print("\n=== 2) 折扣预测力:两种归一化对比 ===")
    events = c.execute(
        "select good_id, ts, price from anomaly_events where good_id is not null "
        "and price > 0 and score >= 52 "
        "and ts between datetime('now','-60 day') and datetime('now','-8 day') "
        "order by ts"
    ).fetchall()
    rows = []
    for e in events:
        t = parse(e["ts"])
        if not t:
            continue
        lo = (t - timedelta(days=30)).strftime("%Y-%m-%d %H:%M:%S.%f")
        hi = t.strftime("%Y-%m-%d %H:%M:%S.%f")
        base = c.execute(
            "select avg(buff_sell_price) av, count(*) n, count(distinct date(created_at)) d "
            "from price_snapshots where good_id=? and buff_sell_price > 0 "
            "and created_at between ? and ?", (e["good_id"], lo, hi)).fetchone()
        if not base or not base["av"] or base["n"] < 20 or base["d"] < 5:
            continue
        p0 = float(e["price"])
        av = float(base["av"])
        disc = (av - p0) / av * 100
        target = t + timedelta(days=7)
        fwd = c.execute(
            "select buff_sell_price p from price_snapshots where good_id=? "
            "and buff_sell_price > 0 and created_at between ? and ? "
            "order by created_at limit 1",
            (e["good_id"], (target - timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S.%f"),
             (target + timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S.%f"))).fetchone()
        if not fwd:
            continue
        p1 = float(fwd["p"])
        rows.append((disc, p0, av, p1))

    n = len(rows)
    print(f"  有效配对 {n} 条(要求基准点数≥20 且覆盖≥5 天)")
    if n >= 10:
        # (a) 旧口径:以 p0 为分母 —— 与折扣共用 p0,存在机械相关
        y_old = [(p1 * COST / p0 - 1) * 100 for _d, p0, _av, p1 in rows]
        x = [d for d, *_ in rows]
        print(f"  (a) 归一化到 p0(旧口径,机械相关): r = {corr(x, y_old):+.3f}")
        # (b) 中性口径:以基准价 av 为分母,消除 1/p0 的机械联系
        y_new = [(p1 * COST - p0) / av * 100 for _d, p0, av, p1 in rows]
        print(f"  (b) 归一化到基准价(消除机械相关): r = {corr(x, y_new):+.3f}")
        # (c) 相对表现:减去同期同批样本的中位,看横截面超额
        med = statistics.median(y_new)
        y_ex = [v - med for v in y_new]
        print(f"  (c) 横截面超额(减中位):        r = {corr(x, y_ex):+.3f}")
        pairs = sorted(zip(x, y_ex))
        k = max(3, n // 3)
        print(f"      折扣最高 1/3 超额中位 {statistics.median([p[1] for p in pairs[-k:]]):+.2f}%"
              f"  |  最低 1/3 超额中位 {statistics.median([p[1] for p in pairs[:k]]):+.2f}%")
    c.close()


if __name__ == "__main__":
    main()
