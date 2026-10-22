# -*- coding: utf-8 -*-
"""V0.3.14 回归:SteamDT 大盘「一直取不到」的根因修复。

老大 2026-10-12 贴了播报里的「⚠️ SteamDT 大盘暂时取不到，CSQAQ 兜底」问"能不能解决"。

实测根因(probe_steamdt_quota.py / probe_steamdt_interval.py):
  · 原始单发 GET /open/cs2/broad/v1/index → HTTP 200 + success:true + 完整数据(指数 732.88)
  · 间隔 1.5s / 15s 再打 → code=4005「当前接口请求已达到上限」
  · 间隔 30s / 60s 再打 → 成功
  ⇒ 限的是**请求间隔(约 30 秒)**,不是出口配额总量。旧客户端失败后 1.5 秒连打 3 次
    (~87% 命中干净口的老经验),等于**自己制造** 4005,再把接口族锁 1800 秒 →
    自 2026-09-04 起每天 ~8 次,用户端天天看到兜底提示。

本测试锁死修复后的四条红线:
  [1] 失败后**只打一次**网络(不再 1.5 秒连试 3 次)
  [2] 首次失败的基础退避 = broad_cooldown_seconds(120s),不是 1800s
  [3] 失败后调度**后台补抓**,delay 到点自动补一次,成功即写缓存并清 streak
  [4] 两次联网之间强制 ≥ broad_min_gap_seconds(打了也是 4005,不打)
  [5] kline 族退避仍是 1800s(不被 broad 的新策略牵连)
  [6] 空数据(success=true 但指数为 0)按失败处理,绝不渲染成真实指数
"""
import pathlib as _pl, sys as _sys          # tests/_bootstrap:保证能 import bot
_sys.path.insert(0, str(_pl.Path(__file__).resolve().parent.parent))
import asyncio
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from bot.config import SteamDTConfig                            # noqa: E402
from bot.steamdt.client import (_BROAD_INDEX_PATH,              # noqa: E402
                                SteamDTClient, SteamDTRateLimit)

PASS = FAIL = 0
# 后台补抓任务必须和发起它的调用在**同一个**事件循环里,否则 await 会报
# "The future belongs to a different loop"(本测试第一版就踩了)。
LOOP = asyncio.new_event_loop()


def ok(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [OK]   {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name}   {extra}")


GOOD = {"success": True, "data": {
    "broadMarketIndex": 732.88, "updateTime": 1791788410,
    "diffYesterday": -6.17, "diffYesterdayRatio": -0.8349,
    "historyMarketIndexList": [[1791698400, 742.34], [1791702000, 741.93]]}}
LIMIT = {"success": False, "data": None, "errorCode": 4005,
         "errorMsg": "当前接口请求已达到上限。详细规则请参阅接口文档。"}
EMPTY = {"success": True, "data": {"broadMarketIndex": 0, "updateTime": 0,
                                   "historyMarketIndexList": []}}


def new_client(**over):
    cfg = SteamDTConfig(api_key="k" * 32, request_interval=0.0,
                        broad_min_gap_seconds=0.0, broad_retry_delay_seconds=0.05,
                        broad_cooldown_seconds=120.0)
    for k, v in over.items():
        setattr(cfg, k, v)
    return SteamDTClient(cfg)


def patch(client, responses):
    """把 _curl_request 换成脚本化回包;返回调用计数器。"""
    calls = []

    async def fake(method, path, payload):
        calls.append((method, path, payload))
        body = responses[min(len(calls) - 1, len(responses) - 1)]
        return 200, json.dumps(body, ensure_ascii=False)

    client._curl_request = fake          # type: ignore[assignment]
    return calls


print("=" * 74)
print("V0.3.14 SteamDT broad 根因修复 —— 回归测试")
print("=" * 74)

# ---------------------------------------------------------------- [1][2] 只打一次
print("\n[1][2] 失败后只打一次 + 首次退避 120s(不是 1800s)")
c = new_client()
calls = patch(c, [LIMIT])
try:
    LOOP.run_until_complete(c.get_broad_index_ex(force=True))
    ok("4005 时应抛错", False, "没有抛错")
except SteamDTRateLimit:
    ok("4005 时抛 SteamDTRateLimit", True)
ok("网络只打了一次(旧实现 3 次)", len(calls) == 1, f"calls={len(calls)}")
left = c._rate_limited.get("broad", 0.0) - time.time()
ok("首次退避 ≈120s(旧实现 1800s)", 100 <= left <= 130, f"left={left:.0f}s")
ok("streak 只 +1(没有双重上锁)", c._rl_fail_streak.get("broad") == 1,
   c._rl_fail_streak)

# ---------------------------------------------------------------- [3] 后台补抓
print("\n[3] 失败后后台补抓(delay 到点自动补,成功写缓存并清 streak)")
c2 = new_client()
calls2 = patch(c2, [LIMIT, GOOD])


async def scenario_refresh():
    try:
        await c2.get_broad_index_ex(force=True)
    except SteamDTRateLimit:
        pass
    task = c2._broad_refresh_task
    if task is not None:
        await asyncio.wait_for(task, timeout=3)


LOOP.run_until_complete(scenario_refresh())
ok("补抓确实又打了一次网络", len(calls2) == 2, f"calls={len(calls2)}")
ok("补抓成功后 streak 清零", c2._rl_fail_streak.get("broad") == 0,
   c2._rl_fail_streak)
ok("补抓成功后缓存里有真数据",
   (c2._cache.get(_BROAD_INDEX_PATH) is not None), c2._cache.keys())
ok("补抓成功后调用方拿到真数据(不需再联网)",
   LOOP.run_until_complete(c2.get_broad_index()).get("index") == 732.88)

# ---------------------------------------------------------------- [4] 最小间隔
print("\n[4] 两次联网之间强制 ≥ broad_min_gap_seconds")
c3 = new_client(broad_min_gap_seconds=30.0)
calls3 = patch(c3, [GOOD, GOOD])


async def scenario_gap():
    d1 = await c3.get_broad_index(force=True)          # 第 1 次联网
    c3._cache.clear()                                  # 清缓存,逼它必须联网
    try:
        await c3.get_broad_index(force=True)           # 30s 内再打 → 应被拦
        return d1, "no-raise"
    except SteamDTRateLimit as exc:
        return d1, str(exc)


d1, why = LOOP.run_until_complete(scenario_gap())
ok("第 1 次拿到真数据", d1.get("index") == 732.88, d1)
ok("30s 内第二次被拦住(打了也是 4005)", "跳过联网" in why, why)
ok("被拦时没有发起网络请求", len(calls3) == 1, f"calls={len(calls3)}")

# ---------------------------------------------------------------- [5] kline 不受牵连
print("\n[5] kline 族退避仍是 1800s")
c4 = new_client()
c4._enter_rate_limit("kline")
left_k = c4._rate_limited["kline"] - time.time()
ok("kline 退避 ≈1800s", 1750 <= left_k <= 1810, f"left={left_k:.0f}s")

# ---------------------------------------------------------------- [6] 空数据
print("\n[6] 空数据按失败处理,绝不渲染成真实指数")
c5 = new_client()
calls5 = patch(c5, [EMPTY, GOOD])
try:
    LOOP.run_until_complete(c5.get_broad_index_ex(force=True))
    ok("空数据应抛错", False, "没有抛错")
except SteamDTRateLimit:
    ok("空数据抛 SteamDTRateLimit", True)
ok("空数据没有留在缓存里", c5._cache.get(_BROAD_INDEX_PATH) is None)
ok("空数据也会调度后台补抓", c5._broad_refresh_task is not None)


async def scenario_empty_refresh():
    t = c5._broad_refresh_task
    if t is not None:
        await asyncio.wait_for(t, timeout=3)


LOOP.run_until_complete(scenario_empty_refresh())
ok("补抓后缓存里是真数据(指数 732.88)",
   (c5._cache.get(_BROAD_INDEX_PATH) is not None
    and c5._parse_broad(c5._cache[_BROAD_INDEX_PATH].data)["index"] == 732.88))
ok("补抓打的仍是 broad 路径", all(p == _BROAD_INDEX_PATH for _, p, _ in calls5),
   calls5)

# ---------------------------------------------------------------- [7] 成功路径不回归
print("\n[7] 成功路径:解析字段与缓存 TTL 不变")
c6 = new_client(cache_ttl_seconds=3600)
patch(c6, [GOOD])
d6 = LOOP.run_until_complete(c6.get_broad_index(force=True))
ok("index 解析", d6["index"] == 732.88, d6.get("index"))
ok("diff_ratio_pct 解析", abs(d6["diff_ratio_pct"] + 0.8349) < 1e-6, d6)
ok("history 解析成二元组", d6["history"][0] == (1791698400.0, 742.34), d6["history"])
ok("update_time 解析", d6["update_time"] == 1791788410, d6)
ok("成功时缓存已写入", c6._cache.get(_BROAD_INDEX_PATH) is not None)

# ---------------------------------------------------------------- [8] 缓存出口也拆信封
print("\n[8] 缓存命中返回的也是解析后的 dict(信封 bug 的真正根因,所有出口都要拆)")
c7 = new_client(cache_ttl_seconds=3600)
patch(c7, [GOOD])
_ = LOOP.run_until_complete(c7.get_broad_index(force=True))     # 第 1 次:联网
d7 = LOOP.run_until_complete(c7.get_broad_index())              # 第 2 次:命中缓存
ok("缓存命中 index 正确(旧实现这里恒为 0)", d7.get("index") == 732.88, d7.get("index"))
ok("缓存命中不含信封字段(success/data)", "success" not in d7, list(d7)[:5])
ok("缓存命中 history 已归一", d7.get("history") == [(1791698400.0, 742.34),
                                                    (1791702000.0, 741.93)], d7.get("history"))
ok("_parse_broad 对已是 data 层的字典也兼容",
   SteamDTClient._parse_broad(GOOD["data"])["index"] == 732.88)
ok("90 条 index=0.00 那种日志不会再出现(解析后再打日志)",
   SteamDTClient._parse_broad(GOOD)["index"] == 732.88)

print("\n" + "=" * 74)
print(f"结果: 通过 {PASS} / 失败 {FAIL}")
print("=" * 74)
sys.exit(1 if FAIL else 0)
