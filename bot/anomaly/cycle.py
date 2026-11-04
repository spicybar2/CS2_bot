# -*- coding: utf-8 -*-
"""资金周期四阶段检测(V0.2.0):建仓 / 启动 / 派发 / 消化。

市场逻辑(方案与老大于 2026-09-06 敲定):
- CS2 饰品市场的资金运作周期 = 建仓(吸筹) → 启动(抬价) → 派发(出货) → 消化(盘整),
  四个检测器**并行独立**(不串联):任何饰品直接出现某阶段指纹即触发,无需按顺序经历。
- 建仓期指纹: BUFF求购在总结窗口持续净增 + BUFF+悠悠在售被消化 + **价格横盘**(价格涨了就进入启动/派发,
  建仓信号只在价格未启动时有效——这是 T+7 买家唯一能低价进场的窗口)。
- 启动期指纹: 求购价上移 + 价格跟涨(建仓结束开始抢货)。
- 派发期指纹: 价格处近期高位 + BUFF+悠悠在售在总结窗口暴增 + 求购停滞(庄家派发+散户跟风出货)。
- 消化期指纹: BUFF+悠悠在售持续递减 + BUFF求购递增 + 价格未选择方向(中性,仅 /分析 展示)。

口径拆分(V0.2.11,实测驱动):
- **求购侧 = BUFF+悠悠合计**:steam_buy_num 是脏数据(Steam 无求购机制),不计入合计；
  缺少任一 BUFF/悠悠求购字段的快照不参与周期判定，避免把单平台数量冒充合计。
- **在售侧 = BUFF+悠悠合计**:异动雷达各阶段统一使用两平台在售。

数据边界(继承 engine.py 铁律):
- CSQAQ 无成交量,一律不伪造成交量;「量」均指求购/在售盘口数量。
- 求购单几乎零成本(先求后付不锁资金),单点增长不可信 —— 因此持久性判定
  (总结窗口内逐段走高/非递减)是检测成立的前提,不是可选项。
- 假买墙过滤: 求购增但价格下跌 + 在售暴增 = 挂单诱多/撤单诱空,直接不触发。

数据可用性: 依赖 item_listing_history 时序(A 池 2min/轮、S 池 30s/轮持续写入);
B 池通过全量两小时总结快照积累历史；没有完整总结点时仍明确返回数据不足。
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Sequence, Tuple

from bot.anomaly import phase_rules
from bot.csqaq.market_counts import (aggregate_buff_yyyp_sell, book_split_inline,
                                     book_split_line, normalize_book)


if TYPE_CHECKING:   # 仅类型注解用,避免与 engine 循环导入(engine 顶层 import 本模块)
    from bot.anomaly.engine import HistoryPoint, MetricSnapshot

# 建仓记忆(递进上下文):good_id -> 最近一次建仓信号 unix 时间。
# 启动确认文案引用它: 有吸筹记录 = 「庄家逻辑链成立」;无 = 「脉冲型,持续性存疑」。
# 内存态,重启清零(6 小时窗口,可接受);建仓事件本身已落 anomaly_events 可追溯。
_ACCUM_SEEN: Dict[int, float] = {}
_ACCUM_MEMORY_SEC = 6 * 3600.0

# 锚点查找所需最少历史点数(低于此数时序太稀,不做周期判定)
_MIN_HISTORY_POINTS = 6


def _parse_ts(ts: str) -> Optional[datetime]:
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(ts, fmt)
        except (ValueError, TypeError):
            continue
    return None


def _anchor(history: Sequence[HistoryPoint], minutes: int, now: datetime,
            min_points: Optional[int] = None) -> Optional[HistoryPoint]:
    """取 minutes 分钟前最近的历史点(容差 = 窗口一半),不足则 None。

    :param min_points: 所需最少历史点数,默认 _MIN_HISTORY_POINTS(6,适配 A 池 2 分钟采样);
                       关注品周期监测为 15 分钟采样(每小时 4~5 点),调用方传 4。
    """
    if not history or len(history) < (min_points or _MIN_HISTORY_POINTS):
        return None
    target = now - timedelta(minutes=minutes)
    picked: Optional[HistoryPoint] = None
    picked_t: Optional[datetime] = None
    for h in history:
        t = _parse_ts(h.ts)
        if t is None or t > target:
            continue
        if picked_t is None or t > picked_t:
            picked, picked_t = h, t
    if picked is None or picked_t is None:
        return None
    if (target - picked_t).total_seconds() > minutes * 60 * 0.5:
        return None
    return picked


def _pct(cur: float, base: float) -> Optional[float]:
    if not base or base <= 0 or cur is None:
        return None
    return (float(cur) / float(base) - 1.0) * 100.0


def _abs_req(cfg: Any, base: int, pct: float) -> int:
    """绝对增量要求(V0.3.0 连续式,取代旧的「基线分档 × accum_abs_mult」)。

    规则:`max(accum_abs_min_units, ceil(base × pct%))` —— 百分比说了算,
    绝对量只当地板(防「2 单涨到 3 单 = +50%」这种小数噪声)。

    旧口径的问题(2026-09-24 实测):分档 3/5/15 单再 ×2,导致 49→50 单跨越档位时
    绝对增量跳 3 倍(10→30 单);而能过 min_price 闸的品里 90.6% 面对 ≥20% 的实际门槛、
    只有 2.1% 真按配置的百分比走。连续式后同一件品的门槛随规模平滑变化。
    """
    floor = int(getattr(cfg, "accum_abs_min_units", 3) or 1)
    return int(max(floor, math.ceil(int(base) * float(pct) / 100.0)))


def _fmt_book(label: str, base: int, cur: int, pct: Optional[float]) -> str:
    return f"{label} {base:,}→{cur:,}({cur - base:+,}/{pct:+.1f}%)" if pct is not None else \
           f"{label} {base:,}→{cur:,}({cur - base:+,})"


def _fmt_qty_line(label: str, base: int, cur: int, pct: Optional[float]) -> str:
    """盘口总量行:先给「起点 → 终点 + 净增/净减 + 百分比」,人一眼能读懂方向。

    旧写法把两个平台的增减并排给(「BUFF -1 个  悠悠有品 +15 个」),读者要自己
    心算才知道总量是增是减。总量行在前、分平台明细在后,两层都不丢。
    """
    delta = int(cur) - int(base)
    if delta > 0:
        word = f"净增 {delta:,} 单"
    elif delta < 0:
        word = f"净减 {abs(delta):,} 单"
    else:
        word = "无变化"
    pct_txt = "" if pct is None else f"　合计 {pct:+.1f}%"
    return f"　{label}　{int(base):,} → {int(cur):,}　{word}{pct_txt}"


def _fmt_price_line(chg: Optional[float]) -> str:
    """最低售价行:方向词 + 百分比,不用读者判断 +0.0% 是涨是跌。"""
    if chg is None:
        return "　最低售价　数据不足"
    if abs(chg) < 0.05:
        return "　最低售价　持平（0.0%）"
    word = "上涨" if chg > 0 else "下跌"
    return f"　最低售价　{word}（{chg:+.1f}%）"


def _fmt_buy_price_line(cur_price: Any, chg: Optional[float]) -> str:
    """最高求购价行。

    CS 市场里「庄家收货」首先表现为**抬价/串价**——把最高求购价往上挪一格去卡卖家,
    而不是原地堆求购量:求购单不锁资金,原地堆单可以零成本造假。
    只有求购量涨、求购价不动时,才更可能是散户跟风或假墙 —— 这一行是区分两者的关键证据。
    """
    try:
        px = float(cur_price or 0)
    except (TypeError, ValueError):
        px = 0.0
    if px <= 0:
        return "　最高求购价　数据不足"
    if chg is None:
        return f"　最高求购价　¥{px:,.2f}"
    if abs(chg) < 0.05:
        return f"　最高求购价　¥{px:,.2f}　持平（0.0%）"
    word = "上移" if chg > 0 else "下移"      # CS 玩家说「串价上移 / 下移」
    return f"　最高求购价　¥{px:,.2f}　{word}（{chg:+.1f}%）"


def _build_reading(phase: str, position: str, pos_pct: Optional[float],
                   buy_chg: Optional[float], sell_chg: Optional[float],
                   price_chg: Optional[float], sell_now: int, buy_now: int,
                   window_points: int, price: Any,
                   window_days: int = 30,
                   bid_chg: Optional[float] = None) -> List[str]:
    """拼装「判读 + 历史参照」两段。

    每句都由当时状态生成(位置分档 / 求购强度与求购价方向 / 价格方向 / 供给方向 / 采样点数),
    历史参照按 `(阶段, 位置)` 查 phase_rules.STATS 并与同位置基准对照 ——
    不出现与数据矛盾的固定结论。
    """
    ratio = (float(sell_now) / float(buy_now)) if buy_now else None
    try:
        price_f = float(price or 0)
    except (TypeError, ValueError):
        price_f = 0.0
    out = ["▍饰品解析"]
    out.extend(f"　{x}" for x in phase_rules.reading(
        phase, position, pos_pct, buy_chg, sell_chg, price_chg, ratio, window_points,
        window_days=window_days, bid_chg=bid_chg))
    out.append(phase_rules.stat_as_of_line())
    out.extend(phase_rules.stat_lines(phase, position, price_f))
    return out


@dataclass
class PhaseSignal:
    """周期阶段信号。"""

    phase: str                       # accum / launch / distribute
    title: str                       # 由位置决定:低位吸筹迹象 / 建仓迹象 / 高位诱多预警
    icon: str                        # 👁 / 🚀 / 🚨
    floor_score: float               # 评分下限(确保推送等级)
    reasons: List[str] = field(default_factory=list)
    lines: List[str] = field(default_factory=list)   # 推送文案的窗口盘口对比块
    position: str = ""               # 低位 / 中位 / 高位(近 N 日区间分位分档)
    pos_pct: Optional[float] = None  # 区间分位 0~100(缺失为 None)
    price_from: Optional[float] = None  # V0.3.38:触发前的售价(启动用),卡片显示「¥旧 → ¥现」
    reading: List[str] = field(default_factory=list)  # 判读 + 历史参照(整段文案)


def detect_phase(snap: MetricSnapshot, history: Sequence[HistoryPoint],
                 now: datetime, cfg: Any,
                 price_chg_60: Optional[float] = None,
                 min_points: Optional[int] = None,
                 remember: bool = True,
                 window_minutes: int = 60,
                 range_30d: Optional[Tuple[float, float]] = None) -> Optional[PhaseSignal]:
    """四阶段指纹检测(并行独立,优先级: 派发 > 启动 > 建仓)。

    :param price_chg_60: 引擎已算好的窗口价格变化%(60分钟时可传入),可为 None。
    :param min_points: 最少历史点数(默认 6=A 池采样;关注品/两小时总结可按调用方放宽)。
    :param remember: 是否写入建仓递进记忆。异动检测保持 True；/分析只读调用传 False。
    :param window_minutes: 对比窗口，默认60分钟；全量总结快照使用120分钟。
    :param range_30d: 近 N 日价格区间 `(最低, 最高)`(V0.3.0,派发「高位」真实口径)。
                      由调用方从 `item_listing_history` 的日收盘聚合而来;None 时回退旧
                      口径 `pos_30`(30 日涨幅),保证区间数据缺失也不会漏判。
    :return: PhaseSignal 或 None(无指纹/数据不足/假买墙)。
    """
    window_minutes = max(1, int(window_minutes or 60))
    window_label = f"{window_minutes / 60:g} 小时"
    p60 = _anchor(history, window_minutes, now, min_points)
    if p60 is None:
        return None
    p30 = _anchor(history, max(1, window_minutes // 2), now, min_points)

    buy_60, buy_now = int(p60.buy_num or 0), int(snap.buy_num or 0)
    sell_60, sell_now = int(p60.sell_num or 0), int(snap.sell_num or 0)
    buy_chg60 = _pct(buy_now, buy_60)
    sell_chg60 = _pct(sell_now, sell_60)
    bp_chg60 = None
    if snap.buy_price and p60.buy_price:
        bp_chg60 = _pct(snap.buy_price, p60.buy_price)
    if price_chg_60 is None and p60.price:
        price_chg_60 = _pct(snap.price, p60.price)
    # V0.3.0:窗口价格绝对变动额(元)。与百分比并列使用 —— 低价品一个最小报价台阶
    # 就是百分之几(¥0.02→¥0.03 = +50%),纯百分比判定在低价段没有分辨力。
    price_abs_delta: Optional[float] = None
    if snap.price and p60.price:
        price_abs_delta = float(snap.price) - float(p60.price)

    # V0.3.12:盘口块优先给**分平台**拆解(BUFF / 悠悠有品 各自的变化量与当前值),
    # 任何一侧字段不全就回退原来的合计口径 —— 判定口径完全不变,只改读法。
    base_book, cur_book = normalize_book(p60), normalize_book(snap)
    _split_buy = book_split_line(base_book, cur_book, "buy")
    _split_sell = book_split_line(base_book, cur_book, "sell")
    book_lines = [
        f"▍盘口变动（近 {window_label}）",
        _fmt_qty_line("求购量", buy_60, buy_now, buy_chg60),
        _fmt_qty_line("在售量", sell_60, sell_now, sell_chg60),
        _fmt_buy_price_line(snap.buy_price, bp_chg60),
        _fmt_price_line(price_chg_60),
    ]
    # 分平台明细只作补充:总量行已经给出方向,这一层是给要核对 BUFF/悠悠 差异的人看。
    if _split_buy:
        book_lines.append(f"　{_split_buy}")
    if _split_sell:
        book_lines.append(f"　{_split_sell}")

    # 区间位置(V0.3.29):决定阶段信号含义的主变量 —— 同一句「求购 +15.6%」,
    # 低位是吸筹迹象、高位是诱多预警。分档阈值与回测脚本 tools/probe_accum_states.py 一致。
    pos_pct = phase_rules.range_pct(range_30d, snap.price)
    position = phase_rules.pos_of(pos_pct)
    # 窗口内实际采样点数:采样稀疏时趋势判断可靠性下降,卡片需要如实说明。
    window_points = sum(1 for h in history if h.ts >= p60.ts)

    # ---- 假买墙过滤(配套,必做):求购增但价格下跌 + 在售暴增 = 挂单诱多 ----
    if (buy_chg60 is not None and buy_chg60 >= cfg.accum_buy_pct_min
            and price_chg_60 is not None and price_chg_60 <= cfg.fake_wall_price_dn
            and sell_chg60 is not None and sell_chg60 >= cfg.fake_wall_sell_up):
        return None

    # ---- ③ 派发预警(最高优先级:高位出货指纹,哪怕求购也在增) ----
    # V0.3.0:「高位」改用**真实区间位置**——现价 ≥ 近 N 日最高价 × distrib_range_pct%。
    # 旧实现 pos30 = price/sell_price_30-1 算的是「30 日涨幅(动量)」,不是位置:
    # 30 天前 ¥100、中途到过 ¥200、现价 ¥108 会被判「近30日高位」,实际是腰斩后反弹;
    # 实测两者一致率仅 81.3%。区间数据缺失时回退旧口径,保证不影响判定连续性。
    pos30 = _pct(snap.price, snap.price_30) if snap.price_30 else None
    if range_30d and range_30d[1] and snap.price:
        hi = float(range_30d[1])
        range_pos = float(snap.price) / hi * 100.0 if hi > 0 else None
        high_ok = range_pos is not None and range_pos >= float(cfg.distrib_range_pct)
        high_txt = (f"价格处近 {int(getattr(cfg, 'distrib_range_days', 30))} 日高位"
                    f"(现价 ¥{snap.price:,.2f} / 区间最高 ¥{hi:,.2f} = {range_pos:.0f}%)"
                    if range_pos is not None else "价格处区间高位")
    else:
        high_ok = pos30 is not None and pos30 >= cfg.distrib_pos_high
        high_txt = f"价格处近30日高位(较30天前 {pos30:+.0f}%,区间数据缺失回退口径)" if pos30 is not None else ""
    # V0.3.30:改为**单边**条件。CS 市场出货时庄家会先撤掉自己的求购单(求购量骤降),
    # 再往上挂卖;旧写法 abs(chg)<2 把「撤单派发」判成「不是停滞」,直接漏掉这类派发。
    # 实测(现价 ≥¥10、在售 +6% 且高位,T+7,见 tools/probe_distribute_buy.py):
    #   停滞组(±2%) 上涨 6.8% / 中位 -11.38%(n=44)
    #   撤单组(≤-2%) 上涨 6.7% / 中位 -13.28%(n=15)  ← 一样负,却被旧条件排除
    #   上升组(≥+2%) 上涨 19.2% / 中位 -9.94%(n=26)  ← 明显更好,应继续排除
    # 于是边界落在单边:求购不增(< +2%)即算派发;信号量 +39%,涨跌分布几乎不变。
    stall = buy_chg60 is None or buy_chg60 < cfg.distrib_buy_stall
    sell_abs_req = _abs_req(cfg, sell_60, cfg.distrib_sell_pct)
    if (high_ok
            and sell_chg60 is not None and sell_chg60 >= cfg.distrib_sell_pct
            and abs(sell_now - sell_60) >= sell_abs_req
            and stall):
        _icon, _title = phase_rules.headline("distribute", position)
        return PhaseSignal(
            phase="distribute", title=_title, icon=_icon,
            floor_score=cfg.phase_score_distrib,
            reasons=[
                high_txt,
                # V0.3.13:同上,窗口名与百分比已在盘口块里,这里只留绝对增量+分平台
                f"在售暴增 +{sell_now - sell_60}单"
                + (f"({book_split_inline(base_book, cur_book, 'sell')})" if _split_sell else ""),
                # 求购方向分三种说:停滞 / 撤单(庄家先撤买单再挂卖)/ 数据缺失
                ("求购数据缺失" if buy_chg60 is None
                 else (f"求购撤单 {buy_chg60:+.1f}%"
                       if buy_chg60 <= -float(cfg.distrib_buy_stall) else "求购停滞")),
            ],
            lines=book_lines, position=position, pos_pct=pos_pct,
            reading=_build_reading("distribute", position, pos_pct, buy_chg60,
                                   sell_chg60, price_chg_60, sell_now, buy_now,
                                   window_points, snap.price,
                                   int(getattr(cfg, "distrib_range_days", 30))))

    # ---- ② 启动确认(求购价上移 + 价格跟涨) ----
    # V0.3.0 补四道锁(实测:启动是唯一「T+24h 起超额就为负、逐窗单调恶化」的阶段,
    # T+7d 中位 -13.28% vs 同期池子 -3.68% = 超额 -9.60pp;而旧版只要两点对比够数就发):
    #   ①持续性:求购价与价格窗口内逐段走高,不认单点跳变
    #   ②供应侧:在售不能同步暴增(「价格涨 + 货暴增」是派发指纹,不是启动)
    #   ③不追高:7 日涨幅超 launch_rate7_max 不再喊启动
    #   ④金额下限:低价品涨几分钱不算跟涨(¥6 品 +1% = ¥0.06)
    bp_mid = float(p30.buy_price or 0) if p30 else 0.0
    price_mid = float(p30.price or 0) if p30 else 0.0
    bp_non_dec = (not bp_mid or not p60.buy_price
                  or (float(snap.buy_price or 0) >= bp_mid >= float(p60.buy_price)))
    price_non_dec = (not price_mid or not p60.price
                     or (float(snap.price or 0) >= price_mid >= float(p60.price)))
    rate7 = float(getattr(snap, "rate_7", 0.0) or 0.0)
    if (bp_chg60 is not None and bp_chg60 >= cfg.launch_buy_price_pct
            and price_chg_60 is not None and price_chg_60 >= cfg.launch_price_min
            and (buy_chg60 is None or buy_chg60 > -5.0)
            and bp_non_dec and price_non_dec
            and (price_abs_delta is None
                 or abs(price_abs_delta) >= float(getattr(cfg, "launch_price_abs_min", 0.0) or 0.0))
            and (sell_chg60 is None or sell_chg60 <= cfg.launch_sell_max)
            and abs(rate7) < float(getattr(cfg, "launch_rate7_max", 0.0) or 100.0)):
        ctx = ""
        seen = _ACCUM_SEEN.get(int(snap.good_id or 0))
        if seen and 0 < time.time() - seen <= _ACCUM_MEMORY_SEC:
            hrs = (time.time() - seen) / 3600.0
            ctx = f"此前 {hrs:.0f} 小时内监测到分批吸筹,吸筹→启动链条成立,持续性更强"
            reason = f"求购价上移 {bp_chg60:+.1f}%,价格跟涨 {price_chg_60:+.1f}%({ctx})"
        else:
            reason = (f"求购价上移 {bp_chg60:+.1f}%,价格跟涨 {price_chg_60:+.1f}%"
                      f"(无吸筹期记录,疑似消息/事件驱动,持续性存疑)")
        _icon, _title = phase_rules.headline("launch", position)
        # V0.3.38:启动卡只写「现价」看不出涨了多少,补上 60 分钟前的售价 → 「¥1203 → ¥1285」。
        _pf = float(p60.price or 0) if p60 else 0.0
        return PhaseSignal(
            phase="launch", title=_title, icon=_icon,
            floor_score=cfg.phase_score_launch,
            reasons=[reason,
                     f"逐段走高确认(求购价与价格窗口内未回落),在售 {sell_chg60:+.1f}% 未暴增"
                     if sell_chg60 is not None else "逐段走高确认(求购价与价格窗口内未回落)"],
            lines=book_lines, position=position, pos_pct=pos_pct,
            price_from=(_pf if _pf > 0 else None),
            reading=_build_reading("launch", position, pos_pct, buy_chg60,
                                   sell_chg60, price_chg_60, sell_now, buy_now,
                                   window_points, snap.price,
                                   int(getattr(cfg, "distrib_range_days", 30))))

    # ---- ① 建仓监测(吸筹期:价格横盘是前提;V0.3.0 连续式绝对量 + 横盘金额豁免) ----
    abs_req = _abs_req(cfg, buy_60, cfg.accum_buy_pct_min)
    non_dec = (buy_now >= (p30.buy_num if p30 else buy_60) >= buy_60)  # 持久性:窗口内逐段不降
    quiet_abs = float(getattr(cfg, "accum_price_quiet_abs", 0.0) or 0.0)
    quiet_pct = price_chg_60 is not None and abs(price_chg_60) < cfg.accum_price_quiet
    quiet_money = (quiet_abs > 0 and price_abs_delta is not None
                   and abs(price_abs_delta) <= quiet_abs)
    if (buy_chg60 is not None and buy_chg60 >= cfg.accum_buy_pct_min
            and (buy_now - buy_60) >= abs_req
            and non_dec
            and (quiet_pct or quiet_money)
            and (sell_chg60 is None or sell_chg60 <= cfg.accum_sell_max)):
        if remember:
            _ACCUM_SEEN[int(snap.good_id or 0)] = time.time()
        accum_reasons = [
            # V0.3.13:去掉与盘口块重复的窗口名("2 小时")与百分比(盘口块已给「合计 x%」),
            # 只留绝对增量+分平台增量+门槛 —— 特征行短一半,信息一条不少。
            f"求购持续净增 +{buy_now - buy_60}单"
            + (f"({book_split_inline(base_book, cur_book, 'buy')})" if _split_buy else "")
            + f",逐段走高;门槛 {cfg.accum_buy_pct_min:g}% 且 ≥{abs_req} 单",
            "价格横盘未启动" if price_chg_60 is not None else "价格横盘(窗口价格缺失)未启动",
            "在售被消化" if (sell_chg60 is not None and sell_chg60 < 0) else "在售平稳",
        ]
        if quiet_money and not quiet_pct and price_abs_delta is not None:
            accum_reasons.append(
                f"横盘按金额豁免:{price_abs_delta:+.2f} 元 ≤ ¥{quiet_abs:g}"
                f"(低价品百分比噪声)")
        # V0.3.29:旧的写死尾注(「n=34 / +2.2pp」式自曝)默认关闭,改由卡片「历史参照」段
        # 按 (位置, 现价档) 查表给出可核对的统计;配置里填了才追加这一行。
        accum_lines = list(book_lines)
        note = str(getattr(cfg, "phase_accum_note", "") or "")
        if note:
            accum_lines.append(f"　{note}")
        # V0.3.31:「求购价是否上移」是 CS 市场里区分「真收货」和「零成本堆量」的唯一硬证据。
        # 实测低位:抬价组 T+7 上涨 39.7%(n=393),未抬价组 32.7%(n=4581),基准 31.6%。
        lift = (bp_chg60 is not None
                and bp_chg60 >= float(getattr(cfg, "launch_buy_price_pct", 3.0) or 3.0))
        _phase_key = "accum+lift" if lift else "accum"
        _icon, _title = phase_rules.headline("accum", position)
        return PhaseSignal(
            phase="accum", title=_title, icon=_icon,
            floor_score=cfg.phase_score_accum,
            reasons=accum_reasons,
            lines=accum_lines, position=position, pos_pct=pos_pct,
            reading=_build_reading(_phase_key, position, pos_pct, buy_chg60,
                                   sell_chg60, price_chg_60, sell_now, buy_now,
                                   window_points, snap.price,
                                   int(getattr(cfg, "distrib_range_days", 30)),
                                   bid_chg=bp_chg60))

    return None


def phase_history_ready(history: Sequence[HistoryPoint], now: datetime,
                        min_points: Optional[int] = None,
                        window_minutes: int = 60) -> bool:
    """周期判定数据是否具备有效窗口锚点；与 detect_phase 使用完全相同的门槛。"""
    return _anchor(history, max(1, int(window_minutes or 60)), now, min_points) is not None


def digest_label(history: Sequence[HistoryPoint], now: datetime, cfg: Any,
                 min_points: Optional[int] = None,
                 window_minutes: int = 60) -> Optional[str]:
    """④ 供给消化标签(/分析 专用,不推送):在售持续递减 + 求购递增 + 方向未选择。"""
    window_minutes = max(1, int(window_minutes or 60))
    p60 = _anchor(history, window_minutes, now, min_points)
    if p60 is None:
        return None
    sell_60, sell_now = int(p60.sell_num or 0), int(history[-1].sell_num if history else 0)
    buy_60, buy_now = int(p60.buy_num or 0), int(history[-1].buy_num if history else 0)
    sell_chg = _pct(sell_now, sell_60)
    buy_chg = _pct(buy_now, buy_60)
    if sell_chg is None or buy_chg is None:
        return None
    # 持续性: 在售逐段递减(非单点抖动)
    non_inc = sell_now <= ((p30.sell_num if (p30 := _anchor(history, max(1, window_minutes // 2), now, min_points)) else sell_60) or 0) <= sell_60
    if (sell_chg <= -cfg.digest_sell_pct and buy_chg >= cfg.digest_buy_pct and non_inc):
        window_label = f"{window_minutes / 60:g} 小时"
        return (f"供给消化: 在售 {window_label} 递减 {sell_chg:+.1f}%({sell_60:,}→{sell_now:,}),"
                f"求购递增 {buy_chg:+.1f}%({buy_60:,}→{buy_now:,})——供给收紧,方向未选择")
    return None


# ---------------- 关注品周期监测(V0.2.3) ----------------

import logging as _logging

_cw_logger = _logging.getLogger("bot.anomaly.cycle")


async def run_cycle_watch(cfg, store: SnapshotStore, client, cycle_rows, prices, now) -> None:
    """V0.2.3 周期监测:对关注类型=cycle 的品跑资金周期四阶段指纹,命中 @ 关注者。

    :param cfg: AnomalyConfig(注意不是 AppConfig——main.py 调用处传 cfg.anomaly)。

    数据: watch_loop 每 15 分钟写一次 item_listing_history 快照(本函数之前)，
    生产配置使用两小时窗口；冷启动窗口未满时明确返回数据不足。
    冷却: 复用 anomaly_push_cooldown(good_id, name),按阶段分级(建仓/启动 30 分,
    派发 20 分);last_type 不同(形态升级)可在冷却内放行——建仓→启动→派发是递进信息。
    措辞: 复用 alert._format_phase_alert(与异动推送同款白盒格式),仅供观察。
    """
    import time as _time
    from datetime import timedelta
    from bot.anomaly.alert import _format_phase_alert
    from bot.anomaly.engine import HistoryPoint, MetricSnapshot
    from bot.anomaly.runner import _parse_dt

    cooldowns = await store.async_list_anomaly_cooldown()
    minutes_by_phase = {"accum": 30, "launch": 30, "distribute": 20}
    sent = 0
    for r in cycle_rows:
        gid = int(r.get("good_id") or 0)
        zh = r.get("zh_name") or ""
        pd = prices.get(r["market_hash_name"])
        if not gid or not pd:
            continue
        raw_hist = await store.async_get_listing_series(gid, zh, hours=2.0)
        # 异动雷达统一采用 BUFF+悠悠 口径；两字段不完整的快照不参与周期判定。
        hist = [h for h in raw_hist
                if h.get("total_sell_num") is not None
                and h.get("total_sell_scope") == "buff_yyyp"
                and h.get("total_buy_num") is not None
                and h.get("total_buy_scope") == "buff_yyyp"]
        counts = aggregate_buff_yyyp_sell(pd)
        if not counts["sell_complete"]:
            # 回包缺少 BUFF 或悠悠在售字段时，不能把 0 当作真实数量。
            continue
        # V0.2.24: 求购侧同步 BUFF+悠悠（详情/合并回包提供两平台求购）
        _buff_buy_raw = pd.get("buffBuyNum") if "buffBuyNum" in pd else pd.get("buff_buy_num")
        _yyyp_buy_raw = pd.get("yyypBuyNum") if "yyypBuyNum" in pd else pd.get("yyyp_buy_num")
        if _buff_buy_raw is None or _yyyp_buy_raw is None:
            continue
        buy_total = int(_buff_buy_raw or 0) + int(_yyyp_buy_raw or 0)
        # V0.3.12:分平台历史值(展示层拆解用;历史行无 yyyp_sell_num 列,按口径标签反推)
        points = [HistoryPoint(ts=h.get("ts", ""),
                               price=float(h.get("buff_sell_price") or 0),
                               sell_num=int(h.get("total_sell_num") or 0),
                               buy_num=int(h.get("total_buy_num") or 0),
                               buy_price=float(h.get("buff_buy_price") or 0),
                               buff_sell_num=(int(h.get("buff_sell_num"))
                                              if h.get("buff_sell_num") is not None else None),
                               yyyp_sell_num=(int(h.get("total_sell_num"))
                                              - int(h.get("buff_sell_num") or 0)
                                              if (h.get("total_sell_scope") == "buff_yyyp"
                                                  and h.get("total_sell_num") is not None
                                                  and h.get("buff_sell_num") is not None
                                                  and int(h.get("total_sell_num"))
                                                  >= int(h.get("buff_sell_num") or 0))
                                              else None),
                               buff_buy_num=(int(h.get("buff_buy_num"))
                                             if h.get("buff_buy_num") is not None else None),
                               yyyp_buy_num=(int(h.get("yyyp_buy_num"))
                                             if h.get("yyyp_buy_num") is not None else None))
                  for h in hist]
        # 关注品 15 分钟采样:最少点数放宽到 4(阈值/口径与推送完全同参)
        phase_window = int(getattr(cfg, "phase_window_minutes", 60))
        # V0.3.0:派发「高位」用真实区间位置;单品查一次(整表聚合已由扫描任务负责)
        rng = None
        try:
            rng = await store.async_get_price_range(
                gid, days=int(getattr(cfg, "distrib_range_days", 30)))
        except Exception:
            rng = None
        sig = detect_phase(
            MetricSnapshot(good_id=gid, name=zh,
                           price=float(pd.get("buffSellPrice") or 0),
                           sell_price=float(pd.get("buffSellPrice") or 0),
                           buy_price=float(pd.get("buffBuyPrice") or 0),
                            sell_num=int(counts["sell_num"] or 0),
                           buy_num=buy_total,
                           # V0.3.12:分平台原值(展示层拆解用)
                           yyyp_sell_num=int(counts.get("yyyp_sell_num") or 0),
                           buff_sell_num=int(counts.get("buff_sell_num") or 0),
                           buff_buy_num=int(_buff_buy_raw or 0),
                           yyyp_buy_num=int(_yyyp_buy_raw or 0),
                           buff_yyyp_complete=True,
                           rate_7=float(pd.get("sell_price_rate_7") or 0),
                           rate_30=float(pd.get("sell_price_rate_30") or 0),
                           price_7=float(pd.get("sell_price_7") or 0),
                           price_30=float(pd.get("sell_price_30") or 0)),
            points, now, cfg, min_points=(2 if phase_window >= 120 else 4),
            window_minutes=phase_window, range_30d=rng)
        if sig is None:
            continue
        # 冷却: 同阶段冷却期内不重推;形态升级(last_type 不同)放行
        cd = cooldowns.get((gid, zh)) or {}
        until = _parse_dt(cd.get("cooldown_until"))
        if (until and now < until and str(cd.get("last_type") or "") == sig.phase):
            continue
        minutes = minutes_by_phase.get(sig.phase, 30)
        # V0.3.29:position 写真实区间分档(此前写死 "—",卡片一直显示兜底值「中位」);
        # detail 用新的「判读 + 历史参照」整段文案,reasons 保留作诊断/评分记录。
        _detail = "\n".join(sig.reading) if sig.reading else " / ".join(sig.reasons)
        row = {"anomaly_type": sig.phase, "level": ("L3" if sig.phase == "distribute" else "L2"),
               "name": zh, "price": float(pd.get("buffSellPrice") or 0),
               "score": sig.floor_score, "position": sig.position or "—",
               "reason": "\n".join(sig.lines), "detail": _detail}
        alert = _format_phase_alert(row)
        uid = r.get("user_openid") or ""
        delivered = False
        if r.get("group_openid"):
            if uid:
                alert = f'<qqbot-at-user id="{uid}" />\n' + alert
            result = await client.push_to_group_markdown(r["group_openid"], alert)
            delivered = result is not False
        else:
            result = await client.push_to_user(r["user_openid"], alert)
            delivered = result is not False
        if not delivered:
            _invalid_group = getattr(client, "is_invalid_group_target", None)
            _invalid_user = getattr(client, "is_invalid_user_target", None)
            invalid_target = (
                bool(r.get("group_openid"))
                and callable(_invalid_group)
                and bool(_invalid_group(r["group_openid"]))
            ) or (
                not r.get("group_openid")
                and callable(_invalid_user)
                and bool(_invalid_user(r.get("user_openid") or ""))
            )
            if invalid_target:
                await store.async_remove_watch_by_id(r["id"])
                _cw_logger.warning(
                    "[watch] 周期监测目标已注销，自动删除失效关注 id=%s item=%s",
                    r.get("id"), zh,
                )
                continue
            # 发送失败不写冷却，下一轮仍可重试，避免提醒永久丢失。
            _cw_logger.warning("[watch] 周期监测推送失败,保留待推状态: %s", zh)
            continue
        until_str = (now + timedelta(minutes=minutes)).strftime("%Y-%m-%d %H:%M:%S")
        await store.async_set_anomaly_cooldown(gid, zh, until_str,
                                               last_score=sig.floor_score, last_type=sig.phase)
        sent += 1
        _cw_logger.info("[watch] 周期监测命中 %s %s(%s) -> %s",
                        sig.phase, zh, sig.floor_score, r.get("user_openid"))
    if sent:
        _cw_logger.info("[watch] 周期监测本轮推送 %d 条", sent)
