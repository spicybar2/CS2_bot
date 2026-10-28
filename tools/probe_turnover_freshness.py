# -*- coding: utf-8 -*-
"""验证 CSQAQ turnover 到底是「数据源老旧」还是「这些品本来就没成交」。

拿高流动性品（武器箱、AK 红线）对比冷门品：
- 如果高流动性品的 period_at 是最近的 → 字段是好的，之前看到的旧日期只是「没成交」
- 如果连变革武器箱都停在一年前 → 数据源整体是死的

    .venv\\Scripts\\python.exe tools\\probe_turnover_freshness.py
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

PROBES = [
    (16283, "变革武器箱"),
    (19521, "千瓦武器箱"),
    (135, "AK-47 | 红线 (久经沙场)"),
    (16905, "印花 | ENCE（金色）| 2023年巴黎锦标赛"),
]


async def main() -> None:
    from bot.csqaq.client import CSQAQClient

    client = CSQAQClient(CFG.csqaq)
    now = datetime.now()
    try:
        for gid, name in PROBES:
            try:
                info = await client.get_good_info(gid)
            except Exception as exc:
                print(f"  gid={gid:<6} {name[:26]:<28} 请求失败: {exc}")
                continue
            num = info.get("turnover_number")
            avg = info.get("turnover_avg_price")
            per = info.get("period_at")
            age = ""
            if per:
                try:
                    dt = datetime.strptime(str(per)[:19], "%Y-%m-%dT%H:%M:%S")
                    age = f"（{(now - dt).days} 天前）"
                except ValueError:
                    age = "（解析失败）"
            print(f"  gid={gid:<6} {name[:26]:<28} 成交量 {str(num):>6}  均价 {str(avg):>8}  周期 {per}{age}")
            # 顺带看看有没有别的成交类字段
            extra = [k for k in info if any(t in k.lower() for t in ("sold", "deal", "trade", "volume"))]
            if extra:
                print(f"         其它可能的成交字段: {[(k, info[k]) for k in extra]}")
    finally:
        await client.close()


if __name__ == "__main__":
    asyncio.run(main())
