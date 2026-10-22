# -*- coding: utf-8 -*-
"""从导出的 JSON 全量替换本地 watchlist + push_groups（防两端分叉）。

双机轮换维护用：把另一台机器导出的 state_export.json 导入本机，
用导出文件里的盯盘/群记录「全量覆盖」本机这两张表，保证两端一致。
注意：price_snapshots(历史快照) 与 goods_index(商品索引) 不在此列，
前者可重新积累, 后者可用 scripts/build_goods_index.py 重建。

用法: python scripts/import_state.py [--in 导入路径]
默认读取: data/state_export.json
安全要求: 请先停止机器人进程再执行本脚本(避免写库冲突)。
"""
import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_IN = os.path.join(BASE_DIR, "data", "state_export.json")

# 与 bot/storage/db.py 的 _SCHEMA 保持一致的最小建表语句
_TABLES = """
CREATE TABLE IF NOT EXISTS push_groups (
    group_openid TEXT PRIMARY KEY,
    created_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);
CREATE TABLE IF NOT EXISTS watchlist (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    group_openid TEXT NOT NULL,
    user_openid TEXT NOT NULL,
    good_id INTEGER DEFAULT 0,
    market_hash_name TEXT NOT NULL,
    zh_name TEXT NOT NULL,
    base_price REAL NOT NULL,
    threshold REAL DEFAULT 10.0,
    target_price REAL,
    created_at TEXT NOT NULL,
    last_alert_at TEXT,
    UNIQUE(group_openid, user_openid, market_hash_name)
);
CREATE INDEX IF NOT EXISTS idx_watch_group_user ON watchlist (group_openid, user_openid);
"""

_REQUIRED = {"group_openid", "user_openid", "market_hash_name", "zh_name", "base_price"}


def main():
    ap = argparse.ArgumentParser(description="导入 watchlist + push_groups(全量替换)")
    ap.add_argument("--in", dest="in_path", default=DEFAULT_IN, help="导出的 JSON 路径")
    args = ap.parse_args()

    if not os.path.exists(args.in_path):
        print(f"[import] 未找到导出文件: {args.in_path}")
        print("[import] 请先在其他机器上运行 export_state.py 并拷贝产物过来")
        sys.exit(1)

    with open(args.in_path, encoding="utf-8") as f:
        payload = json.load(f)
    watches = payload.get("watchlist", [])
    groups = payload.get("push_groups", [])

    # 字段完整性校验, 避免导入脏数据
    for i, w in enumerate(watches):
        missing = _REQUIRED - set(w)
        if missing:
            print(f"[import] watchlist[{i}] 缺少字段 {missing}, 中止导入")
            sys.exit(1)

    db_path = os.path.join(BASE_DIR, "data", "csqaq.db")
    conn = sqlite3.connect(db_path, timeout=30)
    try:
        conn.executescript(_TABLES)
        # 老库补充 target_price 列(与 db.py 迁移逻辑一致)
        cols = [r[1] for r in conn.execute("PRAGMA table_info(watchlist)")]
        if "target_price" not in cols:
            conn.execute("ALTER TABLE watchlist ADD COLUMN target_price REAL")

        # 全量替换, 防止两端 watchlist 分叉
        conn.execute("DELETE FROM watchlist")
        conn.execute("DELETE FROM push_groups")
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        for w in watches:
            conn.execute(
                """INSERT INTO watchlist
                   (id, group_openid, user_openid, good_id, market_hash_name,
                    zh_name, base_price, threshold, target_price, created_at, last_alert_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (w["id"], w["group_openid"], w["user_openid"], w.get("good_id", 0),
                 w["market_hash_name"], w["zh_name"], w["base_price"],
                 w.get("threshold", 10.0), w.get("target_price"),
                 w.get("created_at") or now, w.get("last_alert_at")),
            )
        for g in groups:
            conn.execute("INSERT OR IGNORE INTO push_groups (group_openid) VALUES (?)", (g,))
        # 同步自增序列, 保证后续新增盯盘 id 不冲突
        conn.execute(
            "UPDATE sqlite_sequence SET seq=(SELECT MAX(id) FROM watchlist) WHERE name='watchlist'")
        conn.commit()
    finally:
        conn.close()

    print(f"[import] 完成: {len(watches)} 条盯盘, {len(groups)} 个群")
    print(f"[import] 来源: {payload.get('source', '?')} @ {payload.get('exported_at', '?')}")
    print("[import] 请启动机器人验证 /我的关注 与 /状态")


if __name__ == "__main__":
    main()
