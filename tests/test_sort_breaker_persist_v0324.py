# -*- coding: utf-8 -*-
"""V0.3.24 离线自测:排序熔断状态落盘与重启恢复。

覆盖:落盘字段、重启后仍熔断、失败计数继承(阶梯不倒退)、成功即从文件移除、
过期条目丢弃、文件损坏不影响主流程、原子写入无残留 tmp、手工复位。

用法: .venv\\Scripts\\python.exe -X utf8 test_sort_breaker_persist_v0324.py
"""
import pathlib as _pl, sys as _sys          # tests/_bootstrap:保证能 import bot
_sys.path.insert(0, str(_pl.Path(__file__).resolve().parent.parent))
import json
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from bot.anomaly import runner as R    # noqa: E402

FAILS = []
S = "价格_价格上升(百分比)_近30天"


def ok(name, cond, extra=""):
    if cond:
        print(f"  [PASS] {name}")
    else:
        print(f"  [FAIL] {name}  {extra}")
        FAILS.append(name)


def fresh(path):
    """模拟"全新进程":清内存 + 指向指定状态文件,不自动回读旧文件。"""
    R.SORT_BREAKER_STATE_PATH = path
    R.reset_sort_breaker_state(clear_file=True)


def simulate_restart():
    """模拟重启:内存清空,下次访问时从磁盘恢复。"""
    R._SORT_UNHEALTHY_UNTIL.clear()
    R._SORT_FAILS.clear()
    R._SORT_STATE_LOADED = False


def main():
    with tempfile.TemporaryDirectory() as tmp:
        state = str(Path(tmp) / "sort_breaker.json")

        print("\n[1] 熔断后落盘")
        fresh(state)
        R.mark_sort_unhealthy(S, "(自测)")
        ok("状态文件已生成", Path(state).exists())
        data = json.loads(Path(state).read_text(encoding="utf-8"))
        info = (data.get("sorts") or {}).get(S) or {}
        ok("记录 fails=1", info.get("fails") == 1, info)
        ok("记录墙钟截止时间", float(info.get("until_epoch") or 0) > time.time(), info)
        ok("无残留 tmp 文件", not Path(state + ".tmp").exists())

        print("\n[2] 重启后仍在熔断期(核心诉求)")
        simulate_restart()
        ok("重启后该排序仍不可用", R.sort_healthy(S) is False)
        left = R._SORT_UNHEALTHY_UNTIL.get(S, 0.0) - time.monotonic()
        ok("剩余时间接近 30 分钟", 1700 <= left <= 1805, round(left))

        print("\n[3] 失败计数继承:阶梯不倒退")
        ok("重启后计数仍为 1", R._SORT_FAILS.get(S) == 1, R._SORT_FAILS.get(S))
        R._SORT_UNHEALTHY_UNTIL[S] = time.monotonic() - 1.0     # 伪造到期
        R.mark_sort_unhealthy(S, "(自测 第2次)")
        left2 = R._SORT_UNHEALTHY_UNTIL.get(S, 0.0) - time.monotonic()
        ok("第 2 次失败 → 2 小时", 7100 <= left2 <= 7205, round(left2))
        ok("计数变成 2", R._SORT_FAILS.get(S) == 2, R._SORT_FAILS.get(S))

        print("\n[4] 再重启仍是 2 小时")
        simulate_restart()
        ok("仍不可用", R.sort_healthy(S) is False)
        left3 = R._SORT_UNHEALTHY_UNTIL.get(S, 0.0) - time.monotonic()
        ok("剩余接近 2 小时", 7100 <= left3 <= 7205, round(left3))

        print("\n[5] 成功即从文件移除")
        R.note_sort_ok(S)
        ok("内存已清", R.sort_healthy(S) is True and S not in R._SORT_FAILS)
        after = json.loads(Path(state).read_text(encoding="utf-8"))
        ok("文件里也没有了", S not in (after.get("sorts") or {}), after)
        simulate_restart()
        ok("重启后仍是可用", R.sort_healthy(S) is True)

        print("\n[6] 过期条目丢弃、计数保留")
        Path(state).write_text(json.dumps({"version": 1, "sorts": {
            S: {"fails": 3, "until_epoch": time.time() - 60}}}, ensure_ascii=False),
            encoding="utf-8")
        simulate_restart()
        ok("过期后视为可用", R.sort_healthy(S) is True)
        ok("但计数保留(=3)", R._SORT_FAILS.get(S) == 3, R._SORT_FAILS.get(S))
        R.mark_sort_unhealthy(S, "(自测 第4次)")
        left4 = R._SORT_UNHEALTHY_UNTIL.get(S, 0.0) - time.monotonic()
        ok("第 4 次直接用 6 小时封顶", 21500 <= left4 <= 21605, round(left4))

        print("\n[7] 文件损坏不影响主流程")
        Path(state).write_text("{ 这不是 json", encoding="utf-8")
        simulate_restart()
        ok("损坏时视为全部可用", R.sort_healthy(S) is True)
        R.mark_sort_unhealthy(S, "(自测 重建)")
        ok("还能正常重新落盘", (json.loads(Path(state).read_text(encoding="utf-8"))
                                .get("sorts") or {}).get(S, {}).get("fails") == 1)

        print("\n[8] 手工复位")
        R.reset_sort_breaker_state(clear_file=True)
        ok("文件已删", not Path(state).exists())
        ok("内存已空", R.sort_healthy(S) is True and not R._SORT_FAILS)

    print(f"\n  {len(FAILS)} 个失败" if FAILS else "\n  全部通过 ✓")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
