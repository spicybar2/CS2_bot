# -*- coding: utf-8 -*-
"""往指定群发一次真实的大盘卡片(与 /大盘 走同一条代码路径,含走势图)。

与 announce_v001.py 同模式:不 import main(避开单例锁)、不 client.start()
(不建 websocket),注入独立 BotAPI 走 HTTP OAuth,与常驻实例互不干扰。

用途:V0.3.14 修好 SteamDT 大盘后,在测试群验证用户实际收到的卡片(文字 + 走势图)。

用法:
  .venv\\Scripts\\python.exe -X utf8 scripts\\send_broad_test.py            # 默认 1105775575
  .venv\\Scripts\\python.exe -X utf8 scripts\\send_broad_test.py 975433425  # 指定群号
"""
import asyncio
import os
import sys

BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, BASE_DIR)
os.chdir(BASE_DIR)

import botpy
from botpy import BotAPI
from botpy.http import BotHttp
from bot.config import load_config
from bot.storage.db import SnapshotStore
from bot.analyzer.recommender import Recommender
from bot.steamdt.client import SteamDTClient
from bot.qqbot.bot import CS2BotClient

DEFAULT_GROUP = "1105775575"


async def main():
    arg = (sys.argv[1] if len(sys.argv) > 1 else DEFAULT_GROUP).strip()
    cfg = load_config("config.yaml")
    groups = cfg.announce_groups or {}
    # 支持群号或别名
    gid = groups.get(arg) or arg
    if not gid:
        print(f"[broad-test] 找不到群 {arg}", flush=True)
        return 1
    print(f"[broad-test] 目标群 {arg} → {gid}", flush=True)

    store = SnapshotStore(os.path.join(BASE_DIR, "data", "csqaq.db"))
    sdt = SteamDTClient(cfg.steamdt)
    await sdt.ensure_session()

    client = CS2BotClient(cfg, store, Recommender(cfg.recommend),
                          botpy.Intents(public_messages=True), steamdt=sdt)
    http = BotHttp(timeout=20, app_id=cfg.qq.appid, secret=cfg.qq.secret)
    await http.check_session()
    client.api = BotAPI(http)

    # ① 生成卡片(与 /大盘 完全同一条路径)
    text, chart = await client._broad_report()
    is_fallback = "兜底" in text
    print(f"[broad-test] 卡片来源: {'CSQAQ 兜底(!)' if is_fallback else 'SteamDT 真数据'}", flush=True)
    print(f"[broad-test] 走势图: {chart or '(无)'}", flush=True)
    print("---- 卡片正文 ----", flush=True)
    for line in text.split("\n"):
        print("  " + line, flush=True)
    print("------------------", flush=True)

    # ② 发文字
    sent_text = await client.push_to_group(gid, text)
    print(f"[broad-test] 文字: {'✅ 已发送' if sent_text else '❌ 失败'}", flush=True)

    # ③ 发走势图(富媒体:分片上传 → msg_type=7)
    if chart:
        try:
            await client._send_group_image(gid, chart)
            print("[broad-test] 走势图: ✅ 已发送", flush=True)
        except Exception as exc:
            print(f"[broad-test] 走势图: ❌ {type(exc).__name__}: {exc}", flush=True)
    else:
        print("[broad-test] 走势图: 跳过(无图)", flush=True)

    await sdt.close()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
