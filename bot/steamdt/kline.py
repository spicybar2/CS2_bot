# -*- coding: utf-8 -*-
"""SteamDT 单品 K 线解析与绘图辅助。"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional, Sequence, Tuple

from bot.csqaq.chart import make_trend_png


def normalize_item_kline_rows(raw: Any) -> List[Dict[str, float]]:
    """把 SteamDT K 线响应归一为点序列。"""
    rows: Sequence[Any]
    if isinstance(raw, dict):
        data = raw.get("data")
        if isinstance(data, list):
            rows = data
        elif all(k in raw for k in ("timestamp", "open", "close", "high", "low")):
            rows = list(zip(
                raw.get("timestamp") or [],
                raw.get("open") or [],
                raw.get("close") or [],
                raw.get("high") or [],
                raw.get("low") or [],
            ))
        else:
            rows = []
    else:
        rows = raw or []

    out: List[Dict[str, float]] = []
    for row in rows:
        if isinstance(row, dict):
            ts = row.get("timestamp") or row.get("ts") or row.get("time") or row.get("t")
            op = row.get("open") or row.get("o")
            cl = row.get("close") or row.get("c")
            hi = row.get("high") or row.get("h")
            lo = row.get("low") or row.get("l")
        elif isinstance(row, (list, tuple)) and len(row) >= 5:
            ts, op, cl, hi, lo = row[:5]
        else:
            continue
        try:
            ts_i = int(float(ts))
            if ts_i < 10_000_000_000:
                ts_i *= 1000
            out.append({
                "timestamp": float(ts_i),
                "open": float(op),
                "close": float(cl),
                "high": float(hi),
                "low": float(lo),
            })
        except (TypeError, ValueError):
            continue
    return out


def build_item_kline_labels(
    points: List[Dict[str, float]],
    label_fmt: str = "%m-%d",
    marks: int = 4,
) -> List[Tuple[int, str]]:
    """为长 K 线取少量日期标签。"""
    if not points:
        return []
    idxs = [0, len(points) // 3, (len(points) * 2) // 3, len(points) - 1]
    if marks <= 3:
        idxs = [0, len(points) // 2, len(points) - 1]
    idxs = sorted(set(max(0, min(len(points) - 1, i)) for i in idxs))
    labels: List[Tuple[int, str]] = []
    for i in idxs:
        try:
            ts = points[i]["timestamp"]
            dt = datetime.fromtimestamp(ts / 1000.0)
            labels.append((i, dt.strftime(label_fmt)))
        except Exception:
            continue
    return labels


def make_item_kline_trend_png(
    points: List[Dict[str, float]],
    name: str,
    *,
    source_label: str = "SteamDT 日K",
    label_fmt: str = "%m-%d",
) -> Optional[str]:
    """把 SteamDT 单品 K 线绘成趋势图。"""
    closes = [float(p["close"]) for p in points if float(p.get("close") or 0) > 0]
    if len(closes) < 2:
        return None
    labels = build_item_kline_labels(points, label_fmt=label_fmt)
    title = f"{name}（{source_label}）" if source_label else name
    return make_trend_png(closes, title, closes[-1], labels=labels, synthesized=False)


def _pct(cur: float, base: float) -> Optional[float]:
    if base in (0, None):
        return None
    try:
        return (float(cur) / float(base) - 1.0) * 100.0
    except Exception:
        return None


def summarize_item_kline(points: List[Dict[str, float]]) -> Dict[str, Optional[float]]:
    """按日K摘要 1/7/30 日变化。"""
    closes = [float(p.get("close") or 0) for p in points if float(p.get("close") or 0) > 0]
    out: Dict[str, Optional[float]] = {
        "count": float(len(closes)) if closes else None,
        "latest": closes[-1] if closes else None,
        "pct_1d": None,
        "pct_7d": None,
        "pct_30d": None,
    }
    if len(closes) >= 2:
        out["pct_1d"] = _pct(closes[-1], closes[-2])
    if len(closes) >= 8:
        out["pct_7d"] = _pct(closes[-1], closes[-8])
    if len(closes) >= 31:
        out["pct_30d"] = _pct(closes[-1], closes[-31])
    return out
