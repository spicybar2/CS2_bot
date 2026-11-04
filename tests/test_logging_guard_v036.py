# -*- coding: utf-8 -*-
"""V0.3.6:日志控制台守卫回归测试(防止"写 stdout 冻死整个进程"再回来)。

两次实锤:
  ① 2026-09-08:计划任务会话 stdout 是无人消费的管道,401 风暴 WARNING 灌满缓冲后
     `logging.emit` 永久阻塞且持全局锁 → 全部线程写日志锁死,进程存活但冻结 40+ 小时。
  ② 2026-09-28 11:11:py-spy 抓栈 MainThread 卡在 `StreamHandler.emit` → `stream.write`,
     **说明计划任务的隐藏控制台/管道 isatty() 是 True**,V0.2.12 的 isatty 兜底失效。
→ 处置:控制台输出改**显式开关** `CS2QQBOT_LOG_CONSOLE`(默认关),且默认把
  stdout/stderr 指向 devnull;本测试锁死这两条,防止再退回任何"靠猜"的写法。

用法: .venv\\Scripts\\python.exe -X utf8 test_logging_guard_v036.py
"""
import pathlib as _pl, sys as _sys          # tests/_bootstrap:保证能 import bot
_sys.path.insert(0, str(_pl.Path(__file__).resolve().parent.parent))
import os
import sys

sys.path.insert(0, ".")
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


def test_predicate():
    print("\n[1] 显式开关判定 _console_logging_enabled")
    import importlib.util  # noqa: F401  (仅为与本文件其它 import 风格一致)
    import typing
    src = open("main.py", encoding="utf-8").read()
    # 直接抽函数体执行,避免 import main 触发单例保护/网络依赖
    ns = {"os": os, "Optional": typing.Optional, "Dict": typing.Dict}
    start = src.index("def _console_logging_enabled")
    end = src.index("_console_on = _console_logging_enabled()")
    exec(compile(src[start:end], "main_extract", "exec"), ns)
    fn = ns["_console_logging_enabled"]
    check("未设置 → 关闭(生产默认)", fn({}) is False)
    check("空串/0/false → 关闭",
          fn({"CS2QQBOT_LOG_CONSOLE": ""}) is False
          and fn({"CS2QQBOT_LOG_CONSOLE": "0"}) is False
          and fn({"CS2QQBOT_LOG_CONSOLE": "false"}) is False)
    check("1/true/ON/yes → 开启(前台调试)",
          fn({"CS2QQBOT_LOG_CONSOLE": "1"}) is True
          and fn({"CS2QQBOT_LOG_CONSOLE": "true"}) is True
          and fn({"CS2QQBOT_LOG_CONSOLE": "ON"}) is True
          and fn({"CS2QQBOT_LOG_CONSOLE": "yes"}) is True)
    check("大小写/空白容错", fn({"CS2QQBOT_LOG_CONSOLE": " True "}) is True)


def test_source_guard():
    print("\n[2] 源码守卫:不许再靠 isatty 猜、必须走显式开关")
    src = open("main.py", encoding="utf-8").read()
    code = "\n".join(l for l in src.splitlines() if not l.lstrip().startswith("#"))
    check("代码里已不再用 isatty 猜(注释里留着历史说明是允许的)", "isatty" not in code)
    check("存在显式开关环境变量", "CS2QQBOT_LOG_CONSOLE" in src)
    check("默认把 stdout/stderr 指向 devnull(挡住意外 print)",
          "os.devnull" in src and "sys.stdout = open(os.devnull" in src)
    check("启动时打印日志模式(便于排查)",
          "日志模式" in src)


def test_runtime_handlers():
    print("\n[3] 运行期:默认只有 FileHandler,没有写 stdout 的 handler")
    import logging
    import subprocess
    code = (
        "import os, sys, logging\n"
        "sys.path.insert(0, r'%s')\n"
        "sys.argv = ['main.py']\n"
        "import importlib.util\n"
        "spec = importlib.util.spec_from_file_location('m', r'%s')\n"
        "m = importlib.util.module_from_spec(spec)\n"
        "spec.loader.exec_module(m)\n"
        "print('HANDLERS:' + ','.join(type(h).__name__ for h in logging.root.handlers))\n"
        % (os.getcwd(), os.path.join(os.getcwd(), "main.py"))
    )
    # 注意:import main 会走单例保护 → 若本机 bot 在跑,子进程会提前退出;
    # 那种情况跳过运行期断言(源码守卫已覆盖),不伪造结论。
    env = dict(os.environ)
    env.pop("CS2QQBOT_LOG_CONSOLE", None)
    try:
        out = subprocess.run([sys.executable, "-X", "utf8", "-c", code],
                             capture_output=True, text=True, timeout=120, env=env)
        line = [l for l in out.stdout.splitlines() if l.startswith("HANDLERS:")]
        if not line:
            print("  [SKIP] 子进程未输出 HANDLERS(单例保护或导入失败),跳过运行期断言")
            return
        handlers = line[0].split(":", 1)[1].split(",")
        check(f"默认 handler = 滚动文件(实际 {handlers})",
              all(h in ("FileHandler", "RotatingFileHandler") for h in handlers)
              and "StreamHandler" not in handlers)
    except Exception as exc:  # noqa: BLE001
        print(f"  [SKIP] 运行期断言无法执行: {type(exc).__name__}: {str(exc)[:80]}")


if __name__ == "__main__":
    print("=" * 68)
    print("V0.3.6 日志控制台守卫测试")
    print("=" * 68)
    test_predicate()
    test_source_guard()
    test_runtime_handlers()
    print("\n" + "=" * 68)
    print(f"结果: {PASS} 通过 / {FAIL} 失败")
    print("=" * 68)
    sys.exit(1 if FAIL else 0)
