# -*- coding: utf-8 -*-
"""手动抓取一次 CSQAQ 数据并打印前 N 条（用于验证配置与网络）。

用法:
    python scripts/fetch_once.py [--pages 3] [--page-size 100]
"""

import argparse
import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bot.config import load_config, validate_config
from bot.csqaq.client import CSQAQClient


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--pages", type=int, default=3)
    parser.add_argument("--page-size", type=int, default=100)
    args = parser.parse_args()

    cfg = load_config("config.yaml")
    errors = validate_config(cfg)
    if errors:
        print("配置缺失:")
        for e in errors:
            print("  -", e)
        return 1

    async with CSQAQClient(cfg.csqaq) as api:
        items = await api.get_rank_items_all(
            page_size=args.page_size, max_pages=args.pages)
        print(f"\n共获取 {len(items)} 条饰品数据\n")
        items.sort(key=lambda x: x.main_price, reverse=True)
        for it in items[:10]:
            print(json.dumps(it.to_dict(), ensure_ascii=False, indent=1))
            print("-" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
