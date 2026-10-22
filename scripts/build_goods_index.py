# -*- coding: utf-8 -*-
"""拉取 CSQAQ 全量饰品索引(goods_index 表),供"词典未收录"时按中文名兜底。

用法: python scripts/build_goods_index.py
约 250 页 * 1 秒/页 ≈ 4-5 分钟。可重复执行(按 good_id 去重更新)。
"""
import asyncio
import os
import sys

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE_DIR)

from bot.config import load_config
from bot.csqaq.client import CSQAQClient
from bot.storage.db import SnapshotStore


async def main():
    cfg = load_config(os.path.join(BASE_DIR, "config.yaml"))
    store = SnapshotStore(os.path.join(BASE_DIR, "data", "csqaq.db"))
    seen: set = set()
    async with CSQAQClient(cfg.csqaq) as api:
        for page in range(1, 400):
            try:
                items = await api.get_rank_list(page_index=page, page_size=100)
            except Exception as exc:
                print(f"page {page} 失败(停止): {str(exc)[:100]}")
                break
            if not items:
                print(f"page {page} 为空,结束")
                break
            rows = []
            for it in items:
                gid = getattr(it, "good_id", None) or getattr(it, "id", None)
                if gid is None or gid in seen:
                    continue
                seen.add(gid)
                rows.append({"good_id": gid, "name": it.name,
                             "img": getattr(it, "img", "") or ""})
            if rows:
                await store.async_save_goods_index(rows)
            if page % 10 == 0:
                print(f"page {page} | 累计 {len(seen)}")
    total = await store.async_count_goods_index()
    print(f"完成: 本次新增 {len(seen)} 条,goods_index 共 {total} 条")


if __name__ == "__main__":
    asyncio.run(main())
