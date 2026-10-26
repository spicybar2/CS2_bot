# -*- coding: utf-8 -*-
"""观察模式自查:三个可疑点用数据验证。

1) 30 日中位基准在下跌市里会不会系统性制造假折扣(市场漂移)
2) 基准可靠性能否达标(点数够不够、天数够不够)
3) 「折扣」到底能不能预测未来收益(相关系数/分位数)—— 这是门槛的立论基础
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


def main():
    c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    c.row_factory = sqlite3.Row

    # ---------- 1) 市场漂移 ----------
    print("=== 1) 近 30 天市场漂移(决定基准会不会系统性偏高) ===")
    rows = c.execute(
        "select good_id, min(created_at) t0, max(created_at) t1 from price_snapshots "
        "where buff_sell_price > 0 and created_at >= datetime('now','-30 day') "
        "group by good_id having count(*) >= 20 limit 800"
    ).fetchall()
    rets = []
    for r in rows:
        p0 = c.execute("select buff_sell_price p from price_snapshots where good_id=? "
                       "and buff_sell_price > 0 order by created_at asc limit 1",
                       (r["good_id"],)).fetchone()
        p1 = c.execute("select buff_sell_price p from price_snapshots where good_id=? "
                       "and buff_sell_price > 0 and created_at >= datetime('now','-1 day') "
                       "order by created_at desc limit 1", (r["good_id"],)).fetchone()
        if p0 and p1 and float(p0["p"]) > 0:
            rets.append((float(p1["p"]) / float(p0["p"]) - 1) * 100)
    if rets:
        rets.sort()
        n = len(rets)
        print(f"  样本 {n} 件: 近 30 天价格变化 中位 {statistics.median(rets):+.2f}%  "
              f"均值 {statistics.mean(rets):+.2f}%")
        print(f"  下跌商品占比: {sum(1 for x in rets if x < 0)/n*100:.0f}%  "
              f"跌超 5%: {sum(1 for x in rets if x < -5)/n*100:.0f}%")
        print("  → 若中位为负,说明「相对 30 日中位」天然偏高,会制造假折扣")

    # ---------- 2) 基准可靠性 ----------
    print("\n=== 2) 基准可靠性(点数 / 天数) ===")
    rows = c.execute(
        "select good_id, count(*) n, count(distinct date(created_at)) d "
        "from price_snapshots where buff_sell_price > 0 "
        "and created_at >= datetime('now','-30 day') "
        "and good_id in (select good_id from watch_pool) group by good_id"
    ).fetchall()
    if rows:
        n_ok = sum(1 for r in rows if r["n"] >= 20)
        d_ok = sum(1 for r in rows if r["n"] >= 20 and r["d"] >= 5)
        print(f"  池内商品 {len(rows)} 件: 点数≥20 的 {n_ok} 件 ({n_ok/len(rows)*100:.0f}%)")
        print(f"  点数≥20 且 天数≥5 的 {d_ok} 件 ({d_ok/len(rows)*100:.0f}%)")
        ds = sorted(r["d"] for r in rows)
        print(f"  覆盖天数中位 {ds[len(ds)//2]} 天  P10 {ds[int(len(ds)*0.1)]} 天")

    # ---------- 3) 折扣的预测力 ----------
    print("\n=== 3) 「折扣」能否预测 +7d 净收益(门槛的立论基础) ===")
    events = c.execute(
        "select good_id, ts, price, score from anomaly_events "
        "where good_id is not null and price > 0 and score >= 52 "
        "and ts between datetime('now','-60 day') and datetime('now','-8 day') "
        "order by ts"
    ).fetchall()
    pairs = []
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
        disc = (float(base["av"]) - p0) / float(base["av"]) * 100
        # 未来 7 天卖价(±1 天内)
        target = t + timedelta(days=7)
        lo2 = (target - timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S.%f")
        hi2 = (target + timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S.%f")
        fwd = c.execute(
            "select buff_sell_price p from price_snapshots where good_id=? "
            "and buff_sell_price > 0 and created_at between ? and ? "
            "order by created_at limit 1", (e["good_id"], lo2, hi2)).fetchone()
        if not fwd:
            continue
        net = (float(fwd["p"]) * COST / p0 - 1) * 100
        pairs.append((disc, net))

    if len(pairs) < 10:
        print(f"  有效配对仅 {len(pairs)} 条,样本不足")
    else:
        n = len(pairs)
        xs = [p[0] for p in pairs]
        ys = [p[1] for p in pairs]
        mx, my = statistics.mean(xs), statistics.mean(ys)
        cov = sum((x - mx) * (y - my) for x, y in pairs) / n
        sx = (sum((x - mx) ** 2 for x in xs) / n) ** 0.5
        sy = (sum((y - my) ** 2 for y in ys) / n) ** 0.5
        corr = cov / (sx * sy) if sx and sy else 0
        print(f"  样本 {n} 条: 折扣与 +7d 净收益 相关系数 r = {corr:+.3f}")
        # 分位数对比: 折扣最高的 1/3 vs 最低的 1/3
        pairs.sort()
        k = max(3, n // 3)
        low, high = pairs[:k], pairs[-k:]
        print(f"  折扣最低 1/3(中位 {statistics.median([p[0] for p in low]):+.1f}%): "
              f"后续净收益中位 {statistics.median([p[1] for p in low]):+.2f}%")
        print(f"  折扣最高 1/3(中位 {statistics.median([p[0] for p in high]):+.1f}%): "
              f"后续净收益中位 {statistics.median([p[1] for p in high]):+.2f}%")
        print("  (若高折扣组并未更好,说明用折扣做门槛没有依据)")
    c.close()


if __name__ == "__main__":
    main()
