# -*- coding: utf-8 -*-
"""渲染 V0.3.23 后 /关注、/我的关注、盯盘提醒 的真实文案(取一条真品实时价)。"""
import asyncio
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from bot.config import load_config                        # noqa: E402
from bot.csqaq.client import CSQAQClient                  # noqa: E402
from bot.csqaq.resolver import resolve_prices             # noqa: E402
from bot.csqaq.market_counts import watch_price_pair, watch_primary_price  # noqa: E402
from bot.qqbot import commands as cmds                    # noqa: E402
from bot.storage.db import SnapshotStore                  # noqa: E402


async def main():
    cfg = load_config(str(ROOT / "config.yaml"))
    with tempfile.TemporaryDirectory() as tmp:
        store = SnapshotStore(str(Path(tmp) / "t.db"))
        async with CSQAQClient(cfg.csqaq) as api:
            result = await resolve_prices(api, store, "蝴蝶刀 大马士革钢")
            mhn, row = next(iter(result.items()))
            gid = int(row.get("goodId") or 0)
            if gid:
                detail = await api.get_good_info(gid)
                if detail:
                    row = dict(row)
                    row.update(detail)
            price, scope = watch_primary_price(row)
            buff, yyyp = watch_price_pair(row)
            title = row.get("name") or mhn

            print("  【/关注 的回执】")
            print("  " + cmds.format_watch_added(title, price, None, None, "price", 0.0,
                                                  buff_price=buff, yyyp_price=yyyp,
                                                  scope=scope).replace("\n", "\n  "))
            print("\n  【/我的关注 列表(取迁移后的真实记录)】")
            rows = store.list_all_watches()
        store2 = SnapshotStore(str(ROOT / "data" / "csqaq.db"))
        rows = [r for r in store2.list_all_watches() if "大马士革钢" in r["zh_name"]][:3]
        print("  " + cmds.format_watch_list(rows).replace("\n", "\n  "))
        print("\n  【盯盘提醒(示例:跌 11.1%)】")
        print("  " + cmds.format_watch_alert(title, price * 1.12, price, "跌幅 -11.1% 触发",
                                              buff_price=buff, yyyp_price=yyyp
                                              ).replace("\n", "\n  "))
    return 0


if __name__ == "__main__":
    try:
        code = asyncio.run(main())
    except SystemExit:
        code = 0
    except Exception as exc:  # 渲染探针:出错也不影响结论,只打印
        print('  渲染失败:', exc)
        code = 1
    sys.exit(code)
