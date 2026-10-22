# -*- coding: utf-8 -*-
"""V0.3.20 离线自测:排序级熔断 + "只有真 401 才算漂移"。

覆盖:熔断标记/查询/过滤/到期自动恢复、B 深翻轮转跳过熔断排序、
客户端在"超时不标记漂移、401 标记漂移"上的行为。

用法: .venv\\Scripts\\python.exe -X utf8 test_sort_breaker_v0320.py
"""
import pathlib as _pl, sys as _sys          # tests/_bootstrap:保证能 import bot
_sys.path.insert(0, str(_pl.Path(__file__).resolve().parent.parent))
import asyncio
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from bot.anomaly import runner as R                                  # noqa: E402
# V0.3.24:熔断状态会落盘 —— 测试必须隔离到临时文件,绝不能写真实 data/sort_breaker.json
import tempfile as _tempfile
R.SORT_BREAKER_STATE_PATH = str(Path(_tempfile.gettempdir()) / "test_sort_breaker_state.json")
R.reset_sort_breaker_state(clear_file=True)
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


def fresh():
    R._SORT_UNHEALTHY_UNTIL.clear()
    R._SORT_FAILS.clear()          # V0.3.22:阶梯熔断新增连续失败计数


def main():
    print("\n[1] 熔断基础行为")
    fresh()
    s1, s2, s3 = "价格_售价_降序", "价格_价格下降(百分比)_近30天", "价格_价格上升(百分比)_近30天"
    ok("默认全部健康", all(R.sort_healthy(x) for x in (s1, s2, s3)))
    R.mark_sort_unhealthy(s2, "(自测)")
    ok("被标记的排序不健康", R.sort_healthy(s2) is False)
    ok("其它排序不受影响", R.sort_healthy(s1) is True and R.sort_healthy(s3) is True)
    ok("healthy_sorts 过滤", R.healthy_sorts([s1, s2, s3]) == [s1, s3],
       R.healthy_sorts([s1, s2, s3]))
    # 重复标记不应延长/重置(幂等)
    until_before = R._SORT_UNHEALTHY_UNTIL[s2]
    R.mark_sort_unhealthy(s2, "(自测·第二次)")
    ok("重复标记幂等(不重置计时)", R._SORT_UNHEALTHY_UNTIL[s2] == until_before)

    print("\n[2] 到期自动恢复")
    fresh()
    old = R.SORT_UNHEALTHY_COOLDOWN
    R.SORT_UNHEALTHY_COOLDOWN = 0.05
    R.mark_sort_unhealthy(s2)
    ok("刚标记时不可用", R.sort_healthy(s2) is False)
    time.sleep(0.12)
    ok("熔断到期后自动恢复", R.sort_healthy(s2) is True)
    R.SORT_UNHEALTHY_COOLDOWN = old

    print("\n[3] B 深翻轮转跳过熔断排序")
    fresh()
    sorts = [s1, s2, s3]
    R.mark_sort_unhealthy(s2)
    # 从 0 起:s1 健康,直接拿 s1
    picked0, idx0 = R.next_healthy_sort(0, sorts)
    ok("从 0 起拿到健康的 s1", picked0 == s1 and idx0 == 1, (picked0, idx0))
    # 从 1 起(轮到 s2,已熔断)→ 顺延到 s3
    picked, idx = R.next_healthy_sort(1, sorts)
    ok("轮到熔断的 s2 时顺延到 s3", picked == s3 and idx == 3, (picked, idx))
    picked2, idx2 = R.next_healthy_sort(3, sorts)      # 3 % 3 = 0 → s1
    ok("idx 继续轮转回到 s1", picked2 == s1 and idx2 == 4, (picked2, idx2))
    R.mark_sort_unhealthy(s1)
    R.mark_sort_unhealthy(s3)
    picked3, _ = R.next_healthy_sort(0, sorts)
    ok("全部熔断 → 返回 None", picked3 is None, picked3)
    fresh()

    print("\n[4] 客户端:超时不标记漂移、401 才标记")
    cq._IP_REBIND_UNTIL = 0.0
    cq._IP_DRIFT_UNTIL = 0.0
    cq._IP_REBIND_FAILS = 0
    cq._TIMEOUT_REBIND_LAST = 0.0
    cq._IP_LAST_DRIFT_IP = None

    class _Spy:
        def __init__(self):
            self.calls = 0

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def bind_local_ip(self):
            self.calls += 1
            return False, "绑定失败(自测)"

        async def probe_ip_status(self, **kw):
            return False, None

    spy = _Spy()
    cq.CSQAQClient = (lambda cfg: spy)                  # type: ignore[assignment]
    # 超时触发的重绑:ip_hint 为空
    run(cq.try_rebind_local_ip(CSQAQConfig(api_token="t"), ip_hint=None,
                               source="/info/get_rank_list", bypass_cooldown=True))
    ok("超时重绑:绑定确实被调用", spy.calls == 1, spy.calls)
    ok("超时重绑:不标记漂移冷却", cq._IP_DRIFT_UNTIL == 0.0, cq._IP_DRIFT_UNTIL)

    # 401 触发的重绑:ip_hint 有值
    cq._IP_REBIND_UNTIL = 0.0
    cq._TIMEOUT_REBIND_LAST = 0.0
    spy.calls = 0
    run(cq.try_rebind_local_ip(CSQAQConfig(api_token="t"), ip_hint="14.145.44.39",
                               source="/current_data", bypass_cooldown=True))
    ok("401 重绑:标记了漂移冷却", cq._IP_DRIFT_UNTIL > time.monotonic(), cq._IP_DRIFT_UNTIL)
    ok("401 重绑:记下了漂移 IP", cq._IP_LAST_DRIFT_IP == "14.145.44.39", cq._IP_LAST_DRIFT_IP)

    print(f"\n  {len(FAILS)} 个失败" if FAILS else "\n  全部通过 ✓")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
