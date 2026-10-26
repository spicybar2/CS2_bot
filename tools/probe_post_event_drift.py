# -*- coding: utf-8 -*-
"""异动后的价格漂移:决定「报警后还值不值得高频盯几小时」。

对每条 score>=52 的事件,取事发时与 +1h/+6h/+24h 的 BUFF 最低在售价,
统计漂移分布 —— 若继续下跌,则「等一等再买」有价值,值得留在盯盘池;
若随机/反弹,则长期高频盯盘无边际价值。
"""
import sqlite3
import statistics
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "data" / "csqaq.db"


def parse(ts):
    for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(ts, fmt)
        except (ValueError, TypeError):
            continue
    return None


def price_at(c, gid, when):
    r = c.execute(
        "select buff_sell_price from price_snapshots where good_id=? and buff_sell_price > 0 "
        "and created_at <= ? order by created_at desc limit 1",
        (gid, when.strftime("%Y-%m-%d %H:%M:%S.%f"))).fetchone()
    return float(r["buff_sell_price"]) if r else None


def main():
    c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    events = c.execute(
        "select good_id, ts, score, price from anomaly_events "
        "where score >= 52 and good_id is not null and ts >= datetime('now','-20 day') "
        "order by ts desc limit 300"
    ).fetchall()

    horizons = {"1h": 1, "6h": 6, "24h": 24}
    res = {k: [] for k in horizons}
    for e in events:
        t = parse(e["ts"])
        if not t:
            continue
        p0 = price_at(c, e["good_id"], t)
        if not p0 or p0 <= 0:
            continue
        for label, h in horizons.items():
            p1 = price_at(c, e["good_id"], t + timedelta(hours=h))
            if p1 and p1 > 0:
                res[label].append((p1 - p0) / p0 * 100)

    print("=== 异动后价格漂移(相对事发时 BUFF 最低在售价) ===")
    print(f"  {'窗口':<5} {'样本':>4} {'中位':>7} {'均值':>7} {'下跌占比':>7} {'跌>3%':>7}")
    for label in horizons:
        v = res[label]
        if not v:
            print(f"  {label:<5} 无样本")
            continue
        v_s = sorted(v)
        med = statistics.median(v_s)
        mean = statistics.mean(v)
        down = sum(1 for x in v if x < 0) / len(v) * 100
        deep = sum(1 for x in v if x < -3) / len(v) * 100
        print(f"  {label:<5} {len(v):>4} {med:>6.1f}% {mean:>6.1f}% {down:>6.0f}% {deep:>6.0f}%")

    print("\n=== 对照:同期全体快照的价格漂移(基准) ===")
    base = c.execute(
        "select good_id, created_at, buff_sell_price from price_snapshots "
        "where buff_sell_price > 0 and created_at >= datetime('now','-5 day') "
        "order by random() limit 4000"
    ).fetchall()
    base_vals = {k: [] for k in horizons}
    for r in base:
        t = parse(r["created_at"])
        if not t:
            continue
        p0 = float(r["buff_sell_price"])
        for label, h in horizons.items():
            p1 = price_at(c, r["good_id"], t + timedelta(hours=h))
            if p1 and p1 > 0:
                base_vals[label].append((p1 - p0) / p0 * 100)
    for label in horizons:
        v = base_vals[label]
        if v:
            v_s = sorted(v)
            down = sum(1 for x in v if x < 0) / len(v) * 100
            print(f"  {label:<5} {len(v):>4} 中位 {statistics.median(v_s):+.1f}%  "
                  f"均值 {statistics.mean(v):+.1f}%  下跌占比 {down:.0f}%")
    c.close()


if __name__ == "__main__":
    main()
