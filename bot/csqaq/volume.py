# -*- coding: utf-8 -*-
"""CSQAQ 单品 Steam 成交量字段的归一化与时效判断。"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Dict, Optional


def parse_period_at(value: Any) -> Optional[datetime]:
    """解析 CSQAQ turnover 的所属周期时间。"""
    if not value:
        return None
    text = str(value).strip()
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S",
                "%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(text[:19], fmt)
        except (TypeError, ValueError):
            continue
    return None


def normalize_turnover(data: Optional[Dict[str, Any]],
                       now: Optional[datetime] = None,
                       max_age_hours: float = 48.0) -> Dict[str, Any]:
    """归一化单品详情中的 Steam 成交量。

    ``turnover_number`` 是成交数量，``period_at`` 是该数量所属周期，
    不是详情更新时间。周期缺失或超过 max_age_hours 时标记为过期，
    调用方不得将其作为当前成交量参与量价判断。
    """
    d = data or {}
    raw_num = d.get("turnover_number")
    try:
        number = int(float(raw_num)) if raw_num not in (None, "") else None
    except (TypeError, ValueError):
        number = None
    raw_avg = d.get("turnover_avg_price")
    try:
        avg = float(raw_avg) if raw_avg not in (None, "") else None
    except (TypeError, ValueError):
        avg = None
    period_raw = d.get("period_at")
    period = parse_period_at(period_raw)
    now = now or datetime.now()
    fresh = bool(number is not None and number >= 0 and period is not None
                 and now - timedelta(hours=max_age_hours) <= period <= now + timedelta(hours=6))
    return {
        "turnover_number": number,
        "turnover_avg_price": avg,
        "turnover_period_at": str(period_raw) if period_raw else None,
        "turnover_fresh": fresh,
    }


def turnover_change(current: Any, previous: Any) -> Optional[float]:
    """计算两个成交量周期的百分比变化；基数无效时返回 None。"""
    try:
        cur = float(current)
        prev = float(previous)
    except (TypeError, ValueError):
        return None
    if prev <= 0 or cur < 0:
        return None
    return (cur / prev - 1.0) * 100.0

