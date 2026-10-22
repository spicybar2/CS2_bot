# -*- coding: utf-8 -*-
"""跑全部离线测试:tests/ 下所有 test_*.py,逐个独立进程执行(隔离性好、互不影响)。

用法:
    .venv\\Scripts\\python.exe -X utf8 tests\\run_all.py            # 全跑
    .venv\\Scripts\\python.exe -X utf8 tests\\run_all.py breaker    # 只跑名字含 breaker 的
    .venv\\Scripts\\python.exe -X utf8 tests\\run_all.py -v         # 失败时打印完整输出
"""
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TESTS = Path(__file__).resolve().parent
PY = ROOT / ".venv" / "Scripts" / "python.exe"


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    verbose = "-v" in sys.argv
    keyword = args[0] if args else ""
    files = sorted(p for p in TESTS.glob("test_*.py") if keyword in p.name)
    if not files:
        print("  没有匹配的测试文件")
        return 1

    bad = []
    for f in files:
        proc = subprocess.run([str(PY), "-X", "utf8", str(f)], cwd=str(ROOT),
                              capture_output=True, text=True,
                              encoding="utf-8", errors="replace")
        ok = proc.returncode == 0
        print(("  OK   " if ok else "  FAIL ") + f.name)
        if not ok:
            bad.append(f.name)
            lines = (proc.stdout or "").splitlines()
            for line in lines:
                if "[FAIL]" in line or "Traceback" in line or "Error" in line:
                    print("        " + line.strip()[:160])
            if verbose:
                print("\n".join("        " + l for l in lines[-12:]))
    print(f"\n  共 {len(files)} 个,失败 {len(bad)} 个" + (f": {', '.join(bad)}" if bad else " ✓"))
    return 0 if not bad else 1


if __name__ == "__main__":
    sys.exit(main())
