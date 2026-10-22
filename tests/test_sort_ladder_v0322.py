# -*- coding: utf-8 -*-
"""V0.3.22 离线自测:排序熔断阶梯化(30min → 2h → 6h)+ 成功即恢复。

覆盖:sort_cooldown 纯函数、连续失败计数、阶梯生效、封顶、note_sort_ok 清零、
healthy_sorts / next_healthy_sort 与阶梯的配合。

用法: .venv\\Scripts\\python.exe -X utf8 test_sort_ladder_v0322.py
"""
import pathlib as _pl, sys as _sys          # tests/_bootstrap:保证能 import bot
_sys.path.insert(0, str(_pl.Path(__file__).resolve().parent.parent))
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

FAILS = []


def ok(name, cond, extra=""):
    if cond:
        print(f"  [PASS] {name}")
    else:
        print(f"  [FAIL] {name}  {extra}")
        FAILS.append(name)


def fresh():
    R._SORT_UNHEALTHY_UNTIL.clear()
    R._SORT_FAILS.clear()


def expire(sort):
    """伪造熔断到期(不真等 30 分钟)。"""
    R._SORT_UNHEALTHY_UNTIL[sort] = time.monotonic() - 1.0


def main():
    s = "价格_价格上升(百分比)_近30天"

    print("\n[1] 阶梯纯函数")
    ok("第 1 次 = 30 分钟", R.sort_cooldown(1) == 1800.0, R.sort_cooldown(1))
    ok("第 2 次 = 2 小时", R.sort_cooldown(2) == 7200.0, R.sort_cooldown(2))
    ok("第 3 次 = 6 小时", R.sort_cooldown(3) == 21600.0, R.sort_cooldown(3))
    ok("第 4 次仍封顶 6 小时", R.sort_cooldown(4) == 21600.0, R.sort_cooldown(4))
    ok("第 9 次仍封顶 6 小时", R.sort_cooldown(9) == 21600.0, R.sort_cooldown(9))

    print("\n[2] 连续失败 → 熔断越来越长")
    fresh()
    R.mark_sort_unhealthy(s, "(自测 1)")
    ok("第 1 次计数=1", R._SORT_FAILS[s] == 1, R._SORT_FAILS.get(s))
    ok("第 1 次不可用", R.sort_healthy(s) is False)
    left1 = R._SORT_UNHEALTHY_UNTIL[s] - time.monotonic()
    ok("第 1 次剩 ~30 分钟", 1700 <= left1 <= 1805, round(left1))

    expire(s)
    R.mark_sort_unhealthy(s, "(自测 2)")
    ok("第 2 次计数=2", R._SORT_FAILS[s] == 2, R._SORT_FAILS.get(s))
    left2 = R._SORT_UNHEALTHY_UNTIL[s] - time.monotonic()
    ok("第 2 次剩 ~2 小时", 7100 <= left2 <= 7205, round(left2))

    expire(s)
    R.mark_sort_unhealthy(s, "(自测 3)")
    left3 = R._SORT_UNHEALTHY_UNTIL[s] - time.monotonic()
    ok("第 3 次剩 ~6 小时", 21500 <= left3 <= 21605, round(left3))

    print("\n[3] 熔断期内不重复计时(幂等)")
    until = R._SORT_UNHEALTHY_UNTIL[s]
    R.mark_sort_unhealthy(s, "(自测 再标一次)")
    ok("计时不变", R._SORT_UNHEALTHY_UNTIL[s] == until)
    ok("计数不变", R._SORT_FAILS[s] == 3, R._SORT_FAILS.get(s))

    print("\n[4] 成功即恢复")
    R.note_sort_ok(s)
    ok("失败计数清零", s not in R._SORT_FAILS, R._SORT_FAILS.get(s))
    ok("熔断立即解除", R.sort_healthy(s) is True)
    ok("重新失败从 30 分钟开始", R.sort_cooldown(R._SORT_FAILS.get(s, 0) + 1) == 1800.0)

    print("\n[5] 与轮转配合:长期挂死的排序被跳过")
    fresh()
    a, b, c = "价格_售价_降序", s, "价格_价格下降(百分比)_近30天"
    R.mark_sort_unhealthy(b)
    R.mark_sort_unhealthy(c)
    ok("healthy_sorts 只留 a", R.healthy_sorts([a, b, c]) == [a], R.healthy_sorts([a, b, c]))
    picked, idx = R.next_healthy_sort(1, [a, b, c])      # 从 b 起 → 跳过 b,落到 c? c 也熔断 → 回到 a
    ok("轮转跳过两个熔断项落到 a", picked == a and idx == 4, (picked, idx))

    print(f"\n  {len(FAILS)} 个失败" if FAILS else "\n  全部通过 ✓")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
