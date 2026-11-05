# -*- coding: utf-8 -*-
"""V0.3.39 裸品名直接查价（「@倒货波 手套武器箱」→ 价格）。

覆盖:
 [1] goods_index 名称归一 —— 精确 / 去空格 / 唯一包含 / 歧义不答 / 太短不答
 [2] _bare_item_price —— 命中走查价、未命中返回 None、超长不处理、索引异常不打挂
 [3] 路由位置 —— 只在 cmd=chat 且 AI 之前尝试，不抢 /价格、/推荐 等既有意图

用法: .venv\\Scripts\\python.exe -X utf8 tests\\test_bare_item_price_v0339.py
"""
import pathlib as _pl, sys as _sys          # tests/_bootstrap:保证能 import bot
_sys.path.insert(0, str(_pl.Path(__file__).resolve().parent.parent))
import asyncio
import os
import sys
import tempfile

from bot.storage.db import SnapshotStore

ROOT = _pl.Path(__file__).resolve().parent.parent
FAILS = []


def check(name, cond, extra=""):
    if cond:
        print(f"    [PASS] {name}")
    else:
        print(f"    [FAIL] {name}  {extra}")
        FAILS.append(name)


def make_store(rows):
    tmp = tempfile.mkdtemp()
    store = SnapshotStore(os.path.join(tmp, "t.db"))
    store.save_goods_index([{"good_id": i + 1, "name": n} for i, n in enumerate(rows)])
    return store


class FakeSelf:
    """只喂 _bare_item_price 用到的那两个依赖。"""

    def __init__(self, store, price_reply=None):
        self._store = store
        self.calls = []
        self._price_reply = price_reply or self._default

    async def _default(self, arg):
        self.calls.append(arg)
        return f"价格卡:{arg}"


def main():
    print("=" * 68)
    print("V0.3.39 裸品名直接查价 测试")
    print("=" * 68)

    print("\n[1] goods_index 名称归一")
    store = make_store([
        "手套武器箱",
        "AK-47 | 红线 (久经沙场)",
        "AK-47 | 红线 (崭新出厂)",
        "M4A4 | 喧嚣杀戮 (崭新出厂)",
    ])
    check("精确命中", store.resolve_goods_name("手套武器箱") == "手套武器箱")
    check("带空格也命中", store.resolve_goods_name(" 手套 武器箱 ") == "手套武器箱")
    check("大小写/空格归一",
          store.resolve_goods_name("ak-47 | 红线 (久经沙场)") == "AK-47 | 红线 (久经沙场)")
    check("唯一包含命中(≥4 字)", store.resolve_goods_name("喧嚣杀戮") == "M4A4 | 喧嚣杀戮 (崭新出厂)")
    check("歧义不答(红线 有 2 条)", store.resolve_goods_name("红线") is None)
    check("太短不答", store.resolve_goods_name("刀") is None)
    check("不相干文本不答", store.resolve_goods_name("这破玩意又跌了") is None)

    print("\n[2] _bare_item_price")
    from bot.qqbot.bot import CS2BotClient

    async def t2():
        fs = FakeSelf(store)
        out = await CS2BotClient._bare_item_price(fs, "手套武器箱")
        check("命中 → 走查价", out == "价格卡:手套武器箱" and fs.calls == ["手套武器箱"],
              f"{out} / {fs.calls}")

        fs2 = FakeSelf(store)
        out2 = await CS2BotClient._bare_item_price(fs2, "你好啊")
        check("未命中 → None(继续走 AI/闲聊)", out2 is None and fs2.calls == [], out2)

        fs3 = FakeSelf(store)
        long_txt = "手套武器箱" * 6
        out3 = await CS2BotClient._bare_item_price(fs3, long_txt)
        check(f"超过 24 字不处理(实际 {len(long_txt)} 字)", out3 is None and fs3.calls == [], out3)

        class Boom:
            async def async_resolve_goods_name(self, _t, min_sub_len=4):
                raise RuntimeError("index down")

        fs4 = FakeSelf(Boom())
        out4 = await CS2BotClient._bare_item_price(fs4, "手套武器箱")
        check("索引异常不抛、返回 None", out4 is None, out4)

    asyncio.run(t2())

    print("\n[3] 路由位置")
    src = (ROOT / "bot" / "qqbot" / "bot.py").read_text(encoding="utf-8")
    check("只在 cmd=chat 时尝试", 'if cmd == "chat":\n            bare = await self._bare_item_price(raw)' in src)
    i_bare = src.find("bare = await self._bare_item_price(raw)")
    i_ai = src.find("if self._agent and self._agent.enabled:", i_bare)
    check("在 AI 之前(裸品名不被 AI 抢走)", i_bare > 0 and i_ai > i_bare, f"{i_bare} < {i_ai}")
    check("/价格 走原路径不受影响", "_price_reply(arg)" in src)

    print("\n" + "=" * 68)
    if FAILS:
        print(f"失败 {len(FAILS)} 个: " + ", ".join(FAILS))
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
