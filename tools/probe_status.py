# -*- coding: utf-8 -*-
"""机器人状态快照:数据库各表规模 + 表结构自适应(不硬编码列名)。只读。"""
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent       # tools/ 的上一级才是仓库根
DB = ROOT / "data" / "csqaq.db"


def cols(c, table):
    return [r[1] for r in c.execute(f"pragma table_info({table})")]


def pick(available, *cands):
    for name in cands:
        if name in available:
            return name
    return None


def main():
    c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    print(f"  DB 大小: {DB.stat().st_size / 1024 / 1024:.0f} MB")

    tables = [r[0] for r in c.execute(
        "select name from sqlite_master where type='table' order by name")]
    print("  表:", ", ".join(tables))

    print("\n  === 各表行数 ===")
    for t in tables:
        try:
            n = c.execute(f"select count(*) from {t}").fetchone()[0]
            print(f"    {t:<26} {n:>9}")
        except Exception as exc:
            print(f"    {t:<26} err {exc}")

    print("\n  === 关注池 ===")
    wp = cols(c, "watch_pool")
    pool_col = pick(wp, "pool", "pool_name")
    if pool_col:
        for r in c.execute(f"select {pool_col} p, count(*) n from watch_pool group by p order by p"):
            print(f"    {r['p']}: {r['n']} 件")
    name_col = pick(wp, "name", "item_name", "market_hash_name", "goods_name")
    lvl_col = pick(wp, "level", "score_level")
    time_col = pick(wp, "updated_at", "update_time", "last_seen", "ts")
    if name_col and lvl_col and time_col:
        print("    最近更新(高等级):")
        rows = c.execute(
            f"select {name_col} n, {lvl_col} l, {time_col} t from watch_pool "
            f"order by {time_col} desc limit 5")
        for r in rows:
            print(f"      {str(r['n'])[:30]:<32} L{r['l']}  {r['t']}")

    print("\n  === 快照表 ===")
    ps = cols(c, "price_snapshots")
    tcol = pick(ps, "ts", "snapshot_ts", "created_at", "time")
    if tcol:
        r = c.execute(f"select count(*) n, min({tcol}) a, max({tcol}) b from price_snapshots").fetchone()
        print(f"    price_snapshots: {r['n']} 行   {r['a']}  ~  {r['b']}")

    print("\n  === 情绪归档 ===")
    if "market_sentiment" in tables:
        ms = cols(c, "market_sentiment")
        dcol = pick(ms, "day", "date", "trade_date")
        icol = pick(ms, "index_json", "index_value", "index", "heat_index")
        print("    列:", ", ".join(ms))
        if dcol:
            rows = c.execute(
                f"select * from market_sentiment order by {dcol} desc limit 4").fetchall()
            for r in rows:
                v = r[icol] if icol else None
                print(f"      {r[dcol]}  {str(v)[:60] if v is not None else '(无指数)'}")
    c.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
