# -*- coding: utf-8 -*-
"""项目整理脚本(可复用):根目录瘦身 —— 历史脚本/残留一律**移动**归档,不删除。

只移动、不删除:归档目录随时可整体回退,也不受任何删除拦截器影响。
默认 dry-run 只打印计划;加 ``--apply`` 才执行。

用法:
    .venv\\Scripts\\python.exe -X utf8 scripts\\cleanup_project.py           # 看计划
    .venv\\Scripts\\python.exe -X utf8 scripts\\cleanup_project.py --apply   # 执行
"""
import shutil
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STAMP = "2026-10-22"
ARCH = ROOT / "archive" / STAMP

# ---- 常驻工具:留在 tools/ ----
TOOLS = {
    "probe_status.py": "一键巡检:进程存活/日志计数/DB 规模/各池件数",
    "probe_sort_health.py": "榜单排序可用性(挂死排序排查)",
    "probe_live_pin.py": "钉住服务器 IP 的真机连通验证",
    "probe_egress_ip_drift.py": "出口 IP 漂移诊断(401 根因)",
    "probe_watch_price_fields.py": "/关注 价格字段核实(BUFF/悠悠)",
    "probe_watch_render.py": "/关注、我的关注、提醒 的文案渲染预览",
}

# ---- 可再生成 / 无信息量的残留:归档到 junk/ ----
JUNK_GLOBS = [
    "=", "_c.txt", "?_c.txt",          # 占位,实际用字面量处理
]
JUNK_NAMES = [
    "=", "_c.txt", "_cnt2.txt", "_cnt3.txt", "_fd.txt", "_k.txt", "_s.txt", "_tmp.txt",
    "_uc.txt", "_vf.txt", "procs2.txt", "procs5.txt", "run.log", "run_err.log",
    "csqaq.db",                        # 0 字节误建库(真库在 data/)
]
JUNK_PATTERNS = ["botpy.log.2026-*", "*.pyc", "__pycache__", ".pycache_check"]

# ---- 一次性脚本:归档 ----
ROOT_SCRIPTS = [
    "backtest_signals.py", "diag_validate.py", "preview_push_v0312.py",
    "push_broadcast_now.py", "replay_phase_v030.py", "verify_phase_claims.py",
    "verify_steamdt_star_fix.py", "_filter_demo.py", "_ump_check.py", "_verify_filter.py",
]
SCRIPTS = [
    "announce_v001.py", "e2e_steamdt_kline_v001.py", "push_report_once.py",
    "send_free_notice.py", "test_offline.py", "trace_analysis_conclusion.py",
]
LOOSE_FILES = [
    "cs2-qq-bot-deploy.zip", "5166401cf10277b6449be1193b0abdeb.mp4",
    "config.yaml.bak-20260929-1343",
]
DESIGN_DOCS = ["CS2机器人-熟悉项目提示词.md", "CS2饰品市场规律与机器人模型评估.md",
               "日K落库设计_V0.3.4.md"]


def collect():
    plans = []                                     # (src, dst)

    for name, _desc in TOOLS.items():
        p = ROOT / name
        if p.exists():
            plans.append((p, ROOT / "tools" / name))
    for p in sorted(ROOT.glob("probe_*.py")):
        if p.name not in TOOLS:
            plans.append((p, ARCH / "probes" / p.name))

    for name in JUNK_NAMES:
        p = ROOT / name
        if p.exists():
            if name == "csqaq.db" and p.stat().st_size > 0:
                continue                           # 有量的库不动
            plans.append((p, ARCH / "junk" / name))
    for pattern in JUNK_PATTERNS:
        for p in sorted(ROOT.glob(pattern)):
            if ".venv" in p.parts or "archive" in p.parts:
                continue
            plans.append((p, ARCH / "junk" / p.name))

    for name in ROOT_SCRIPTS:
        p = ROOT / name
        if p.exists():
            plans.append((p, ARCH / "root" / name))
    for name in SCRIPTS:
        p = ROOT / "scripts" / name
        if p.exists():
            plans.append((p, ARCH / "scripts" / name))
    for name in LOOSE_FILES:
        p = ROOT / name
        if p.exists():
            plans.append((p, ARCH / "files" / name))
    for name in DESIGN_DOCS:
        p = ROOT / name
        if p.exists():
            plans.append((p, ROOT / "docs" / name))
    return plans


def main() -> int:
    apply = "--apply" in sys.argv
    plans = collect()
    if not plans:
        print("  没有需要整理的东西")
        return 0

    print(f"  {'[执行]' if apply else '[计划]'} 共 {len(plans)} 项 "
          f"(全部为移动,不删除任何文件)\n")
    done, failed = 0, []
    for src, dst in plans:
        rel = src.relative_to(ROOT)
        print(f"    {rel}  ->  {dst.relative_to(ROOT)}")
        if not apply:
            continue
        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
            if dst.exists():                       # 目标已存在:整目录合并
                if src.is_dir():
                    for child in src.iterdir():
                        shutil.move(str(child), str(dst / child.name))
                    src.rmdir()
                else:
                    dst.unlink()
                    shutil.move(str(src), str(dst))
            else:
                shutil.move(str(src), str(dst))
            done += 1
        except OSError as exc:
            failed.append((str(rel), str(exc)[:90]))

    if not apply:
        print("\n  这是 dry-run。确认无误后执行: --apply")
        return 0

    print(f"\n  完成 {done} 项")
    for name, err in failed:
        print(f"    跳过 {name}: {err}")
    print(f"  时间: {time.strftime('%H:%M:%S')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
