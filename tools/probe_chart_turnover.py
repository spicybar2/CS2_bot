# -*- coding: utf-8 -*-
"""B 方案核心探测：CSQAQ /info/chart 的「日成交量」是不是新鲜的。

正确参数（来自 docs.csqaq.com/api-187131781 的 OpenAPI 枚举）：
    key      = turnover_number   （日成交量，**仅 platform=3 Steam 支持**）
    platform = 3                 （1-BUFF 2-悠悠 3-Steam 4-C5GAME）
    period   = 7/15/30/90/180/365/1095
    style    = all_style

背景：/info/good 的 turnover_number 连武器箱都停在 354~367 天前；
文档里「实时成交数据」整栏标「暂停更新」。
如果这里的日成交量序列是近期的，B 方案（流动性优先选品）就成立。

    .venv\\Scripts\\python.exe tools\\probe_chart_turnover.py
"""

from __future__ import annotations

import asyncio
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from bot.config import load_config  # noqa: E402

CFG = load_config(str(ROOT / "config.yaml"))

# 高流动性品 + 一个冷门品做对照
PROBES = [
    (16283, "变革武器箱"),
    (19521, "千瓦武器箱"),
    (16905, "印花 | ENCE（金色）| 2023年巴黎锦标赛"),
]
PERIOD = 30


async def main() -> None:
    from bot.csqaq.client import CSQAQClient

    client = CSQAQClient(CFG.csqaq)
    now = datetime.now()
    print(f"当前 {now:%Y-%m-%d %H:%M}，period={PERIOD} 天\n")
    try:
        for gid, name in PROBES:
            payload = {"good_id": gid, "key": "turnover_number",
                       "platform": 3, "period": PERIOD, "style": "all_style"}
            try:
                res = await client._post("/info/chart", payload)
            except Exception as exc:
                print(f"  {name[:24]:<26} 请求失败: {exc}")
                continue
            data = (res or {}).get("data")
            if not isinstance(data, dict):
                print(f"  {name[:24]:<26} 返回异常: {str(data)[:100]}")
                continue
            ts = data.get("timestamp") or []
            vals = data.get("main_data") or []
            if not ts:
                print(f"  {name[:24]:<26} timestamp 为空")
                continue
            latest = datetime.fromtimestamp(ts[-1] / 1000)
            age_h = (now - latest).total_seconds() / 3600
            nz = sum(1 for v in vals if v)
            print(f"  {name[:24]:<26} 点数 {len(ts):<4} "
                  f"最新 {latest:%Y-%m-%d %H:%M} ({age_h:.1f} 小时前)  "
                  f"非零 {nz}/{len(vals)}")
            print(f"      最近 5 天: {list(zip([datetime.fromtimestamp(t/1000).strftime('%m-%d') for t in ts[-5:]], vals[-5:]))}")
            await asyncio.sleep(1.2)
    finally:
        await client.close()


if __name__ == "__main__":
    asyncio.run(main())
