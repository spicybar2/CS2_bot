# -*- coding: utf-8 -*-
"""跟踪路径失败环节归因:逐件记录引擎返回的 rejected/score,统计各闸门拦截占比。"""
import sqlite3
import sys
from collections import Counter
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
        "select good_id, name from item_listing_history "
        "where ts >= datetime('now','-3 day') group by good_id "
        "having count(*) >= 20 order by count(*) desc limit 300"
    ).fetchall()

    reasons = Counter()
    scores = []
    buy_lt50 = 0
    total = 0
    for it in items:
        rows = c.execute(
            "select * from item_listing_history where good_id=? order by ts desc limit 60",
            (it["good_id"],)).fetchall()
        if len(rows) < 20:
            continue
        last = rows[0]
        total += 1

        def n(v):
            return v if v is not None else 0

        sell_total = int(n(last["total_sell_num"])) if last["total_sell_scope"] == "buff_yyyp" \
            else int(n(last["buff_sell_num"]))
        buy_total = int(n(last["total_buy_num"])) if last["total_buy_scope"] == "buff_yyyp" \
            else int(n(last["buff_buy_num"]))
        if buy_total < 50:
            buy_lt50 += 1
        snap = MetricSnapshot(
            good_id=it["good_id"], name=it["name"],
            price=float(n(last["buff_sell_price"])),
            sell_price=float(n(last["buff_sell_price"])),
            buy_price=float(n(last["buff_buy_price"])),
            sell_num=sell_total, buy_num=buy_total,
            buy_num_observed=(last["total_buy_scope"] == "buff_yyyp"),
            yyyp_sell_num=max(0, sell_total - int(n(last["buff_sell_num"]))),
            buff_yyyp_complete=(last["total_sell_scope"] == "buff_yyyp"),
        )
        pts = _history_points([dict(r) for r in reversed(rows)])
        res = engine.evaluate(snap, pts)
        scores.append(float(res.score))
        rej = (res.details or {}).get("rejected")
        reasons[rej or "无拦截"] += 1

    print(f"=== 跟踪路径逐件归因(样本 {total} 件) ===")
    for k, v in reasons.most_common():
        print(f"  {k:<22} {v:>4} 件  {v/max(1,total)*100:5.1f}%")
    print(f"\n  求购数 <50(min_anomaly_buy_num)的: {buy_lt50} 件 "
          f"({buy_lt50/max(1,total)*100:.0f}%)")
    if scores:
        nz = sum(1 for x in scores if x > 0)
        print(f"  分数 >0 的: {nz} 件 ({nz/len(scores)*100:.0f}%)")
    c.close()


if __name__ == "__main__":
    main()
