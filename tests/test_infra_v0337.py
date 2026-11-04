# -*- coding: utf-8 -*-
"""V0.3.37 三项基础设施改动离线自测。

覆盖:
 [1] 日志滚动 —— main.py 用 RotatingFileHandler(20MB × 6),不再是只增不减的 FileHandler
 [2] 保留期配置化 —— config.storage.history_retention_days(默认 45)、SnapshotStore 生效、
     最小 7 天兜底、SQL 用参数绑定而不是硬编码字符串
 [3] 超时不重绑 —— 已在 test_pacing_v039 [3] 覆盖,这里只校验模块级接口存在且语义正确

用法: .venv\\Scripts\\python.exe -X utf8 tests\\test_infra_v0337.py
"""
import pathlib as _pl, sys as _sys          # tests/_bootstrap:保证能 import bot
_sys.path.insert(0, str(_pl.Path(__file__).resolve().parent.parent))
import os
import sys
import tempfile

import yaml

import bot.csqaq.client as C
from bot.config import AppConfig, load_config
from bot.storage.db import SnapshotStore

ROOT = _pl.Path(__file__).resolve().parent.parent
FAILS = []


def check(name, cond, extra=""):
    if cond:
        print(f"    [PASS] {name}")
    else:
        print(f"    [FAIL] {name}  {extra}")
        FAILS.append(name)


def main():
    print("=" * 68)
    print("V0.3.37 日志滚动 / 保留期配置 / 超时不重绑 测试")
    print("=" * 68)

    # ---------- [1] 日志滚动 ----------
    print("\n[1] 日志滚动")
    src = (ROOT / "main.py").read_text(encoding="utf-8")
    check("使用 RotatingFileHandler", "RotatingFileHandler(" in src)
    check("单文件上限 20MB", "maxBytes=20 * 1024 * 1024" in src)
    check("保留 5 份", "backupCount=5" in src)
    check("不再用裸 FileHandler 写 bot.log",
          "logging.FileHandler(" not in src)

    # ---------- [2] 保留期配置化 ----------
    print("\n[2] 保留期配置化")
    check("AppConfig 默认 45 天", AppConfig().history_retention_days == 45,
          AppConfig().history_retention_days)

    tmp = tempfile.mkdtemp()
    cfg_path = os.path.join(tmp, "config.yaml")
    with open(cfg_path, "w", encoding="utf-8") as fh:
        yaml.safe_dump({"csqaq": {"api_token": "x"}, "storage": {"history_retention_days": 60}}, fh)
    check("config 读到 60 天", load_config(cfg_path).history_retention_days == 60,
          load_config(cfg_path).history_retention_days)

    with open(cfg_path, "w", encoding="utf-8") as fh:
        yaml.safe_dump({"csqaq": {"api_token": "x"}}, fh)
    check("缺 storage 段时回退 45", load_config(cfg_path).history_retention_days == 45)

    store = SnapshotStore(os.path.join(tmp, "t.db"), history_retention_days=30)
    check("SnapshotStore 接受参数", store._retention_days == 30, store._retention_days)
    store2 = SnapshotStore(os.path.join(tmp, "t2.db"), history_retention_days=1)
    check("低于 7 天被夹到 7", store2._retention_days == 7, store2._retention_days)
    store3 = SnapshotStore(os.path.join(tmp, "t3.db"))
    check("不传参默认 45", store3._retention_days == 45, store3._retention_days)

    db_src = (ROOT / "bot" / "storage" / "db.py").read_text(encoding="utf-8")
    check("DELETE 用参数绑定", "datetime('now','localtime',?)" in db_src)
    check("不再硬编码 -30 days", "'-30 days'" not in db_src)

    # ---------- [3] 超时不重绑的模块接口 ----------
    print("\n[3] 超时不重绑(接口层)")
    check("note_timeout 存在", callable(getattr(C, "note_timeout", None)))
    check("reset_timeout_streak 存在", callable(getattr(C, "reset_timeout_streak", None)))
    check("阈值 = 5 次", C._TIMEOUT_ROTATE_STREAK == 5, C._TIMEOUT_ROTATE_STREAK)
    cli_src = (ROOT / "bot" / "csqaq" / "client.py").read_text(encoding="utf-8")
    check("超时分支改为累计", "await note_timeout(self._cfg, path)" in cli_src)
    check("超时不再走「重绑后重试」老路径", "超时后重绑成功" not in cli_src)
    check("401 仍保留重绑(真漂移要治)", "if status == 401 and not skip_ip_rebind" in cli_src)
    check("候选全失败不再清空 _PIN_IP",
          "_PIN_IP = None\n    logger.warning(\"[csqaq.pin] 候选全部失败" not in cli_src)

    print("\n" + "=" * 68)
    if FAILS:
        print(f"失败 {len(FAILS)} 个: " + ", ".join(FAILS))
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
