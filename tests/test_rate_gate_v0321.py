# -*- coding: utf-8 -*-
"""V0.3.21 离线自测:全局限流下沉到 client + 429 全局退避(打破自锁)。

覆盖:令牌桶按 max_req_per_min 放行、启动不爆冲、429 指数惩罚/成功清零、
惩罚期内不放行、rate_stats 统计、_throttle 真的走限流。

用法: .venv\\Scripts\\python.exe -X utf8 test_rate_gate_v0321.py
"""
import pathlib as _pl, sys as _sys          # tests/_bootstrap:保证能 import bot
_sys.path.insert(0, str(_pl.Path(__file__).resolve().parent.parent))
import asyncio
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from bot.config import CSQAQConfig                                  # noqa: E402
from bot.csqaq import client as cq                                  # noqa: E402

FAILS = []


def ok(name, cond, extra=""):
    if cond:
        print(f"  [PASS] {name}")
    else:
        print(f"  [FAIL] {name}  {extra}")
        FAILS.append(name)


def run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def reset(rpm=10.0, tokens=2.0):
    cq._RATE_RPM = rpm
    cq._RATE_TOKENS = tokens
    cq._RATE_UPDATED = time.monotonic()
    cq._RATE_PENALTY = 0.0
    cq._RATE_PENALTY_UNTIL = 0.0
    cq._RATE_429_TOTAL = 0
    cq._RATE_REQ_TS.clear()
    cq._RATE_429_TS.clear()


def main():
    cfg = CSQAQConfig(api_token="t")

    print("\n[1] 令牌桶按预算放行(rpm=600 → 10 次/秒)")
    reset(rpm=600.0, tokens=1.0)
    t0 = time.monotonic()
    for _ in range(25):
        run(cq._rate_acquire(cfg))
    el = time.monotonic() - t0
    # 25 个令牌,首 1 个立即,其余 24 个按 0.1s/个 → 约 2.3s
    ok("25 次请求被压到 ~2.3 秒以上(不是瞬发)", el >= 2.0, f"{el:.2f}s")
    ok("速率≈预算(600/分 → 25 次约 2.4s)", el <= 4.0, f"{el:.2f}s")
    ok("rate_stats 记到 25 次", cq.rate_stats()["rpm_60s"] >= 20, cq.rate_stats())

    print("\n[2] 启动不爆冲(默认只给 2 个令牌)")
    reset()
    stats = cq.rate_stats()
    ok("预算=10(实测安全线)", stats["budget"] == 10.0, stats)
    t0 = time.monotonic()
    run(cq._rate_acquire(cfg))
    run(cq._rate_acquire(cfg))
    el2 = time.monotonic() - t0
    ok("前 2 个令牌立即放行(启动不卡)", el2 < 1.0, f"{el2:.2f}s")
    t0 = time.monotonic()
    run(cq._rate_acquire(cfg))
    el3 = time.monotonic() - t0
    ok("第 3 个要等补令牌(~6s @10/分)", el3 >= 2.0, f"{el3:.2f}s")

    print("\n[3] 429 全局惩罚:指数增长 + 拦所有人")
    reset(rpm=6000.0, tokens=100.0)
    cq.note_rate_limited()
    ok("第一次惩罚 = 10 秒", cq._RATE_PENALTY == 10.0, cq._RATE_PENALTY)
    ok("惩罚期已生效", cq._RATE_PENALTY_UNTIL > time.monotonic())
    cq.note_rate_limited()
    ok("第二次翻倍 = 20 秒", cq._RATE_PENALTY == 20.0, cq._RATE_PENALTY)
    for _ in range(6):
        cq.note_rate_limited()
    ok("封顶 = 120 秒", cq._RATE_PENALTY == 120.0, cq._RATE_PENALTY)
    ok("429 计数=8", cq.rate_stats()["http429_total"] == 8, cq.rate_stats())
    # 惩罚期内即使令牌充足也必须等
    cq._RATE_PENALTY_UNTIL = time.monotonic() + 0.3
    cq._RATE_PENALTY = 0.3
    cq._RATE_TOKENS = 100.0
    t0 = time.monotonic()
    run(cq._rate_acquire(cfg))
    el = time.monotonic() - t0
    ok("惩罚期内不放行(等到期)", el >= 0.2, f"{el:.2f}s")

    print("\n[4] 成功清零惩罚")
    cq.note_rate_ok()
    ok("惩罚归零", cq._RATE_PENALTY == 0.0)
    ok("rate_stats 惩罚=0", cq.rate_stats()["penalty_s"] == 0.0)

    print("\n[5] _throttle 真的走限流")
    reset(rpm=6000.0, tokens=50.0)
    cli = cq.CSQAQClient(cfg)

    async def thrice():
        for _ in range(3):
            await cli._throttle()
    t0 = time.monotonic()
    run(thrice())
    el = time.monotonic() - t0
    ok("3 次 _throttle 各占一个令牌", el >= 0.1, f"{el:.2f}s")
    ok("3 次都记进了 60 秒窗口", cq.rate_stats()["rpm_60s"] == 3, cq.rate_stats())

    print(f"\n  {len(FAILS)} 个失败" if FAILS else "\n  全部通过 ✓")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
