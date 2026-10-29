# -*- coding: utf-8 -*-
"""周期阶段卡片的「状态 → 结论」规则表。

设计原则（2026-10-29 定）
------------------------------------------------------------------
卡片上每一句结论都必须由**当时的实际数据**决定，不允许写死。
同一句「求购 +15.6%」在不同位置的含义是相反的：

    低位（≤30% 分位） → 吸筹迹象，可以观察
    中位（30-70%）    → 方向不明，只观察
    高位（≥70% 分位） → 诱多预警，风险提示

所以标题、图标、判读、结论、尾注全部由 `(phase, position, 求购强度, 价格方向, 供给方向)`
组合生成，不出现与数据矛盾的固定句子。

统计量来源
------------------------------------------------------------------
现价 ≥¥100 的可交易品，T+7 前瞻，样本 2,483 例建仓事件
（回测脚本：tools/probe_accum_states.py，2026-10-29 跑）。

关键实测：上涨率几乎全部来自「位置」，建仓信号本身的增量很小

    位置     建仓后上涨率   该位置基准上涨率   建仓带来的增量
    低位        30.9%          31.2%            -0.3pp
    中位         7.5%           9.6%            -2.1pp
    高位         3.9%           3.3%            +0.6pp

也就是说：**低位的 30% 上涨率是位置本身的功劳，不是求购的功劳**。
卡片必须把这个对照写出来，否则等于把位置效应冒充成信号效应。

统计有保质期：更新数据后重跑 tools/probe_accum_states.py 覆盖 STATS 即可。
"""

from __future__ import annotations

from typing import Dict, List, Optional, Sequence, Tuple


# ---------------------------------------------------------------- 统计参照
# T+7 前瞻。n=样本数，up=上涨概率(%)，med=中位涨幅(%)，crash=跌超15%占比(%)。
# price_tier：统计所限定的现价档位，低于该价位的品不适用。
STATS_AS_OF = "2026-10-29"

STATS: Dict[Tuple[str, str], Dict[str, object]] = {
    # ---- 建仓：按位置分档（现价 ≥¥100）----
    ("accum", "低位"): {"n": 1757, "up": 30.9, "med": -1.82, "crash": 0.2,
                        "tier": "≥¥100", "base_up": 31.2, "base_n": 37400},
    ("accum", "中位"): {"n": 573, "up": 7.5, "med": -4.82, "crash": 1.4,
                        "tier": "≥¥100", "base_up": 9.6, "base_n": 16943},
    ("accum", "高位"): {"n": 153, "up": 3.9, "med": -6.57, "crash": 8.5,
                        "tier": "≥¥100", "base_up": 3.3, "base_n": 4736},
    # ---- 启动：追涨指纹，实测为负（现价 ≥¥10；样本偏薄，仅作参考）----
    ("launch", "低位"): {"n": 19, "up": 21.1, "med": -9.02, "crash": None,
                         "tier": "≥¥10", "base_up": 22.7, "base_n": 102060},
    ("launch", "中位"): {"n": 19, "up": 21.1, "med": -9.02, "crash": None,
                         "tier": "≥¥10", "base_up": 22.7, "base_n": 102060},
    ("launch", "高位"): {"n": 19, "up": 21.1, "med": -9.02, "crash": None,
                         "tier": "≥¥10", "base_up": 22.7, "base_n": 102060},
    # ---- 派发：唯一在可交易品上依然强有效的方向信号（现价 ≥¥10）----
    ("distribute", "低位"): {"n": 35, "up": 2.9, "med": -13.55, "crash": None,
                             "tier": "≥¥10", "base_up": 22.7, "base_n": 102060},
    ("distribute", "中位"): {"n": 35, "up": 2.9, "med": -13.55, "crash": None,
                             "tier": "≥¥10", "base_up": 22.7, "base_n": 102060},
    ("distribute", "高位"): {"n": 35, "up": 2.9, "med": -13.55, "crash": None,
                             "tier": "≥¥10", "base_up": 22.7, "base_n": 102060},
}

# 分档阈值（与回测脚本保持完全一致，改这里必须同步重跑回测）
POS_LOW_MAX = 30.0      # ≤30% 分位 = 低位
POS_HIGH_MIN = 70.0     # ≥70% 分位 = 高位

_POS_ORDER = ("低位", "中位", "高位")


# ---------------------------------------------------------------- 状态计算
def range_pct(range_30d: Optional[Sequence[float]], price: Optional[float]) -> Optional[float]:
    """现价在近 N 日价格区间中的分位（0=区间最低，100=区间最高）。

    与「30 日涨幅」不同：涨过又跌回来的品不会被误判为高位。
    区间退化（最高=最低，如长期无成交）或数据缺失时返回 None。
    """
    if not range_30d or not price:
        return None
    try:
        lo, hi = float(range_30d[0]), float(range_30d[1])
    except (TypeError, ValueError):
        return None
    if not (hi > lo) or price <= 0:
        return None
    pct = (float(price) - lo) / (hi - lo) * 100.0
    return max(0.0, min(100.0, pct))


def pos_of(pct: Optional[float]) -> str:
    """分位 → 位置档。分位缺失时返回「中位」（最保守：既不称低位也不称高位）。"""
    if pct is None:
        return "中位"
    if pct <= POS_LOW_MAX:
        return "低位"
    if pct >= POS_HIGH_MIN:
        return "高位"
    return "中位"


def buy_strength(buy_chg: Optional[float]) -> str:
    """求购变化强度分档(与回测的 30/15 分界一致)。

    用「偏大/中等/偏小」而不是「强/中/弱」:本信号的门槛是 +12%,能触发的都不是弱需求,
    说「弱」会和「净增 140 单」的事实打架。
    """
    if buy_chg is None:
        return "未知"
    if buy_chg >= 30:
        return "偏大"
    if buy_chg >= 15:
        return "中等"
    return "偏小"


def sell_trend(sell_chg: Optional[float]) -> str:
    """供给方向：收缩 / 持平 / 增加。"""
    if sell_chg is None:
        return "未知"
    if sell_chg <= -3.0:
        return "收缩"
    if sell_chg >= 3.0:
        return "增加"
    return "持平"


def price_trend(price_chg: Optional[float]) -> str:
    """价格方向：横盘 / 微涨 / 微跌。"""
    if price_chg is None:
        return "未知"
    if abs(price_chg) < 1.0:
        return "横盘"
    return "微涨" if price_chg > 0 else "微跌"


# ---------------------------------------------------------------- 标题与图标
_HEADLINES = {
    ("accum", "低位"): ("👁", "低位吸筹迹象"),
    ("accum", "中位"): ("👁", "建仓迹象"),
    ("accum", "高位"): ("🚨", "高位诱多预警"),
    ("launch", "低位"): ("🚀", "启动确认"),
    ("launch", "中位"): ("🚀", "启动确认"),
    ("launch", "高位"): ("🚀", "高位追涨确认"),
    ("distribute", "低位"): ("🚨", "派发预警"),
    ("distribute", "中位"): ("🚨", "派发预警"),
    ("distribute", "高位"): ("🚨", "派发预警"),
}


def headline(phase: str, position: str) -> Tuple[str, str]:
    """(图标, 标题)。未知组合回退到建仓中位口径。"""
    return _HEADLINES.get((phase, position)) or _HEADLINES[("accum", "中位")]


# ---------------------------------------------------------------- 判读文案
def _pos_sentence(pct: Optional[float], position: str) -> str:
    if pct is None:
        return f"近 30 日区间数据不足，位置按{position}处理（低位 ≤30%、高位 ≥70%）。"
    return (f"现价位于近 30 日区间 {pct:.0f}% 分位"
            f"（低位 ≤{POS_LOW_MAX:.0f}%、高位 ≥{POS_HIGH_MIN:.0f}%），属{position}。")


def _supply_sentence(sell_chg: Optional[float], ratio: Optional[float]) -> str:
    trend = sell_trend(sell_chg)
    ratio_txt = f"在售/求购 = {ratio:.1f}" if (ratio and ratio > 0) else ""
    if trend == "收缩":
        core = "在售下降，供给正在被消化"
    elif trend == "增加":
        core = "在售同步增加，抛压未消化"
    elif trend == "持平":
        core = "在售基本持平，供给未变化"
    else:
        core = "在售数据缺失"
    return f"{core}" + (f"（{ratio_txt}）" if ratio_txt else "") + "。"


def reading(phase: str, position: str, pct: Optional[float],
            buy_chg: Optional[float], sell_chg: Optional[float],
            price_chg: Optional[float], ratio: Optional[float],
            samples: Optional[int] = None) -> List[str]:
    """生成「判读」三段：位置 + 盘口性质 + 该位置的判断。全部由状态决定。"""
    lines = [_pos_sentence(pct, position)]
    strength = buy_strength(buy_chg)
    pt = price_trend(price_chg)

    if phase == "accum":
        if pt == "横盘":
            move = "求购持续累积、价格未抬升"
        elif pt == "微跌":
            move = "求购累积而价格小幅下跌"
        else:
            move = "求购累积且价格小幅抬升"
        if position == "低位":
            judge = "低位出现该形态，接近真实吸筹，是三个位置里唯一值得跟踪的。"
        elif position == "高位":
            judge = "高位出现该形态多为出货前的诱多，性质与低位吸筹相反。"
        else:
            judge = "中位下方无区间参照，吸筹与出货难以区分。"
        lines.append(f"{move}（求购强度：{strength}），{_supply_sentence(sell_chg, ratio)}")
        lines.append(judge)
    elif phase == "launch":
        lines.append(f"求购价上移、价格跟涨，属追涨形态（求购{strength}）。"
                     + _supply_sentence(sell_chg, ratio))
        lines.append("该形态在可交易品上历史为负超额，不宜追入。")
    elif phase == "distribute":
        lines.append("价格处区间高位，在售快速增加，求购停滞。" + _supply_sentence(sell_chg, ratio))
        lines.append("属出货期指纹，是负面信号。")
    else:
        lines.append(_supply_sentence(sell_chg, ratio))

    if samples is not None and samples < 3:
        lines.append(f"注意：窗口内仅 {samples} 个采样点，趋势判断可靠性有限。")
    return lines


# ---------------------------------------------------------------- 统计参照
def stat_lines(phase: str, position: str, price: Optional[float]) -> List[str]:
    """「历史参照」：查表 + 与同位置基准对照 + 由数据推出的结论。"""
    st = STATS.get((phase, position))
    if not st:
        return ["　该状态样本不足，不作统计引用。"]
    tier = str(st.get("tier") or "")
    if price and tier.startswith("≥¥"):
        try:
            floor = float(tier[2:])
        except ValueError:
            floor = 0.0
        if price < floor:
            return [f"　本形态统计仅覆盖现价 {tier} 的品，当前价 ¥{price:,.0f} 不在统计范围内。"]

    n = int(st["n"])
    up = float(st["up"])
    med = float(st["med"])
    base_up = st.get("base_up")
    head = f"　{position}形态（现价 {tier}）{n:,} 例，T+7：上涨 {up:.1f}%、中位 {med:+.2f}%"
    crash = st.get("crash")
    if crash:
        head += f"、跌超15% 占 {float(crash):.1f}%"
    lines = [head]

    if base_up is not None:
        delta = up - float(base_up)
        lines.append(f"　对照：该位置任意时刻上涨 {float(base_up):.1f}%")
        if delta >= 3.0:
            judge = f"本形态较该位置基准高 {delta:+.1f}pp，有额外信息量"
        elif delta <= -3.0:
            judge = f"本形态较该位置基准低 {delta:+.1f}pp，反而更差"
        else:
            judge = f"本形态较该位置基准仅 {delta:+.1f}pp，无显著信息量"
        lines.append(f"　结论：{judge}；中位 {med:+.2f}%，{'不构成买点' if med < 0 else '中位为正'}")
    if n < 50:
        lines.append(f"　（样本仅 {n} 例，统计稳定性有限）")
    return lines


def stat_as_of_line() -> str:
    return f"▍历史参照（统计截至 {STATS_AS_OF}）"


# ---------------------------------------------------------------- 尾注
def footer(phase: str, position: str) -> str:
    """卡片最后一行的行动提示，由阶段与位置决定。"""
    if phase == "accum":
        if position == "高位":
            return "⚠️ 高位不建议介入；若已持有，注意及时止盈。"
        if position == "低位":
            return "⚠️ 观察类信号。真要介入，等价格企稳、在售转为下降后再看。"
        return "⚠️ 观察类信号，非买入依据。"
    if phase == "launch":
        return "⚠️ 追涨形态历史为负超额，不建议追入。"
    if phase == "distribute":
        return "⚠️ 持有者宜考虑减仓；未持有者暂缓介入。"
    return "⚠️ 仅供观察，不构成买卖建议。"


def position_line(phase: str, position: str, pct: Optional[float]) -> str:
    """「位置」一句话（写进卡片的位置段）。"""
    if pct is None:
        return f"近 30 日位置　{position}（区间数据不足）"
    return f"近 30 日位置　{position}（{pct:.0f}% 分位）"
