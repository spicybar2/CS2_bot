# -*- coding: utf-8 -*-
"""库存监控 = 真正的大户收货数据：规模、新鲜度、能不能聚合成「建仓」信号。

CSQAQ /monitor/get_task_trends 返回被监控 Steam 账号的库存变动，
每条带 type：0 默认 1 买入 2 卖出 3 存入 4 取出 5 cd恢复 6 取出/恢复 7 卖出/存入
info 里有 steam_name / market_name / good_id / created_at / num / yyyp_sell_price。

这才是「谁在建仓」的直接证据 —— 求购/在售只有总数，看不到人。

    .venv\\Scripts\\python.exe tools\\probe_inventory_monitor.py
"""
from __future__ import annotations

import asyncio
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from bot.config import load_config  # noqa: E402

CFG = load_config(str(ROOT / "config.yaml"))

TYPE_NAME = {0: "默认库存", 1: "买入", 2: "卖出", 3: "存入", 4: "取出",
             5: "cd恢复", 6: "取出/恢复", 7: "卖出/存入"}


def flatten(groups) -> list:
    out = []
    for g in groups or []:
        if isinstance(g, list):
            out.extend(x for x in g if isinstance(x, dict))
        elif isinstance(g, dict):
            out.append(g)
    return out


async def main() -> None:
    from bot.csqaq.client import CSQAQClient

    client = CSQAQClient(CFG.csqaq)
    now = datetime.now()
    try:
        tasks = await client._post("/monitor/get_task_list",
                                   {"page_index": 1, "page_size": 50, "order": "HOT"})
        tl = tasks.get("data") or []
        print(f"=== 被监控账号（前 50 个里）: {len(tl)} 个 ===")
        fresh = [t for t in tl if str(t.get("traded_at", ""))[:10] >= "2026-10-20"]
        print(f"  近 8 天有变动的: {len(fresh)} 个")

        await asyncio.sleep(1.5)
        trends = await client._post("/monitor/get_task_trends",
                                    {"page_index": 1, "page_size": 200})
        rows = flatten(trends.get("data"))
        print(f"\n=== 变动记录（1 页）: {len(rows)} 条 ===")
        if not rows:
            return

        types = Counter(r.get("type") for r in rows)
        print("  类型分布: " + "  ".join(
            f"{TYPE_NAME.get(t, t)}={n}" for t, n in types.most_common()))

        ages = []
        for r in rows:
            info = r.get("info") or {}
            try:
                dt = datetime.strptime(str(info.get("created_at"))[:19], "%Y-%m-%dT%H:%M:%S")
                ages.append((now - dt).total_seconds() / 3600)
            except Exception:
                pass
        if ages:
            ages.sort()
            print(f"  新鲜度: 最新 {ages[0]:.1f} 小时前  中位 {ages[len(ages)//2]:.1f} 小时  "
                  f"最旧 {ages[-1]:.0f} 小时")

        buys = [r for r in rows if r.get("type") == 1]
        print(f"\n=== 买入事件 {len(buys)} 条 ===")
        if buys:
            per_item = defaultdict(lambda: {"n": 0, "accts": set(), "name": ""})
            for r in buys:
                info = r.get("info") or {}
                gid = info.get("good_id")
                k = per_item[gid]
                k["n"] += int(r.get("count") or 1)
                k["name"] = info.get("market_name") or k["name"]
                if info.get("steam_name"):
                    k["accts"].add(info["steam_name"])
            print("  被买入最多的饰品（按件数）:")
            for gid, v in sorted(per_item.items(), key=lambda kv: -kv[1]["n"])[:8]:
                print(f"    gid={str(gid):<7} {str(v['name'])[:26]:<28} "
                      f"{v['n']:>3} 件 / {len(v['accts'])} 个账号")
            multi = [(g, v) for g, v in per_item.items() if len(v["accts"]) >= 2]
            print(f"\n  **多个账号同时买入**的饰品: {len(multi)} 种")
            for gid, v in sorted(multi, key=lambda kv: -len(kv[1]["accts"]))[:8]:
                print(f"    gid={str(gid):<7} {str(v['name'])[:26]:<28} "
                      f"{len(v['accts'])} 个账号 / {v['n']} 件")

        sells = [r for r in rows if r.get("type") in (2, 7)]
        if sells:
            per_item = Counter()
            for r in sells:
                info = r.get("info") or {}
                per_item[(info.get("good_id"), info.get("market_name"))] += int(r.get("count") or 1)
            print(f"\n=== 卖出/存入事件 {len(sells)} 条，最多的品 ===")
            for (gid, nm), n in per_item.most_common(5):
                print(f"    gid={str(gid):<7} {str(nm)[:26]:<28} {n} 件")
    finally:
        await client.close()


if __name__ == "__main__":
    asyncio.run(main())
