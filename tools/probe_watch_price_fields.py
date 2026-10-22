# -*- coding: utf-8 -*-
"""一次性核实:批量查价接口 / 单品详情里到底有哪些价格字段(决定 /关注 能不能取到悠悠价)。"""
import asyncio
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from bot.config import load_config                      # noqa: E402
from bot.csqaq.client import CSQAQClient                # noqa: E402
from bot.csqaq.resolver import resolve_prices           # noqa: E402
from bot.csqaq.market_counts import watch_primary_price  # noqa: E402
from bot.storage.db import SnapshotStore                # noqa: E402


async def main():
    cfg = load_config(str(ROOT / "config.yaml"))
    with tempfile.TemporaryDirectory() as tmp:
        store = SnapshotStore(str(Path(tmp) / "t.db"))
        async with CSQAQClient(cfg.csqaq) as api:
            result = await resolve_prices(api, store, "蝴蝶刀 大马士革钢")
            if not result:
                print("  没查到,换个词")
                return 1
            mhn, row = next(iter(result.items()))
            print("  命中:", mhn)
            keys = [k for k in row if "rice" in k]
            print("  价格字段:", sorted(keys))
            print("  buffSellPrice =", row.get("buffSellPrice"))
            print("  yyypSellPrice =", row.get("yyypSellPrice"))
            print("  yyyp_sell_price =", row.get("yyyp_sell_price"))
            price, scope = watch_primary_price(row)
            print(f"  → 主用价 {price} 口径 {scope}")

            gid = int(row.get("goodId") or row.get("good_id") or 0)
            print("  goodId =", gid)
            if gid:
                detail = await api.get_good_info(gid)
                if detail:
                    dkeys = sorted(k for k in detail if "rice" in k)
                    print("  详情价格字段:", dkeys)
                    print("  详情 yyypSellPrice =", detail.get("yyypSellPrice"))
                    print("  详情 yyyp_sell_price =", detail.get("yyyp_sell_price"))
                    merged = dict(row)
                    merged.update(detail)
                    print("  → 合并后主用价", watch_primary_price(merged))
        store.close() if hasattr(store, "close") else None
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
