# -*- coding: utf-8 -*-
"""V0.3.38 启动卡显示「¥旧 → ¥现」。

覆盖:
 [1] 卡片渲染 —— 有 price_from 时显示「价格 ¥A → ¥B（+x%）」，无则回退「现价 ¥B」
 [2] 数据链路 —— PhaseSignal.price_from 默认 None；engine 写进 details；runner 写进事件行
 [3] 落库 —— anomaly_events.price_from 可写可读，老库自动补列

用法: .venv\\Scripts\\python.exe -X utf8 tests\\test_launch_price_arrow_v0338.py
"""
import pathlib as _pl, sys as _sys          # tests/_bootstrap:保证能 import bot
_sys.path.insert(0, str(_pl.Path(__file__).resolve().parent.parent))
import os
import sqlite3
import sys
import tempfile

from bot.anomaly import alert, cycle
from bot.storage.db import SnapshotStore

ROOT = _pl.Path(__file__).resolve().parent.parent
FAILS = []


def check(name, cond, extra=""):
    if cond:
        print(f"    [PASS] {name}")
    else:
        print(f"    [FAIL] {name}  {extra}")
        FAILS.append(name)


def ev(**kw):
    r = {"good_id": 1, "name": "AK-47 | 红线 (崭新出厂)", "anomaly_type": "launch",
         "level": "L2", "price": 1285.0, "position": "低位", "reason": "求购价上移 +4.0%",
         "detail": "", "score": 60}
    r.update(kw)
    return r


def main():
    print("=" * 68)
    print("V0.3.38 启动卡「¥旧 → ¥现」测试")
    print("=" * 68)

    print("\n[1] 卡片渲染")
    txt = alert.format_row_alert(ev(price_from=1203.0))
    check("有旧价 → 显示箭头", "价格 ¥1,203 → ¥1,285" in txt, txt.splitlines()[3:5])
    check("带涨幅", "（+6.8%）" in txt, [l for l in txt.splitlines() if "→" in l])
    check("不再写「现价」", "现价" not in txt)

    txt2 = alert.format_row_alert(ev(price_from=0))
    check("无旧价 → 回退现价", "现价 ¥1,285" in txt2)
    check("无旧价 → 没有箭头", "→" not in txt2)

    txt3 = alert.format_row_alert(ev(price_from=1285.0))
    check("旧价=现价 → 回退现价(不显示 +0.0%)", "现价 ¥1,285" in txt3 and "→" not in txt3)

    txt4 = alert.format_row_alert(ev(anomaly_type="accum", price_from=1203.0))
    check("建仓卡不受影响(仍是现价)", "现价 ¥1,285" in txt4,
          [l for l in txt4.splitlines() if "¥" in l][:2])

    print("\n[2] 数据链路")
    check("PhaseSignal 默认 price_from=None", cycle.PhaseSignal(
        phase="accum", title="t", icon="i", floor_score=0).price_from is None)
    check("PhaseSignal 可携带旧价", cycle.PhaseSignal(
        phase="launch", title="t", icon="i", floor_score=0, price_from=1203.0).price_from == 1203.0)
    eng = (ROOT / "bot" / "anomaly" / "engine.py").read_text(encoding="utf-8")
    check("engine 把旧价写进 details 顶层", 'details["price_from"] = phase_sig.price_from' in eng)
    run = (ROOT / "bot" / "anomaly" / "runner.py").read_text(encoding="utf-8")
    check("runner 把旧价写进事件行", '"price_from": float(d.get("price_from") or 0)' in run)
    cyc = (ROOT / "bot" / "anomaly" / "cycle.py").read_text(encoding="utf-8")
    check("cycle 用 60 分钟前售价做旧价", "price_from=(_pf if _pf > 0 else None)" in cyc)

    print("\n[3] 落库")
    tmp = tempfile.mkdtemp()
    dbp = os.path.join(tmp, "t.db")
    store = SnapshotStore(dbp)
    base = {"drop_pct": 0, "abs_drop": 0, "window_min": 0, "sell_prev": 0, "sell_cur": 0,
            "level": "L2", "detail": ""}
    store.insert_anomaly_events([dict(base, **{
        "good_id": 42, "name": "测试品", "anomaly_type": "launch",
        "price": 1285.0, "price_from": 1203.0, "position": "低位", "reason": "r",
    })], ts="2026-11-04 12:00:00")
    conn = sqlite3.connect(dbp)
    row = conn.execute("select price, price_from from anomaly_events").fetchone()
    check("写库后能读回 price_from", row == (1285.0, 1203.0), row)
    cols = [c[1] for c in conn.execute("pragma table_info(anomaly_events)")]
    check("表里有 price_from 列", "price_from" in cols)
    # 缺字段的老调用方仍能写入(走默认值)
    store.insert_anomaly_events([dict(base, **{
        "good_id": 43, "name": "老调用", "anomaly_type": "accum", "price": 10.0,
    })], ts="2026-11-04 12:01:00")
    row2 = conn.execute("select price_from from anomaly_events where good_id=43").fetchone()
    check("不传 price_from 时默认 0", row2 == (0.0,), row2)
    conn.close()

    print("\n[4] 端到端:真实 detect_phase 的启动场景")
    from datetime import datetime, timedelta
    from bot.anomaly.engine import HistoryPoint, MetricSnapshot
    from bot.anomaly.cycle import detect_phase
    from bot.config import AnomalyConfig

    CFG = AnomalyConfig()
    BASE = datetime(2026, 11, 4, 12, 0, 0)
    # 与 test_cycle_phases 的启动用例同构:13 点 ×5 分钟 = 60 分钟窗口,
    # 价格 100.0 → 102.5、求购价 95.0 → 98.5、在售递减
    px = [100.0, 100.2, 100.4, 100.6, 100.8, 101.0, 101.2, 101.5, 101.8, 102.0, 102.1, 102.3, 102.5]
    bp = [95.0, 95.3, 95.6, 95.9, 96.2, 96.5, 96.8, 97.1, 97.4, 97.7, 98.0, 98.2, 98.5]
    sn = [3000, 2995, 2990, 2985, 2980, 2970, 2960, 2955, 2950, 2945, 2940, 2930, 2920]
    h = [HistoryPoint(ts=(BASE - timedelta(minutes=5 * (12 - i))).strftime("%Y-%m-%d %H:%M:%S"),
                      price=px[i], sell_num=sn[i], buy_num=1060, buy_price=bp[i])
         for i in range(13)]
    s = MetricSnapshot(good_id=7, name="测试品", price=102.5, sell_price=102.5,
                       buy_price=98.5, sell_num=2920, buy_num=1060,
                       rate_7=0.0, rate_30=0.0, price_7=100.5, price_30=97.0)
    sig = detect_phase(s, h, BASE, CFG)
    check("触发 launch", sig is not None and sig.phase == "launch",
          sig.phase if sig else None)
    check(f"price_from = 60 分钟前售价(实际 {getattr(sig, 'price_from', None)})",
          sig is not None and abs(float(sig.price_from) - 100.0) < 0.01,
          getattr(sig, "price_from", None))
    if sig:
        card = alert.format_row_alert(ev(price=102.5, price_from=sig.price_from))
        check("小数位自洽:印出来的数与涨幅一致", "（+2.0%）" in card)
        check("卡片渲染出「¥100 → ¥102（+2.0%）」",
              "价格 ¥100 → ¥102（+2.0%）" in card,
              [l for l in card.splitlines() if "→" in l])

    print("\n[5] 运行时:引擎 evaluate 把 price_from 带进 details")
    from bot.anomaly.engine import AnomalyEngine
    engi = AnomalyEngine(CFG)
    r = engi.evaluate(s, h, now=BASE)
    check(f"引擎判定 launch(实际 {r.anomaly_type})", r.anomaly_type == "launch", r.anomaly_type)
    check(f"details['price_from'] = 100.0(实际 {(r.details or {}).get('price_from')})",
          abs(float((r.details or {}).get("price_from") or 0) - 100.0) < 0.01,
          (r.details or {}).get("price_from"))
    check("runner 的取法能读到(顶层同平面)", bool((r.details or {}).get("price_from")))

    print("\n" + "=" * 68)
    if FAILS:
        print(f"失败 {len(FAILS)} 个: " + ", ".join(FAILS))
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
