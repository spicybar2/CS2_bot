# -*- coding: utf-8 -*-
"""一次性:按今天日志里两个挂死排序的连续失败次数(各 5 次)预置熔断状态,避免上线时再白试一轮。"""
import json
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent      # scripts/ 的上一级才是仓库根
path = ROOT / "data" / "sort_breaker.json"

DEAD = ["价格_价格上升(百分比)_近30天", "价格_价格下降(百分比)_近30天"]
FAILS = 5
HOURS = 6.0

now = time.time()
state = {"version": 1,
         "sorts": {s: {"fails": FAILS, "until_epoch": now + HOURS * 3600} for s in DEAD}}
path.parent.mkdir(parents=True, exist_ok=True)
path.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
print("  已写入", path)
print("  排序:", ", ".join(DEAD))
print(f"  连续失败次数: {FAILS} → 熔断 {HOURS:.0f} 小时, 解禁时间:",
      time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now + HOURS * 3600)))
