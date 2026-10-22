# -*- coding: utf-8 -*-
"""导出需要跨机同步的状态表(watchlist + push_groups)为 JSON。

双机轮换维护用：公司/家里两台电脑轮流跑机器人时，
用本脚本把「盯盘列表 + 已记录推送群」导出为一个小 JSON 文件，
拷到另一台机器后用 import_state.py 导入，保证两端盯盘数据一致。

用法: python scripts/export_state.py [--out 输出路径]
默认输出: data/state_export.json
只读操作，不修改数据库，可在机器人运行中执行。
"""
import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_OUT = os.path.join(BASE_DIR, "data", "state_export.json")


def main():
    ap = argparse.ArgumentParser(description="导出 watchlist + push_groups 为 JSON")
    ap.add_argument("--out", default=DEFAULT_OUT, help="输出 JSON 路径")
    args = ap.parse_args()

    db_path = os.path.join(BASE_DIR, "data", "csqaq.db")
    if not os.path.exists(db_path):
        print(f"[export] 数据库不存在: {db_path}")
        sys.exit(1)

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        watches = [dict(r) for r in conn.execute(
            "SELECT * FROM watchlist ORDER BY id")]
        groups = [r[0] for r in conn.execute(
            "SELECT group_openid FROM push_groups ORDER BY created_at")]
    finally:
        conn.close()

    payload = {
        "version": 1,
        "exported_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "source": os.environ.get("COMPUTERNAME", "unknown"),
        "watchlist": watches,
        "push_groups": groups,
    }
    out_dir = os.path.dirname(args.out)
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)

    print(f"[export] 完成: {len(watches)} 条盯盘, {len(groups)} 个群")
    print(f"[export] 输出: {args.out} ({os.path.getsize(args.out)} bytes)")
    print("[export] 将该文件拷贝到另一台机器, 再运行 import_state.py 导入")


if __name__ == "__main__":
    main()
