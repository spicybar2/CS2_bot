# -*- coding: utf-8 -*-
"""V0.3.9:全局限速 + 超时后立即重绑 + 漂移暂停解耦。

背景(实测 2026-09-28):
  · 16:00 启动那一分钟出现 **30 次 429**,而稳态只有 1~3 次/分钟;
  · probe_429_vs_ip.py 在两个 WAN 上各打 10 次 /current_data,**10/10 全部 200**
    → 既不是 IP 被风控、也不是配额用尽,而是**突发**(节流原来是每实例一份)。
本测试锁死:
  [1] `_throttle` 是**全局**的(两个实例交替调用仍被拉开间隔,不再各自发车)
  [2] 漂移暂停与重绑失败退避解耦(暂停 120s,不是 600s)
  [3] 请求超时/连接错会**立即触发单飞重绑**并马上重试(skip_ip_rebind 时不动)
无网络请求。

用法: .venv\\Scripts\\python.exe -X utf8 test_pacing_v039.py
"""
import pathlib as _pl, sys as _sys          # tests/_bootstrap:保证能 import bot
_sys.path.insert(0, str(_pl.Path(__file__).resolve().parent.parent))
import asyncio
import os
import sys
import time

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


class _TimeoutCtx:
    """模拟 aiohttp:进入 async with 时抛 TimeoutError(=连接挂死被超时掐掉)。"""

    def __init__(self, delay):
        self._delay = delay

    async def __aenter__(self):
        await asyncio.sleep(self._delay)
        raise asyncio.TimeoutError()

    async def __aexit__(self, *a):
        return False


class FakeSession:
    def __init__(self, delay=0.0):
        self.calls = 0
        self._delay = delay

    def post(self, *a, **kw):
        self.calls += 1
        return _TimeoutCtx(self._delay)

    async def close(self):
        pass


async def main():
    from bot.config import CSQAQConfig
    from bot.csqaq import client as cmod
    # V0.3.21:本测试专注"重试/信号量"语义,不测限流 → 把全局 RPM 闸门放开,
    # 免得被 13 次/分的预算喂慢(限流本身由 test_rate_gate_v0321.py 覆盖)
    cmod._RATE_RPM = 1e6
    cmod._RATE_TOKENS = 1e6
    cmod._RATE_UPDATED = time.monotonic()
    cmod._RATE_PENALTY = 0.0
    cmod._RATE_PENALTY_UNTIL = 0.0
    cmod._RATE_PENALTY_BASE = 0.0     # 429 惩罚基数归零:本测试不验证限流
    cmod._RATE_PENALTY_MAX = 0.0

    print("\n[1] 节流是全局的(跨实例)")
    cfg = CSQAQConfig(api_token="d", request_interval=0.35)
    a, b = cmod.CSQAQClient(cfg), cmod.CSQAQClient(cfg)
    cmod._GLOBAL_LAST_REQUEST_AT = 0.0
    t0 = time.monotonic()
    await a._throttle()          # 第一次:无等待
    await b._throttle()          # 立刻再来一次(另一个实例):必须等满一个间隔
    gap = time.monotonic() - t0
    check(f"两实例连续节流被拉开(实测 {gap:.2f}s >= 0.30)", gap >= 0.30)
    c = cmod.CSQAQClient(cfg)
    t1 = time.monotonic()
    await c._throttle()
    gap2 = time.monotonic() - t1
    check(f"第三个实例同样受限(实测 {gap2:.2f}s >= 0.30)", gap2 >= 0.30)
    check("全局时间戳被更新", cmod._GLOBAL_LAST_REQUEST_AT > 0)

    print("\n[2] 漂移暂停与重绑退避解耦")
    check(f"默认暂停 120s(实测 {cmod._drift_pause_seconds(cfg)})",
          cmod._drift_pause_seconds(cfg) == 120)
    cfg2 = CSQAQConfig(api_token="d", drift_pause_seconds=45, rebind_cooldown_seconds=600)
    check("可配置覆盖", cmod._drift_pause_seconds(cfg2) == 45)
    check("缺失字段时回退 120", cmod._drift_pause_seconds(object()) == 120)
    cmod._IP_DRIFT_UNTIL = 0.0
    cmod._mark_ip_drift(None, cmod._drift_pause_seconds(cfg2))
    left = cmod._drift_cooldown_left()
    check(f"标记后剩余 {left}s ∈ (40, 46]", 40 < left <= 46)
    cmod._IP_DRIFT_UNTIL = 0.0

    print("\n[3] 超时/连接错 → 立即单飞重绑并重试")
    calls = {"n": 0}

    async def fake_rebind(cfg_, **kw):
        calls["n"] += 1
        return True, "fake-bound"

    orig_rebind = cmod.try_rebind_local_ip
    cmod.try_rebind_local_ip = fake_rebind
    try:
        cfg3 = CSQAQConfig(api_token="d", request_interval=0.02, retry_times=0)
        cli = cmod.CSQAQClient(cfg3)
        sess = FakeSession()
        cli._session = sess
        orig_shared = cmod.shared_session

        async def fake_shared(_cfg=None):
            return sess

        cmod.shared_session = fake_shared
        try:
            t0 = time.monotonic()
            err = ""
            try:
                await cli._post("/x", {"a": 1}, allow_drift_skip=False)
            except Exception as exc:
                err = type(exc).__name__
            dt = time.monotonic() - t0
            # attempts = retry_times(0) + 3 = 3;每次都超时→每次重绑→最后抛错
            check(f"尝试了 3 次(实际 {sess.calls})", sess.calls == 3)
            check(f"每次超时都触发重绑(实际 {calls['n']} 次)", calls["n"] >= 2)
            check(f"最终抛 RuntimeError(实际 {err})", err == "RuntimeError")
            check(f"没有退避睡死(耗时 {dt:.2f}s < 3s)", dt < 3.0)

            # skip_ip_rebind=True(bind 接口自身)不应触发重绑
            calls["n"] = 0
            sess2 = FakeSession()

            async def fake_shared2(_cfg=None):
                return sess2

            cmod.shared_session = fake_shared2
            cli2 = cmod.CSQAQClient(cfg3)
            cli2._session = sess2
            try:
                await cli2._post("/sys/bind_local_ip", {}, allow_drift_skip=False,
                                 skip_ip_rebind=True)
            except Exception:
                pass
            check(f"skip_ip_rebind=True 时不重绑(实际 {calls['n']})", calls["n"] == 0)
        finally:
            cmod.shared_session = orig_shared
    finally:
        cmod.try_rebind_local_ip = orig_rebind

    print("\n[4] 既有行为未回归")
    check("401 仍不重试", not cmod.CSQAQClient._is_retryable_status(401))
    check("429 仍重试", cmod.CSQAQClient._is_retryable_status(429))
    from bot.config import CSQAQConfig as C2
    check(f"request_timeout 默认 15s(实测 {C2().request_timeout})", C2().request_timeout == 15.0)


if __name__ == "__main__":
    print("=" * 68)
    print("V0.3.9 全局限速 / 超时重绑 / 漂移暂停 测试")
    print("=" * 68)
    asyncio.run(main())
    print("\n" + "=" * 68)
    print(f"结果: {PASS} 通过 / {FAIL} 失败")
    print("=" * 68)
    sys.exit(1 if FAIL else 0)
