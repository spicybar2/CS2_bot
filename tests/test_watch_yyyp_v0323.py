# -*- coding: utf-8 -*-
"""V0.3.23 离线自测:/关注 双平台价格(悠悠优先)+ 基准口径迁移。

覆盖:
  - watch_primary_price 优先级(悠悠 → BUFF → Steam)、缺失回退、全缺返回 (0, '')
  - camelCase / snake_case 两种字段名(接口回包 vs 内部快照)
  - watch_price_pair 同时给出两个价
  - /关注 回执、我的关注列表、提醒文案都列出两个价
  - DB:老库自动补 base_price_scope 列(默认 buff);新关注写 yyyp;update_watch_base 可改口径

用法: .venv\\Scripts\\python.exe -X utf8 test_watch_yyyp_v0323.py
"""
import pathlib as _pl, sys as _sys          # tests/_bootstrap:保证能 import bot
_sys.path.insert(0, str(_pl.Path(__file__).resolve().parent.parent))
import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from bot.csqaq.market_counts import (platform_sell_price, watch_price_pair,   # noqa: E402
                                     watch_primary_price)
from bot.qqbot import commands as cmds                                       # noqa: E402
from bot.storage.db import SnapshotStore                                      # noqa: E402

FAILS = []


def ok(name, cond, extra=""):
    if cond:
        print(f"  [PASS] {name}")
    else:
        print(f"  [FAIL] {name}  {extra}")
        FAILS.append(name)


def main():
    print("\n[1] 主用价:悠悠优先")
    row = {"buffSellPrice": 100.0, "yyypSellPrice": 88.0}
    price, scope = watch_primary_price(row)
    ok("取悠悠价", price == 88.0 and scope == "yyyp", (price, scope))

    print("\n[2] 悠悠缺失 → 回退 BUFF")
    price, scope = watch_primary_price({"buffSellPrice": 100.0, "yyypSellPrice": 0})
    ok("回退 BUFF", price == 100.0 and scope == "buff", (price, scope))
    price, scope = watch_primary_price({"buffSellPrice": 100.0})       # 字段都不存在
    ok("字段缺失也回退 BUFF", price == 100.0 and scope == "buff", (price, scope))

    print("\n[3] 两平台都缺 → Steam")
    price, scope = watch_primary_price({"steamSellPrice": 55.5})
    ok("回退 Steam", price == 55.5 and scope == "steam", (price, scope))

    print("\n[4] 全缺 → (0, '')")
    price, scope = watch_primary_price({"buffSellPrice": 0, "yyypSellPrice": 0})
    ok("无价返回空口径", price == 0.0 and scope == "", (price, scope))
    ok("None 也不炸", watch_primary_price(None) == (0.0, ""))

    print("\n[5] snake_case 字段(内部快照行)同样可用")
    price, scope = watch_primary_price({"buff_sell_price": 66.0, "yyyp_sell_price": 60.0})
    ok("snake_case 取悠悠", price == 60.0 and scope == "yyyp", (price, scope))

    print("\n[6] 双平台价格对")
    buff, yyyp = watch_price_pair({"buffSellPrice": 100.0, "yyypSellPrice": 88.0})
    ok("两个价都给", buff == 100.0 and yyyp == 88.0, (buff, yyyp))
    buff, yyyp = watch_price_pair({"buffSellPrice": 100.0})
    ok("缺悠悠记 0", buff == 100.0 and yyyp == 0.0, (buff, yyyp))
    ok("platform_sell_price 单取",
       platform_sell_price({"yyypSellPrice": 12.0}, "yyyp") == 12.0)

    print("\n[7] /关注 回执:两个价 + 平台标注")
    text = cmds.format_watch_added("蝴蝶刀", 88.0, None, None, "price", 0.0,
                                   buff_price=100.0, yyyp_price=88.0, scope="yyyp")
    ok("基准注明悠悠", "基准价 ¥88.00（悠悠有品）" in text, text)
    ok("列出 BUFF 价", "BUFF ¥100.00" in text, text)
    ok("列出悠悠价", "悠悠有品 ¥88.00" in text, text)

    print("\n[8] 我的关注列表:基准带平台标注")
    lst = cmds.format_watch_list([{"zh_name": "蝴蝶刀", "alert_type": "price", "param": 0,
                                   "base_price": 88.0, "base_price_scope": "yyyp"}])
    ok("列表标注悠悠", "基准 ¥88.00（悠悠有品）" in lst, lst)
    lst_old = cmds.format_watch_list([{"zh_name": "旧品", "alert_type": "price", "param": 0,
                                       "base_price": 50.0, "base_price_scope": "buff"}])
    ok("旧记录标注 BUFF", "基准 ¥50.00（BUFF）" in lst_old, lst_old)

    print("\n[9] 提醒文案:两个价都在")
    alert = cmds.format_watch_alert("蝴蝶刀", 90.0, 80.0, "跌幅 -11.1% 触发",
                                    buff_price=95.0, yyyp_price=80.0)
    ok("提醒含基准→现价", "基准价 ¥90.00 → 现价 ¥80.00" in alert, alert)
    ok("提醒含双平台", "BUFF ¥95.00" in alert and "悠悠有品 ¥80.00" in alert, alert)

    print("\n[10] 老库迁移:自动补 base_price_scope")
    with tempfile.TemporaryDirectory() as tmp:
        db_path = str(Path(tmp) / "old.db")
        # 造一个"老库":watchlist 没有 base_price_scope 列
        raw = sqlite3.connect(db_path)
        raw.execute("""CREATE TABLE watchlist (
            id INTEGER PRIMARY KEY AUTOINCREMENT, group_openid TEXT NOT NULL,
            user_openid TEXT NOT NULL, good_id INTEGER DEFAULT 0,
            market_hash_name TEXT NOT NULL, zh_name TEXT NOT NULL,
            base_price REAL NOT NULL, threshold REAL DEFAULT 10.0,
            target_price REAL, created_at TEXT NOT NULL, last_alert_at TEXT,
            last_sell_num_scope TEXT NOT NULL DEFAULT 'buff',
            UNIQUE(group_openid, user_openid, market_hash_name))""")
        raw.execute("""INSERT INTO watchlist (group_openid,user_openid,market_hash_name,
                       zh_name,base_price,created_at) VALUES ('g','u','mhn','蝴蝶刀',50.0,
                       '2026-10-20 10:00:00')""")
        raw.commit()
        raw.close()

        store = SnapshotStore(db_path)
        conn = sqlite3.connect(db_path)
        cols = [r[1] for r in conn.execute("PRAGMA table_info(watchlist)")]
        ok("补上了 base_price_scope", "base_price_scope" in cols, cols)
        old_scope = conn.execute("select base_price_scope from watchlist").fetchone()[0]
        ok("老记录默认 buff 口径", old_scope == "buff", old_scope)

        store.add_watch("g", "u2", "mhn2", "AK-47", 88.0, 10.0, 0,
                        base_price_scope="yyyp")
        new_scope = conn.execute(
            "select base_price_scope from watchlist where user_openid='u2'").fetchone()[0]
        ok("新关注写 yyyp 口径", new_scope == "yyyp", new_scope)

        wid = conn.execute("select id from watchlist where user_openid='u2'").fetchone()[0]
        store.update_watch_base(wid, 77.0, "buff")
        base, scope = conn.execute(
            "select base_price, base_price_scope from watchlist where id=?", (wid,)).fetchone()
        ok("update_watch_base 同时改价与口径", (base, scope) == (77.0, "buff"), (base, scope))
        conn.close()
        store.close() if hasattr(store, "close") else None

    print(f"\n  {len(FAILS)} 个失败" if FAILS else "\n  全部通过 ✓")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
