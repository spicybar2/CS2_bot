# -*- coding: utf-8 -*-
"""实时查价与走势输出。"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional, Tuple

from bot.csqaq.chart import make_trend_png
from bot.csqaq.resolver import resolve_prices
from bot.steamdt.client import SteamDTClient
from bot.steamdt.kline import make_item_kline_trend_png

logger = logging.getLogger("price_lookup")

PRICE_HINTS = [
    "多少钱一个", "现在多少钱", "多少钱啊", "多少钱", "什么价", "价格",
    "多少钱收", "值多少钱", "价格多少", "价格怎么样", "查价", "报价",
    "卖价", "现价", "市价", "多少钱呢", "多少",
]

WEAR_CN = ["崭新出厂", "略有磨损", "久经沙场", "破损不堪", "战痕累累"]
WEAR_EN = ["Factory New", "Minimal Wear", "Field-Tested", "Well-Worn", "Battle-Scarred"]
WEAR_SHORT = ["FN", "MW", "FT", "WW", "BS"]


def has_wear(text: str) -> bool:
    if not text:
        return False
    upper = text.upper()
    return any(w in text for w in WEAR_CN) or any(w in upper for w in WEAR_EN + WEAR_SHORT)


def extract_keyword(text: str) -> str:
    t = (text or "").strip()
    if not t:
        return ""
    for hint in sorted(PRICE_HINTS, key=len, reverse=True):
        t = t.replace(hint, "")
    t = re.sub(r"^[\s|（）()【】\[\],，。!?？:：]+", "", t)
    t = re.sub(r"[\s,，。!?？:：]+$", "", t)
    t = re.sub(r"^(现在|目前|请问|麻烦|帮我看看|看看|查一查|查查|查一下)\s*", "", t).strip()
    return t.strip()


def ensure_default_wear(keyword: str) -> str:
    kw = (keyword or "").strip()
    if not kw or has_wear(kw):
        return kw
    return f"{kw} (崭新出厂)"


async def fetch_live(api, store, keyword: str) -> Optional[Dict[str, Any]]:
    result = await resolve_prices(api, store, keyword)
    if not result:
        return None
    mhn, row = next(iter(result.items()))
    good_id = int(row.get("goodId") or 0)
    gi: Dict[str, Any] = {}
    if good_id:
        try:
            gi = await api.get_good_info(good_id) or {}
        except Exception as exc:
            logger.warning("[price_lookup] get_good_info(%s) failed: %s", good_id, exc)
    merged: Dict[str, Any] = dict(row)
    merged.update(gi)
    merged.setdefault("_market_hash_name", mhn)
    return merged


def _num(data: Dict[str, Any], *keys: str) -> float:
    for k in keys:
        v = data.get(k)
        if v not in (None, "", 0, "0"):
            try:
                return float(v)
            except (TypeError, ValueError):
                continue
    return 0.0


def _int(data: Dict[str, Any], *keys: str) -> int:
    try:
        return int(_num(data, *keys))
    except (TypeError, ValueError):
        return 0


def format_price_card(data: Dict[str, Any]) -> str:
    name = (
        data.get("name")
        or data.get("market_hash_name")
        or data.get("marketHashName")
        or data.get("_market_hash_name")
        or "未知饰品"
    )
    buff = _num(data, "buffSellPrice", "buff_sell_price")
    buff_num = _int(data, "buffSellNum", "buff_sell_num")
    uu = _num(data, "yyypSellPrice", "yyyp_sell_price")
    uu_num = _int(data, "yyypSellNum", "yyyp_sell_num")
    steam = _num(data, "steamSellPrice", "steam_sell_price")
    steam_num = _int(data, "steamSellNum", "steam_sell_num")
    rate7 = _num(data, "sell_price_rate_7")
    conv = _num(data, "steam_buff_sell_conversion")
    lines = [name]
    if buff:
        lines.append(f"BUFF: ¥{buff:,.2f}" + (f"（在售{buff_num}）" if buff_num else ""))
    if uu:
        lines.append(f"UU: ¥{uu:,.2f}" + (f"（在售{uu_num}）" if uu_num else ""))
    if steam:
        lines.append(f"Steam: ¥{steam:,.2f}" + (f"（在售{steam_num}）" if steam_num else ""))
    if conv:
        lines.append(f"挂刀率 {conv * 100:.1f}%")
    if rate7:
        arrow = "▲" if rate7 >= 0 else "▼"
        lines.append(f"7天 {arrow}{abs(rate7):.1f}%")
    return "\n".join(lines)


def make_sparkline(prices: List[float]) -> str:
    if len(prices) < 2:
        return ""
    bars = "▁▂▃▄▅▆▇█"
    lo, hi = min(prices), max(prices)
    if hi == lo:
        return bars[3] * len(prices)
    span = hi - lo
    out = []
    for p in prices:
        idx = int((p - lo) / span * (len(bars) - 1) + 0.5)
        idx = max(0, min(len(bars) - 1, idx))
        out.append(bars[idx])
    return "".join(out)


def _synthesize_trend(data: Dict[str, Any], current: float) -> List[float]:
    def back(rate: Optional[float]) -> Optional[float]:
        if rate is None or rate <= -100:
            return None
        return current / (1 + rate / 100.0)

    pts = [
        back(_num(data, "sell_price_rate_90")),
        back(_num(data, "sell_price_rate_30")),
        back(_num(data, "sell_price_rate_7")),
        current,
    ]
    return [p for p in pts if p is not None and p > 0]


async def render_trend(
    store,
    good_id: int,
    name: str,
    current: float,
    data: Dict[str, Any],
    steamdt: Optional[SteamDTClient] = None,
) -> Tuple[Optional[str], str]:
    steam_prices: List[float] = []
    steam_name = str(
        data.get("_market_hash_name")
        or data.get("market_hash_name")
        or data.get("marketHashName")
        or name
        or ""
    ).strip()

    if steamdt and steam_name:
        try:
            kline = await steamdt.get_item_kline(steam_name, periods=2)
            points = kline.get("points") or []
            if len(points) >= 2:
                path = make_item_kline_trend_png(points, name or steam_name)
                if path:
                    return path, ""
                steam_prices = [float(p.get("close") or 0) for p in points if float(p.get("close") or 0) > 0]
        except Exception as exc:
            logger.warning("[price_lookup] SteamDT 单品K线失败: %s", exc)

    prices: List[float] = steam_prices
    if good_id:
        try:
            await store.async_add_price_history(good_id, name, current)
            local_prices = await store.async_query_price_history(good_id, days=30)
            if len(local_prices) >= 2 and len(prices) < 2:
                prices = local_prices
        except Exception as exc:
            logger.warning("[price_lookup] 趋势历史读写失败: %s", exc)

    if len(prices) >= 2:
        path = make_trend_png(prices, name, current, synthesized=False)
        if path:
            return path, ""
        return None, f"📈 走势: {make_sparkline(prices)}  ({len(prices)}次记录)"

    synth = _synthesize_trend(data, current)
    if len(synth) >= 2:
        labels = ["90天前", "30天前", "7天前", "现在"][-len(synth):]
        path = make_trend_png(synth, name, current, labels=labels, synthesized=True)
        if path:
            return path, ""
        return None, f"📈 走势(估算): {make_sparkline(synth)}  (查询累计不足)"
    return None, ""


async def answer_price(api, store, text: str, steamdt: Optional[SteamDTClient] = None) -> Tuple[str, Optional[str]]:
    kw = extract_keyword(text)
    if not kw:
        return "没看懂你要查什么，试试: M4A4 Griffin 多少钱", None
    try:
        data = await fetch_live(api, store, ensure_default_wear(kw) if not has_wear(kw) else kw)
        if not data and not has_wear(kw):
            data = await fetch_live(api, store, kw)
    except Exception as exc:
        logger.exception("[price_lookup] 查价失败 %s: %s", kw, exc)
        return "机器人维护中，请稍后再试", None
    if not data:
        return f"CSQAQ 实时查询未找到 {kw}，确认下名字或去 csqaq.com 搜索", None

    card = format_price_card(data)
    try:
        good_id = int(data.get("goodId") or data.get("good_id") or 0)
        name = str(data.get("name") or data.get("_market_hash_name") or "")
        current = _num(data, "buffSellPrice", "buff_sell_price")
        chart_path, trend = await render_trend(store, good_id, name, current, data, steamdt=steamdt)
    except Exception as exc:
        logger.warning("[price_lookup] 走势生成失败: %s", exc)
        chart_path, trend = None, ""
    if chart_path:
        return card, chart_path
    return card + ("\n" + trend if trend else ""), None
