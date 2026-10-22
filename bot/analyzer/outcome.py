# -*- coding: utf-8 -*-
"""信号结果闭环:把异动事件之后的真实价格变动结算成前瞻收益(V0.2.29)。

动机
----
评分、质量闸门、推送策略全部是"事前"判断,系统此前从不记录"事后"结果 ——
于是「建仓/启动信号到底有没有前瞻性」只能靠争论,无法用数据回答。
本模块只做一件事:给定事件与其后的真实价格序列,算出 T+1d/3d/7d 收益。

口径(严格遵守项目「数据边界铁律」)
--------------------------------
- 基准价 = 事件落库时的 `price`(A1 口径:榜单最低**在售价**= 要价,不是成交价);
- 前瞻价 = 目标时刻 ±`tol_hours` 内**最近的一条真实落库快照**(BUFF 售价);
- 取不到就写 None —— 不插值、不换平台、不用别的时点价格冒充;
- 收益 = (前瞻价/基准价 - 1) × 100,衡量的是**要价变动**,
  不等于可实现收益(未扣平台手续费、未计滑点与成交难度)。汇总文案必须按此表述。

统计诚实性
----------
前瞻收益只回答"价格之后怎么走",不回答"你能不能按这个价卖出"。单一行情区间
(如 2026-09 的单边下行)样本不足以证明策略有效或无效,结论只能写
"未观察到正向优势",不得写成"已证明无效"。
"""

import logging
from datetime import datetime, timedelta
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

logger = logging.getLogger("analyzer")

# 前瞻窗口:小时 → 列名
HORIZONS: Tuple[Tuple[int, str], ...] = ((24, "fwd_24h"), (72, "fwd_72h"), (168, "fwd_168h"))
# 目标时刻的取价容差(小时):快照稀疏时允许向前后各取这么久内的最近一条
DEFAULT_TOL_HOURS = 6.0


def _parse_ts(ts: Any) -> Optional[datetime]:
    if isinstance(ts, datetime):
        return ts
    if not ts:
        return None
    text = str(ts).strip().replace("T", " ")
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(text[:19], fmt)
        except ValueError:
            continue
    return None


def pick_forward_price(series: Sequence[Tuple[str, float]], target: datetime,
                       tol_hours: float = DEFAULT_TOL_HOURS) -> Optional[float]:
    """取目标时刻前后容差内最近的一条真实快照价;无则 None(绝不插值)。"""
    tol = timedelta(hours=tol_hours)
    best: Optional[Tuple[float, float]] = None   # (|Δt| 秒, price)
    for ts, price in series:
        t = _parse_ts(ts)
        if t is None or not price or price <= 0:
            continue
        delta = abs((t - target).total_seconds())
        if delta > tol.total_seconds():
            continue
        if best is None or delta < best[0]:
            best = (delta, float(price))
    return best[1] if best else None


def settle_event(event: Dict[str, Any], series: Sequence[Tuple[str, float]],
                 tol_hours: float = DEFAULT_TOL_HOURS) -> Dict[str, Any]:
    """结算单个事件 → signal_outcomes 行(缺失窗口写 None)。"""
    base = float(event.get("price") or 0)
    ts = _parse_ts(event.get("ts"))
    row: Dict[str, Any] = {
        "event_id": int(event["id"] if "id" in event else event["event_id"]),
        "good_id": int(event.get("good_id") or 0),
        "name": event.get("name") or "",
        "anomaly_type": event.get("anomaly_type") or "",
        "level": event.get("level") or "",
        "score": float(event.get("score") or 0),
        "event_ts": str(event.get("ts") or ""),
        "base_price": base,
    }
    for col in ("fwd_24h", "fwd_72h", "fwd_168h"):
        row[col] = None
    if base <= 0 or ts is None:
        return row
    for hours, col in HORIZONS:
        fwd = pick_forward_price(series, ts + timedelta(hours=hours), tol_hours)
        if fwd is not None:
            row[col] = round((fwd / base - 1.0) * 100.0, 2)
    return row


def _median(values: List[float]) -> Optional[float]:
    if not values:
        return None
    vals = sorted(values)
    n = len(vals)
    mid = n // 2
    return vals[mid] if n % 2 else (vals[mid - 1] + vals[mid]) / 2.0


def summarize(rows: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
    """按异动类型汇总:各前瞻窗口的样本数/中位收益/胜率。

    胜率 = 收益 > 0 的占比;样本 <5 时只报中位数不报胜率(避免 1/1=100% 误导)。
    """
    buckets: Dict[str, List[Dict[str, Any]]] = {}
    for r in rows:
        buckets.setdefault(r.get("anomaly_type") or "unknown", []).append(r)
    out: Dict[str, Any] = {}
    for atype, items in sorted(buckets.items()):
        stat: Dict[str, Any] = {"n": len(items)}
        for _, col in HORIZONS:
            vals = [float(r[col]) for r in items if r.get(col) is not None]
            stat[col] = {
                "n": len(vals),
                "median": (round(_median(vals), 2) if vals else None),
                "win_rate": (round(sum(1 for v in vals if v > 0) / len(vals) * 100, 1)
                             if len(vals) >= 5 else None),
            }
        out[atype] = stat
    return out


def format_summary(summary: Dict[str, Any], top: int = 12) -> str:
    """控制台可读汇总(按 168h 样本数降序)。"""
    lines = ["异动信号前瞻收益(要价口径,未扣手续费/滑点)",
             f"{'类型':<12}{'样本':>5}{'24h中位':>9}{'24h胜率':>8}"
             f"{'72h中位':>9}{'72h胜率':>8}{'168h中位':>10}{'168h胜率':>9}"]
    def _fmt(v, suffix="%"):
        return "—" if v is None else f"{v:+.2f}{suffix}"
    ranked = sorted(summary.items(), key=lambda kv: -(kv[1]["fwd_168h"]["n"]))
    for atype, stat in ranked[:top]:
        s24, s72, s168 = stat["fwd_24h"], stat["fwd_72h"], stat["fwd_168h"]
        lines.append(
            f"{atype:<12}{stat['n']:>5}{_fmt(s24['median']):>9}{_fmt(s24['win_rate']):>8}"
            f"{_fmt(s72['median']):>9}{_fmt(s72['win_rate']):>8}"
            f"{_fmt(s168['median']):>10}{_fmt(s168['win_rate']):>9}"
        )
    if len(summary) > top:
        lines.append(f"... 其余 {len(summary) - top} 类略(共 {len(summary)} 类)")
    lines.append("注:单一行情区间样本不足以证明有效/无效,只回答「是否观察到正向优势」。")
    return "\n".join(lines)
