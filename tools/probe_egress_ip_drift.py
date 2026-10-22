# -*- coding: utf-8 -*-
"""V0.3.8 探针:出口 IP 漂移机制(只读,不改配置、不碰 CSQAQ 配额)。

背景:本机只有一条默认路由(WLAN 2),却观察到两个公网出口 IP
(14.145.31.44 / 14.145.53.119) → 漂移发生在**路由器/运营商按连接选 WAN**这一层。
要回答的问题:能否在本地"钉住"出口 IP?

关键判据:
  [1] 20 条**新建**连接 → 若出现多个 IP,说明"每条新连接都在赌"
  [2] 同一条 keep-alive 连接上连打 12 次 → 若始终同一个 IP,说明**长连接=钉住**
  [3] 6 条并发连接 → 若出现多个 IP,说明"并发多连接"正是漂移的直接来源
  [4] 再开一条新连接 → 与 [2] 的 IP 是否相同(判断连接级粘性/轮询)

用法: .venv\\Scripts\\python.exe -X utf8 probe_egress_ip_drift.py
"""
import asyncio
import re
import sys

import aiohttp

ECHOES = [
    ("https://myip.ipip.net", "ipip"),
    ("https://api.ip.sb/ip", "plain"),
    ("https://ip.useragentinfo.com/json", "json_ip"),
    ("http://ip-api.com/json", "json_query"),
    ("https://www.taobao.com/help/getip.php", "callback"),
    ("https://ifconfig.me/ip", "plain"),
    ("https://api.ipify.org?format=json", "json_ip"),
]

IP_RE = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")


def parse(kind: str, text: str):
    if kind == "json_ip":
        m = re.search(r'"ip"\s*:\s*"([\d.]+)"', text)
        return m.group(1) if m else None
    if kind == "json_query":
        m = re.search(r'"query"\s*:\s*"([\d.]+)"', text)
        return m.group(1) if m else None
    m = IP_RE.search(text or "")
    return m.group(0) if m else None


async def pick_echo(session):
    for url, kind in ECHOES:
        try:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=8)) as r:
                if r.status != 200:
                    continue
                ip = parse(kind, await r.text())
                if ip:
                    return url, kind, ip
        except Exception:
            continue
    return None, None, None


async def one_get(session, url, kind):
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as r:
            return parse(kind, await r.text()) or f"HTTP{r.status}"
    except Exception as exc:
        return f"ERR:{type(exc).__name__}"


async def t_new_conn(url, kind, n=20):
    ips = []
    conn = aiohttp.TCPConnector(force_close=True)   # 每条请求一条新连接
    async with aiohttp.ClientSession(connector=conn) as s:
        for _ in range(n):
            ips.append(await one_get(s, url, kind))
            await asyncio.sleep(0.25)
    return ips


async def t_reuse_conn(url, kind, n=12):
    ips = []
    conn = aiohttp.TCPConnector(limit=1, limit_per_host=1, force_close=False,
                                keepalive_timeout=300)
    async with aiohttp.ClientSession(connector=conn) as s:
        for _ in range(n):
            ips.append(await one_get(s, url, kind))
            await asyncio.sleep(0.6)
    return ips


async def t_concurrent(url, kind, n=6):
    async with aiohttp.ClientSession() as s:
        return list(await asyncio.gather(*[one_get(s, url, kind) for _ in range(n)]))


def show(tag, ips):
    real = [i for i in ips if not str(i).startswith(("ERR", "HTTP"))]
    uniq = sorted(set(real))
    print(f"    {ips}")
    print(f"    → 有效 {len(real)}/{len(ips)} 次,不同 IP {len(uniq)} 个: {uniq}")
    return uniq


async def main():
    async with aiohttp.ClientSession() as s:
        url, kind, ip0 = await pick_echo(s)
    if not url:
        print("所有 echo 服务都不可达(本机国际出口可能受限),无法采样")
        return 1
    print(f"echo 服务: {url}   首次出口 IP = {ip0}\n")

    print("[1] 20 条新建连接(force_close=True)")
    a = show("new", await t_new_conn(url, kind))

    print("\n[2] 同一条 keep-alive 连接连打 12 次(limit=1)")
    b = await t_reuse_conn(url, kind)
    bu = show("reuse", b)

    print("\n[3] 6 条并发连接")
    c = show("conc", await t_concurrent(url, kind))

    print("\n[4] 再开一条新连接(reuse 之后)")
    d = await t_reuse_conn(url, kind, 3)
    du = show("reuse2", d)

    print("\n===== 判读 =====")
    print(f"  新建连接命中 IP: {a}")
    print(f"  并发连接命中 IP: {c}")
    print(f"  长连接命中 IP  : {bu}")
    if len(a) > 1 or len(c) > 1:
        print("  → 漂移确实存在,且**并发/新建连接**会落到不同 WAN")
    if len(bu) == 1:
        print(f"  → 单条长连接 12 次请求全程 {bu[0]}:**长连接可以把出口 IP 钉住**")
    elif len(bu) == 0:
        print("  → 长连接组全失败,无法判读")
    else:
        print(f"  → 同一条连接中途也换了 IP({bu}):运营商侧 NAT 迁移,本地钉不住")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
