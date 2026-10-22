# -*- coding: utf-8 -*-
"""V0.3.10:全局单飞信号量(消灭单连接的队头级联超时)。

问题(V0.3.9 部署后实测):单连接(limit=1)时多余请求**在 aiohttp 连接池里排队**,而
`ClientTimeout(total=15)` 的计时**包含排队时间** → 队头一条慢请求会把排队的请求一起
"等成"超时;更糟的是 V0.3.9 的"超时即重绑"会拿这种假超时去误触发重绑
(日志特征:`ip_rebind_start ip=` 为空)。部署后 19 分钟内有 18 次超时。

本测试锁死:
  [1] 同时在飞的请求数**永远 ≤1**(信号量真的在起作用)
  [2] 排队请求正常完成(不再有"等出来的超时")
  [3] 抛异常也会**释放**名额(不会把后续请求全卡死)
  [4] 429 退避等待期间**不占名额**(别人能正常发车)
  [5] 401(真漂移)仍然触发重绑;静态语义无回归
无网络请求。

用法: .venv\\Scripts\\python.exe -X utf8 test_semaphore_v0310.py
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


class FakeResp:
    def __init__(self, status, body):
        self.status = status
        self._body = body

    async def text(self):
        return self._body

    async def json(self):
        import json
        return json.loads(self._body)


class _Ctx:
    def __init__(self, resp, delay, tracker):
        self._resp = resp
        self._delay = delay
        self._t = tracker

    async def __aenter__(self):
        self._t["inflight"] += 1
        self._t["max"] = max(self._t["max"], self._t["inflight"])
        if self._delay:
            await asyncio.sleep(self._delay)
        return self._resp

    async def __aexit__(self, *a):
        self._t["inflight"] -= 1
        return False


class FakeSession:
    """按序列返回响应;可注入延迟与异常;统计同时在飞数。"""

    def __init__(self, seq=None, delay=0.0, raise_exc=None):
        self.seq = list(seq or [])
        self.delay = delay
        self.raise_exc = raise_exc
        self.tracker = {"inflight": 0, "max": 0, "calls": 0}

    def _next(self):
        self.tracker["calls"] += 1
        if self.raise_exc is not None:
            exc = self.raise_exc
            self.raise_exc = None
            raise exc
        if self.seq:
            return self.seq.pop(0)
        return FakeResp(200, '{"code":200,"data":{}}')

    def post(self, *a, **kw):
        try:
            resp = self._next()
        except Exception as exc:          # 连接层异常:进入 async with 前抛出
            class _R:
                async def __aenter__(self_inner):
                    raise exc

                async def __aexit__(self_inner, *x):
                    return False
            return _R()
        if isinstance(resp, Exception):
            class _R2:
                async def __aenter__(self_inner):
                    raise resp

                async def __aexit__(self_inner, *x):
                    return False
            return _R2()
        return _Ctx(resp, self.delay, self.tracker)

    def get(self, *a, **kw):
        return self.post(*a, **kw)

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

    cmod._IP_DRIFT_UNTIL = 0.0

    def wire(sess):
        async def fake_shared(_cfg=None):
            return sess
        cmod.shared_session = fake_shared

    orig_shared = cmod.shared_session
    rebinds = {"n": 0}

    async def fake_rebind(cfg_, **kw):
        rebinds["n"] += 1
        return True, "fake"

    orig_rebind = cmod.try_rebind_local_ip
    cmod.try_rebind_local_ip = fake_rebind
    ok_body = '{"code":200,"data":{"ok":1}}'
    try:
        cfg = CSQAQConfig(api_token="d", request_interval=0.02, retry_times=0)
        print("\n[1] 同时在飞 ≤1(5 个并发请求)")
        sess = FakeSession(delay=0.15)
        wire(sess)
        api = cmod.CSQAQClient(cfg)
        res = await asyncio.gather(*[api._post("/a", {"i": i}, allow_drift_skip=False)
                                     for i in range(5)])
        check(f"5 个请求全部成功({len(res)})", len(res) == 5)
        check(f"同时在飞峰值 = {sess.tracker['max']} (应 ==1)", sess.tracker["max"] == 1)
        check(f"请求次数 = {sess.tracker['calls']}", sess.tracker["calls"] == 5)

        print("\n[2] 排队请求不会假超时(串行 4 个,各自 0.12s)")
        sess2 = FakeSession(delay=0.12)
        wire(sess2)
        api2 = cmod.CSQAQClient(CSQAQConfig(api_token="d", request_interval=0.01, retry_times=0))
        t0 = time.monotonic()
        out = await asyncio.gather(*[api2._post("/b", {}, allow_drift_skip=False)
                                    for _ in range(4)], return_exceptions=True)
        dt = time.monotonic() - t0
        errs = [o for o in out if isinstance(o, Exception)]
        check(f"4 个全部成功(异常 {len(errs)})", not errs)
        check(f"确实是串行({dt:.2f}s ≈ 4×0.12s)", 0.45 < dt < 1.6)

        print("\n[3] 异常会释放名额(不卡死后续请求)")
        sess3 = FakeSession(delay=0.0)
        sess3.raise_exc = asyncio.TimeoutError()
        wire(sess3)
        api3 = cmod.CSQAQClient(CSQAQConfig(api_token="d", request_interval=0.01, retry_times=0))
        try:
            await api3._post("/c", {}, allow_drift_skip=False)
        except Exception:
            pass
        # 后续请求必须能正常拿到名额并成功(若信号量泄漏,这里会挂到超时)
        api3_b = cmod.CSQAQClient(CSQAQConfig(api_token="d", request_interval=0.01, retry_times=0))
        got = await asyncio.wait_for(
            api3_b._post("/c2", {}, allow_drift_skip=False), timeout=5.0)
        check("超时异常后,新请求仍能成功(信号量已释放)", isinstance(got, dict))

        print("\n[4] 429 退避等待期间不占名额")
        # 第一枪 429(backoff 1×3=3s),第二枪 200;期间别人的请求应能立刻发车
        sess4 = FakeSession(seq=[FakeResp(429, '{"code":429}')], delay=0.0)
        wire(sess4)
        cfg4 = CSQAQConfig(api_token="d", request_interval=0.01, retry_times=1)
        slow = cmod.CSQAQClient(cfg4)
        t0 = time.monotonic()
        task_slow = asyncio.create_task(slow._post("/slow", {}, allow_drift_skip=False,
                                                   skip_ip_rebind=True))
        await asyncio.sleep(0.15)          # 让 slow 进入 429 退避
        other = cmod.CSQAQClient(CSQAQConfig(api_token="d", request_interval=0.01, retry_times=0))
        t1 = time.monotonic()
        await asyncio.wait_for(other._post("/other", {}, allow_drift_skip=False), timeout=5.0)
        fast_dt = time.monotonic() - t1
        check(f"退避期间其他请求未被阻塞({fast_dt:.2f}s < 1.0)", fast_dt < 1.0)
        await asyncio.wait_for(task_slow, timeout=10.0)
        check(f"退避后重试成功(总耗时 {time.monotonic()-t0:.1f}s ≈ 3s)", 2.0 < time.monotonic() - t0 < 6.0)

        print("\n[5] 401 仍触发重绑;静态语义无回归")
        rebinds["n"] = 0
        drift_body = ('{"code":401,"msg":"请求的地址非法 环境IP:1.2.3.4"}')
        sess5 = FakeSession(seq=[FakeResp(401, drift_body)], delay=0.0)
        wire(sess5)
        api5 = cmod.CSQAQClient(CSQAQConfig(api_token="d", request_interval=0.01, retry_times=0))
        cmod._IP_DRIFT_UNTIL = 0.0
        await api5._post("/d", {}, allow_drift_skip=False)
        check(f"401 触发了一次重绑(实际 {rebinds['n']})", rebinds["n"] == 1)
        cmod._IP_DRIFT_UNTIL = 0.0
        check("401 仍不重试", not cmod.CSQAQClient._is_retryable_status(401))
        check("429 仍重试", cmod.CSQAQClient._is_retryable_status(429))
        check("信号量容量 == 单连接数(1)", cmod._global_sem_obj()._value == cmod._PIN_LIMIT == 1)
    finally:
        cmod.shared_session = orig_shared
        cmod.try_rebind_local_ip = orig_rebind
        cmod._IP_DRIFT_UNTIL = 0.0


if __name__ == "__main__":
    print("=" * 68)
    print("V0.3.10 全局单飞信号量测试")
    print("=" * 68)
    asyncio.run(main())
    print("\n" + "=" * 68)
    print(f"结果: {PASS} 通过 / {FAIL} 失败")
    print("=" * 68)
    sys.exit(1 if FAIL else 0)
