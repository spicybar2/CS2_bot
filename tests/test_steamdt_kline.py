# -*- coding: utf-8 -*-
"""SteamDT 单品 K 线回归。"""
from __future__ import annotations

import pathlib as _pl, sys as _sys          # tests/_bootstrap:保证能 import bot
_sys.path.insert(0, str(_pl.Path(__file__).resolve().parent.parent))

import asyncio
import os

from bot.csqaq.price_lookup import render_trend
from bot.steamdt.client import _ITEM_KLINE_PATH
from bot.steamdt.kline import normalize_item_kline_rows


class FakeStore:
    async def async_add_price_history(self, *args, **kwargs):
        return None

    async def async_query_price_history(self, *args, **kwargs):
        return []


class FakeSteamDT:
    def __init__(self):
        self.calls = []

    async def get_item_kline(self, market_hash_name, *, periods=2, max_time=None, force=False):
        self.calls.append((market_hash_name, periods, max_time, force))
        return {
            "points": [
                {"timestamp": 1693795200000, "close": 93.02},
                {"timestamp": 1693881600000, "close": 79.50},
            ],
            "raw": {"success": True},
        }


def main() -> None:
    parsed = normalize_item_kline_rows({"data": [[1693795200, 93.02, 79.50, 94.10, 78.80]]})
    assert len(parsed) == 1 and abs(parsed[0]["close"] - 79.50) < 1e-6
    assert _ITEM_KLINE_PATH == "/open/cs2/item/v1/kline"

    fake = FakeSteamDT()
    path, trend = asyncio.run(
        render_trend(FakeStore(), 1182, "Glove Case", 79.5, {"name": "Glove Case", "_market_hash_name": "Glove Case"}, steamdt=fake)
    )
    assert path and os.path.exists(path)
    assert trend == ""
    assert fake.calls and fake.calls[0][0] == "Glove Case" and fake.calls[0][1] == 2
    if path and os.path.exists(path):
        os.remove(path)


if __name__ == "__main__":
    main()
