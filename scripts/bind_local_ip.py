# -*- coding: utf-8 -*-
"""一键把当前出口 IP 自动绑定到 CSQAQ 白名单。

官方「绑定本机白名单IP」接口: POST /api/v1/sys/bind_local_ip
- 无需传 IP,服务端自动取请求方出口 IP 绑定(适用于非固定 IP / NAT 漂移)
- 频率限制: 30 秒/次

用法: python scripts/bind_local_ip.py
也可用 --json 输出结构化结果便于脚本判断。
"""
import argparse
import asyncio
import json
import os
import re
import sys

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE_DIR)

from bot.config import load_config
from bot.csqaq.client import CSQAQClient


def _extract_ip(detail: str) -> str:
    m = re.search(r"([\d.]+)\s*$", detail)
    return m.group(1) if m else ""


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true", help="输出 JSON 结果")
    args = ap.parse_args()

    cfg = load_config(os.path.join(BASE_DIR, "config.yaml"))
    async with CSQAQClient(cfg.csqaq) as api:
        ok, info = await api.bind_local_ip()
        if ok:
            # 绑定后复测确认
            ok2, _ = await api.probe_ip_status()
            result = {
                "ok": ok2,
                "bound_ip": _extract_ip(info) or None,
                "detail": info,
            }
        else:
            result = {"ok": False, "bound_ip": None, "detail": info}

    if args.json:
        print(json.dumps(result, ensure_ascii=False))
    else:
        if result["ok"]:
            print(f"[OK] 白名单绑定成功,当前 IP: {result['bound_ip']}")
            print(f"      {result['detail']}")
        else:
            print(f"[FAIL] 绑定失败: {result['detail']}")
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
