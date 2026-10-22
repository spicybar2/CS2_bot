# -*- coding: utf-8 -*-
"""榜单排序健康检查:逐个排序打一页,量真实耗时,找出"服务端挂死"的排序。

背景(2026-10-21 实测):`价格_价格下降(百分比)_近30天` / `价格_价格上升(百分比)_近30天` 两个排序
会在服务端**挂死**(80 秒级无响应,6 轮重试全超时),而同一条连接上的其它 8 个排序只要 1~4 秒。
它们还会被超时路径误判成"IP 漂移",触发冷却跳过 + 无谓重绑。

用法: .venv\\Scripts\\python.exe -X utf8 probe_sort_health.py
退出码: 0=全部健康, 1=有排序异常(打印是哪些)
"""
import asyncio
import sys
import time

from bot.config import load_config
from bot.csqaq import client as C


async def main():
    cfg = load_config("config.yaml")
    c = cfg.csqaq
    c.request_interval = 1.2
    c.request_timeout = 8.0          # 卡住就快速判死,别等 15s×6 轮
    ip = await C.select_pin_ip(c)
    print(f"  当前钉住: {ip}")
    sorts = [v for k, v in vars(C).items() if k.startswith("RANK_SORT_") and isinstance(v, str)]
    seen, uniq = set(), []
    for s in sorts:
        if s not in seen:
            seen.add(s)
            uniq.append(s)
    bad = []
    async with C.CSQAQClient(c) as cli:
        for s in uniq:
            t0 = time.monotonic()
            try:
                data = await cli.get_rank_list(page_index=1, page_size=100, sort=s)
                print(f"    {s:<30} OK   {len(data):>3} 条  {time.monotonic()-t0:>6.2f}s")
            except Exception as exc:
                bad.append(s)
                print(f"    {s:<30} BAD  {type(exc).__name__}  {time.monotonic()-t0:>6.2f}s")
    print(f"\n  异常排序: {bad if bad else '无'}  ({len(uniq)} 个共测)")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
