# -*- coding: utf-8 -*-
"""V0.3.8:CSQAQ 单连接钉住(出口 IP 不再漂移)回归测试。

背景(实测 probe_egress_ip_drift.py,2026-09-28):
  本机单默认路由,但路由器/运营商**按 TCP 连接**双 WAN 轮询 ——
  20 条新建连接严格交替 14.145.31.44 / 14.145.65.164;同一长连接 12 次请求全程同一 IP;
  6 条并发 3+3 劈开。→ 让 CSQAQ 只走一条 keep-alive 连接即可钉住出口 IP。

本测试锁死:
  [1] 连接器就是单连接 + 长 keepalive(limit=1/limit_per_host=1/keepalive 600/不 force_close)
  [2] 多实例共享同一个会话对象(不再每实例一个连接池)
  [3] **实例 close() 不关共享会话**(否则 main.py 里 5 处短命 async with 会掐断雷达的连接)
  [4] 进程收尾 close_shared_session() 关掉后能重新建
无网络请求,纯本地对象断言。

用法: .venv\\Scripts\\python.exe -X utf8 test_conn_pinning_v038.py
"""
import pathlib as _pl, sys as _sys          # tests/_bootstrap:保证能 import bot
_sys.path.insert(0, str(_pl.Path(__file__).resolve().parent.parent))
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

PASS = 0
FAIL = 0


def check(label, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [OK] {label}")
    else:
        FAIL += 1
        print(f"  [FAIL] {label}")


async def main():
    import aiohttp
    from bot.csqaq import client as cmod
    from bot.config import CSQAQConfig

    print("\n[1] 连接器 = 单连接 + 长 keepalive")
    check("_PIN_LIMIT == 1(只允许一条连接 = 只落一条 WAN)", cmod._PIN_LIMIT == 1)
    check("keepalive >= 300s(默认 30s 太短会被动换连接)",
          float(cmod._PIN_KEEPALIVE) >= 300.0)
    s = await cmod.shared_session()
    conn = s.connector
    check(f"实际连接器是 TCPConnector({type(conn).__name__})",
          isinstance(conn, aiohttp.TCPConnector))
    check(f"connector.limit == 1(实际 {conn.limit})", conn.limit == 1)
    check(f"connector.limit_per_host == 1(实际 {conn.limit_per_host})",
          conn.limit_per_host == 1)
    ka = getattr(conn, "_keepalive_timeout", None)
    check(f"connector keepalive >= 300(实际 {ka})", ka is not None and float(ka) >= 300.0)
    check(f"未强制短连接(_force_close={getattr(conn, '_force_close', None)})",
          getattr(conn, "_force_close", None) is False)

    print("\n[2] 多实例共享同一个会话")
    cfg = CSQAQConfig(api_token="dummy")
    a, b = cmod.CSQAQClient(cfg), cmod.CSQAQClient(cfg)
    sa = await a._ensure_session()
    sb = await b._ensure_session()
    check("两个实例拿到同一对象", sa is sb)
    check("与 shared_session() 一致", sa is await cmod.shared_session())
    check("共享会话未关闭", not sa.closed)

    print("\n[3] 实例 close() 绝不关闭共享会话(致命细节)")
    await a.close()
    check("close() 后共享会话仍打开", not sa.closed)
    sb2 = await b._ensure_session()
    check("另一个实例仍能用同一会话(雷达不会被掐断)", sb2 is sa and not sb2.closed)
    check("close() 只把本实例引用置空", a._session is None)

    print("\n[4] 进程收尾可关闭并重建")
    await cmod.close_shared_session()
    check("close_shared_session() 后会话已关闭", sa.closed)
    s2 = await cmod.shared_session()
    check("再次取用会新建一个会话", s2 is not sa and not s2.closed)
    c = cmod.CSQAQClient(cfg)
    check("新实例自动用上新会话", (await c._ensure_session()) is s2)
    await cmod.close_shared_session()

    print("\n[5] 静态方法未受影响(既有行为回归)")
    check("401 不重试", not cmod.CSQAQClient._is_retryable_status(401))
    check("429 重试", cmod.CSQAQClient._is_retryable_status(429))
    check("_looks_like_ip_drift 仍识别环境IP", cmod._looks_like_ip_drift("环境IP:1.2.3.4 非法"))


if __name__ == "__main__":
    print("=" * 68)
    print("V0.3.8 CSQAQ 单连接钉住测试")
    print("=" * 68)
    asyncio.run(main())
    print("\n" + "=" * 68)
    print(f"结果: {PASS} 通过 / {FAIL} 失败")
    print("=" * 68)
    sys.exit(1 if FAIL else 0)
