# -*- coding: utf-8 -*-
"""核对两平台价差的原始记录:差价巨大的是价格真实差异,还是字段污染。"""
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "data" / "csqaq.db"


def main():
    c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    c.row_factory = sqlite3.Row

    print("=== 差价 >50% 的样本(随机 12 条) ===")
    rows = c.execute(
        "select name, buff_sell_price b, yyyp_sell_price y, main_price, created_at "
        "from price_snapshots where buff_sell_price > 0 and yyyp_sell_price > 0 "
        "and created_at >= datetime('now','-3 day') "
        "and abs(buff_sell_price - yyyp_sell_price) / min(buff_sell_price, yyyp_sell_price) > 0.5 "
        "order by random() limit 12"
    ).fetchall()
    for r in rows:
        b, y = float(r["b"]), float(r["y"])
        print(f"  {r['name'][:34]:<36} BUFF ¥{b:<10.2f} 悠悠 ¥{y:<10.2f} main ¥{float(r['main_price'] or 0):<10.2f} "
              f"比 {max(b,y)/min(b,y):.1f}x")

    print("\n=== 差价 <10% 的样本(随机 6 条,作对照) ===")
    rows = c.execute(
        "select name, buff_sell_price b, yyyp_sell_price y, created_at "
        "from price_snapshots where buff_sell_price > 0 and yyyp_sell_price > 0 "
        "and created_at >= datetime('now','-3 day') "
        "and abs(buff_sell_price - yyyp_sell_price) / min(buff_sell_price, yyyp_sell_price) < 0.10 "
        "order by random() limit 6"
    ).fetchall()
    for r in rows:
        b, y = float(r["b"]), float(r["y"])
        print(f"  {r['name'][:34]:<36} BUFF ¥{b:<10.2f} 悠悠 ¥{y:<10.2f}")

    print("\n=== 极端值检查:悠悠价比 BUFF 低 80% 以上的条数与商品数 ===")
    r = c.execute(
        "select count(*) n, count(distinct good_id) g from price_snapshots "
        "where buff_sell_price > 0 and yyyp_sell_price > 0 "
        "and created_at >= datetime('now','-3 day') "
        "and yyyp_sell_price < buff_sell_price * 0.2"
    ).fetchone()
    print(f"  条数 {r['n']}  商品数 {r['g']}")

    print("\n=== 抖音/低价品常态:0-50 元区间里,两平台差 >50% 的占比 ===")
    r = c.execute(
        "select sum(abs(buff_sell_price - yyyp_sell_price)/min(buff_sell_price,yyyp_sell_price) > 0.5) n, count(*) t "
        "from price_snapshots where buff_sell_price > 0 and yyyp_sell_price > 0 "
        "and buff_sell_price < 50 and created_at >= datetime('now','-3 day')"
    ).fetchone()
    if r["t"]:
        print(f"  {r['n']}/{r['t']} = {r['n']/r['t']*100:.0f}%")
    c.close()


if __name__ == "__main__":
    main()
