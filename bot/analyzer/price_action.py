# -*- coding: utf-8 -*-
"""成交量/盘口量价解算。

说明:
- 有新鲜的真实 Steam 成交量时，优先用成交量及其周期变化；
  没有或过期时才使用 CSQAQ 能拿到的盘口活跃度（求购/在售数量及其变化）。
- 目标是把“量增价涨 / 量缩价跌 / 量价背离”等关系统一用于
  `/分析`、`/异动` 和异动推送。
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Sequence

from bot.csqaq.volume import normalize_turnover, turnover_change

_BOOK_SIG = 5.0
_PRICE_SIG = 1.0
_THIN_ACTIVITY = 6.0
_HEAVY_ACTIVITY = 20.0


def _get(obj: Any, key: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _num(obj: Any, *keys: str, default: Optional[float] = None) -> Optional[float]:
    for key in keys:
        v = _get(obj, key, None)
        if v is not None:
            try:
                return float(v)
            except (TypeError, ValueError):
                continue
    return default


def _int(obj: Any, *keys: str, default: int = 0) -> int:
    v = _num(obj, *keys, default=None)
    if v is None:
        return default
    return int(v)


def _parse_ts(ts: Any) -> Optional[datetime]:
    if not ts:
        return None
    text = str(ts)
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(text, fmt)
        except (ValueError, TypeError):
            continue
    return None


def _pct(cur: Optional[float], base: Optional[float]) -> Optional[float]:
    if cur is None or base in (None, 0):
        return None
    try:
        cur_f = float(cur)
        base_f = float(base)
    except (TypeError, ValueError):
        return None
    if base_f == 0:
        return None
    return (cur_f / base_f - 1.0) * 100.0


def _find_past_point(history: Sequence[Any], minutes: int,
                     now: datetime) -> Optional[Any]:
    if not history:
        return None
    target = now - timedelta(minutes=minutes)
    picked = None
    picked_t = None
    for h in history:
        t = _parse_ts(_get(h, "ts"))
        if t is None or t > target:
            continue
        if picked_t is None or t > picked_t:
            picked = h
            picked_t = t
    if picked is None or picked_t is None:
        return None
    if (target - picked_t).total_seconds() > minutes * 60 * 0.5:
        return None
    return picked


def _price_of(item: Any) -> Optional[float]:
    return _num(item, "price", "buff_sell_price", "main_price")


def _buy_of(item: Any) -> Optional[int]:
    """取该点的求购数：优先 BUFF+悠悠合计(V0.2.24 统一口径)，回退 BUFF 单平台。"""
    if _get(item, "total_buy_scope") == "buff_yyyp":
        total = _num(item, "total_buy_num", default=None)
        if total is not None:
            return int(total)
    return _int(item, "buy_num", "buff_buy_num", "steam_buy_num", default=0)


def _sell_of(item: Any) -> Optional[int]:
    """取该点的在售数：优先 BUFF+悠悠合计，回退 BUFF 单平台。"""
    if _get(item, "total_sell_scope") == "buff_yyyp":
        total = _num(item, "total_sell_num", default=None)
        if total is not None:
            return int(total)
    return _int(item, "sell_num", "buff_sell_num", "steam_sell_num", default=0)


def _normalize_position(position: Optional[str], rate_30: Optional[float],
                        price_30: Optional[float] = None,
                        price: Optional[float] = None) -> str:
    if position:
        if "低" in position:
            return "低位"
        if "高" in position:
            return "高位"
        return "中位"
    if rate_30 is not None:
        if rate_30 <= -8:
            return "低位"
        if rate_30 >= 8:
            return "高位"
    if price is not None and price_30 not in (None, 0):
        pos = _pct(price, price_30)
        if pos is not None:
            if pos <= -8:
                return "低位"
            if pos >= 8:
                return "高位"
    return "中位"


def _price_state(price_chg: Optional[float]) -> str:
    if price_chg is None:
        return "平"
    if price_chg >= _PRICE_SIG:
        return "涨"
    if price_chg <= -_PRICE_SIG:
        return "跌"
    return "平"


def _book_state(book_change_pct: Optional[float],
                buy_change: Optional[float],
                sell_change: Optional[float]) -> str:
    if book_change_pct is not None:
        if book_change_pct >= _BOOK_SIG:
            return "增"
        if book_change_pct <= -_BOOK_SIG:
            return "缩"
        return "平"

    pressure = (buy_change or 0.0) - (sell_change or 0.0)
    if pressure >= _BOOK_SIG:
        return "增"
    if pressure <= -_BOOK_SIG:
        return "缩"
    return "平"


def _relation(book_state: str, price_state: str) -> str:
    return {
        ("增", "涨"): "量增价涨",
        ("缩", "跌"): "量缩价跌",
        ("平", "涨"): "量平价涨",
        ("平", "跌"): "量平价跌",
        ("缩", "涨"): "量缩价涨",
        ("增", "跌"): "量增价跌",
        ("平", "平"): "量平价平",
        ("增", "平"): "量增价平",
        ("缩", "平"): "量缩价平",
        # V0.2.9: 盘口数据不足的显式未知态(不伪装成量平)
        ("未知", "涨"): "盘口未知·价涨",
        ("未知", "跌"): "盘口未知·价跌",
        ("未知", "平"): "盘口未知·价平",
    }.get((book_state, price_state), "量平价平")


def _specials(activity: Optional[float], relation: str, position: str,
              price_state: str, price_chg_30m: Optional[float],
              book_state: Optional[str] = None) -> List[str]:
    """特殊形态(盘口语义比喻)。一致性约束:
    - 「放量/天量」类只在盘口为 增/平 时触发——book_state=缩(净需求收缩,含在售暴增的卖压)
      时 activity 可能被卖侧绝对值抬高,此时标「放量」会与 relation=量缩* 同屏矛盾。
    - activity=None(双侧数据缺失)时跳过所有活动度类特殊形态:缺失≠无量。
    """
    out: List[str] = []
    thin = activity is not None and activity <= _THIN_ACTIVITY
    heavy = activity is not None and activity >= _HEAVY_ACTIVITY
    heavy_ok = book_state in (None, "增", "平")
    if thin and position == "低位" and price_state in ("跌", "平"):
        out.append("低活跃度低位")
    if thin and price_state == "涨":
        out.append("低活跃度上涨")
    if thin and price_state == "跌":
        out.append("低活跃度下跌")
    if heavy_ok and heavy and position == "高位" and price_state == "涨":
        out.append("高活跃度高位")
    if heavy_ok and heavy and position == "低位":
        out.append("底部活跃度增强")
    if heavy_ok and heavy and position == "高位" and price_state == "平":
        out.append("高位活跃度增加滞涨")
    if relation == "量缩价涨" and position == "高位":
        out.append("量价背离预警")
    if relation == "量增价跌" and position == "低位":
        out.append("最后一跌/恐慌出清")
    if price_chg_30m is not None and price_chg_30m < -15 and relation in ("量缩价跌", "量平价跌"):
        out.append("深跌后趋稳")
    return out


def _conclusion(relation: str, position: str, special: List[str],
                book_pressure: Optional[float] = None,
                rate_7: Optional[float] = None) -> str:
    if special:
        primary = special[0]
        return {
            "低活跃度低位": "市场极度冷清，卖无可卖，关注止跌",
            "低活跃度上涨": "活跃度偏低但价格上涨，高位尤其危险",
            "低活跃度下跌": "盘口承接偏弱，弱势延续",
            "高活跃度高位": "活跃度极高且处高位，多空分歧极致",
            "底部活跃度增强": "底部盘口活跃度增强，重点观察",
            "高位活跃度增加滞涨": "高位盘口活跃度增加但价格滞涨，谨慎追高",
            "量价背离预警": "价格与盘口方向不一致，先防回落",
            "最后一跌/恐慌出清": "最后一跌或洗盘，等确认",
            "深跌后趋稳": "活跃度收缩的止跌迹象，观察承接",
        }.get(primary, primary)

    # 中期趋势语境:平/盘整类 relation 只描述"当前短窗口",结论需带上 7d 背景,
    # 否则会出现「7d 大涨却只报多空僵持」的误读(如 KOI 全息 7d+37% 高位横盘)。
    mid_up = rate_7 is not None and rate_7 >= 8
    mid_dn = rate_7 is not None and rate_7 <= -8

    if relation.startswith("盘口未知"):
        # V0.2.9: 盘口数据不足的诚实结论(带中期背景),不伪装成量平类结论
        if mid_dn:
            return "盘口变化数据不足，价格弱势，以中期趋势为主要参考"
        if mid_up:
            return "盘口变化数据不足，价格强势，以中期趋势为主要参考"
        return "盘口变化数据不足，以价格方向与中期结构为主要参考"

    if relation == "量增价涨":
        return "低位=启动 / 中位=健康上行 / 高位=防诱多"
    if relation == "量缩价跌":
        return "低位=筑底 / 中位=正常回调 / 高位=下跌中继"
    if relation == "量平价涨":
        if mid_up:
            return "中期上行中的小幅走高，量能未跟上，防冲高乏力"
        return "惜售锁仓，但要看后续接力"
    if relation == "量平价跌":
        if mid_up:
            return "中期上行中的回调，关注买盘承接"
        return "阴跌格局，承接乏力"
    if relation == "量缩价涨":
            return "盘口活跃度收缩但价格上涨，动能衰竭"
    if relation == "量增价跌":
        # 区分量的来源:买盘净增(吸筹,如求购+20% 价跌)≠ 卖盘涌出(恐慌)。
        # CSQAQ 无成交量,仅靠盘口净压力方向近似——买盘驱动时勿误报"恐慌出逃"。
        if book_pressure is not None and book_pressure > 0:
            return "价跌但买盘净增：疑似低位吸筹，先辨真伪"
        return "卖压主导的盘口增加下跌，谨防恐慌出逃"
    if relation == "量增价平":
        return "盘口活跃度增加但价格横盘，供需分歧加大"
    if relation == "量缩价平":
        if mid_up:
            return "盘口活跃度收缩的高位整理，惜售与滞涨待辨"
        return "盘口活跃度收缩横盘，等待方向"
    if relation == "量平价平":
        if mid_up:
            if position == "高位":
                return "大涨后高位横盘：消化获利盘，防滞涨；再上攻需量能配合"
            return "上行中继蓄势，中期趋势未破，等方向选择"
        if mid_dn:
            return "下跌后的弱势横盘，反弹乏力"
        return "多空僵持，等待方向"
    return "多空僵持，等待方向"


def _t7_context(rate_7: Optional[float], rate_30: Optional[float]) -> str:
    """Return the medium-term context used by the T+7 view.

    A seven-day holding decision should be driven by daily structure rather
    than a 30-minute order-book sample.  The thresholds are intentionally
    broad; this is a context label, not an entry signal.
    """
    r7 = float(rate_7) if rate_7 is not None else None
    r30 = float(rate_30) if rate_30 is not None else None
    if r7 is None and r30 is None:
        return "中期数据不足"
    if r30 is not None and r30 >= 8 and r7 is not None and r7 <= -1:
        return "中期上行中的回调"
    if r30 is not None and r30 <= -8 and r7 is not None and r7 >= 1:
        return "中期下行中的反弹"
    if r7 is not None and r7 >= 3 and (r30 is None or r30 >= -3):
        return "中期偏强"
    if r7 is not None and r7 <= -3 and (r30 is None or r30 <= 3):
        return "中期偏弱"
    if r30 is not None and r30 >= 8:
        return "中期上行"
    if r30 is not None and r30 <= -8:
        return "中期下行"
    return "中期震荡"


def _t7_conclusion(context: str) -> str:
    """Human-readable T+7 conclusion; short-book data is explicitly optional."""
    if context == "中期上行中的回调":
        return "T+7以中期上行结构为主，当前处于回调观察期；30分钟盘口仅作执行参考"
    if context == "中期下行中的反弹":
        return "T+7仍处于中期下行结构，当前反弹先观察；30分钟盘口仅作执行参考"
    if context in ("中期偏强", "中期上行"):
        return "T+7中期结构偏强，关注位置与全平台流动性；30分钟盘口仅作执行参考"
    if context in ("中期偏弱", "中期下行"):
        return "T+7中期结构偏弱，控制追涨风险并等待企稳；30分钟盘口仅作执行参考"
    return "T+7中期方向暂不明确，以日K结构、位置和流动性继续观察；30分钟盘口仅作执行参考"


def analyze_price_action(
    *,
    price: float,
    buy_num: Optional[int] = None,
    sell_num: Optional[int] = None,
    history: Optional[Sequence[Any]] = None,
    now: Optional[datetime] = None,
    price_change_5m: Optional[float] = None,
    price_change_30m: Optional[float] = None,
    price_change_1d: Optional[float] = None,
    rate_7: Optional[float] = None,
    rate_30: Optional[float] = None,
    buy_change: Optional[float] = None,
    sell_change: Optional[float] = None,
    position: Optional[str] = None,
    market: Optional[Dict[str, Any]] = None,
    transaction_volume: Optional[float] = None,
    previous_transaction_volume: Optional[float] = None,
    transaction_period_at: Optional[str] = None,
    transaction_now: Optional[datetime] = None,
    transaction_max_age_hours: float = 48.0,
    transaction_volume_change: Optional[float] = None,
    transaction_avg_price: Optional[float] = None,   # V0.2.9: 修复 V0.2.8 传入报错(TypeError)
    include_transaction_reference: bool = False,
    book_scope: str = "",
    t7_mode: bool = False,
) -> Dict[str, Any]:
    """把真实成交量（优先）或盘口变化解算成可读的量价结论。

    :param price_change_1d: 近1日涨跌%(实时源如 CSQAQ sell_price_rate_1)。
        短窗口(30m/5m)历史快照稀疏反推不出时回退用它判方向——「判不出」≠「平静」。
    :param rate_7/rate_30: 中期涨跌%,用于位置与阶段修正(阶段会体现"上行/下行背景")。
    :param t7_mode: T+7 持有视角；日K/中期结构决定主结论，短线盘口仅作辅助展示。
    """
    now = now or datetime.now()
    history = history or []

    # 成交量只有在所属周期足够新、且存在可比较的上一周期时才参与“量价”关系。
    # 单个过期/孤立 turnover_number 只能展示，不能伪造成增减方向。
    volume_meta = normalize_turnover({
        "turnover_number": transaction_volume,
        "period_at": transaction_period_at,
    }, now=transaction_now or now, max_age_hours=transaction_max_age_hours)
    volume_fresh = bool(volume_meta["turnover_fresh"] and previous_transaction_volume is not None)
    volume_chg = (transaction_volume_change if volume_fresh and transaction_volume_change is not None
                  else (turnover_change(transaction_volume, previous_transaction_volume)
                        if volume_fresh else None))

    prev_30 = _find_past_point(history, 30, now)
    prev_5 = _find_past_point(history, 5, now)

    if price_change_30m is None and prev_30 is not None:
        price_change_30m = _pct(price, _price_of(prev_30))
    if price_change_5m is None and prev_5 is not None:
        price_change_5m = _pct(price, _price_of(prev_5))

    book_change_pct = None
    book_change_abs = None
    book_pressure = None
    if buy_num is not None and sell_num is not None and prev_30 is not None:
        prev_total = _buy_of(prev_30) + _sell_of(prev_30)
        cur_total = int(buy_num) + int(sell_num)
        book_change_pct = _pct(cur_total, prev_total)
        if prev_total is not None:
            book_change_abs = cur_total - prev_total

    if book_change_pct is None and (buy_change is not None or sell_change is not None):
        book_pressure = (buy_change or 0.0) - (sell_change or 0.0)
        book_change_abs = int(abs(book_pressure))

    # 价格窗口:优先 30m,退 5m。短窗口全部不可用时(如该品 history 几小时才一条快照,
    # 30m 内无可用点),回退近1日方向并显式标注来源——不能把"判不出"默认成"平静"。
    window_chg = price_change_30m if price_change_30m is not None else price_change_5m
    window_note = ""
    if window_chg is None:
        if price_change_1d is not None:
            window_chg = price_change_1d
            window_note = f"30分钟级盘口快照稀疏,价格方向按近1日 {price_change_1d:+.1f}% 计"
        else:
            window_note = "30分钟级盘口快照缺失,仅按盘口静态面判断"
    price_state = _price_state(window_chg)
    # V0.2.9: 盘口变化数据不足(无 30 分钟前快照且无变化率)时显式标"未知"——
    # 量平=真的持平,未知=没有对比数据,两者语义必须分开;旧版伪装成"量平"会误导
    # (如 B 池品 1 小时 0 快照,输出"量平价跌"实为"盘口变化未知,仅价格在跌")。
    book_known = (book_change_pct is not None or buy_change is not None
                  or sell_change is not None)
    book_state = (_book_state(book_change_pct, buy_change, sell_change)
                  if book_known else "未知")
    # Steam 成交量仅作辅助展示，核心量价关系始终采用盘口状态。
    volume_state = None
    relation = _relation(book_state, price_state)
    pos = _normalize_position(position, rate_30, _price_of(prev_30), price)

    # 盘口活跃度:双侧变化绝对值之和。两侧都无数据(缺失)时 activity=None——
    # 缺失≠无量,不能触发「无量/地量/放量」类特殊形态(否则会误标)。
    activity: Optional[float] = None
    if buy_change is not None or sell_change is not None:
        activity = abs(float(buy_change or 0.0)) + abs(float(sell_change or 0.0))
    special_book_state = book_state
    # T+7 视角不把短线盘口特殊形态升级为持有结论；异动/短线调用仍保留原逻辑。
    special = ([] if t7_mode else
               _specials(activity, relation, pos, price_state, price_change_30m,
                         special_book_state))
    t7_context = _t7_context(rate_7, rate_30) if t7_mode else ""
    summary = (f"T+7核心：{t7_context}｜{pos}" if t7_mode
               else f"{relation}｜{pos}")
    if special:
        summary += f"｜{special[0]}"

    market_note = ""
    if market:
        idx = market.get("index_chg")
        heat = market.get("heat")
        up = market.get("up")
        down = market.get("down")
        if t7_mode:
            bullish = "上行" in t7_context or "偏强" in t7_context
            bearish = "下行" in t7_context or "偏弱" in t7_context
        else:
            bullish = relation in ("量增价涨", "量平价涨", "量缩价涨")
            bearish = relation in ("量增价跌", "量平价跌", "量缩价跌")
        notes: List[str] = []
        if idx is not None:
            if bullish and idx >= 0.3:
                notes.append("顺大盘")
            elif bullish and idx <= -0.3:
                notes.append("逆势走强")
            elif bearish and idx <= -0.3:
                notes.append("顺势走弱")
            elif bearish and idx >= 0.3:
                notes.append("逆势回落")
        if up is not None and down is not None:
            if up > down * 1.5 and bullish:
                notes.append("今日上涨偏多")
            elif down > up * 1.5 and bearish:
                notes.append("今日下跌偏多")
        if heat:
            notes.append(f"热度{heat}")
        if notes:
            market_note = " / ".join(notes)

    if t7_mode:
        # T+7 阶段由日 K 的中期结构定义；盘口快照只作为后面的短线辅助行。
        phase = t7_context
    else:
        phase = "趋势健康"
        if relation.startswith("盘口未知"):
            phase = "盘口数据不足"
        elif relation in ("量缩价涨", "量增价跌"):
            phase = "趋势存疑"
        elif relation in ("量平价平", "量平价涨", "量平价跌", "量缩价平", "量增价平"):
            # 平/盘整类:结合中期趋势给语境,避免「7d大涨却只报多空僵持」的误读。
            # 阶段描述的是"当前短窗口盘面",中期方向单列说明背景。
            if rate_7 is not None and rate_7 >= 8:
                phase = "短窗口盘整(中期上行背景)"
            elif rate_7 is not None and rate_7 <= -8:
                phase = "短窗口盘整(中期下行背景)"
            else:
                phase = "盘整观察"
        if special:
            phase = "极端形态"

    if t7_mode:
        lines = [
            f"T+7核心判断：{t7_context}",
            f"阶段：{phase}",
            f"位置：{pos}",
            "注：T+7主结论基于日K趋势、位置与全平台流动性；30分钟盘口仅作短线预警",
        ]
        if book_change_pct is not None:
            lines.append(f"短线盘口（辅助）：{book_state}({book_change_pct:+.1f}%)")
        elif book_pressure is not None:
            lines.append(f"短线盘口（辅助）：{book_state}(压力{book_pressure:+.1f})")
        else:
            lines.append("短线盘口（辅助）：数据不足（不影响 T+7 结论）")
    else:
        lines = [
            f"盘口量价解算：{relation}",
            f"阶段：{phase}",
            f"位置：{pos}",
        ]
        if window_note:
            lines.append(f"注：{window_note}")
        if book_change_pct is not None:
            lines.append(f"盘口变：{book_state}({book_change_pct:+.1f}%)")
        elif book_pressure is not None:
            lines.append(f"盘口变：{book_state}(压力{book_pressure:+.1f})")
        else:
            lines.append("盘口变：数据不足(无 30 分钟前盘口快照对比)")
    # V0.2.9: 当前盘口绝对数量(求购/在售),与变化率互补
    if buy_num is not None and sell_num is not None:
        scope = f"（{book_scope}）" if book_scope else ""
        lines.append(f"当前盘口{scope}：求购 {int(buy_num):,} / 在售 {int(sell_num):,}")
    if (include_transaction_reference and transaction_volume is not None
            and volume_meta["turnover_fresh"]):
        ref = f"Steam成交参考：{int(transaction_volume):,}件（仅代表 Steam）"
        if transaction_avg_price:
            ref += f"，均价 ¥{transaction_avg_price:,.2f}"
        if volume_chg is not None:
            ref += f"，较上一周期 {volume_chg:+.1f}%"
        lines.append(ref)
    if special:
        lines.append(f"特殊：{' / '.join(special)}")
    if market_note:
        lines.append(f"大盘：{market_note}")
    conclusion = (_t7_conclusion(t7_context) if t7_mode
                  else _conclusion(relation, pos, special, book_pressure, rate_7))
    lines.append(f"结论：{conclusion}")

    return {
        "relation": relation,
        "phase": phase,
        "position": pos,
        "price_state": price_state,
        "book_state": book_state,
        "volume_state": volume_state,
        "volume_change": volume_chg,
        "volume_fresh": volume_fresh,
        "window_note": window_note,
        "book_change_pct": book_change_pct,
        "book_pressure": book_pressure,
        "book_change_abs": book_change_abs,
        "special": special,
        "summary": summary,
        "market_note": market_note,
        "activity": activity,
        "t7_mode": t7_mode,
        "t7_context": t7_context or None,
        "lines": lines,
    }


def format_price_action_block(view: Dict[str, Any]) -> str:
    return "\n".join(view.get("lines") or [])
