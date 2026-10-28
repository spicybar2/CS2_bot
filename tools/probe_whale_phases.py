"""三个庄家阶段（建仓/拉升/出货）在求购+在售数据里的可辨别性与前瞻表现。

用「每个品自己的同视界基准」做配对对照，避免被品类/流动性差异污染。
"""
import bisect, sqlite3, statistics as st
from collections import defaultdict
from datetime import datetime

c = sqlite3.connect("file:D:/cs2-qq-bot/data/csqaq.db?mode=ro", uri=True)
c.row_factory = sqlite3.Row
rows = c.execute("""select good_id, ts, buff_sell_price, buff_sell_num, buff_buy_num
                    from item_listing_history where ts >= '2026-09-20'
                    and buff_sell_price > 0 order by good_id, ts""").fetchall()

seq = defaultdict(list)
for r in rows:
    seq[r["good_id"]].append((datetime.strptime(r["ts"], "%Y-%m-%d %H:%M:%S").timestamp(),
                              r["buff_sell_price"], r["buff_sell_num"], r["buff_buy_num"]))
print(f"{len(seq)} 个品、{len(rows)} 行")

H = {"24h": 86400, "72h": 259200, "7d": 604800}

# ---------- 每品的分视界基准（该品所有采样点里，n 小时后上涨的比例） ----------
base = {h: defaultdict(list) for h in H}
nxt = {}
for gid, items in seq.items():
    ts = [x[0] for x in items]
    nxt[gid] = {}
    for h, sec in H.items():
        for i in range(len(items)):
            tgt = ts[i] + sec
            j = bisect.bisect_left(ts, tgt)
            if j < len(items) and abs(ts[j] - tgt) <= sec * 0.5:
                nxt[gid].setdefault(h, {})[i] = j
                base[h][gid].append(1 if items[j][1] > items[i][1] else 0)

item_base = {h: {g: 100.0 * sum(v) / len(v) for g, v in d.items() if v}
             for h, d in base.items()}
print(f"基准覆盖品数: " + "  ".join(f"{h}={len(item_base[h])}" for h in H))

# ---------- 三阶段指纹 ----------
cls = {k: defaultdict(list) for k in
       ("建仓(求购+10%)", "拉升(在售-10%+价+1%)", "出货(在售+10%,价不动)", "砸盘出货(在售+10%+价跌)")}

for gid, items in seq.items():
    for i in range(len(items) - 1):
        a, b = items[i], items[i + 1]
        dt = b[0] - a[0]
        if dt <= 0 or dt > 240 * 60:
            continue
        s0, s1, b0, b1, p0, p1 = a[2], b[2], a[3], b[3], a[1], b[1]
        if not s0 or not s1 or not p0:
            continue
        ds = (s1 - s0) / s0 * 100
        db = ((b1 - b0) / b0 * 100) if (b0 and b1) else 0.0
        dp = (p1 - p0) / p0 * 100

        keys = []
        if db >= 10 and abs(ds) <= 5 and abs(dp) < 1:
            keys.append("建仓(求购+10%)")
        if ds <= -10 and dp >= 1:
            keys.append("拉升(在售-10%+价+1%)")
        if ds >= 10 and abs(dp) < 1:
            keys.append("出货(在售+10%,价不动)")
        if ds >= 10 and dp <= -1:
            keys.append("砸盘出货(在售+10%+价跌)")
        for k in keys:
            for h in H:
                j = nxt.get(gid, {}).get(h, {}).get(i + 1)
                if j is not None:
                    cls[k][h].append((gid, (items[j][1] / p1 - 1) * 100))

print("\n" + "=" * 96)
print("三阶段指纹的前瞻表现（每格：涨幅中位 / 上涨比例 / 相对该品自身基准）")
print("=" * 96)
for k, per_h in cls.items():
    print(f"\n{k}")
    for h in H:
        v = per_h[h]
        if not v:
            continue
        rets = [x[1] for x in v]
        ups = 100.0 * sum(1 for x in rets if x > 0) / len(rets)
        ex = [ups - item_base[h].get(g, ups) for g, _ in v]
        print(f"  +{h:<4} n={len(v):<6} 中位 {st.median(rets):+7.2f}%  "
              f"上涨 {ups:>5.1f}%  相对自基准 {st.median(ex):+6.1f}pp")
c.close()
