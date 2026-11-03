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
# 每条统计的字段：
#   n / up / med      —— T+7 样本数、上涨率%、未扣费中位涨幅%
#   n_24h / up_24h / med_24h —— T+24h 同口径
#   base_up*          —— 同时点、同位置的**上涨率**基准（任意时刻，不看信号）
#   win_net_7d / win_net_24h         —— **扣费胜率**：买入按最低售价、卖出扣 2.5% 手续费后仍赚钱的比例
#   base_win_7d / base_win_24h       —— 同位置基准的扣费胜率（读者需要一个参照物）
#   net_med_7d        —— 扣费后的 T+7 中位净收益%
# 为什么区分「上涨率」和「扣费胜率」：涨 0% 是亏的（要付 2.5% 手续费），
# 盈亏平衡点 = 1/(1-0.025)-1 = +2.56%。上涨率 40.8% 的形态，扣费后只有 18.1% 是赚的。
#
# ⚠️ 这是**滚动 30 天快照**，不是累计样本：item_listing_history 只保留 30 天（db.py:948），
# 每次重算样本会上下浮动 10% 左右（11-03 重算时抬价·低位 n 从 393 掉到 370）。
# 别把 n 的变化读成「信号变强/变弱」，只看 up / 胜率 / 中位三者的变化方向。
STATS_AS_OF = "2026-11-03"

# 键 = (阶段, 位置)；位置为 "*" 表示「不分位置」的合计样本。
# 启动/派发没有按位置拆的样本量，硬凑三档等于伪造粒度，所以只给合计。
STATS: Dict[Tuple[str, str], Dict[str, object]] = {
    # ---- 建仓：按位置分档（现价 ≥¥100，tools/probe_net_winrate.py）----
    # 「求购价未抬」= 求购量涨但最高求购价没动 —— 求购单不锁资金,这种堆积零成本,所以没有增量。
    ("accum", "低位"): {"n": 4642, "n_items": 484, "up": 33.3, "med": -1.54, "crash": None,
                        "tier": "≥¥100", "base_up": 31.5, "base_med": -1.59, "base_n": 61704,
                        "up_24h": 35.7, "med_24h": -0.15, "base_up_24h": 34.4, "n_24h": 7263,
                        "win_net_7d": 15.4, "win_net_24h": 12.4,
                        "base_win_7d": 12.6, "base_win_24h": 8.9, "net_med_7d": -4.00},
    ("accum", "中位"): {"n": 2743, "n_items": 424, "up": 15.3, "med": -2.76, "crash": None,
                        "tier": "≥¥100", "base_up": 12.7, "base_med": -3.00, "base_n": 42194,
                        "up_24h": 24.1, "med_24h": -0.51, "base_up_24h": 25.0, "n_24h": 3883,
                        "win_net_7d": 4.0, "win_net_24h": 4.3,
                        "base_win_7d": 2.6, "base_win_24h": 3.6, "net_med_7d": -5.19},
    ("accum", "高位"): {"n": 620, "n_items": 253, "up": 4.8, "med": -6.37, "crash": None,
                        "tier": "≥¥100", "base_up": 5.0, "base_med": -7.09, "base_n": 7480,
                        "up_24h": 12.8, "med_24h": -1.19, "base_up_24h": 14.6, "n_24h": 701,
                        "win_net_7d": 2.3, "win_net_24h": 5.4,
                        "base_win_7d": 2.8, "base_win_24h": 5.7, "net_med_7d": -8.72},
    # ---- 建仓 + 求购价上移 ≥3%（「抬价收货」：庄家愿意多付,卖家还在让价）----
    # 整套信号里唯一在低位有明显增量的子形态：胜率 +5.5pp（18.1% vs 基准 12.6%），上涨率 +9.3pp。
    # 但典型涨跌仍是 -0.96%、扣费后 -3.44%，是「赢面变大」不是「正期望」。
    # 中位只有 +6.7pp 且 n=140；高位 n=52、扣费胜率 0.0%、中位 -10.54% —— 高位抬价是毒药。
    ("accum+lift", "低位"): {"n": 370, "n_items": 216, "up": 40.8, "med": -0.96, "crash": None,
                             "tier": "≥¥100", "base_up": 31.5, "base_med": -1.59, "base_n": 61704,
                             "up_24h": 42.7, "med_24h": -0.01, "base_up_24h": 34.4, "n_24h": 546,
                             "win_net_7d": 18.1, "win_net_24h": 26.2,
                             "base_win_7d": 12.6, "base_win_24h": 8.9, "net_med_7d": -3.44},
    ("accum+lift", "中位"): {"n": 140, "n_items": 102, "up": 14.3, "med": -3.99, "crash": None,
                             "tier": "≥¥100", "base_up": 12.7, "base_med": -3.00, "base_n": 42194,
                             "up_24h": 27.1, "med_24h": -0.31, "base_up_24h": 25.0, "n_24h": 203,
                             "win_net_7d": 9.3, "win_net_24h": 13.8,
                             "base_win_7d": 2.6, "base_win_24h": 3.6, "net_med_7d": -6.39},
    ("accum+lift", "高位"): {"n": 52, "n_items": 39, "up": 1.9, "med": -10.54, "crash": None,
                             "tier": "≥¥100", "base_up": 5.0, "base_med": -7.09, "base_n": 7480,
                             "up_24h": 18.8, "med_24h": -1.27, "base_up_24h": 14.6, "n_24h": 64,
                             "win_net_7d": 0.0, "win_net_24h": 10.9,
                             "base_win_7d": 2.8, "base_win_24h": 5.7, "net_med_7d": -12.77},
    # ---- 启动：追涨指纹。24 小时有效、7 天衰减（这不是「负」而是「短命」）----
    # T+24h 上涨 44.6% vs 基准 30.2%、胜率 29.1% vs 7.0%（全部信号里最高的 24h 胜率）；
    # T+7 掉到 上涨 27.0%、胜率 11.3%（基准 8.1%）—— 7 天持有等于把优势耗光。
    ("launch", "*"): {"n": 115, "n_items": 100, "up": 27.0, "med": -3.16, "crash": None,
                      "tier": "≥¥100", "base_up": 22.6, "base_med": -2.53, "base_n": 111378,
                      "up_24h": 44.6, "med_24h": -0.01, "base_up_24h": 30.2, "n_24h": 175,
                      "win_net_7d": 11.3, "win_net_24h": 29.1, "show_24h": True,
                      "base_win_7d": 8.1, "base_win_24h": 7.0, "net_med_7d": -5.58},
    # ---- 派发：唯一在可交易品上依然强有效的方向信号（现价 ≥¥100）----
    # T+24h 上涨 22.2% vs 基准 30.2%；T+7 上涨 12.5%、胜率 7.5%、中位 -9.63% —— 方向仍是全表最差。
    ("distribute", "*"): {"n": 40, "n_items": 31, "up": 12.5, "med": -9.63, "crash": None,
                          "tier": "≥¥100", "base_up": 22.6, "base_med": -2.53, "base_n": 111378,
                          "up_24h": 22.2, "med_24h": -1.97, "base_up_24h": 30.2, "n_24h": 45,
                          "win_net_7d": 7.5, "win_net_24h": 20.0,
                          "base_win_7d": 8.1, "base_win_24h": 7.0, "net_med_7d": -11.89},
}

# 位置档（用于标题与措辞）
POS_LOW_MAX = 30.0      # ≤30% 分位 = 低位
POS_HIGH_MIN = 70.0     # ≥70% 分位 = 高位

# 卖出费率决定盈亏平衡点：按最低售价买入、再按最低售价挂出，涨 0% 是亏的。
# BUFF 卖出手续费 2.5% ⇒ 必须涨到 1/(1-0.025)-1 = +2.56% 才刚好回本。
SELL_FEE_PCT = 2.5
BREAKEVEN_PCT = (1.0 / (1.0 - SELL_FEE_PCT / 100.0) - 1.0) * 100.0


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
    med = float(st["med"])
    # 注：STATS 里的 up / base_up（上涨率）不再上卡，只留作原始测量数据；
    # 卡片上的结论一律由「胜率」推出，读者不必再自己做减法。
    n_items = st.get("n_items")
    # 事件数远大于独立品数时（同一个品被重复采样），必须把两个数都写出来 ——
    # 「393 例」听起来像 393 个机会，实际只是 213 个品。
    _sample = (f"{n:,} 次（{int(n_items):,} 个品）" if n_items and int(n_items) < n
               else f"{n:,} 例")
    lines = [f"　{scope_txt}（现价 {tier}）{_sample}"]

    # 24h 只在「持有周期本来就是 24 小时」的阶段（启动）才印，其余只给 T+7。
    # 2026-10-29 老大砍掉「上涨率」行：它和胜率是两个数（启动 44.2% vs 31.9%），
    # 差额全来自「涨 0.1% 也算赢」，对做决定没有额外价值，留着反而要读者自己相减。
    _show24 = bool(st.get("show_24h")) and st.get("up_24h") is not None

    # 1) 扣去手续费后胜率：涨过手续费门槛才算赢，是「赚没赚」
    w7, w24 = st.get("win_net_7d"), st.get("win_net_24h")
    bw7, bw24 = st.get("base_win_7d"), st.get("base_win_24h")
    if w7 is not None:
        _w = []
        if _show24 and w24 is not None:
            _w.append(f"T+24h {float(w24):.1f}%")
        _w.append(f"T+7 {float(w7):.1f}%")
        _base = ""
        if bw7 is not None:
            _where = "同位置基准" if by_position else "全市场基准"
            if _show24 and bw24 is not None:
                _base = f"（{_where} {float(bw24):.1f}% / {float(bw7):.1f}%）"
            else:
                _base = f"（{_where} {float(bw7):.1f}%）"
        lines.append(f"　扣去手续费后胜率　" + "｜".join(_w) + _base)

    # 2) 典型涨跌：排正中间那次的幅度（不是概率），并列扣费后的同一数字
    net_med = st.get("net_med_7d")
    tail = f"（扣费后 {float(net_med):+.2f}%）" if net_med is not None else ""
    if _show24:
        lines.append(f"　典型涨跌　T+24h {float(st['med_24h']):+.2f}%｜T+7 {med:+.2f}%{tail}")
    else:
        lines.append(f"　典型涨跌　{med:+.2f}%{tail}")

    # ---- 结论：拿卡片上印得出的数（胜率、典型涨跌）跟基准比，不引用没上卡的数 ----
    if w7 is not None and bw7 is not None:
        d7 = float(w7) - float(bw7)
        d24 = (float(w24) - float(bw24)
               if (w24 is not None and bw24 is not None) else None)
        base_med = st.get("base_med")
        d_med = (med - float(base_med)) if base_med is not None else None
        if _show24 and d24 is not None and d24 >= 5.0:
            # 启动：7 日与基准持平，优势全在 24 小时内，必须点出来。
            judge = (f"7 日胜率 {float(w7):.1f}% 与基准 {float(bw7):.1f}% 相近，"
                     f"但 24h 高 {d24:.1f}pp")
        elif d7 >= 3.0:
            judge = f"胜率较基准 {float(bw7):.1f}% 高 {abs(d7):.1f}pp"
        elif d7 <= -3.0:
            judge = f"胜率较基准 {float(bw7):.1f}% 低 {abs(d7):.1f}pp"
        elif d_med is not None and abs(d_med) >= 3.0:
            # 胜率打平时，真正的分辨力在「跌多少」上——派发就是这样：
            # 胜率 7.5% 与基准 8.1% 几乎一样，但典型跌 -9.63% 是基准 -2.53% 的近四倍。
            judge = (f"胜率与基准 {float(bw7):.1f}% 接近，但典型涨跌 {med:+.2f}%，"
                     f"比基准 {float(base_med):+.2f}% 低 {abs(d_med):.1f}pp")
        else:
            judge = f"胜率较基准 {float(bw7):.1f}% 仅差 {abs(d7):.1f}pp，无显著差异"
        if med < 0:
            # 即使胜率更高，只要典型涨跌为负，就必须说清「不是正期望」，别让读者以为能稳赚。
            lines.append(f"　结论：{judge}；涨不过 +{BREAKEVEN_PCT:.2f}% 就是亏，仍非正期望")
        else:
            lines.append(f"　结论：{judge}")
    if n < 100:
        # 100 例以下不足以支撑 pp 级的结论，必须自曝样本量。
        lines.append(f"　（样本仅 {n} 次，统计稳定性有限，仅作方向参考）")
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
            return "⚠️ 高位不建议介入；持有者考虑止盈。"
        # 低位/中位不另加尾注：上面的「结论」已经把话说完，再说一遍是啰嗦。
        return ""
    if phase == "launch":
        # 启动是短周期信号:24h 扣费胜率 31.9%(基准 7.0%),7 天掉到 9.5%(基准 9.2%)。
        return "⚠️ 只在 24 小时内有效，快进快出，别过夜持有。"
    if phase == "distribute":
        return "⚠️ 持有者考虑减仓；未持有者暂缓介入。"
    return ""
