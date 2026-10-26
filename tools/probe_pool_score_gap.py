# -*- coding: utf-8 -*-
"""无网络复现 A/S 跟踪路径评分:直接读库里的跟踪快照 + 历史,复算 score。

用于回答:跟踪路径的 0 分,是「数据源缺字段」还是「我探针没带 carry」造成的。
"""
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from bot.anomaly.engine import AnomalyEngine, MetricSnapshot  # noqa: E402
from bot.anomaly.runner import _history_points        # noqa: E402
from bot.config import load_config                    # noqa: E402

DB = ROOT / "data" / "csqaq.db"


def main():
    cfg = load_config(str(ROOT / "config.yaml"))
    engine = AnomalyEngine(cfg.anomaly)
    c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    c.row_factory = sqlite3.Row

    # 近 2 小时跟踪最密的 3 件(这些就是跟踪路径实际在算的样本)
    picks = c.execute(
        "select good_id, name, count(*) n from item_listing_history "
        "where ts >= datetime('now','-2 hour') group by good_id order by n desc limit 3"
    ).fetchall()

    for p in picks:
        gid = p["good_id"]
        rows = c.execute(
            "select * from item_listing_history where good_id=? order by ts desc limit 40", (gid,)
        ).fetchall()
        last = rows[0]
        keys = last.keys()
        print(f"\n=== {p['name']} (gid={gid}, 历史 {p['n']} 点) ===")
        print(f"  最新跟踪行字段: {sorted(keys)}")

        scoped = (last["total_sell_scope"] or "") == "buff_yyyp"
        sell_total = (int(last["total_sell_num"] or 0) if scoped and last["total_sell_num"] is not None
                      else int(last["buff_sell_num"] or 0))
        buff_sell = int(last["buff_sell_num"] or 0)
        buy_scoped = (last["total_buy_scope"] or "") == "buff_yyyp"
        buy_total = (int(last["total_buy_num"] or 0) if buy_scoped and last["total_buy_num"] is not None
                     else int(last["buff_buy_num"] or 0))
        snap = MetricSnapshot(
            good_id=gid,
            name=p["name"],
            price=float(last["buff_sell_price"] or 0),
            sell_price=float(last["buff_sell_price"] or 0),
            buy_price=float(last["buff_buy_price"] or 0),
            sell_num=sell_total,
            buy_num=buy_total,
            buy_num_observed=buy_scoped,
            yyyp_sell_num=(sell_total - buff_sell) if scoped and sell_total >= buff_sell else None,
            buff_yyyp_complete=scoped,
            rate_7=0.0, rate_30=0.0, spread_chg=None,
        )
        series = [dict(r) for r in reversed(rows)]
        pts = _history_points(series)
        print(f"  快照: price={snap.price} sell={snap.sell_num} buy={snap.buy_num} rate7={snap.rate_7}")
        print(f"  历史点: {len(pts)}")
        res = engine.evaluate(snap, pts)
        print(f"  >>> score={res.score} level={getattr(res,'level','')}")
        print(f"      details={ {k: v for k, v in (res.details or {}).items() if v} }")

        # 对照:如果 rate_7/30 有真实值(比如 12%),分数会不会变
        snap2 = MetricSnapshot(**{**snap.__dict__, "rate_7": 12.0, "rate_30": 18.0,
                                  "spread_chg": 1.5})
        res2 = engine.evaluate(snap2, pts)
        print(f"  >>> 假设 rate7=12% rate30=18% 时 score={res2.score} "
              f"details={ {k: v for k, v in (res2.details or {}).items() if v} }")
    c.close()


if __name__ == "__main__":
    main()
