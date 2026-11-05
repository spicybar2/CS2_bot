# -*- coding: utf-8 -*-
"""QQ 机器人客户端：接收群聊 @消息，路由指令并回复。"""

import asyncio
import datetime
import hashlib
import logging
import os
import re
from typing import Any, Dict, List, Optional, Tuple, Union

import aiohttp
import botpy
from botpy import logging as botpy_logging
from botpy.message import GroupMessage

from bot.analyzer.recommender import Recommender
from bot.analyzer.price_action import analyze_price_action
from bot.anomaly.alert import fetch_market_snapshot   # V0.3.2:/分析 复用推送同源的大盘环境
from bot.anomaly.market_sentiment import archive_sentiment   # V0.3.17 情绪按日归档
from bot.config import AppConfig
from bot.csqaq.client import CSQAQClient
from bot.csqaq.resolver import resolve_prices
from bot.csqaq.market_counts import (aggregate_buff_yyyp_prices, aggregate_buff_yyyp_sell,
                                     aggregate_market_counts, book_split_line,
                                     cross_spread_pct, dual_price_columns, normalize_book,
                                     watch_price_pair, watch_primary_price)
from bot.csqaq.skins import zh_to_en_candidates
from bot.llm.agent import CS2Agent
from bot.storage.db import SnapshotStore
from bot.qqbot import commands as cmds
from bot.steamdt.client import SteamDTClient, SteamDTRateLimit
from bot.steamdt.kline import make_item_kline_trend_png, summarize_item_kline
from bot.csqaq.chart import make_trend_png

# 自然语言盯盘触发词:以这些开头视为"关注/盯盘"指令(不走 AI,保证确定性)
# 注意"盯着"必须排在"盯"前,否则"着"会被残留
_WATCH_RE = re.compile(r"^(?:请|帮我|给我)?(?:关注|盯着|看着|盯)\s*(.*)$")

logger = logging.getLogger("bot.qqbot")   # bot.* 前缀:命中 main.py _BizOnlyFilter,群消息/指令日志进 bot.log
_log = botpy_logging.get_logger()


def _safe_remove(path: str) -> None:
    """安全删除临时走势图文件(忽略异常)。"""
    try:
        if path and os.path.exists(path):
            os.remove(path)
    except Exception:
        pass


def _mid_trend_line(points: List[Dict[str, Any]], price: float) -> Optional[str]:
    """由 SteamDT 日K 生成「中期结构」参考行(T+7 持有视角核心参照)。

    用真实逐日收盘:20/60 日均线方向 + 现价在近 20 日区间的位置分位。
    7 天持有者真正能依赖的是日K级结构(均线/区间位置),而非 30m 盘口。
    返回 None(数据不足/异常)时调用方跳过该行,不影响整体分析。
    """
    try:
        closes = [float(p.get("close") or 0) for p in (points or [])
                  if float(p.get("close") or 0) > 0]
        if len(closes) < 20 or price <= 0:
            return None
        seg = closes[-20:]
        ma20 = sum(seg) / len(seg)
        lo, hi = min(seg), max(seg)
        pos = (price - lo) / (hi - lo) * 100.0 if hi > lo else 50.0
        parts = [f"20日线 ¥{ma20:,.0f}({'上' if price >= ma20 else '下'})"]
        if len(closes) >= 60:
            ma60 = sum(closes[-60:]) / 60.0
            parts.append(f"60日线 ¥{ma60:,.0f}({'上' if price >= ma60 else '下'})")
        parts.append(f"近20日区间位置 {pos:.0f}%")
        return "中期结构: " + " / ".join(parts)
    except Exception:
        return None


def _mid_term_view(points: List[Dict[str, Any]], price: float,
                   rate_7: Optional[float], rate_30: Optional[float],
                   context: str = "") -> Dict[str, Any]:
    """生成 `/分析` 的中期结构数据；无数据时显式不可判，不用“中位”兜底。"""
    closes = [float(p.get("close") or 0) for p in (points or [])
              if float(p.get("close") or 0) > 0]
    view: Dict[str, Any] = {
        "context": context or "中期数据不足",
        "rate_7": rate_7,
        "rate_30": rate_30,
        "position_zone": "unknown",
        "position_text": "位置不可判（日K不足20根）",
        "ma20": None,
        "ma60": None,
        "dist20": None,
        "dist60": None,
    }
    if len(closes) >= 20 and price > 0:
        recent = closes[-20:]
        lo, hi = min(recent), max(recent)
        ma20 = sum(recent) / 20.0
        view["ma20"] = ma20
        view["dist20"] = (price / ma20 - 1.0) * 100.0 if ma20 else None
        if hi > lo:
            pos = (price - lo) / (hi - lo) * 100.0
            view["position_pct"] = max(0.0, min(100.0, pos))
            if price > hi:
                view["position_zone"] = "breakout"
                view["position_text"] = "高位，现价已突破近20日区间上沿"
            elif price < lo:
                view["position_zone"] = "low"
                view["position_text"] = "低位，现价已跌破近20日区间下沿"
            elif pos >= 75.0:
                view["position_zone"] = "high"
                view["position_text"] = f"高位（近20日区间位置 {max(0.0, min(100.0, pos)):.0f}%）"
            elif pos <= 25.0:
                view["position_zone"] = "low"
                view["position_text"] = f"低位（近20日区间位置 {max(0.0, min(100.0, pos)):.0f}%）"
            else:
                view["position_zone"] = "middle"
                view["position_text"] = f"中位（近20日区间位置 {max(0.0, min(100.0, pos)):.0f}%）"
        else:
            view["position_zone"] = "middle"
            view["position_text"] = "中位（近20日价格区间过窄）"
    if len(closes) >= 60 and price > 0:
        ma60 = sum(closes[-60:]) / 60.0
        view["ma60"] = ma60
        view["dist60"] = (price / ma60 - 1.0) * 100.0 if ma60 else None
    return view


def _format_mid_term_section(view: Dict[str, Any]) -> List[str]:
    def _pct(value: Optional[float]) -> str:
        return "N/A" if value is None else f"{value:+.1f}%"

    context = str(view.get("context") or "中期数据不足")
    label = context.removeprefix("中期") or "不可判"
    lines = [
        "【中期结构】",
        f"{label}：7日 {_pct(view.get('rate_7'))} / 30日 {_pct(view.get('rate_30'))}",
    ]
    ma_parts: List[str] = []
    for days in (20, 60):
        dist = view.get(f"dist{days}")
        if dist is None:
            ma_parts.append(f"{days}日线不可用")
        elif dist >= 0:
            ma_parts.append(f"高于{days}日线约{abs(dist):.1f}%")
        else:
            ma_parts.append(f"低于{days}日线约{abs(dist):.1f}%")
    # V0.3.13:「位置」与「均线」回答的是同一个问题(现价在哪),并成一行(老大反馈冗余)。
    lines.append(f"位置：{view.get('position_text') or '不可判'}　｜　均线：" + "，".join(ma_parts))
    return lines


def _format_t7_section(view: Dict[str, Any], rate_1: Optional[float],
                       phase: Optional[str], phase_ready: bool) -> List[str]:
    context = str(view.get("context") or "中期数据不足")
    zone = str(view.get("position_zone") or "unknown")
    strong = context in ("中期偏强", "中期上行")
    weak = context in ("中期偏弱", "中期下行")

    if phase == "accum":
        first = "出现疑似分批建仓迹象，价格尚未启动，当前属于吸筹观察语境。"
    elif phase == "launch":
        first = "出现抬价启动迹象，当前属于右侧启动观察语境。"
    elif phase == "distribute":
        first = "出现高位供给释放迹象，当前属于疑似派发观察语境。"
    elif phase == "digest":
        first = "供给正在消化，但方向尚未选择。"
    elif strong and zone in ("high", "breakout"):
        first = "中期趋势仍偏强，但价格处于高位延伸区，短期回撤风险上升。"
    elif strong:
        first = "中期趋势偏强，但资金周期阶段尚未确认。"
    elif weak:
        # V0.3.13:原来写"中期结构偏弱，当前仍以弱势或反弹背景观察"——与上方【中期结构】
        # 首行字面重复(老大反馈同一条消息里"偏弱"说 3 遍),改为承接式表述。
        first = "承接上方结构，暂以弱势/反弹背景观察。"
    else:
        first = "中期方向暂不明确，当前仅能依据日K结构观察。"

    if rate_1 is None:
        second = "1日涨跌数据不足，暂不能判定短线承接或卖压。"
    elif not phase_ready:
        if rate_1 < -0.1:
            second = (f"1日下跌 {rate_1:+.1f}% 只能作为回撤背景，"
                      "暂不能判定为承接式下跌或卖压主导。")
        elif rate_1 > 0.1:
            second = (f"1日上涨 {rate_1:+.1f}% 只能作为短期价格背景，"
                      "暂不能判定为启动或真实承接。")
        else:
            second = "1日价格基本持平，但盘口历史不足，暂不能判断动态承接。"
    else:
        direction = "上涨" if rate_1 > 0.1 else ("下跌" if rate_1 < -0.1 else "基本持平")
        second = f"1日{direction} {_pct_abs(rate_1)}，结合上方1小时盘口阶段观察。"
    return ["【T+7分析】", first, second]


def _pct_abs(value: float) -> str:
    """T+7 文案中的带符号百分比。"""
    return f"{value:+.1f}%"


def _net_proceeds_line(price: float, buy_price: Optional[float],
                       buff_fee: float, yyyp_fee: float,
                       raw: Optional[Dict[str, Any]] = None) -> str:
    """一行给出「挂卖 / 秒出」各自到手最高的平台(V0.3.13)。

    旧版用 4 行讲一件事(两平台在售价 / 两平台求购价 / 到手差价 / 挂价对比),老大
    2026-10-10 反馈"这一段没必要"。收敛成一行:两个决策(挂着等 vs 立刻出)各自扣
    各自平台的费率后,钱最多的那家。费率相同则只写一个数,不啰嗦两平台。
    """
    p = aggregate_buff_yyyp_prices(raw or {})
    bs = float(p.get("buff_sell_price") or price or 0.0)
    ys = float(p.get("yyyp_sell_price") or 0.0)
    bb = float(p.get("buff_buy_price") or buy_price or 0.0)
    yb = float(p.get("yyyp_buy_price") or 0.0)
    parts: List[str] = []
    sell_cand: List[Any] = []
    if bs > 0:
        sell_cand.append(("BUFF", bs * (1 - buff_fee / 100.0)))
    if ys > 0:
        sell_cand.append(("悠悠", ys * (1 - yyyp_fee / 100.0)))
    if sell_cand:
        plat, net = max(sell_cand, key=lambda kv: kv[1])
        parts.append(f"挂卖 {plat} ¥{net:,.2f}")
    buy_cand: List[Any] = []
    if bb > 0:
        buy_cand.append(("BUFF", bb * (1 - buff_fee / 100.0)))
    if yb > 0:
        buy_cand.append(("悠悠", yb * (1 - yyyp_fee / 100.0)))
    if buy_cand:
        plat2, net2 = max(buy_cand, key=lambda kv: kv[1])
        parts.append(f"秒出 {plat2} ¥{net2:,.2f}")
    if not parts:
        return ""
    fee_txt = (f"{buff_fee:g}%" if abs(yyyp_fee - buff_fee) < 1e-9
               else f"BUFF {buff_fee:g}% / 悠悠 {yyyp_fee:g}%")
    return f"到手（{fee_txt}）: " + " ｜ ".join(parts)


def _proceeds_lines(price: float, buy_price: Optional[float], fees: Any,
                    raw: Optional[Dict[str, Any]] = None) -> List[str]:
    """成交与到手(V0.3.2;V0.3.13 压缩为 2 行)。

    CS2 饰品是分层流动性市场:同一件的「在售价」和「求购价」常态差 3~7%(实测价差
    中位 1.070),只给在售价等于给了一个「卖不掉的价」;高价品的手续费本身也是决策
    量级的金额。V0.3.13 起只保留三样:求购价(含立即出手折价)、一行到手结论、
    跨平台价差 ≥5% 时的倒货提示(罕见但最值钱)。
    """
    out: List[str] = []
    buff_fee = float(getattr(fees, "buff_sell_pct", 2.5) or 0.0)
    yyyp_fee = float(getattr(fees, "yyyp_sell_pct", 2.0) or 0.0)
    if buy_price and buy_price > 0 and price > 0:
        spread = (price / buy_price - 1.0) * 100.0
        ease = ("价差小,出手容易" if spread <= 5.0 else
                ("价差中等" if spread <= 12.0 else "价差大,挂单价短期难成交"))
        # V0.3.13:原写法「价差 +7.4%」被老大读成"求购价更高"(带 + 号像涨幅)。改为
        # 直说「立即出手折价 x%」——语义是"想马上卖掉,要比挂价让掉多少",不绕弯。
        out.append(f"求购价 ¥{buy_price:,.2f}（立即出手折价 {spread:.1f}%,{ease}）")
    elif price > 0:
        out.append("求购价不可用(接口未返回 BUFF 求购价)")
    net_line = _net_proceeds_line(price, buy_price, buff_fee, yyyp_fee, raw=raw)
    if net_line:
        out.append(net_line)
    if raw:
        dev = cross_spread_pct(raw)
        if dev is not None and abs(dev) >= 5.0:
            p = aggregate_buff_yyyp_prices(raw)
            bs = float(p.get("buff_sell_price") or 0.0)
            ys = float(p.get("yyyp_sell_price") or 0.0)
            if bs > 0 and ys > 0:
                cheap = "BUFF" if bs <= ys else "悠悠"
                out.append(f"跨平台价差 {abs(dev):.1f}%:买便宜的{cheap}侧、卖贵的那侧"
                           f"(账面价差,未计滑点/冷却期/成交概率)")
    return out


# V0.3.13:加入「结论：」——它与【T+7分析】段同义(实测同一条消息里"中期结构偏弱"说了 3 遍),
# 正文不再重复;大盘/短线盘口两行保留。
_ACTION_SKIP_PREFIX = ("T+7核心判断", "阶段：", "位置：", "当前盘口", "结论：")


def _action_extra_lines(action: Optional[Dict[str, Any]]) -> List[str]:
    """从量价解算结果里取**正文尚未展示**的行(V0.3.2)。

    `analyze_price_action(t7_mode=True)` 的 lines 含 T+7核心判断/阶段/位置/短线盘口辅助/
    当前盘口/Steam成交参考/大盘/结论;其中核心判断、阶段、位置、当前盘口与【中期结构】
    【流动性】两段重复,过滤掉,避免同一句出现两遍。
    """
    out: List[str] = []
    for line in (action or {}).get("lines") or []:
        text = str(line).strip()
        if not text or text.startswith(_ACTION_SKIP_PREFIX):
            continue
        out.append(text)
    return out


def _left_right_buy_points(
    closes: List[float],
    price: float,
    pct_7d: Optional[float],
    pct_30d: Optional[float],
    book_state: Optional[str] = None,
    book_pressure: Optional[float] = None,
    use_short_book: bool = True,
) -> Optional[List[str]]:
    """左侧/右侧买点双体系(纯函数,仅候选/观察,不构成建议)。

    V0.3.2 按实测调权重(2026-09-24 池内 1168 品 / 2313 样本 / T+7 超额剔除大盘):
      - 「低位 + 区间深跌≤-8%」单独看是**负优势**(超额中位 -3.76pp、胜率 32.7%,n=886)
        → 候选条件不再等于"可以看",必须**确认项 ≥2 项**才提示"确认成立"。
      - 「深跌 + 近7日转正」= 规则里的确认项,是唯一带正信息的组合(超额 +6.35pp、胜率 60%,
        n=15 样本小) → 确认项前置,候选条件降级为"背景"。
      - 20/60 日均线口径本地无法验证(库只有 27 天日收盘),文案不据此下结论。

    数据全部复用 /分析 已拿到的 SteamDT 日K closes + 盘口净压力:
    - 左侧 = 逆势埋伏:靠近近期低位(近20日区间下部/距低点10%内) + 30日深跌(≤-8%)。
    - 右侧 = 顺势确认:收盘站上20日线 或 站上后回踩不破(±1%)。
    - 确认项以 ✓/✗ 白盒标注;绝不说「现在就该买」。
    返回待渲染的文本行列表;数据不足(<20 根日K 或价格异常)返回 None。
    """
    if not closes or len(closes) < 20 or not price or price <= 0:
        return None
    try:
        seg = closes[-20:]
        lo20, hi20 = min(seg), max(seg)
        span20 = (hi20 - lo20) or 1.0
        pos20 = (price - lo20) / span20 * 100.0
        near_low_pct = (price / lo20 - 1.0) * 100.0 if lo20 > 0 else None
        ma20 = sum(seg) / 20.0
        ma60 = (sum(closes[-60:]) / 60.0) if len(closes) >= 60 else None
        ma20_prev = (sum(closes[-21:-1]) / 20.0) if len(closes) >= 21 else ma20
        last = closes[-1]

        book_better = use_short_book and (
            bool(book_state == "增")
            or bool(book_pressure is not None and book_pressure > 0)
        )
        book_worse = use_short_book and (
            bool(book_state == "缩")
            or bool(book_pressure is not None and book_pressure < 0)
        )

        # ================= 左侧: 逆势埋伏(风险高·仓位轻·仅候选) =================
        deep30 = pct_30d is not None and pct_30d <= -8.0
        lower_zone = pos20 <= 45.0 or (near_low_pct is not None and near_low_pct <= 10.0)
        bg_left = bool(lower_zone and deep30)          # 仅"背景"满足
        # 确认项(V0.3.2:这三项才是真正带信息的,必须 ≥2 才提示"确认成立")
        c_no_new_low = len(closes) >= 7 and last > min(closes[-7:-1])
        c_ease = pct_7d is not None and pct_7d > -6.0 and (pct_30d is None or pct_7d > pct_30d)
        c_short = len(closes) >= 6 and last > closes[-6]
        conf_n = sum(1 for x in (c_no_new_low, c_ease, c_short) if x)
        left_present = bool(bg_left and conf_n >= 2)   # 候选成立 = 背景 + 确认≥2
        trig: List[str] = []
        if price <= lo20:
            trig.append(f"已触及/跌破近20日低点 ¥{lo20:,.2f}")
        elif near_low_pct is not None:
            trig.append(f"靠近近期低位(现价高于近20日低点 {near_low_pct:+.1f}%)")
        else:
            trig.append(f"近20日区间位置 {pos20:.0f}%(下部)")
        if pct_30d is not None:
            trig.append(f"30日深跌 {pct_30d:+.1f}%")
        if pct_7d is not None and pct_30d is not None and pct_7d > pct_30d:
            trig.append(f"跌势钝化(7日 {pct_7d:+.1f}% 较30日收窄)")
        if book_better:
            trig.append("盘口承接增强(求购净增)")
        left_fail = f"收盘跌破近20日低点 ¥{lo20:,.2f}"
        if use_short_book:
            left_fail += " / 盘口承接转弱"
        if price <= lo20:
            left_fail += "  ← 已破前低,候选作废"
        left_lines = [
            "◆ 左侧买点（逆势埋伏 · 风险高仓位轻 · 仅候选）:",
            f"　候选背景: {' / '.join(trig[:4]) or '低位深跌区'}",
            f"　确认项 {conf_n}/3: 不再创新低{'✓' if c_no_new_low else '✗'} / "
            f"7日跌幅收敛{'✓' if c_ease else '✗'} / 短期转强{'✓' if c_short else '✗'}"
            + ("　→ 确认成立" if conf_n >= 2 else "　→ 确认不足,仅观察"),
            f"　失效: {left_fail}",
            "　数据: 本区间「低位+深跌」单独无优势(超额 -3.8pp/胜率 33%),确认项≥2 项才是关键",
        ]

        # ================= 右侧: 顺势确认(等信号·仅候选) =================
        # 候选/失效以「现价(price,即 BUFF 现价)」判,与顶部现价/中期结构行同口径;
        # 需同时「站上20日线 + 现价 ≥ 近6日最高收盘」(创近6日新高)——贴线横盘不算突破,防误报。
        recent_hi = max(closes[-6:])
        above20 = price >= ma20 and price >= recent_hi
        rtrig: List[str] = []
        if above20:
            rtrig.append(f"现价站上20日线并创近6日新高(现 ¥{price:,.2f} ≥ 20线 ¥{ma20:,.2f})")
            if ma60 is not None and price > ma60 * 1.10:
                rtrig.append("已高于60日线10%+,防追高")
        elif price >= ma20 * 0.99:
            rtrig.append(f"现价贴近20日线未站稳(现 ¥{price:,.2f} ≈ ¥{ma20:,.2f},需突破新高确认)")
        else:
            rtrig.append(f"现价未站上20日线(现 ¥{price:,.2f} < ¥{ma20:,.2f})")
        if pct_7d is not None:
            rtrig.append(f"近7日 {pct_7d:+.1f}%")
        if book_better:
            rtrig.append("盘口同步改善")
        hold3 = len(closes) >= 3 and sum(1 for c in closes[-3:] if c >= ma20_prev) >= 2
        close_below = last < ma20  # 日K 收盘仍在20线下方(未获日K确认)
        c_7d_pos = pct_7d is not None and pct_7d > 0
        r_conf_n = sum(1 for x in (hold3, c_7d_pos) if x)
        right_present = bool(above20 and r_conf_n >= 1)   # V0.3.2:突破 + 至少1项日K确认
        book_confirm_label = "盘口同步改善" if use_short_book else "短线盘口同步(可选)"
        book_confirm_state = '✓' if book_better else ('✗' if book_worse else '-')
        right_lines = [
            "◇ 右侧买点（顺势确认后介入 · 等信号 · 仅候选）:",
            f"　候选: {' / '.join(rtrig[:3]) or '等待现价站上20日线'}",
            f"　确认项 {r_conf_n}/2: 连续2-3根日K站稳20日线"
            f"{'✗(日K收盘未站稳)' if (hold3 is False and close_below) else ('✓' if hold3 else '✗')} / "
            f"7日转正{'✓' if c_7d_pos else '✗'}（+ {book_confirm_label}{book_confirm_state}）"
            + ("　→ 确认成立" if r_conf_n >= 1 else "　→ 确认不足,仅观察"),
            f"　失效: 现价回落跌破20日线(现 ¥{price:,.2f} / 20线 ¥{ma20:,.2f})",
            "　风险: 防追高与假突破;等回踩不破再考虑更稳",
        ]

        # ================= 结论 =================
        if left_present and right_present:
            concl = "左侧/右侧候选并存:风格二选一——左侧轻仓埋伏、右侧等确认,勿同仓双开"
        elif left_present:
            concl = "当前偏左侧(逆势埋伏候选):右侧确认未成立,待站上20日线后再评估"
        elif right_present:
            concl = ("当前偏右侧(T+7顺势候选):等 站稳20日线+7日转正再评估；"
                     "短线盘口仅作执行参考"
                     if not use_short_book else
                     "当前偏右侧(顺势候选):等 站稳20日线+7日转正+盘口改善 全满足再考虑")
        elif bg_left or above20:
            concl = ("暂不构成买点:条件已出现但**确认项不足**(左侧需确认≥2/3、右侧需日K站稳或7日转正),"
                     "按实测这类样本没有正优势")
        else:
            concl = "暂无买点:左侧(低位深跌)与右侧(站上20日线)候选均未成立,继续观察"

        out: List[str] = []
        if left_present or bg_left:
            out.extend(left_lines)
        if right_present or above20:
            out.extend(right_lines)
        if not out:
            # V0.3.13:删掉原来的第 3 行「结论: 暂无买点…」—— 与第 1 行完全同义(老大反馈冗余);
            # 「口径」行不再占正文,由调用方收口到消息末尾。
            out.append("◆ 买点: 暂无(左侧低位深跌 / 右侧站上20日线并创新高 候选均未成立,继续观察)")
            out.append("　观察点: 左侧等 深跌钝化+不再创新低;右侧等 站上20日线并创近6日新高、日K连续站稳")
        else:
            out.append(f"结论: {concl}")
        out.append("口径: 位置/均线用日K;20/60日线口径本地未验证(库仅 27 天日收盘),仅作结构参考")
        return out
    except Exception:
        return None


class CS2BotClient(botpy.Client):
    """CS2 饰品行情机器人。"""

    def __init__(self, app_config: AppConfig, store: SnapshotStore,
                 recommender: Recommender, intents, steamdt: Optional[SteamDTClient] = None):
        self._cfg = app_config
        self._store = store
        self._recommender = recommender
        self._steamdt = steamdt
        self._reply_lock = asyncio.Lock()
        self.known_groups: Dict[str, str] = {}  # group_openid -> 最近一次@来源用户
        # QQ 对已注销的群/用户会返回“资源不存在”。缓存这些终态目标，
        # 由盯盘循环清理对应记录，避免每轮调度重复请求并刷屏。
        self._invalid_group_openids: set[str] = set()
        self._invalid_user_openids: set[str] = set()
        # AI 智能体:自然语言由 LLM 理解意图并调工具查真实数据(配置未就绪时自动降级本地意图)
        # 传入共享 steamdt 客户端,使 AI 播报与指令/盯盘共用同一缓存与限频退避状态
        self._agent: Optional[CS2Agent] = CS2Agent(app_config, store, recommender,
                                                   steamdt=steamdt) if app_config.llm.enable else None
        super().__init__(intents=intents)

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    async def on_ready(self):
        _log.info(f"robot 「{self.robot.name}」 已连接，等待群消息...")

    # ------------------------------------------------------------------
    # 群消息
    # ------------------------------------------------------------------

    async def on_group_at_message_create(self, message: GroupMessage):
        """群内 @机器人 时触发。"""
        group_openid = getattr(message, "group_openid", "")
        author = getattr(getattr(message, "author", None), "member_openid", "")
        if group_openid:
            self.known_groups[group_openid] = author or ""
            # 持久化记录群，用于每日定时播报
            await asyncio.to_thread(self._store.record_group, group_openid)
        _log.info("[group=%s] %s", group_openid, message.content)
        # 业务日志(落 bot.log):打印群 openid,便于把新群加入 free/vip 白名单
        if group_openid:
            logger.info("[group] 收到群消息 group_openid=%s from=%s", group_openid, author or "")

        try:
            reply = await self._handle_message(message)
        except Exception as exc:  # 防止异常导致进程崩溃
            logger.exception("处理消息失败: %s", exc)
            reply = "抱歉，处理出错了，请稍后再试。"

        text, image_path = reply if isinstance(reply, tuple) else (reply, None)
        await self._reply_group(message, text, image_path=image_path)

    async def on_c2c_message_create(self, message: GroupMessage):
        """用户私聊机器人时触发。"""
        user_openid = getattr(message, "author", None)
        user_openid = getattr(user_openid, "user_openid", "") if user_openid else ""
        _log.info("[c2c=%s] %s", user_openid, message.content)

        try:
            reply = await self._handle_message(message)
        except Exception as exc:
            logger.exception("处理私聊消息失败: %s", exc)
            reply = "抱歉，处理出错了，请稍后再试。"

        text, image_path = reply if isinstance(reply, tuple) else (reply, None)
        await self._reply_c2c(message, text, image_path=image_path)

    async def _handle_message(self, message: GroupMessage) -> str:
        content = getattr(message, "content", "") or ""
        raw = cmds.strip_at(content)
        if not raw:
            return "没有听懂哦~ 发送 /帮助 查看我能做什么 🎮"

        # 斜杠指令:确定性操作永远走本地(快、稳、零成本)
        if raw.startswith("/"):
            return await self._handle_slash(raw, message)

        # 群功能开关自然语言(管理员私聊): "开启 817105769 异动" / "关闭 121350016 盯盘"
        m = re.match(r"^(开启|打开|关闭|关掉)\s*(?:群)?\s*([0-9A-Za-z]+)\s*(异动|盯盘|播报|分析)$",
                     raw.strip())
        if m:
            sw = "开" if m.group(1) in ("开启", "打开") else "关"
            return await self._feature_reply(f"{m.group(2)} {m.group(3)} {sw}", message)

        # 自然语言:AI 优先(配置启用时),异常/超时降级本地意图识别
        return await self._handle_natural(raw, self._session_key(message), message)

    @staticmethod
    def _extract_watch_arg(raw: str) -> Optional[str]:
        """从自然语言中提取盯盘参数:"关注 蝴蝶刀 蓝钢 7700" -> "蝴蝶刀 蓝钢 7700"。

        不匹配(如普通查价/闲聊)返回 None,不走盯盘。
        """
        m = _WATCH_RE.match(raw.strip())
        if m:
            arg = m.group(1).strip().lstrip("，,。:： ")
            return arg or None
        return None

    @staticmethod
    def _user_openid(message) -> str:
        """从消息中提取用户 openid(群消息 member_openid / 私聊 user_openid)。"""
        author = getattr(message, "author", None)
        if getattr(message, "group_openid", ""):
            return getattr(author, "member_openid", "") or ""
        return getattr(author, "user_openid", "") or ""

    def _session_key(self, message) -> str:
        group = getattr(message, "group_openid", "")
        if group:
            return f"g:{group}"
        author = getattr(message, "author", None)
        user = getattr(author, "user_openid", "") if author else ""
        return f"u:{user}" if user else "default"

    def _is_vip_group(self, group_openid: str) -> bool:
        """会员群权限检查:群聊需在白名单;私聊(group 为空)默认允许。"""
        return self._watch_tier(group_openid) == "vip"

    def _watch_tier(self, group_openid: str) -> str:
        """盯盘权限分层: 'vip'(不限量) / 'free'(限量) / 'none'(无)。"""
        if not group_openid:
            return "vip"   # 私聊默认按会员处理(不限量)
        if self._cfg.vip.watch_enable and group_openid in self._cfg.vip.group_openids:
            return "vip"
        if self._cfg.free.watch_enable and group_openid in self._cfg.free.group_openids:
            return "free"
        return "none"

    async def _handle_slash(self, raw: str, message=None) -> str:
        sraw = raw.strip()
        # 大盘指数专用指令:走 SteamDT 源,返回 (文本, 走势PNG)
        if sraw.startswith(("/大盘", "/指数", "/broad", "/index")):
            return await self._broad_index_reply(message)

        cmd, arg = cmds.parse_message(raw, self._cfg.qq)

        if cmd == "help":
            return cmds.format_help()

        if cmd == "status":
            fetch_time = await self._store.async_last_fetch_time()
            latest = await self._store.async_query_latest(limit=1)
            return cmds.format_status(fetch_time, len(latest) if fetch_time else 0)

        if cmd == "report":
            return await self._report_reply(message)

        if cmd == "recommend":
            return await self._recommend_reply(arg)

        if cmd == "price":
            return await self._price_reply(arg)

        if cmd == "rank":
            return await self._rank_reply(arg)

        if cmd in ("watch", "unwatch", "watches"):
            return await self._watch_slash(cmd, arg, message)

        if cmd == "announce":
            return await self._announce_reply(arg, message)

        if cmd == "feature":
            return await self._feature_reply(arg, message)

        if cmd == "featureopen":
            return await self._feature_open_reply(arg, message)

        if cmd == "analysis":
            return await self._analysis_reply(arg, message)

        if cmd == "anomaly":
            return await self._anomaly_reply(arg)

        return cmds.format_chat_fallback()

    # ------------------------------------------------------------------
    # 私聊公告指令
    # ------------------------------------------------------------------

    async def _announce_reply(self, arg: Optional[str], message=None) -> str:
        """私聊发公告: /公告 <群号|别名|全部> <内容>

        仅私聊场景可用(群内使用返回提示);只有 admin.user_openids 白名单用户能发;
        目标群通过 config announce.groups 的群号/别名映射定位,「全部」发到所有已配置群。
        """
        group = getattr(message, "group_openid", "") if message else ""
        if group:
            return "公告指令请在私聊里使用: 私聊我发送 /公告 <群号> <内容>"
        user = self._user_openid(message) if message else ""
        if not user:
            return "无法识别你的身份,稍后再试"
        admins = self._cfg.admin_user_openids or []
        if user not in admins:
            return "你没有发公告的权限(仅管理员可发)"

        arg = (arg or "").strip()
        if not arg:
            return "用法: /公告 <群号或别名> <内容>，如 /公告 121350016 今晚8点开箱活动\n可用群: " + self._announce_targets_hint()
        parts = arg.split(maxsplit=1)
        target, content = parts[0].strip(), parts[1].strip() if len(parts) > 1 else ""
        if not content:
            return "公告内容不能为空。用法: /公告 <群号或别名> <内容>"

        groups = self._cfg.announce_groups or {}
        # 「全部」: 发到所有已配置群(去重)
        if target in ("全部", "all", "*"):
            targets = list(dict.fromkeys(groups.values()))
            ok, fail = 0, []
            for gid in targets:
                try:
                    result = await self.push_to_group(gid, content)
                    if result is False:
                        fail.append(gid)
                    else:
                        ok += 1
                except Exception as exc:
                    logger.exception("[announce] 推送到 %s 失败: %s", gid, exc)
                    fail.append(gid)
            if fail:
                return f"公告已发送 {ok} 个群 ✅，{len(fail)} 个失败 ❌"
            return f"公告已发送到 {ok} 个群 ✅"
        # 指定群: 群号/别名 -> openid
        gid = groups.get(target) or (target if target in groups.values() else "")
        # 容错: 群号输错(多打/少打/多一位)时,若与某已配置群唯一匹配则自动修正
        if not gid:
            fuzzy = [k for k in groups if target in k or k in target]
            if len(fuzzy) == 1:
                gid = groups[fuzzy[0]]
                target = fuzzy[0]
        if not gid:
            return f"找不到群「{target}」。可用: " + self._announce_targets_hint()
        try:
            result = await self.push_to_group(gid, content)
            if result is False:
                return f"公告发送失败: 群「{target}」接口返回失败"
        except Exception as exc:
            logger.exception("[announce] 推送到 %s 失败: %s", target, exc)
            return f"公告发送失败: {exc}"
        return f"公告已发送到群「{target}」✅（已按该群号匹配）"

    async def _feature_reply(self, arg: Optional[str], message=None) -> str:
        """私聊管理群功能开关: /功能 <群号> <异动|盯盘|播报|分析> <开|关>
        仅私聊 + 管理员白名单;群号经 announce.groups 映射(兼容模糊匹配)。
        """
        group = getattr(message, "group_openid", "") if message else ""
        if group:
            return "功能开关请在私聊里使用: 私聊我发送 /功能 <群号> <功能> 开/关"
        user = self._user_openid(message) if message else ""
        if not user or user not in (self._cfg.admin_user_openids or []):
            return "你没有管理权限(仅管理员可操作)"

        arg = (arg or "").strip()
        if not arg:
            return ("用法: /功能 <群号> <功能> <开|关>，如 /功能 817105769 异动 开\n"
                    "功能: 异动(anomaly) 盯盘(watch) 播报(report) 分析(analysis)")
        parts = arg.split()
        if len(parts) != 3:
            return "格式: /功能 <群号> <功能> <开|关>"
        gname, feat, sw = parts[0].strip(), parts[1].strip().lower(), parts[2].strip().lower()

        # 群号/别名 -> openid(announce.groups 优先,兼容模糊匹配)
        groups = self._cfg.announce_groups or {}
        openid = groups.get(gname)
        if not openid:
            fuzzy = [k for k in groups if gname in k or k in gname]
            if len(fuzzy) == 1:
                openid = groups[fuzzy[0]]
                gname = fuzzy[0]
        if not openid:
            return f"找不到群「{gname}」。可用: " + "、".join(list(groups.keys()) or ["(无)"])

        feat_map = {"异动": "anomaly", "anomaly": "anomaly", "扫货": "anomaly",
                    "盯盘": "watch", "watch": "watch", "关注": "watch",
                    "播报": "report", "report": "report", "大盘": "report",
                    "分析": "analysis", "analysis": "analysis", "状态分析": "analysis"}
        key = feat_map.get(feat)
        if not key:
            return "未知功能: 仅支持 异动/盯盘/播报/分析"
        if sw not in ("开", "关", "on", "off", "1", "0"):
            return "开关参数: 开 或 关"
        enabled = sw in ("开", "on", "1")
        await self._store.async_set_group_feature(openid, key, enabled)
        cur = await self._store.async_get_group_features()
        return (f"✅ 已{'开启' if enabled else '关闭'} 群「{gname}」的「{feat}」功能\n"
                f"当前开关: 异动={'开' if cur.get((openid, 'anomaly'), True) else '关'} | "
                f"盯盘={'开' if cur.get((openid, 'watch'), True) else '关'} | "
                f"播报={'开' if cur.get((openid, 'report'), True) else '关'} | "
                f"分析={'开' if cur.get((openid, 'analysis'), True) else '关'}")

    def _announce_targets_hint(self) -> str:
        groups = self._cfg.announce_groups or {}
        if not groups:
            return "(config announce.groups 未配置任何群)"
        return "、".join(groups.keys())

    # ------------------------------------------------------------------
    # 群功能开放管理 + 饰品分析
    # ------------------------------------------------------------------

    # 功能名 -> 内部 key(与 group_features 表一致)
    _FEAT_MAP = {"异动": "anomaly", "anomaly": "anomaly", "扫货": "anomaly",
                 "盯盘": "watch", "watch": "watch", "关注": "watch",
                 "播报": "report", "report": "report", "大盘": "report",
                 "分析": "analysis", "analysis": "analysis", "状态分析": "analysis"}
    _FEAT_LABEL = {"anomaly": "异动", "watch": "盯盘", "report": "播报", "analysis": "分析"}

    async def _feature_allowed(self, group_openid: str, feature: str) -> bool:
        """某群某功能是否开放(无记录默认开,管理员 /功能 或 /功能开放 控制)。"""
        try:
            feats = await self._store.async_get_group_features()
            return feats.get((group_openid, feature), True)
        except Exception:
            return True

    async def _feature_open_reply(self, arg: Optional[str], message=None) -> str:
        """私聊管理群功能开放:/功能开放 <功能> <群号> 或 /功能开放 状态(查询全部群)。
        仅私聊 + 管理员。功能: 异动/盯盘/播报/分析。"""
        group = getattr(message, "group_openid", "") if message else ""
        if group:
            return "功能开放管理请在私聊里使用: 私聊我发送 /功能开放 分析 <群号> 开"
        user = self._user_openid(message) if message else ""
        if not user or user not in (self._cfg.admin_user_openids or []):
            return "你没有管理权限(仅管理员可操作)"

        arg = (arg or "").strip()
        # 查询模式: 无参数 / "状态" / "列表"
        if not arg or arg in ("状态", "列表", "list", "status", "全部"):
            return await self._feature_open_status_reply()

        parts = arg.split()
        if len(parts) < 2:
            return ("用法: /功能开放 <功能> <群号> [开|关]，如 /功能开放 分析 817105769 开\n"
                    "查询: /功能开放 状态\n功能: 异动/盯盘/播报/分析")
        feat, gname = parts[0].strip(), parts[1].strip()
        sw = parts[2].strip().lower() if len(parts) > 2 else "开"
        key = self._FEAT_MAP.get(feat.lower() if feat.isascii() else feat)
        if not key:
            return "未知功能: 仅支持 异动/盯盘/播报/分析"
        if sw not in ("开", "关", "on", "off", "1", "0", "开放", "关闭"):
            return "开关参数: 开 或 关"

        groups = self._cfg.announce_groups or {}
        openid = groups.get(gname)
        if not openid:
            fuzzy = [k for k in groups if gname in k or k in gname]
            if len(fuzzy) == 1:
                openid, gname = groups[fuzzy[0]], fuzzy[0]
        if not openid:
            return f"找不到群「{gname}」。可用: " + "、".join(list(groups.keys()) or ["(无)"])

        enabled = sw in ("开", "on", "1", "开放")
        await self._store.async_set_group_feature(openid, key, enabled)
        return f"✅ 群「{gname}」的「{self._FEAT_LABEL[key]}」功能已{'开放' if enabled else '关闭'}"

    async def _feature_open_status_reply(self) -> str:
        """列出所有已配置群的功能开放状态。"""
        groups = self._cfg.announce_groups or {}
        feats = await self._store.async_get_group_features()
        lines = ["📋 群功能开放状态:"]
        feats_order = ["anomaly", "watch", "report", "analysis"]
        for gname, openid in groups.items():
            marks = []
            for k in feats_order:
                on = feats.get((openid, k), True)
                marks.append(f"{self._FEAT_LABEL[k]}:{'✓' if on else '✗'}")
            lines.append(f"• {gname}  「{marks[0]} | {marks[1]} | {marks[2]} | {marks[3]}」")
        lines.append("")
        lines.append("修改: 私聊 /功能开放 <功能> <群号> 开/关")
        return "\n".join(lines)

    @staticmethod
    def _analyze_good_state(d: Dict[str, Any], trend: Optional[Dict[str, Any]] = None) -> str:
        """根据 get_good_info 实时数据判定当前状态(全平台盘口 + 中期趋势)。"""
        price = float(d.get("buff_sell_price") or 0)
        buy_price = float(d.get("buff_buy_price") or 0)
        # 全平台在售/求购(6 平台合计):BUFF+Steam+悠悠+C5+IGXE+ECO
        _SELL = ("buff_sell_num", "steam_sell_num", "yyyp_sell_num",
                 "c5_sell_num", "igxe_sell_num", "eco_sell_num")
        _BUY = ("buff_buy_num", "steam_buy_num", "yyyp_buy_num",
                "c5_buy_num", "igxe_buy_num", "eco_buy_num")
        total_sell = sum(int(d.get(k) or 0) for k in _SELL)
        total_buy = sum(int(d.get(k) or 0) for k in _BUY)
        # Steam 市场 buy order 巨量(如手套武器箱 47 万)会淹没国内求购口径,
        # 求购比单独展示;判定主要靠价差(全平台买卖贴近=有真实交易)。
        ratio_cn = int(d.get("buff_buy_num") or 0) / int(d.get("buff_sell_num") or 1)
        r1 = d.get("sell_price_rate_1")
        r7 = d.get("sell_price_rate_7")
        r30 = d.get("sell_price_rate_30")
        rate1 = float(r1) if r1 is not None else None
        rate7 = float(r7) if r7 is not None else None
        rate30 = float(r30) if r30 is not None else None
        if trend:
            if trend.get("pct_1d") is not None:
                rate1 = float(trend["pct_1d"])
            if trend.get("pct_7d") is not None:
                rate7 = float(trend["pct_7d"])
            if trend.get("pct_30d") is not None:
                rate30 = float(trend["pct_30d"])

        def f(v):
            return "N/A" if v is None else f"{v:+.1f}%"

        spread = (price - buy_price) / buy_price * 100 if (price and buy_price) else None
        # 盘口健康度:价差小(<10%)=买卖贴近,有真实交易(海量在售的箱子类 BUFF 求购比低,
        # 但价差 0.4% 说明深度极好,不能按求购比判买盘稀薄)
        healthy = spread is not None and spread < 10.0

        # 中期方向(7d/30d 至少一个有效)
        up7 = rate7 is not None and rate7 > 1
        dn7 = rate7 is not None and rate7 < -1
        up30 = rate30 is not None and rate30 > 2
        dn30 = rate30 is not None and rate30 < -2
        rising = up7 or up30
        falling = dn7 or dn30

        # 盘口结构(全平台口径)
        book = f"全平台在售 {total_sell:,} / 求购 {total_buy:,}"
        if healthy:
            book += f"，价差健康(BUFF 仅 {spread:.1f}%)"
        elif total_buy < 20:
            book += "，买盘几乎无人(全平台求购稀薄,转手难)"
        elif ratio_cn < 0.3:
            book += f"，BUFF 求购偏少(在售/求购比 {(1/ratio_cn if ratio_cn else 0):.0f}:1)"
        elif spread is not None and spread > 25:
            book += f"，价差大({spread:.0f}%),求购压价低,转手要割肉"
        else:
            book += f"，盘口中性(BUFF 求购/在售 {ratio_cn:.2f})"

        # 状态判定
        if rising and healthy:
            state = "📈 上行且盘口健康(价差小,承接真实)"
        elif rising and not healthy and (spread is None or spread > 20) and ratio_cn < 0.3:
            state = "⚠️ 价升但买盘稀薄/价差大(背离风险,谨防回落)"
        elif rising:
            state = "📈 上行趋势,盘口中性"
        elif falling and healthy:
            state = "📉 回调中盘口仍健康(买卖贴近,关注企稳)"
        elif falling and ratio_cn >= 0.8:
            state = "📉 回调中买盘承接(关注企稳)"
        elif falling:
            state = "📉 下行弱势(卖压主导)"
        elif rate30 is not None and rate30 < -15 and (rate7 is None or rate7 > -2):
            state = "🔄 深跌后趋稳(超跌观察)"
        else:
            state = "➖ 横盘观望(方向不明)"

        pos = ""
        if rate30 is not None and rate30 < -8:
            pos = "近期低位"
        elif rate30 is not None and rate30 > 8:
            pos = "近期高位"
        label = "价格走势"
        if trend and trend.get("count"):
            label = f"价格走势(SteamDT日K {int(trend['count'])}根)"
        lines = [f"{label}: 近1日 {f(rate1)} / 近7日 {f(rate7)} / 近30日 {f(rate30)}",
                 f"盘口结构: {book}",
                 f"状态判定: {state}"]
        if pos:
            lines.append(f"位置参考: {pos}")
        return "\n".join(lines)

    async def _analysis_reply(self, arg: Optional[str], message=None) -> Union[str, Tuple[str, Optional[str]]]:
        """/分析 <饰品名>:资金周期 → 中期结构 → T+7分析 → BUFF+悠悠流动性。
        群内使用需该群开放「分析」功能(默认开);私聊不限。"""
        keyword = (arg or "").strip()
        if not keyword:
            return "请带上饰品名，如 /分析 手套武器箱"
        group = getattr(message, "group_openid", "") if message else ""
        if group and not await self._feature_allowed(group, "analysis"):
            return "该群未开放「分析」功能(请管理员私聊: /功能开放 分析 <群号> 开)"

        try:
            hits = await self._store.async_query_goods_by_keyword(keyword, limit=10)
            # 容错:原样 LIKE 无结果时,拆词(空格/|/括号分隔)取最长 token 再搜,按全部 token 过滤
            # (例:用户输「澄澈之水 略有磨损」,库内名是「澄澈之水 (略有磨损)」带括号,原样 LIKE 不中)
            tokens = [t for t in re.split(r"[\s|｜()（）]+", keyword) if len(t) >= 2]
            if not hits and tokens:
                for t in sorted(tokens, key=len, reverse=True):
                    hits = await self._store.async_query_goods_by_keyword(t, limit=20)
                    if hits:
                        break
                hits = [h for h in hits if all(tk in h["name"] for tk in tokens)]
            # 精准优先:全名/等长匹配
            exact = [h for h in hits if h["name"] == keyword or keyword in h["name"]]
            cand = exact or hits
            if not cand:
                return f"未在饰品索引中找到「{keyword}」。可尝试更完整名称(含磨损),或 /价格 {keyword}"
            gid, name = int(cand[0]["good_id"]), cand[0]["name"]
            if len(cand) > 1:
                other = "、".join(h["name"] for h in cand[1:4])
                hint = f"\n(另有匹配: {other},可输入更完整名称区分)"
            else:
                hint = ""

            from bot.csqaq.client import CSQAQClient
            async with CSQAQClient(self._cfg.csqaq) as api:
                d = await api.get_good_info(gid)
            if not d or not d.get("name"):
                return "机器人维护中，请稍后再试"

            counts = aggregate_market_counts(d)
            liquidity = aggregate_buff_yyyp_sell(d)
            # V0.2.24: 求购侧统一 BUFF+悠悠(与在售侧对称、与资金周期判定同口径)。
            # 单品详情提供两平台求购字段;Steam 求购字段是脏数据(实测 47 万"求购"),继续排除。
            buy_buff_i = int(d.get("buff_buy_num") or 0)
            buy_yyyp_i = int(d.get("yyyp_buy_num") or 0)
            buy_total_i = buy_buff_i + buy_yyyp_i
            analysis_buy_num = buy_total_i
            analysis_sell_num = (int(liquidity["sell_num"])
                                 if liquidity["sell_complete"]
                                 else int(d.get("buff_sell_num") or 0))
            analysis_scope = "BUFF+悠悠"
            try:
                # 分析查询也喂一条盘口快照，供下一次分析/周期监测比较。
                await self._store.async_save_listing_history_batch([{
                    "good_id": gid, "name": d.get("name") or name,
                    "ts": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "buff_sell_num": int(d.get("buff_sell_num") or 0),
                    "steam_sell_num": int(d.get("steam_sell_num") or 0),
                    "buff_buy_num": buy_buff_i,
                    "steam_buy_num": int(d.get("steam_buy_num") or 0),
                    "yyyp_buy_num": buy_yyyp_i,
                    "buff_sell_price": float(d.get("buff_sell_price") or 0),
                    "buff_buy_price": float(d.get("buff_buy_price") or 0),
                    # V0.3.4: 双平台价格(BUFF+悠悠)
                    **dual_price_columns(d),
                    "total_sell_num": liquidity["sell_num"] if liquidity["sell_complete"] else None,
                    "total_buy_num": buy_total_i,
                    "total_sell_scope": "buff_yyyp" if liquidity["sell_complete"] else None,
                    "total_buy_scope": "buff_yyyp" if buy_total_i > 0 else None,
                }])
            except Exception as exc:
                logger.warning("[analysis] 盘口快照落库失败(不影响分析): %s", exc)

            try:
                if d.get("turnover_number") is not None:
                    await self._store.async_upsert_turnover_snapshot([{
                        "good_id": gid, "name": d.get("name") or name,
                        "turnover_number": d.get("turnover_number"),
                        "turnover_avg_price": d.get("turnover_avg_price"),
                        "period_at": d.get("period_at"),
                    }])
            except Exception as exc:
                logger.warning("[analysis] 成交量快照落库失败(降级显示): %s", exc)

            price = float(d.get("buff_sell_price") or 0)
            # 量价解算使用 BUFF+悠悠求购/在售合计；缺少双平台字段的历史点不参与周期判定。
            # 周期总结快照按 2 小时采样，保留 24 小时历史用于阶段持续性判断。
            history_rows = await self._store.async_get_listing_series(gid, name, hours=24.0)
            steam_points: List[Dict[str, Any]] = []
            steam_summary: Optional[Dict[str, Any]] = None
            steam_name = str(
                d.get("_market_hash_name")
                or d.get("market_hash_name")
                or d.get("marketHashName")
                or name
                or ""
            ).strip()
            if self._steamdt is not None and steam_name:
                try:
                    kline = await self._steamdt.get_item_kline(steam_name, periods=2)
                    steam_points = kline.get("points") or []
                    if len(steam_points) >= 2:
                        steam_summary = summarize_item_kline(steam_points)
                except Exception as exc:
                    logger.warning("[analysis] SteamDT 单品K线获取失败: %s", exc)
            # 1/7/30 日涨跌:SteamDT 日K 摘要优先,CSQAQ rate 兜底(与买点/量价共用同一口径)
            _sm = steam_summary or {}
            rate1_eff = (_sm.get("pct_1d") if _sm.get("pct_1d") is not None
                         else (float(d["sell_price_rate_1"]) if d.get("sell_price_rate_1") is not None else None))
            rate7_eff = (_sm.get("pct_7d") if _sm.get("pct_7d") is not None
                         else (float(d.get("sell_price_rate_7") or 0) if d.get("sell_price_rate_7") is not None else None))
            rate30_eff = (_sm.get("pct_30d") if _sm.get("pct_30d") is not None
                          else (float(d.get("sell_price_rate_30") or 0) if d.get("sell_price_rate_30") is not None else None))
            action = analyze_price_action(
                price=price,
                buy_num=analysis_buy_num,
                sell_num=analysis_sell_num,
                history=history_rows,
                price_change_1d=rate1_eff,
                rate_7=rate7_eff,
                rate_30=rate30_eff,
                book_scope=analysis_scope,
                t7_mode=True,
                # V0.3.2:接上大盘环境(与异动推送同源,5 分钟缓存),否则「顺大盘/逆势」这行
                # 在 /分析 里永远为空 —— 单品涨跌不与大盘对照就分不清"它弱"还是"全场弱"。
                market=await fetch_market_snapshot(self._cfg.csqaq, self._store),
            )

            # 资金周期：复用异动推送 detect_phase 的同阈值/同口径；只读，不写递进记忆。
            # V0.2.24 口径统一：求购=BUFF+悠悠、在售=BUFF+悠悠；缺任一平台在售字段均不可判。
            from bot.anomaly.cycle import detect_phase, digest_label, phase_history_ready
            from bot.anomaly.engine import HistoryPoint as _HP, MetricSnapshot as _MS
            analysis_now = datetime.datetime.now()
            phase_rows = [h for h in history_rows
                          if h.get("total_sell_num") is not None
                          and h.get("total_sell_scope") == "buff_yyyp"
                          and h.get("total_buy_num") is not None
                          and h.get("total_buy_scope") == "buff_yyyp"]
            phase_points = [
                _HP(ts=h.get("ts", ""),
                    price=float(h.get("buff_sell_price") or 0),
                    sell_num=int(h.get("total_sell_num") or 0),
                    buy_num=int(h.get("total_buy_num") or 0),
                    buy_price=float(h.get("buff_buy_price") or 0))
                for h in phase_rows
            ]
            phase_window = int(getattr(self._cfg.anomaly, "phase_window_minutes", 60))
            phase_ready = bool(
                liquidity["sell_complete"]
                and phase_history_ready(phase_points, analysis_now,
                                       min_points=(2 if phase_window >= 120 else None),
                                       window_minutes=phase_window)
            )
            phase_sig = None
            digest = None
            # V0.2.24: 单品详情自带两平台求购 → 阶段判定快照用 BUFF+悠悠求购合计
            buy_buff_i = int(d.get("buff_buy_num") or 0)
            buy_yyyp_i = int(d.get("yyyp_buy_num") or 0)
            buy_total_i = buy_buff_i + buy_yyyp_i
            if phase_ready:
                phase_snap = _MS(
                    good_id=gid, name=d.get("name") or name,
                    price=price, sell_price=price,
                    buy_price=float(d.get("buff_buy_price") or 0),
                    sell_num=int(liquidity["sell_num"] or 0),
                    buy_num=buy_total_i,
                    yyyp_sell_num=int(liquidity["yyyp_sell_num"] or 0),
                    buff_yyyp_complete=True,
                    rate_7=float(rate7_eff or 0),
                    rate_30=float(rate30_eff or 0),
                    price_7=float(d.get("sell_price_7") or 0),
                    price_30=float(d.get("sell_price_30") or 0),
                )
                phase_sig = detect_phase(
                    phase_snap, phase_points, analysis_now, self._cfg.anomaly,
                    remember=False,
                    min_points=(2 if phase_window >= 120 else None),
                    window_minutes=phase_window,
                )
                if phase_sig is None:
                    digest = digest_label(
                        phase_points, analysis_now, self._cfg.anomaly,
                        min_points=(2 if phase_window >= 120 else None),
                        window_minutes=phase_window,
                    )

            phase_key: Optional[str] = phase_sig.phase if phase_sig else ("digest" if digest else None)
            phase_lines = ["【资金周期】"]
            if not liquidity["sell_complete"]:
                phase_lines.append("不可判：当前缺少完整的 BUFF+悠悠在售数据。")
            elif not phase_ready:
                # V0.3.13:附上可执行动作 —— 老大问过"怎么才能进 B 池/怎么才有 2 小时快照",
                # 答案就是 /关注 它(watch_loop 每 15 分钟写一个历史点),这里直接说清楚。
                phase_lines.append(
                    "不可判：2小时总结快照历史不足（至少需要2个有效点，约2小时）。"
                    "该品不在雷达持续采样范围，想要 2 小时粒度请 /关注 它（每 15 分钟一个点）。"
                )
            elif phase_sig is not None:
                # V0.3.29:标题按「阶段 + 区间位置」取,不再固定说「吸筹期」——
                # 高位求购堆积是诱多,与低位吸筹含义相反。
                _pos = phase_sig.position or ""
                phase_lines.append(
                    f"{phase_sig.title}" + (f"（近 30 日区间{_pos}）" if _pos else "") + "。"
                )
                phase_lines.extend(line.strip() for line in phase_sig.lines[1:])
            elif digest:
                phase_lines.extend(["供给消化中（方向尚未选择）。", digest])
            else:
                phase_lines.append(
                    "暂无明确阶段：近1小时数据完整，但未满足建仓、启动、派发或消化阈值。"
                )

            mid_view = _mid_term_view(
                steam_points, price, rate7_eff, rate30_eff,
                str(action.get("t7_context") or ""),
            )
            # V0.3.2:成交口径(求购价/价差/扣费后到手)—— CS2 是分层流动性市场,只给在售价
            # 等于给了个卖不掉的价;高价品手续费本身也是决策量级。
            buy_px = float(d.get("buff_buy_price") or 0)
            proceeds = _proceeds_lines(price, buy_px, getattr(self._cfg, "fees", None), raw=d)
            action_extra = _action_extra_lines(action)
            # V0.3.13:「注：…」是口径说明而非数据,从【盘口与大盘】里摘出来,统一收口到末尾。
            action_notes = [x[2:].strip() for x in action_extra if x.startswith("注：")]
            action_extra = [x for x in action_extra if not x.startswith("注：")]
            buy_points = _left_right_buy_points(
                [float(p.get("close") or 0) for p in steam_points
                 if float(p.get("close") or 0) > 0],
                price, rate7_eff, rate30_eff,
                book_state=None, book_pressure=None,
                use_short_book=False,          # T+7 视角:短线盘口只作辅助,不进入买点条件
            )
            head = [
                f"📊 饰品分析: {d.get('name', name)}",
                f"当前售价: ¥{price:,.2f}",
                *proceeds,
                "",
                *phase_lines,
                "",
                *_format_mid_term_section(mid_view),
                "",
                *_format_t7_section(mid_view, rate1_eff, phase_key, phase_ready),
                "",
                "【盘口与大盘】",
                *(action_extra or ["盘口数据不足(无 30 分钟前快照对比)"]),
                "",
                "【流动性】",
            ]
            buff_sell = liquidity.get("buff_sell_num")
            yyyp_sell = liquidity.get("yyyp_sell_num")
            if liquidity["sell_complete"]:
                head.append(
                    f"BUFF+悠悠在售 {int(liquidity['sell_num'] or 0):,}"
                    f"（BUFF {int(buff_sell or 0):,} / 悠悠 {int(yyyp_sell or 0):,}）"
                )
            else:
                head.append("BUFF+悠悠在售不可用（两平台字段不完整）")
            # V0.2.24: 求购侧同步展示 BUFF+悠悠 合计（与资金周期判定同口径）
            head.append(
                f"BUFF+悠悠求购 {buy_total_i:,}"
                f"（BUFF {buy_buff_i:,} / 悠悠 {buy_yyyp_i:,}）"
            )
            # V0.3.12:分平台「变化量 + 当前值」(与异动推送同格式)。锚点行取与阶段判定
            # 相同的窗口(phase_window_minutes),口径一致;历史不足或字段不全时不显示,
            # 绝不显示半截数据。
            _win_min = int(getattr(self._cfg.anomaly, "phase_window_minutes", 60))
            _cut_ts = (analysis_now - datetime.timedelta(minutes=_win_min)
                       ).strftime("%Y-%m-%d %H:%M:%S")
            _base_row = None
            for _h in phase_rows:            # phase_rows 按时间升序
                if str(_h.get("ts") or "") <= _cut_ts:
                    _base_row = _h
                else:
                    break
            _cur_row = {"buff_sell_num": int(buff_sell or 0),
                        "yyyp_sell_num": int(yyyp_sell or 0),
                        "buff_buy_num": buy_buff_i,
                        "yyyp_buy_num": buy_yyyp_i}
            if _base_row is not None:
                for _side in ("sell", "buy"):
                    _line = book_split_line(normalize_book(_base_row),
                                            normalize_book(_cur_row), _side)
                    if _line:
                        head.append(f"{_line}　（近 {_win_min // 60} 小时）")
            # V0.3.13:历史不足时不再补一句"分平台变化:暂不可比"——上面【资金周期】已经在说
            # 不可判,老大 2026-10-10 反馈这是废话。没有可比的就不显示,不占行。

            buff_buy = buy_total_i
            buff_sell_i = int(buff_sell or 0)
            sell_total_i = int(liquidity.get("sell_num") or buff_sell_i)
            if sell_total_i > 0:
                ratio = buff_buy / sell_total_i
                static_state = ("静态买盘偏薄" if ratio < 0.3 else
                                ("静态买盘相对充足" if ratio >= 0.8 else "静态盘口中性"))
            else:
                static_state = "静态盘口比例不可判"
            dynamic_state = ("动态承接暂不可判" if not phase_ready
                             else "动态盘口见上方资金周期判定")
            head.append(
                f"当前 BUFF+悠悠 盘口：求购 {buy_total_i:,}、在售 {int(liquidity.get('sell_num') or buff_sell_i):,}；"
                f"{static_state}，{dynamic_state}。"
            )
            # V0.3.2:买点候选/确认项(此前 `_left_right_buy_points` 是死代码,正文从不显示;
            # 现按实测调权重接回:候选只是背景,确认项≥2 才提示"确认成立")
            if buy_points:
                # V0.3.13:买点块的「口径」行也收口到末尾,正文只留候选与观察点。
                bp_body = [x for x in buy_points
                           if not str(x).strip().startswith("口径")]
                buy_notes = [str(x).strip()[3:].strip() for x in buy_points
                             if str(x).strip().startswith("口径")]
                head.extend(["", "【买点候选】", *bp_body])
            else:
                buy_notes = []
                head.extend(["", "【买点候选】不可判：SteamDT 日K不足 20 根(★ 品/新品常见)"])
            tail_note = "；".join(x for x in [*action_notes, *buy_notes] if x)
            if tail_note:
                head.extend(["", f"⚠️ 口径: {tail_note}"])
            head.extend([f"⚠️ 分析仅供参考,不构成买卖建议。{hint}".strip()])
            chart = None
            if len(steam_points) >= 2:
                chart = make_item_kline_trend_png(steam_points, d.get("name", name))
            text = "\n".join(head)
            return (text, chart) if chart else text
        except Exception as exc:
            logger.exception("[analysis] 分析失败: %s", exc)
            return "机器人维护中，请稍后再试"

    # ------------------------------------------------------------------
    # 自选盯盘指令
    # ------------------------------------------------------------------

    async def _watch_slash(self, cmd: str, arg: Optional[str], message) -> str:
        group = getattr(message, "group_openid", "") if message else ""
        user = self._user_openid(message) if message else ""
        if not user:
            return "无法识别你的身份,稍后再试"

        # 群「盯盘」功能开关(V0.0.12):未开放的群全部拦截(含查询),私聊不限
        if group and not await self._feature_allowed(group, "watch"):
            return "该群未开放「盯盘」功能(管理员私聊: /功能 盯盘 <群号> 开)"

        if cmd == "watches":
            rows = await self._store.async_list_watches_by_user(group, user)
            idx_rows = await self._store.async_list_index_watches_by_user(group, user)
            text = cmds.format_watch_list(rows)
            if idx_rows:
                text += "\n\n" + cmds.format_index_watch_list(idx_rows)
            return text

        # 权限分层: vip(不限量) / free(限量) / none(无)
        tier = self._watch_tier(group)

        # 大盘指数盯盘:仅会员群
        idx_arg = (arg or "").strip()
        if idx_arg.startswith(("大盘", "指数", "市场")):
            if tier == "vip":
                return await self._index_watch_slash(cmd, idx_arg, group, user)
            return cmds.format_index_watch_vip_only()

        # 物品盯盘:非白名单群给出使用说明(openid 便于管理员加白)
        if tier == "none":
            return cmds.format_watch_intro(tier="none", group_openid=group)

        name, threshold, target, alert_type, param = cmds.parse_watch_arg(arg)
        if not name:
            usage = ("使用方式: /关注 饰品名 [目标价|涨跌X%|7日涨X%|7日跌X%|在售降X%|周期监测]\n"
                     "如 /关注 蝴蝶刀 大马士革钢 4500\n"
                     "/关注 蝴蝶刀 大马士革钢 15%\n"
                     "/关注 蝴蝶刀 大马士革钢 7日跌5%\n"
                     "/关注 蝴蝶刀 大马士革钢 在售降30%\n"
                     "/关注 蝴蝶刀 大马士革钢 周期监测  (四阶段:建仓/启动/派发,命中即@你)\n"
                     "—— 大盘指数盯盘(会员群专属) ——\n"
                     "/关注 大盘 跌3%  | /关注 大盘 涨5%\n"
                     "/关注 大盘 跌破2000 | /关注 大盘 突破2100")
            if cmd == "unwatch":
                usage = "用法: /取消关注 饰品名 ｜ /取消关注大盘"
            if tier == "free":
                usage += (f"\n\n✅ 本群已开启盯盘（免费群）\n"
                          f"⚠️ 每用户最多 {self._cfg.free.max_watches_per_user} 个盯盘项；"
                          f"升级会员群解锁不限量 + 大盘指数盯盘")
            return usage

        if cmd == "unwatch":
            name = (name or "").strip()
            if name in ("大盘", "指数", "市场"):
                n = await self._store.async_remove_all_index_watches_by_user(group, user)
                return "✅ 已取消关注大盘指数盯盘" if n else "你没关注过大盘指数"
            rows = await self._store.async_list_watches_by_user(group, user)
            matched = [r for r in rows if name in r["zh_name"] or name in r["market_hash_name"]]
            if not matched:
                return "你没关注过这个饰品"
            await self._store.async_remove_watch_by_id(matched[0]["id"])
            return cmds.format_watch_removed(matched[0]["zh_name"])

        # 免费群:每用户数量上限校验(取消关注不受限)
        if tier == "free":
            cur = len(await self._store.async_list_watches_by_user(group, user))
            if cur >= self._cfg.free.max_watches_per_user:
                return cmds.format_watch_limit_reached(self._cfg.free.max_watches_per_user)

        # 关注:解析英文名(词典 -> 全量索引) + 实时拿基准价
        try:
            async with CSQAQClient(self._cfg.csqaq) as api:
                result = await resolve_prices(api, self._store, name)
        except Exception as exc:
            logger.exception("[watch] 实时查价失败: %s", exc)
            return "机器人维护中，请稍后再试"
        if not result:
            return f"没查到「{name}」的价格,确认下名字,或到 csqaq.com 网页搜全名"
        mhn, row = next(iter(result.items()))
        # 批量查价回包通常缺少全平台求购/C5/IGXE/ECO 数量；关注建立时
        # 补取一次单品详情，确保在售基准和后续周期快照使用完整平台口径。
        gid = int(row.get("goodId") or row.get("good_id") or 0)
        if gid:
            try:
                async with CSQAQClient(self._cfg.csqaq) as api:
                    detail = await api.get_good_info(gid)
                if detail:
                    row = dict(row)
                    row.update(detail)
                    mhn = detail.get("market_hash_name") or mhn
            except Exception as exc:
                logger.warning("[watch] 关注全平台盘口补查失败 gid=%s: %s", gid, exc)
        zh = row.get("name") or mhn
        # V0.3.23:基准价以悠悠有品为主(BUFF 作对照一起列出);缺失时自动回退 BUFF/Steam。
        price, price_scope = watch_primary_price(row)
        if price <= 0:
            return "该饰品暂无价格数据,换个试试"
        buff_price, yyyp_price = watch_price_pair(row)
        # 在售骤降类型:以六个平台合计在售件数作为初始基准,避免首轮误触发。
        counts = aggregate_market_counts(row)
        sell_num = counts["sell_num"]
        await self._store.async_add_watch(
            group, user, mhn, zh, price, threshold or 10.0, int(row.get("goodId") or 0),
            target_price=target, alert_type=alert_type, param=param, last_sell_num=sell_num,
            last_sell_num_scope=("all" if counts["sell_complete"] else "partial"),
            base_price_scope=price_scope)
        _log.info("[watch] %s 关注 %s @ %s (类型%s 参数%s 阈值%s 目标%s 基准%s %.2f)",
                  user, zh, group or "私聊", alert_type, param, threshold or 10, target or "-",
                  price_scope or "-", price)
        return cmds.format_watch_added(zh, price, threshold, target, alert_type, param,
                                       buff_price=buff_price, yyyp_price=yyyp_price,
                                       scope=price_scope)

    async def _index_watch_slash(self, cmd: str, idx_arg: str, group: str, user: str) -> str:
        """大盘指数盯盘指令处理(触发源 = SteamDT 大盘指数)。"""
        # 去掉前缀"大盘/指数/市场"
        sub = re.sub(r"^(大盘|指数|市场)\s*", "", idx_arg).strip()
        if cmd == "unwatch":
            n = await self._store.async_remove_all_index_watches_by_user(group, user)
            return "✅ 已取消关注大盘指数盯盘" if n else "你没关注过大盘指数"
        if cmd == "watches":
            rows = await self._store.async_list_index_watches_by_user(group, user)
            return cmds.format_index_watch_list(rows)
        # 关注
        alert_type, param = cmds.parse_index_watch_arg(sub)
        if alert_type is None:
            return ("用法: /关注 大盘 [跌X%|涨X%|跌破N|突破N]\n"
                    "如 /关注 大盘 跌3%\n"
                    "/关注 大盘 涨5%\n"
                    "/关注 大盘 跌破2000\n"
                    "/关注 大盘 突破2100")
        if self._steamdt is not None:
            try:
                bi = await self._steamdt.get_broad_index()
                hint = f"\n当前指数 {bi['index']:,.2f} ({bi['diff_ratio_pct']:+.2f}%)"
            except Exception:
                hint = ""
        else:
            hint = ""
        await self._store.async_add_index_watch(group, user, alert_type, param)
        _log.info("[watch] %s 关注大盘 @ %s (类型%s 参数%s)", user, group or "私聊", alert_type, param)
        return cmds.format_index_watch_added(alert_type, param) + hint

    async def push_to_user(self, user_openid: str, content: str) -> bool:
        """主动私聊推送(用于盯盘提醒)，返回是否发送成功。"""
        if not user_openid:
            return False
        if self.is_invalid_user_target(user_openid):
            return False
        if len(content) > 3000:
            content = content[:2900] + "\n...(内容过长已截断)"
        async with self._reply_lock:
            try:
                await self.api.post_c2c_message(
                    openid=user_openid, msg_type=0, content=content)
                _log.info("[watch] 已私聊提醒 %s", user_openid)
                return True
            except Exception as exc:
                if self._is_gone_target_error(exc):
                    self._invalid_user_openids.add(str(user_openid))
                    logger.warning("[push] 私聊目标不存在/已注销，已标记为失效 user=%s", user_openid)
                else:
                    logger.exception("私聊推送失败 %s: %s", user_openid, exc)
                return False

    @staticmethod
    def _is_gone_target_error(exc: BaseException) -> bool:
        """识别 QQ 对已注销群/用户返回的终态错误。"""
        msg = str(exc or "")
        return any(token in msg for token in (
            "请求的资源不存在", "用户/群已注销", "资源不存在",
        ))

    def is_invalid_group_target(self, group_openid: str) -> bool:
        return bool(group_openid) and str(group_openid) in self._invalid_group_openids

    def is_invalid_user_target(self, user_openid: str) -> bool:
        return bool(user_openid) and str(user_openid) in self._invalid_user_openids

    async def _handle_natural(self, raw: str, session_key: str, message=None) -> str:
        # 固定信息类意图(帮助/菜单/状态)永远走本地,保证确定性,不被 AI 顶掉
        cmd, _ = cmds.parse_message(raw, self._cfg.qq)
        if cmd == "help":
            return cmds.format_help()
        if cmd == "status":
            fetch_time = await self._store.async_last_fetch_time()
            latest = await self._store.async_query_latest(limit=1)
            return cmds.format_status(fetch_time, len(latest) if fetch_time else 0)
        # 自然语言盯盘指令(关注/盯/盯着/帮我关注...):走本地盯盘,不走 AI
        watch_arg = self._extract_watch_arg(raw)
        if watch_arg is not None:
            return await self._watch_slash("watch", watch_arg, message)
        # 自然语言分析指令("分析 xxx"/"帮我分析 xxx"/"分析一下 xxx"):走本地 /分析,不走 AI
        m = re.match(r"^(?:帮我|帮忙)?分析(?:一下|下)?\s*(.+)$", raw.strip())
        if m and m.group(1).strip():
            return await self._analysis_reply(m.group(1).strip(), message)
        # V0.3.39:只发了一个饰品名(「@机器人 手套武器箱」)—— 本地索引能唯一命中就直接回实时价格。
        # 之前这种消息落到 chat,要么被 AI 闲聊掉、要么回「没有听懂」,而价格其实一直在库里。
        if cmd == "chat":
            bare = await self._bare_item_price(raw)
            if bare is not None:
                return bare
        # 其余自然语言:AI 优先
        if self._agent and self._agent.enabled:
            if self._cfg.llm.route_all_nl:
                return await self._ai_or_fallback(raw, session_key, message)
            # route_all_nl=False:仅本地未识别意图交给 AI
            if cmd == "chat":
                return await self._ai_or_fallback(raw, session_key, message)
        return await self._local_intent(raw, message)

    async def _ai_or_fallback(self, raw: str, session_key: str,
                              message=None) -> Union[str, Tuple[str, Optional[str]]]:
        """调 AI 智能体,超时或异常则降级本地意图。"""
        try:
            reply = await asyncio.wait_for(
                self._agent.handle(raw, session_key), timeout=25)
        except asyncio.TimeoutError:
            logger.warning("[agent] 回复超时(>25s),降级本地意图")
            reply = await self._local_intent(raw, message)
        except Exception as exc:
            logger.exception("[agent] AI 处理失败,降级本地意图: %s", exc)
            reply = await self._local_intent(raw, message)
        # 取出 agent 暂存的走势图(若有),与文本一起回传
        chart = self._agent.pop_chart(session_key) if self._agent else None
        return (reply, chart) if chart else reply

    async def _local_intent(self, raw: str, message=None) -> str:
        """本地意图识别(原逻辑,作为 AI 不可用时的降级)。"""
        cmd, arg = cmds.parse_message(raw, self._cfg.qq)

        if cmd == "help":
            return cmds.format_help()

        if cmd == "status":
            fetch_time = await self._store.async_last_fetch_time()
            latest = await self._store.async_query_latest(limit=1)
            return cmds.format_status(fetch_time, len(latest) if fetch_time else 0)

        if cmd == "report":
            return await self._report_reply(message)

        if cmd == "recommend":
            return await self._recommend_reply(arg)

        if cmd == "price":
            return await self._price_reply(arg)

        if cmd == "rank":
            return await self._rank_reply(arg)

        if cmd == "chat":
            return cmds.format_chat_fallback()

        return "没有听懂哦~ 发送 /帮助 查看我能做什么 🎮"

    # ------------------------------------------------------------------
    # 各指令实现
    # ------------------------------------------------------------------

    async def _broad_report(self) -> Tuple[str, Optional[str]]:
        """统一大盘播报:SteamDT 大盘指数卡 + 真实历史走势 PNG。

        所有播报入口(/大盘、/播报、自然语言"大盘播报"、每日定时、AI 播报)共用,
        保证内容一致且只有 SteamDT 单源(大盘走 SteamDT,不混入 CSQAQ 指数)。
        SteamDT 不可用时回退 CSQAQ 饰品指数(纯文本,无图)。
        """
        if self._steamdt is None:
            return "大盘数据源未配置(SteamDT),请在 config.yaml 填 steamdt.api_key", None
        try:
            bi = await self._steamdt.get_broad_index()
        except SteamDTRateLimit as exc:
            # 良性限频/配额耗尽(含空数据软降级):预期内降级,单行 WARNING 不打全栈
            logger.warning("[broad] SteamDT 限频/配额耗尽(%s),走 CSQAQ 兜底", str(exc)[:80])
            try:
                return await self._csqaq_fallback_card(), None
            except Exception:
                return "机器人维护中，请稍后再试", None
        except Exception as exc:
            logger.exception("[broad] SteamDT 大盘获取失败: %s", exc)
            try:
                return await self._csqaq_fallback_card(), None
            except Exception:
                return "机器人维护中，请稍后再试", None
        # CSQAQ 市场全景(大盘头条以外的市场数据,标注来源;失败不影响 SteamDT 卡)
        csqaq_data = None
        try:
            async with CSQAQClient(self._cfg.csqaq) as api:
                csqaq_data = await api.get_current_data(type="init")
            # V0.3.17:大盘/播报/每日定时播报三条入口都经这里 → 顺带按日归档情绪
            await archive_sentiment(self._store, csqaq_data)
        except Exception as exc:
            logger.warning("[broad] CSQAQ 市场全景获取失败(仍发 SteamDT 卡): %s", exc)
        text = cmds.format_broad_full(bi, csqaq_data)
        chart = None
        hist = bi.get("history") or []
        if len(hist) >= 2:
            prices = [v for _, v in hist]
            labels = [datetime.datetime.fromtimestamp(ts).strftime("%m-%d %H") for ts, _ in hist]
            try:
                chart = make_trend_png(prices, "CS2 大盘指数", prices[-1], labels=labels)
            except Exception as exc:
                logger.warning("[broad] 走势图生成失败: %s", exc)
                chart = None
        return text, chart

    async def _csqaq_fallback_card(self) -> str:
        """SteamDT 不可用时的 CSQAQ 兜底卡(V0.2.2):饰品指数头条 + 完整市场全景。

        V0.0.7 的兜底只渲染了「饰品指数」两行,而美金卡价/在线人数/涨跌分布/热度/
        分类指数**就在同一个 get_current_data 回包里**却被丢弃——用户查 /大盘 时
        看到内容"缺了一大截"。本方法复用 format_broad_panorama 渲染全部全景,
        与正常路径(SteamDT 头条 + CSQAQ 全景)只差头条来源。
        """
        async with CSQAQClient(self._cfg.csqaq) as api:
            c = await api.get_current_data(type="init")
        # V0.3.17:SteamDT 挂掉时也照样归档情绪(这条路径同样拿到完整回包)
        await archive_sentiment(self._store, c)
        d = (c.get("data") or {}).get("sub_index_data") or []
        main = d[0] if d else {}
        head = ("⚠️ SteamDT 大盘暂时取不到，CSQAQ 兜底\n"
                f"📊 饰品指数：{float(main.get('market_index') or 0):,.2f} "
                f"({float(main.get('chg_rate') or 0):+.2f}%)")
        pano = cmds.format_broad_panorama(c)
        if pano:
            head += "\n" + pano
        head += "\n数据来源：CSQAQ.COM ｜ 仅供参考，投资有风险"
        return head

    async def _anomaly_reply(self, kind: Optional[str] = None) -> str:
        """/异动 命令:异动雷达榜。支持子命令 强/上涨/下跌/需求/低位/状态。"""
        # 状态面板:B/A/S 三层池规模 + 健康度
        if kind == "status":
            counts = await self._store.async_count_watch_pool()
            rows = await self._store.async_list_active_anomalies(50)
            latest = rows[0].get("ts") if rows else None
            return cmds.format_anomaly_status(counts, latest, api_ok=True,
                                              recent_cnt=len(rows))
        rows = await self._store.async_list_active_anomalies(50)
        rows = cmds.filter_anomalies(rows, kind)
        return cmds.format_anomaly_report(rows, top_n=10, kind=kind)

    async def _report_reply(self, message=None) -> str:
        """/播报:与 /大盘 统一为 SteamDT 大盘卡(避免与定时播报/大盘指令内容不一致)。
        群内使用需该群开放「播报」功能(默认开);私聊不限。"""
        group = getattr(message, "group_openid", "") if message else ""
        if group and not await self._feature_allowed(group, "report"):
            return "该群未开放「播报」功能(管理员私聊: /功能 播报 <群号> 开)"
        text, chart = await self._broad_report()
        return (text, chart) if chart else text

    async def _broad_index_reply(self, message=None):
        """/大盘 指令:SteamDT 大盘指数卡片 + 真实历史走势 PNG。
        群内使用需该群开放「播报」功能(默认开);私聊不限。"""
        group = getattr(message, "group_openid", "") if message else ""
        if group and not await self._feature_allowed(group, "report"):
            return "该群未开放「播报」功能(管理员私聊: /功能 播报 <群号> 开)"
        return await self._broad_report()

    async def _recommend_reply(self, arg: Optional[str]) -> str:
        strategy = cmds.resolve_strategy(arg)
        # 优先走 CSQAQ 官方实时榜单，直接对齐网页排行
        try:
            async with CSQAQClient(self._cfg.csqaq) as api:
                result = await self._recommender.recommend_realtime(api, strategy)
        except Exception as exc:
            logger.exception("[recommend] 实时推荐失败: %s", exc)
            return "机器人维护中，请稍后再试"
        # 实时为空时回退本地快照（liquid/day_move 等无官方排序的策略）
        if not result.items and strategy not in ("low_7d", "dip_7d", "hot_7d"):
            rows = await self._store.async_query_latest(limit=2000)
            if not rows:
                return "数据采集中，请 5 分钟后再试 📡"
            result = self._recommender.recommend(rows, strategy)
        fetch_time = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        return cmds.format_recommend(result, fetch_time)

    async def _bare_item_price(self, raw: str) -> Optional[Union[str, Tuple[str, Optional[str]]]]:
        """裸品名 → 实时价格(V0.3.39)。

        「@倒货波 手套武器箱」这种消息没有任何指令词,原来会落到 chat。
        这里用本地 goods_index 归一:能唯一命中才查价(一次 CSQAQ API),命中不了就返回 None
        让上游继续走 AI/闲聊 —— 不猜、不瞎答。
        """
        txt = (raw or "").strip()
        # 超过 24 字多半是句子,不是品名;太短(1 字)根本没法判断
        if not txt or len(txt) > 24:
            return None
        try:
            name = await self._store.async_resolve_goods_name(txt)
        except Exception as exc:  # 索引异常绝不能把消息处理打挂
            logger.warning("[price] 裸品名查询失败: %s", exc)
            return None
        if not name:
            return None
        logger.info("[price] 裸品名命中: %s → %s", txt, name)
        return await self._price_reply(name)

    async def _price_reply(self, arg: Optional[str]) -> Union[str, Tuple[str, Optional[str]]]:
        keyword = (arg or "").strip()
        if not keyword:
            return "请带上饰品名，如 /价格 爪子刀 外表生锈"
        # 全实时查价,固定格式输出(不走本地快照)
        try:
            from bot.csqaq.price_lookup import answer_price
            async with CSQAQClient(self._cfg.csqaq) as api:
                text, chart = await answer_price(api, self._store, keyword, steamdt=self._steamdt)
        except Exception as exc:
            logger.exception("[price] 实时查价失败: %s", exc)
            return "机器人维护中，请稍后再试"
        # 优先带走势图(图片由 _reply_group/_reply_c2c 发送)
        return (text, chart) if chart else text

    async def _rank_reply(self, arg: Optional[str]) -> str:
        # 与 /推荐 统一:跌/涨榜复用实时官方榜 + format_recommend,内容完全一致
        direction = "down" if arg and "跌" in arg else "up"
        strategy = "hot_7d" if direction == "up" else "dip_7d"
        try:
            async with CSQAQClient(self._cfg.csqaq) as api:
                result = await self._recommender.recommend_realtime(api, strategy)
        except Exception as exc:
            logger.exception("[rank] 实时排行失败: %s", exc)
            return "机器人维护中，请稍后再试"
        fetch_time = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        return cmds.format_recommend(result, fetch_time)

    # ------------------------------------------------------------------
    # 发送
    # ------------------------------------------------------------------

    async def _reply_group(self, message: GroupMessage, content: str, image_path: Optional[str] = None):
        group_openid = getattr(message, "group_openid", "")
        if not group_openid:
            return
        # 群消息长度限制约 4000 字符，超长截断
        if len(content) > 3000:
            content = content[:2900] + "\n...(内容过长已截断)"
        async with self._reply_lock:
            try:
                await self.api.post_group_message(
                    group_openid=group_openid,
                    msg_type=0,
                    msg_id=message.id,
                    content=content,
                )
            except Exception as exc:
                logger.exception("发送群消息失败: %s", exc)
        # 走势图:作为第二条消息(富媒体)随文本发出
        if image_path and os.path.exists(image_path):
            try:
                await self._send_group_image(group_openid, image_path, msg_id=message.id)
            except Exception as exc:
                logger.exception("[media] 群走势图发送失败: %s", exc)
            finally:
                _safe_remove(image_path)

    async def _reply_c2c(self, message: GroupMessage, content: str, image_path: Optional[str] = None):
        """回复私聊消息。"""
        author = getattr(message, "author", None)
        user_openid = getattr(author, "user_openid", "") if author else ""
        if not user_openid:
            return
        if len(content) > 3000:
            content = content[:2900] + "\n...(内容过长已截断)"
        async with self._reply_lock:
            try:
                await self.api.post_c2c_message(
                    openid=user_openid,
                    msg_type=0,
                    msg_id=message.id,
                    content=content,
                )
            except Exception as exc:
                logger.exception("发送私聊消息失败: %s", exc)
        if image_path and os.path.exists(image_path):
            try:
                await self._send_c2c_image(user_openid, image_path, msg_id=message.id)
            except Exception as exc:
                logger.exception("[media] 私聊走势图发送失败: %s", exc)
            finally:
                _safe_remove(image_path)

    # ------------------------------------------------------------------
    # 走势图发送(QQ 群/私聊富媒体:先上传拿 file_info,再以 msg_type=7 发出)
    # ------------------------------------------------------------------

    async def _upload_group_media(self, group_openid: str, image_path: str) -> Optional[str]:
        """上传群图片(分片上传),返回 file_info;失败返回 None。"""
        return await self._upload_media("group", group_openid, image_path)

    async def _upload_c2c_media(self, openid: str, image_path: str) -> Optional[str]:
        """上传私聊图片(分片上传),返回 file_info;失败返回 None。"""
        return await self._upload_media("c2c", openid, image_path)

    async def _upload_media(self, scene: str, openid: str, image_path: str) -> Optional[str]:
        """QQ 群/私聊本地图片上传(四步分片上传),返回 file_info。

        群/私聊机器人不支持频道那种 multipart file_image 直发,必须:
        1) upload_prepare 拿 upload_id + block_size + 各分片预签名 URL
        2) 逐片 PUT 到预签名 URL(COS)
        3) upload_part_finish 通知每片完成
        4) POST .../files 带 upload_id 合并,返回 file_info
        最后发消息用 msg_type=7 + media={"file_info": ...}
        """
        try:
            token = self.api._http._token
            if token is None or not getattr(token, "access_token", None):
                await self.api._http.check_session()
                token = self.api._http._token
            headers = {
                "Authorization": token.get_string(),
                "X-Union-Appid": token.app_id,
            }
            seg = "groups" if scene == "group" else "users"
            base = f"https://api.sgroup.qq.com/v2/{seg}/{openid}"
            with open(image_path, "rb") as f:
                data = f.read()
            if not data:
                logger.warning("[media] 图片为空,放弃上传")
                return None

            # 1) 预上传
            async with aiohttp.ClientSession() as s:
                async with s.post(f"{base}/upload_prepare", headers=headers, json={
                    "file_name": "trend.png",
                    "file_size": len(data),
                    "file_type": 1,            # 1=图片(注意不是 upload_type)
                    "target_id": openid,
                }) as r:
                    try:
                        prep = await r.json(content_type=None)
                    except Exception:
                        prep = {}
            if not prep.get("upload_id"):
                logger.warning("[media] upload_prepare 失败: %s", prep)
                return None
            upload_id = prep["upload_id"]
            block_size = int(prep.get("block_size", 5 * 1024 * 1024))
            parts = prep.get("parts") or []

            # 2)+3) 逐片上传并确认
            async with aiohttp.ClientSession() as s:
                for part in parts:
                    idx = int(part.get("index", 1))
                    start = (idx - 1) * block_size
                    end = min(start + block_size, len(data))
                    chunk = data[start:end]
                    async with s.put(part["presigned_url"], data=chunk,
                                     headers={"Content-Type": "application/octet-stream"}) as r:
                        if r.status != 200:
                            logger.warning("[media] 分片 PUT 失败 status=%s", r.status)
                            return None
                    async with s.post(f"{base}/upload_part_finish", headers=headers, json={
                        "upload_id": upload_id,
                        "part_index": idx,
                        "block_size": str(len(chunk)),
                        "md5": hashlib.md5(chunk).hexdigest(),
                    }) as r:
                        if r.status != 200:
                            logger.warning("[media] upload_part_finish 失败 status=%s", r.status)
                            return None

            # 4) 合并 -> file_info
            async with aiohttp.ClientSession() as s:
                async with s.post(f"{base}/files", headers=headers, json={
                    "file_type": 1,
                    "srv_send_msg": False,
                    "file_name": "trend.png",
                    "upload_id": upload_id,
                }) as r:
                    try:
                        j = await r.json(content_type=None)
                    except Exception:
                        j = {}
            _log.info("[media] 上传合并返回 code=%s", j.get("code"))
            return (j or {}).get("file_info")
        except Exception as exc:
            logger.exception("[media] 图片上传失败: %s", exc)
            return None

    async def _send_group_image(self, group_openid: str, image_path: str, msg_id: Optional[str] = None):
        """群内发送走势图(作为对用户消息的第二条回复,msg_seq=2 串联)。"""
        file_info = await self._upload_group_media(group_openid, image_path)
        if not file_info:
            logger.warning("[media] 群图片 file_info 为空,放弃发送")
            return
        await self.api.post_group_message(
            group_openid=group_openid,
            msg_type=7,
            media={"file_info": file_info},
            msg_id=msg_id,
            msg_seq=2,
        )

    async def _send_c2c_image(self, openid: str, image_path: str, msg_id: Optional[str] = None):
        """私聊发送走势图(作为第二条回复,msg_seq=2 串联)。"""
        file_info = await self._upload_c2c_media(openid, image_path)
        if not file_info:
            logger.warning("[media] 私聊图片 file_info 为空,放弃发送")
            return
        await self.api.post_c2c_message(
            openid=openid,
            msg_type=7,
            media={"file_info": file_info},
            msg_id=msg_id,
            msg_seq=2,
        )

    async def push_to_group(self, group_openid: str, content: str) -> bool:
        """主动推送消息到指定群（用于定时推送）。

        返回 True 表示 QQ 接口确认成功，False 表示发送失败。调用方需要
        根据返回值决定是否把业务事件标记为已发送，避免异常被吞掉后永久漏推。
        """
        if self.is_invalid_group_target(group_openid):
            return False
        if len(content) > 3000:
            content = content[:2900] + "\n...(内容过长已截断)"
        async with self._reply_lock:
            try:
                await self.api.post_group_message(
                    group_openid=group_openid,
                    msg_type=0,
                    content=content,
                )
                _log.info("[push] 已推送到群 %s", group_openid)
                return True
            except Exception as exc:
                if self._is_gone_target_error(exc):
                    self._invalid_group_openids.add(str(group_openid))
                    logger.warning("[push] 群不存在/已注销，已标记为失效 group=%s", group_openid)
                else:
                    logger.exception("推送失败 group=%s: %s", group_openid, exc)
                return False

    async def push_to_group_markdown(self, group_openid: str, content: str) -> bool:
        """主动推送 markdown 消息到指定群（用于含 @ 标签的提醒）。

        注意: QQ 的 <qqbot-at-user id="xxx" /> 标签仅在 markdown 消息(msg_type=2)
        中才会渲染成真正的 @用户,纯文本消息(msg_type=0)不渲染,只会显示原始标签。
        """
        if self.is_invalid_group_target(group_openid):
            return False
        if len(content) > 3000:
            content = content[:2900] + "\n...(内容过长已截断)"
        async with self._reply_lock:
            try:
                await self.api.post_group_message(
                    group_openid=group_openid,
                    msg_type=2,
                    markdown={"content": content},
                )
                _log.info("[push] 已推送 markdown 到群 %s", group_openid)
                return True
            except Exception as exc:
                if self._is_gone_target_error(exc):
                    self._invalid_group_openids.add(str(group_openid))
                    logger.warning("[push] 群不存在/已注销，已标记为失效 group=%s", group_openid)
                else:
                    logger.exception("推送 markdown 失败 group=%s: %s", group_openid, exc)
                return False
