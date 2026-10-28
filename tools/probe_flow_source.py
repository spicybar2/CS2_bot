# -*- coding: utf-8 -*-
"""B 方案前置调研：成交流水（成交量/成交额）到底能不能拿到。

只读探测，不写库、不影响运行中的实例。每个数据源只打 1~2 次请求。

    .venv\\Scripts\\python.exe tools\\probe_flow_source.py

探测三个来源：
1. CSQAQ /info/good      —— 现在返回的 turnover_number / period_at 是什么
2. SteamDT /item/v1/kline —— K 线原始字段里有没有成交量（我们只解析了 OHLC）
3. Steam 社区市场 priceoverview —— 公开接口，volume 字段就是 24 小时成交量
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from bot.config import load_config  # noqa: E402

CFG = load_config(str(ROOT / "config.yaml"))

# 挑两件池子里的货（一件便宜一件贵），避免用冷门品得出错误结论
PROBES = [
    (16905, "印花 | ENCE（金色）| 2023年巴黎锦标赛"),
    (13366, None),   # 从库里补名字
]


def _dump(label: str, obj: object, keys=None) -> None:
    print(f"\n  {label}")
    if isinstance(obj, dict):
        items = obj.items() if keys is None else ((k, obj.get(k)) for k in keys)
        for k, v in items:
            text = str(v)
            if len(text) > 90:
                text = text[:90] + "..."
            print(f"     {k:<26} {text}")
    else:
        print(f"     {obj}")


async def probe_csqaq() -> None:
    print("\n" + "=" * 78)
    print("[1] CSQAQ /info/good —— 成交流水字段")
    print("=" * 78)
    from bot.csqaq.client import CSQAQClient

    client = CSQAQClient(CFG.csqaq)
    try:
        for gid, name in PROBES:
            try:
                info = await client.get_good_info(gid)
            except Exception as exc:
                print(f"\n  gid={gid} 请求失败: {exc}")
                continue
            print(f"\n  gid={gid} {name or info.get('name', '')}")
            hit = {k: v for k, v in info.items()
                   if any(t in k.lower() for t in ("turnover", "period", "volume", "sold", "deal"))}
            if hit:
                _dump("含成交/周期字样的字段", hit)
            else:
                _dump("没有成交相关字段；前 20 个键", dict(list(info.items())[:20]))
            print(f"     字段总数 {len(info)}")
    finally:
        await client.close()


async def probe_steamdt() -> None:
    print("\n" + "=" * 78)
    print("[2] SteamDT /item/v1/kline —— K 线里有成交量吗")
    print("=" * 78)
    from bot.steamdt.client import SteamDTClient

    client = SteamDTClient(CFG.steamdt)
    try:
        name = "M4A4 | Griffin (Factory New)"
        try:
            res = await client.get_item_kline(name, periods=2)
        except Exception as exc:
            print(f"\n  请求失败: {exc}")
            return
        raw = res.get("raw")
        print(f"\n  {name}")
        print(f"     解析出的点数 {len(res.get('points') or [])}")
        if isinstance(raw, dict):
            _dump("响应顶层键", raw)
            data = raw.get("data")
            if isinstance(data, dict):
                _dump("data 内的键", data)
                rows = data.get("data") or data.get("list") or data.get("rows")
            else:
                rows = data
            if isinstance(rows, list) and rows:
                print(f"     行数 {len(rows)}")
                first = rows[0]
                if isinstance(first, dict):
                    _dump("第一行的所有字段", first)
                elif isinstance(first, (list, tuple)):
                    print(f"     第一行只含 {len(first)} 个值: {first}")
    finally:
        await client.close()


async def probe_steam_market() -> None:
    print("\n" + "=" * 78)
    print("[3] Steam 社区市场 priceoverview —— volume = 24h 成交量")
    print("=" * 78)
    import aiohttp

    names = ["M4A4 | Griffin (Factory New)",
             "Sticker | ENCE (Gold) | Paris 2023"]
    url = "https://steamcommunity.com/market/priceoverview/"
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
    try:
        async with aiohttp.ClientSession(headers=headers,
                                         timeout=aiohttp.ClientTimeout(total=15)) as s:
            for n in names:
                params = {"appid": 730, "currency": 23, "market_hash_name": n}
                try:
                    async with s.get(url, params=params) as resp:
                        body = await resp.text()
                        print(f"\n  {n}")
                        print(f"     HTTP {resp.status}")
                        print(f"     {body[:300]}")
                except Exception as exc:
                    print(f"\n  {n}\n     请求失败: {exc}")
    except Exception as exc:
        print(f"  会话创建失败: {exc}")


async def main() -> None:
    await probe_csqaq()
    await probe_steamdt()
    await probe_steam_market()
    print("\n" + "=" * 78)
    print("探测结束")
    print("=" * 78)


if __name__ == "__main__":
    asyncio.run(main())
