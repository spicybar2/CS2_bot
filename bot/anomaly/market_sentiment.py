# -*- coding: utf-8 -*-
"""市场情绪归档(V0.3.17):把 CSQAQ /current_data 回包里的情绪类字段按自然日存一行。

背景(2026-10-14 老大选定「做 D」):
  大盘卡目前只用了一个静态「市场热度」词(中等/高/低),而同一个回包里其实还带着
  `greedy`(67 点热度历史序列,含同日重复点)与 `rate_data`(同一批约 2.6 万件商品在
  1/7/15/30/90/180 天窗口下的涨跌家数——各窗口三态之和**恒等 26,090**,实测已核)。
  这些不存下来就永远答不了这类问题:
      「热度 ≥70 的那几天推的建仓信号,T+24h 胜率是不是更低?」
  ——与 signal_outcomes(fwd_24h/72h/168h)按日期 join 即可。

每天一行(day 为主键),同日重复采样**覆盖更新**(只保留当天最后一次)。
⚠️ 历史无法回补:接口只给 67 个热度点、且不含涨跌分布历史,所以只能从建档当天开始积累。
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any, Dict, Optional

logger = logging.getLogger("bot.anomaly.market_sentiment")


def latest_heat_value(greedy: Any) -> Optional[float]:
    """取 greedy 序列的最新值。同一日期可能有 2 个点(盘中更新)→ 取当天最后一个。"""
    if not isinstance(greedy, list) or not greedy:
        return None
    best_day, best_val = "", None
    for item in greedy:
        try:
            day, val = str(item[0]), float(item[1])
        except (TypeError, IndexError, ValueError):
            continue
        if day >= best_day:
            best_day, best_val = day, val
    return best_val


def _int_or_none(raw: Any) -> Optional[int]:
    if raw is None:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def sentiment_row(data: Dict[str, Any],
                  now: Optional[datetime] = None) -> Optional[Dict[str, Any]]:
    """从 /current_data 回包抽出要归档的一行;回包没有情绪/指数数据时返回 None。"""
    d = (data or {}).get("data") or {}
    if not d:
        return None
    gs = d.get("greedy_status") or {}
    rd = d.get("rate_data") or {}
    subs = d.get("sub_index_data") or []
    if not (gs or rd or subs):
        return None
    main = next((s for s in subs if s.get("name") == "饰品指数"), None) or {}
    index_chg: Optional[float] = None
    if main.get("chg_rate") is not None:
        try:
            index_chg = float(main.get("chg_rate"))
        except (TypeError, ValueError):
            index_chg = None
    snap = [{"name": s.get("name"),
             "market_index": s.get("market_index"),
             "chg_rate": s.get("chg_rate")}
            for s in subs if isinstance(s, dict)]
    now = now or datetime.now()
    return {
        "day": now.strftime("%Y-%m-%d"),
        "updated_at": now.strftime("%Y-%m-%d %H:%M:%S"),
        "heat_label": gs.get("label"),
        "heat_level": gs.get("level"),
        "heat_value": latest_heat_value(d.get("greedy")),
        "index_chg": index_chg,
        "up_1": _int_or_none(rd.get("count_positive_1")),
        "down_1": _int_or_none(rd.get("count_negative_1")),
        "flat_1": _int_or_none(rd.get("count_zero_1")),
        "up_7": _int_or_none(rd.get("count_positive_7")),
        "down_7": _int_or_none(rd.get("count_negative_7")),
        "flat_7": _int_or_none(rd.get("count_zero_7")),
        "up_30": _int_or_none(rd.get("count_positive_30")),
        "down_30": _int_or_none(rd.get("count_negative_30")),
        "flat_30": _int_or_none(rd.get("count_zero_30")),
        "index_json": json.dumps(snap, ensure_ascii=False) if snap else None,
    }


async def archive_sentiment(store: Any, data: Dict[str, Any]) -> bool:
    """按自然日归档市场情绪。

    任何异常都吞掉并只记 debug —— 归档是附带动作,绝不能让播报/推送失败。
    :param store: SnapshotStore(需要 async_upsert_market_sentiment);None 直接跳过。
    """
    if store is None:
        return False
    try:
        row = sentiment_row(data)
        if not row:
            return False
        await store.async_upsert_market_sentiment(row)
        return True
    except Exception as exc:  # noqa: BLE001 归档失败不影响主流程
        logger.debug("[sentiment] 归档失败(不影响播报): %s", exc)
        return False
