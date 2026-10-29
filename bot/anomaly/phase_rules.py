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

# 键 = (阶段, 位置)；位置为 "*" 表示「不分位置」的合计样本。
# 启动/派发没有按位置拆的样本量(启动 n≈19、派发 n≈59),硬凑三档等于伪造粒度,
# 所以只给合计,并在文案里注明「不限位置」。
STATS: Dict[Tuple[str, str], Dict[str, object]] = {
    # ---- 建仓：按位置分档（现价 ≥¥100，tools/probe_bid_lift.py 的 E 组）----
    # 「进价未抬」= 求购量涨但最高求购价没动 —— 求购单不锁资金,这种堆积零成本,所以没有增量。
    ("accum", "低位"): {"n": 4581, "up": 32.7, "med": -1.91, "crash": None,
                        "tier": "≥¥100", "base_up": 31.6, "base_n": 57360,
                        "up_24h": 36.2, "med_24h": -0.14, "base_up_24h": 34.1, "n_24h": 6402},
    ("accum", "中位"): {"n": 2171, "up": 12.2, "med": -4.39, "crash": None,
                        "tier": "≥¥100", "base_up": 11.3, "base_n": 27560,
                        "up_24h": 21.7, "med_24h": -0.63, "base_up_24h": 22.8, "n_24h": 3214},
    ("accum", "高位"): {"n": 840, "up": 6.0, "med": -5.78, "crash": None,
                        "tier": "≥¥100", "base_up": 4.4, "base_n": 9508,
                        "up_24h": 17.9, "med_24h": -0.39, "base_up_24h": 17.6, "n_24h": 942},
    # ---- 建仓 + 求购价上移 ≥3%（「抬价收货」：庄家愿意多付,卖家还在让价）----
    # 这是整套信号里唯一在低位有明显增量的子形态:T+24h 44.2% vs 基准 34.1%(+10.1pp),
    # T+7 39.7% vs 31.6%(+8.1pp)；但中位仍为负(-1.30%),是「赢面变大」不是「稳赚」。
    # 中位只有 +3.5pp、高位样本 n=50 且中位更差,都不足以支撑结论。
    ("accum+lift", "低位"): {"n": 393, "up": 39.7, "med": -1.30, "crash": None,
                             "tier": "≥¥100", "base_up": 31.6, "base_n": 57360,
                             "up_24h": 44.2, "med_24h": -0.01, "base_up_24h": 34.1, "n_24h": 502},
    ("accum+lift", "中位"): {"n": 108, "up": 14.8, "med": -4.28, "crash": None,
                             "tier": "≥¥100", "base_up": 11.3, "base_n": 27560,
                             "up_24h": 24.6, "med_24h": -0.72, "base_up_24h": 22.8, "n_24h": 142},
    ("accum+lift", "高位"): {"n": 50, "up": 12.0, "med": -7.50, "crash": None,
                             "tier": "≥¥100", "base_up": 4.4, "base_n": 9508,
                             "up_24h": 16.4, "med_24h": -0.96, "base_up_24h": 17.6, "n_24h": 61},
    # ---- 启动：追涨指纹，实测为负（现价 ≥¥10；样本薄，仅作方向参考）----
    ("launch", "*"): {"n": 19, "up": 21.1, "med": -9.02, "crash": None,
                      "tier": "≥¥10", "base_up": 22.7, "base_n": 102060},
    # ---- 派发：唯一在可交易品上依然强有效的方向信号（现价 ≥¥10）----
    # 口径已按 V0.3.30 单边条件(求购不增)重算：24h n=131 上涨 11.5% / 中位 -3.31%;
    # T+7 n=59 上涨 6.8% / 中位 -11.90%;基准 24h 上涨 29.5% / T+7 上涨 24.5% / 中位 -3.12%。
    # 派发是短周期事件,24h 的信息量比 7 日更直接(7 日后价格已经开始新的平衡),所以两个周期都给。
    ("distribute", "*"): {"n": 59, "up": 6.8, "med": -11.90, "crash": None,
                          "tier": "≥¥10", "base_up": 24.5, "base_n": 131337,
                          "up_24h": 11.5, "med_24h": -3.31, "base_up_24h": 29.5,
                          "n_24h": 131},
}

# 位置档（用于标题与措辞）
POS_LOW_MAX = 30.0      # ≤30% 分位 = 低位
POS_HIGH_MIN = 70.0     # ≥70% 分位 = 高位


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
def _pos_sentence(pct: Optional[float], position: str, window_days: int = 30) -> str:
    if pct is None:
        return (f"近 {window_days} 日区间数据不足，位置按{position}处理"
                f"（低位 ≤{POS_LOW_MAX:.0f}%、高位 ≥{POS_HIGH_MIN:.0f}%）。")
    return (f"现价位于近 {window_days} 日区间 {pct:.0f}% 分位"
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
            samples: Optional[int] = None, window_days: int = 30,
            bid_chg: Optional[float] = None) -> List[str]:
    """生成「判读」三段：位置 + 盘口性质 + 该位置的判断。全部由状态决定。"""
    lines = [_pos_sentence(pct, position, window_days)]
    strength = buy_strength(buy_chg)
    pt = price_trend(price_chg)

    if phase.startswith("accum"):
        lift = phase == "accum+lift"
        # 价格方向必须四个分支都写全:"未知"不能落到"小幅抬升"那一档(旧写法会默认抬升)。
        move = {"横盘": "求购持续累积、价格未抬升",
                "微跌": "求购累积而价格小幅下跌",
                "微涨": "求购累积且价格小幅抬升",
                "未知": "求购持续累积（价格数据缺失）"}.get(pt, "求购持续累积")
        if lift and bid_chg is not None:
            move += f"、最高求购价上移 {bid_chg:+.1f}%"
        # 判读句必须和「历史参照」用同一个数字，否则会出现
        # 判读说「值得跟踪」、下一段结论说「无显著信息量」的自相矛盾。
        st, _scope, _by_pos = _stat_for(phase, position)
        edge = None
        if st and st.get("base_up") is not None:
            edge = float(st["up"]) - float(st["base_up"])
        if position == "低位":
            if lift:
                judge = ("低位抬价收货：有人愿意多付钱卡卖家，而卖方还在让价 —— "
                         "这是本指纹里最接近真实建仓的一种，也是唯一在低位有明显增量的子形态。")
            elif edge is None:
                judge = "低位出现该形态：下方有区间支撑，但样本不足，只作观察记录。"
            elif edge >= 3.0:
                judge = f"低位出现该形态：较该位置基准多 {edge:+.1f}pp，可作跟踪目标。"
            else:
                judge = ("低位出现该形态：求购量涨了但求购价没动，"
                         "而求购单不锁资金、堆量零成本，所以这些增量不说明有资金进场。")
        elif position == "高位":
            judge = "高位出现该形态多为出货前的诱多：求购堆积却不推价，是挂给跟风盘看的。"
        else:
            judge = "中位下方没有区间参照，吸筹与出货难以区分。"
        lines.append(f"{move}（求购强度：{strength}），{_supply_sentence(sell_chg, ratio)}")
        lines.append(judge)
    elif phase == "launch":
        lines.append(f"求购价上移、价格跟涨，属追涨形态（求购强度：{strength}）。"
                     + _supply_sentence(sell_chg, ratio))
        lines.append("该形态在可交易品上历史为负，且样本偏薄，不宜追入。")
    elif phase == "distribute":
        # 不在这里再写一遍「在售增加」——_supply_sentence 已经说过,重复会读成两件事。
        ratio_txt = f"（在售/求购 = {ratio:.1f}）" if (ratio and ratio > 0) else ""
        # 求购侧分两种说法:撤单(庄家先撤买单)是最典型的 CS 出货前动作,值得单独点出来。
        if buy_chg is not None and buy_chg <= -2.0:
            buy_txt = f"求购撤单 {buy_chg:+.1f}%"
        else:
            buy_txt = "求购未增"
        lines.append(f"价格处区间高位，在售快速增加，{buy_txt}{ratio_txt}。")
        lines.append("属出货期指纹：高位放货而需求端没有承接，是负面信号。")
    else:
        lines.append(_supply_sentence(sell_chg, ratio))

    if samples is not None and samples < 3:
        lines.append(f"注意：窗口内仅 {samples} 个采样点，趋势判断可靠性有限。")
    return lines


# ---------------------------------------------------------------- 统计参照
def _stat_for(phase: str, position: str) -> Tuple[Optional[Dict[str, object]], str, bool]:
    """取统计条目：(条目, 口径说明, 是否按位置分档)。

    启动/派发样本量本来就薄，按位置硬拆会得到三个互不相同却同样不可靠的数，
    宁可统一给合计、并在文案里说明「不分位置」。
    """
    st = STATS.get((phase, position))
    if st:
        return st, f"{position}形态", True
    st = STATS.get((phase, "*"))
    return (st, "不分位置合计", False) if st else (None, "", False)


def stat_lines(phase: str, position: str, price: Optional[float]) -> List[str]:
    """「历史参照」：查表 + 与基准对照 + 由数据推出的结论。"""
    st, scope_txt, by_position = _stat_for(phase, position)
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
    _parts = []
    if st.get("up_24h") is not None:
        _parts.append(f"T+24h 上涨 {float(st['up_24h']):.1f}%、中位 {float(st['med_24h']):+.2f}%")
    _parts.append(f"T+7 上涨 {up:.1f}%、中位 {med:+.2f}%")
    head = f"　{scope_txt}（现价 {tier}）{n:,} 例，" + "；".join(_parts)
    crash = st.get("crash")
    if crash:
        head += f"；跌超15% 占 {float(crash):.1f}%"
    lines = [head]

    if base_up is not None:
        delta = up - float(base_up)
        _where = "该位置任意时刻" if by_position else "基准（全体可交易品）"
        _sep = " " if by_position else ""      # 中文里「该位置任意时刻 24h」需要空格
        if st.get("base_up_24h") is not None:
            lines.append(f"　对照：{_where}{_sep}24h 上涨 {float(st['base_up_24h']):.1f}%、"
                         f"7 日上涨 {float(base_up):.1f}%")
        else:
            lines.append(f"　对照：{_where}上涨 {float(base_up):.1f}%")
        if delta >= 3.0:
            judge = f"本形态较基准高 {abs(delta):.1f}pp，有额外信息量"
        elif delta <= -3.0:
            judge = f"本形态较基准低 {abs(delta):.1f}pp，反而更差"
        else:
            judge = f"本形态较基准仅 {delta:+.1f}pp，无显著信息量"
        lines.append(f"　结论：{judge}；中位 {med:+.2f}%，{'不构成买点' if med < 0 else '中位为正'}")
    if n < 100:
        # 100 例以下不足以支撑「-17.7pp」这种量级的结论,必须自曝样本量。
        lines.append(f"　（样本仅 {n} 例，统计稳定性有限，仅作方向参考）")
    return lines


def stat_as_of_line() -> str:
    return f"▍历史参照（统计截至 {STATS_AS_OF}）"


# ---------------------------------------------------------------- 尾注
def footer(phase: str, position: str) -> str:
    """卡片最后一行的行动提示，由阶段与位置决定。

    注意别在这里写「等在售转为下降」这类动作 —— 实测在售方向对后续涨跌没有分辨力
    （2,483 例建仓里 2,478 例在售变动在 ±3% 内），拿它当介入条件是给自己加戏。
    """
    if phase.startswith("accum"):
        if position == "高位":
            return "⚠️ 高位不建议介入；若已持有，注意及时止盈。"
        if phase == "accum+lift" and position == "低位":
            return ("⚠️ 赢面变大但不是稳赚：T+7 上涨率 39.7% vs 基准 31.6%，"
                    "中位仍 -1.30%。可小仓跟踪，别当无风险。")
        if position == "低位":
            return "⚠️ 观察类信号。低位只是相对不那么差，中位涨幅仍为负，不构成买点。"
        return "⚠️ 观察类信号；中位建仓历史弱于基准，不构成买点。"
    if phase == "launch":
        return "⚠️ 追涨形态样本偏薄且历史为负，不建议追入。"
    if phase == "distribute":
        return "⚠️ 持有者宜考虑减仓；未持有者暂缓介入。"
    return "⚠️ 仅供观察，不构成买卖建议。"
