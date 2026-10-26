# -*- coding: utf-8 -*-
"""模拟:补齐 rate_7/rate_30/spread_chg 后,A/S 跟踪路径的分数会怎样。

四种口径对比(同一批商品、同一历史):
  A) 现状:rate=0, spread=None          (批量查价真实情形)
  B) 只补真实 rate(从 price_snapshots 读)
  C) 只补 spread_chg(从跟踪历史算)
  D) 全补

输出各口径下分数中位数、以及达到保活线 38 / 促升线 52 的比例。
"""
import sqlite3
import statistics
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from bot.anomaly.engine import AnomalyEngine, MetricSnapshot  # noqa: E402
from bot.anomaly.runner import _history_points                 # noqa: E402
from bot.config import load_config                             # noqa: E402

DB = ROOT / "data" / "csqaq.db"


def main():
    cfg = load_config(str(ROOT / "config.yaml"))
    engine = AnomalyEngine(cfg.anomaly)
    c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    c.row_factory = sqlite3.Row

    items = c.execute(
        "select good_id, name, count(*) n from item_listing_history "
        "where ts >= datetime('now','-3 day') group by good_id "
        "having n >= 30 order by n desc limit 120"
    ).fetchall()

    buckets = {"A": [], "B": [], "C": [], "D": []}
    for it in items:
        gid = it["good_id"]
        rows = c.execute(
            "select * from item_listing_history where good_id=? order by ts desc limit 60", (gid,)
        ).fetchall()
        if len(rows) < 20:
            continue
        last = rows[0]
        series = [dict(r) for r in reversed(rows)]

        def n(v):
            return v if v is not None else 0

        sell_total = int(n(last["total_sell_num"])) if last["total_sell_scope"] == "buff_yyyp" \
            else int(n(last["buff_sell_num"]))
        buy_total = int(n(last["total_buy_num"])) if last["total_buy_scope"] == "buff_yyyp" \
            else int(n(last["buff_buy_num"]))
        buff_sell = int(n(last["buff_sell_num"]))
        base = dict(
            good_id=gid, name=it["name"],
            price=float(n(last["buff_sell_price"])),
            sell_price=float(n(last["buff_sell_price"])),
            buy_price=float(n(last["buff_buy_price"])),
            sell_num=sell_total, buy_num=buy_total,
            buy_num_observed=(last["total_buy_scope"] == "buff_yyyp"),
            yyyp_sell_num=max(0, sell_total - buff_sell),
            buff_yyyp_complete=(last["total_sell_scope"] == "buff_yyyp"),
        )

        # 真实 rate:取跟踪时间点之前最近一条带 rate 的快照
        snap_rate = c.execute(
            "select sell_price_rate_7, sell_price_rate_30 from price_snapshots "
            "where good_id=? and (sell_price_rate_7 <> 0 or sell_price_rate_30 <> 0) "
            "order by created_at desc limit 1", (gid,)).fetchone()
        rate7 = float(snap_rate["sell_price_rate_7"] or 0) if snap_rate else 0.0
        rate30 = float(snap_rate["sell_price_rate_30"] or 0) if snap_rate else 0.0

        # spread_chg:近 30 分钟「卖-买」价差变化
        spread_chg = None
        try:
            p_now, b_now = float(n(last["buff_sell_price"])), float(n(last["buff_buy_price"]))
            old = series[0]
            p_old, b_old = float(n(old["buff_sell_price"])), float(n(old["buff_buy_price"]))
            if p_now > 0 and p_old > 0 and b_now > 0 and b_old > 0:
                spread_chg = (p_now - b_now) - (p_old - b_old)
        except Exception:
            spread_chg = None

        pts = _history_points(series)
        variants = {
            "A": dict(rate_7=0.0, rate_30=0.0, spread_chg=None),
            "B": dict(rate_7=rate7, rate_30=rate30, spread_chg=None),
            "C": dict(rate_7=0.0, rate_30=0.0, spread_chg=spread_chg),
            "D": dict(rate_7=rate7, rate_30=rate30, spread_chg=spread_chg),
        }
        for k, extra in variants.items():
            try:
                snap = MetricSnapshot(**base, **extra)
                res = engine.evaluate(snap, pts)
                buckets[k].append(float(res.score))
            except Exception:
                continue

    print("=== 四种口径的分数分布(同一批商品) ===")
    print(f"  {'口径':<4} {'样本':>4} {'中位':>6} {'均值':>6} {'≥38':>6} {'≥52':>6} {'≥70':>6}")
    for k, label in (("A", "现状"), ("B", "+rate"), ("C", "+spread"), ("D", "全补")):
        v = [x for x in buckets[k] if x is not None]
        if not v:
            print(f"  {label:<4} 无样本")
            continue
        v_s = sorted(v)
        n = len(v)
        p38 = sum(1 for x in v if x >= 38) / n * 100
        p52 = sum(1 for x in v if x >= 52) / n * 100
        p70 = sum(1 for x in v if x >= 70) / n * 100
        print(f"  {label:<4} {n:>4} {statistics.median(v_s):>6.1f} {statistics.mean(v):>6.1f} "
              f"{p38:>5.0f}% {p52:>5.0f}% {p70:>5.0f}%")
    c.close()


if __name__ == "__main__":
    main()
