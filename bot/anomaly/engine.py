"""AnomalyEngine:饰品异动评分与类型判定(PRD V1.0)。

四条硬原则(不得违反):
1. 数据优先:单品详情有新鲜 Steam 成交量时使用真实成交数量；缺失/过期时**不伪造成交量**。
   需求增强统一表述为「求购数量增加 + 在售数量减少」(PRD §20 禁用词表)。
2. 缺失不填 0:某指标历史不足时为 unavailable,按**可用权重重新归一化**,
   避免新商品因缺历史被误判为强异动(PRD §26)。
3. 可解释:每个分量都产出人类可读原因,供 QQ 推送展示。
4. 纯逻辑:不依赖网络与 DB,便于离线测试(PRD §18)。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Sequence

from bot.anomaly.cycle import PhaseSignal, detect_phase
from bot.config import AnomalyConfig
from bot.csqaq.market_counts import book_split_inline, normalize_book
from bot.csqaq.volume import normalize_turnover, turnover_change

# 冷启动:各窗口所需的最少历史快照数(PRD §27)
_MIN_POINTS = {5: 3, 30: 10, 60: 20}

# 价格变化达到多少(绝对值 %)算满分 —— 越低越敏感。
# 标定依据:CS2 饰品短周期波动极小,5 分钟涨 1.5% 已是显著异动,
# 若按股票那套(3%~12%)标定会让真实异动全部沉到 L1 以下。
_FULL_AT = {5: 1.5, 30: 4.0, 60: 8.0}

# 求购/在售数量变化达到多少(绝对值 %)算满分
_FULL_AT_NUM = {"buy": 30.0, "sell": 25.0}

# 归一化置信度:可用权重占比低于此值按比例打折,防止"缺历史的新品"被误判强异动(PRD §26)
_CONFIDENCE_FULL_COVERAGE = 0.7

# 供需共振加成系数:求购增 + 在售减 同时显著时,OrderScore 额外加成比例。
# 必要性:PriceScore 占 35 分权重,而「需求异动」的特征恰恰是价格没动(PRD §19.3),
# 若无共振加成,求购 +32%/在售 -22% 这种强信号会被 35 分的零分稀释到 33 分(L0)而漏报。
_RESONANCE_BONUS = 0.4


@dataclass
class MetricSnapshot:
    """单品当前快照,全部来自真实 API 字段(禁止任何估算值)。"""

    good_id: int = 0
    name: str = ""
    price: float = 0.0              # 主参考价(BUFF 售价优先)
    sell_price: float = 0.0         # BUFF 售价
    buy_price: float = 0.0          # BUFF 求购价
    sell_num: int = 0               # 异动雷达在售数量(BUFF+悠悠)
    buy_num: int = 0                # 求购数量 BUFF+悠悠合计(不含Steam)
    yyyp_sell_num: int = 0          # 悠悠有品在售数
    buff_yyyp_complete: bool = False  # 两个平台在售字段是否同时存在
    rate_7: float = 0.0             # 7 日涨跌%  sell_price_rate_7
    rate_30: float = 0.0            # 30 日涨跌% sell_price_rate_30
    # V0.2.29:近1日涨跌%。榜单源若回包含 sell_price_rate_1 才有值,否则为 None ——
    # 历史实现把 rate_7 存进 price_change_1d 列再当"近1日"喂给量价解算,口径错配。
    rate_1: Optional[float] = None
    price_7: float = 0.0            # 7 日前价格 sell_price_7
    price_30: float = 0.0           # 30 日前价格 sell_price_30
    spread_chg: Optional[float] = None  # 价差变化%,仅榜单源提供(buff_price_chg 同源字段)
    transaction_volume: Optional[int] = None       # Steam 成交数量(单品详情)
    transaction_avg_price: Optional[float] = None  # Steam 成交均价($)
    transaction_period_at: Optional[str] = None    # 成交量所属周期
    previous_transaction_volume: Optional[int] = None
    buy_num_observed: bool = True   # False 表示缺实时求购字段（沿用值不能参与质量判死）
    # V0.3.12:分平台盘口原值,仅供**展示层**拆解(判定仍用上面的合计口径)。
    # None = 该字段本次不可得,展示时必须回退合计文案,绝不用 0 冒充。
    buff_sell_num: Optional[int] = None   # BUFF 在售
    buff_buy_num: Optional[int] = None    # BUFF 求购
    yyyp_buy_num: Optional[int] = None    # 悠悠求购(悠悠在售已在上面 yyyp_sell_num)


@dataclass
class HistoryPoint:
    """历史快照点(来自 item_listing_history)。"""

    ts: str
    price: float = 0.0
    sell_num: int = 0
    buy_num: int = 0
    buy_price: float = 0.0
    # V0.3.12:分平台历史值(展示层拆解用;None = 该点缺字段,不参与拆解)
    buff_sell_num: Optional[int] = None
    yyyp_sell_num: Optional[int] = None
    buff_buy_num: Optional[int] = None
    yyyp_buy_num: Optional[int] = None


@dataclass
class AnomalyResult:
    """评分结果。"""

    good_id: int
    name: str
    score: float = 0.0
    level: str = "L0"                       # L0 正常/L1 观察/L2 预警/L3 强异动/L4 极端
    direction: str = "MIXED"                # UP / DOWN / MIXED
    anomaly_type: str = "normal"            # surge/dump/demand/supply/divergence/stabilize/normal
    reasons: List[str] = field(default_factory=list)
    parts: Dict[str, float] = field(default_factory=dict)   # 各分量得分明细
    details: Dict[str, Any] = field(default_factory=dict)   # 各指标原始值(供推送展示)


def _parse_ts(ts: str) -> Optional[datetime]:
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(ts, fmt)
        except (ValueError, TypeError):
            continue
    return None


def _ramp(value: Optional[float], full_at: float, weight: float) -> float:
    """把指标绝对值线性映射到 0~weight,达到 full_at 即封顶。"""
    if value is None or full_at <= 0 or weight <= 0:
        return 0.0
    return max(0.0, min(weight, abs(float(value)) / full_at * weight))


def _pct(cur: float, base: float) -> Optional[float]:
    """变化百分比;基数非法时返回 None(绝不返回 0 冒充)。"""
    if not base or base <= 0 or cur is None:
        return None
    return (float(cur) / float(base) - 1.0) * 100.0


class AnomalyEngine:
    """异动评分引擎:价格 / 供需 / 价差 / 趋势 / 历史位置 五维综合。"""

    def __init__(self, cfg: AnomalyConfig):
        self._cfg = cfg
        # 近 N 日价格区间缓存(V0.3.0):{good_id: (最低, 最高)} + 每件品的刷新时间戳。
        # 整表聚合要 20+ 秒(1.3GB 库),故改为「只取本轮要评估的品、按品缓存 TTL」。
        self.range_map: Dict[int, tuple] = {}
        self.range_map_ts: Dict[int, float] = {}

    # ---------- 历史指标 ----------

    def _past_point(self, history: Sequence[HistoryPoint], minutes: int,
                    now: datetime) -> Optional[HistoryPoint]:
        """取 minutes 分钟前最近的一个历史点;快照数不足则 None(冷启动保护)。"""
        if not history or minutes not in _MIN_POINTS:
            return None
        need = _MIN_POINTS[minutes]
        if len(history) < need:
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
        # 容差:该点距目标时刻不得超过窗口的一半,否则视为数据不足
        if (target - picked_t).total_seconds() > minutes * 60 * 0.5:
            return None
        return picked

    def _price_change(self, history: Sequence[HistoryPoint], cur_price: float,
                      minutes: int, now: datetime) -> Optional[float]:
        p = self._past_point(history, minutes, now)
        if p is None:
            return None
        return _pct(cur_price, p.price)

    def _num_change(self, history: Sequence[HistoryPoint], cur: int,
                    minutes: int, now: datetime, key: str) -> Optional[float]:
        p = self._past_point(history, minutes, now)
        if p is None:
            return None
        base = p.sell_num if key == "sell" else p.buy_num
        return _pct(cur, base)

    # ---- 盘口信号三重判定(2026-09-04):百分比 + 绝对增量 + 基线分档 ----
    # 500→550(+50,+10%)=强;5→6(+1,+20%)=绝对量不足判噪声。白盒:结果写回
    # details(buy_signal/sell_signal/delta_abs/base),供 pool 连续两轮确认与推送展示。
    def _book_signal(self, history: Sequence[HistoryPoint], cur: int,
                     minutes: int, now: datetime, key: str,
                     detail_key: str, details: Dict[str, Any]) -> Optional[str]:
        """返回该侧盘口信号:strong(强信号) / weak(百分比够但绝对量不足=噪声) / None(无)。"""
        p = self._past_point(history, minutes, now)
        if p is None:
            return None
        base = p.sell_num if key == "sell" else p.buy_num
        pct = _pct(cur, base)
        abs_delta = cur - base
        details[f"{detail_key}_delta_abs"] = int(abs_delta)
        details[f"{detail_key}_base"] = int(base)
        if pct is None:
            return None
        c = self._cfg
        # 基线分档绝对量要求:低基线严(≥3),中(≥5),高(≥15);百分比下限 8%
        if base < c.book_base_low:
            abs_req = c.book_abs_low
        elif base < c.book_base_mid:
            abs_req = c.book_abs_mid
        else:
            abs_req = c.book_abs_high
        details[f"{detail_key}_abs_req"] = int(abs_req)
        pct_ok = (pct >= c.book_pct_min if key == "buy" else pct <= -c.book_pct_min)
        abs_ok = abs(abs_delta) >= abs_req
        if pct_ok and abs_ok:
            return "strong"
        if pct_ok:   # 百分比达标但绝对量不足:如 5→6(+20%/+1),噪声
            return "weak"
        return None

    # ---------- 五维评分 ----------

    def _score_price(self, history: Sequence[HistoryPoint], snap: MetricSnapshot,
                     now: datetime, parts: Dict[str, float], details: Dict[str, Any],
                     reasons: List[str], used: List[float]) -> None:
        """PriceScore 0~35:5m/30m/1h 价格变化(涨跌都计分)。

        V0.2.29:加入**绝对金额下限** —— 与盘口数量的三重判定对称。
        百分比达标的低价品若绝对变动不足 `price_abs_min`(元)则该窗口不计分
        (权重仍占位,避免归一化虚高),防止 ¥9 品 3 毛钱波动被百分比放大成异动。
        """
        c = self._cfg
        for minutes, weight, label in (
            (5, c.w_price_5m, "5m"),
            (30, c.w_price_30m, "30m"),
            (60, c.w_price_1h, "1h"),
        ):
            p = self._past_point(history, minutes, now)
            chg = _pct(snap.price, p.price) if p is not None else None
            key = f"price_change_{label}"
            details[key] = chg
            if chg is None:
                continue  # 历史不足 -> unavailable,不参与归一化
            abs_move = abs(float(snap.price) - float(p.price))
            details[f"price_delta_abs_{label}"] = round(abs_move, 2)
            used.append(weight)      # 数据可用 → 权重占位;金额不足时计 0 分
            if abs_move < c.price_abs_min:
                details[f"price_abs_floor_{label}"] = True
                if abs(chg) >= 3.0:
                    reasons.append(
                        f"{label}价格 {chg:+.1f}% 但绝对仅 ¥{abs_move:.2f}"
                        f"(< ¥{c.price_abs_min:g}),金额过小不计分")
                continue
            got = _ramp(chg, _FULL_AT[minutes], weight)
            parts[f"price_{label}"] = round(got, 2)
            if got >= weight * 0.6:
                arrow = "上涨" if chg > 0 else "下跌"
                reasons.append(f"{label}价格{arrow} {chg:+.1f}%")

    def _score_order(self, history: Sequence[HistoryPoint], snap: MetricSnapshot,
                     now: datetime, parts: Dict[str, float], details: Dict[str, Any],
                     reasons: List[str], used: List[float]) -> None:
        """OrderScore 0~25:求购增加 / 在售减少 / 买卖盘数量比异常。

        2026-09-04:求购/在售变化从「只看百分比」升级为三重判定(百分比+绝对增量+基线)。
        strong 才计分;weak(百分比够但绝对量不足,如 5→6)=噪声,归零并在原因里注明。
        """
        c = self._cfg
        buy_sig = (self._book_signal(history, snap.buy_num, 30, now, "buy", "buy", details)
                   if snap.buy_num_observed else None)
        sell_sig = self._book_signal(history, snap.sell_num, 30, now, "sell", "sell", details)
        buy_chg = (self._num_change(history, snap.buy_num, 30, now, "buy")
                   if snap.buy_num_observed else None)
        sell_chg = self._num_change(history, snap.sell_num, 30, now, "sell")
        details["buy_change"] = buy_chg
        details["sell_change"] = sell_chg
        details["buy_signal"] = buy_sig
        details["sell_signal"] = sell_sig
        # V0.3.12:分平台拆解(展示用)。数据不全时返回 None → 文案保持合计口径,绝不猜。
        _p30 = self._past_point(history, 30, now)
        _base_book = normalize_book(_p30) if _p30 is not None else None
        _cur_book = normalize_book(snap)
        _buy_in = book_split_inline(_base_book, _cur_book, "buy")
        _sell_in = book_split_inline(_base_book, _cur_book, "sell")
        _buy_in_txt = f";{_buy_in}" if _buy_in else ""
        _sell_in_txt = f";{_sell_in}" if _sell_in else ""

        if buy_sig == "strong":
            got = _ramp(buy_chg if buy_chg > 0 else 0.0, _FULL_AT_NUM["buy"], c.w_buy_change)
            parts["buy_change"] = round(got, 2)
            used.append(c.w_buy_change)
            reasons.append(f"求购数量增加 {buy_chg:+.0f}%(+{details['buy_delta_abs']}单,"
                           f"基线{details['buy_base']}{_buy_in_txt},强信号)")
        elif buy_sig == "weak":
            used.append(c.w_buy_change)   # 占位:弱信号不计分,但权重已不可用,避免归一化虚高
            reasons.append(f"求购 +{buy_chg:+.0f}% 但绝对仅 +{details['buy_delta_abs']} 单"
                           f"(基线{details['buy_base']}{_buy_in_txt}),噪声不计")
        if sell_sig == "strong":
            # 在售减少(负向)才计分
            got = _ramp(sell_chg if sell_chg < 0 else 0.0, _FULL_AT_NUM["sell"], c.w_sell_change)
            parts["sell_change"] = round(got, 2)
            used.append(c.w_sell_change)
            reasons.append(f"在售数量减少 {sell_chg:+.0f}%({details['sell_delta_abs']}单,"
                           f"基线{details['sell_base']}{_sell_in_txt},强信号)")
        elif sell_sig == "weak":
            used.append(c.w_sell_change)
            reasons.append(f"在售 {sell_chg:+.0f}% 但绝对仅 {details['sell_delta_abs']} 单"
                           f"(基线{details['sell_base']}{_sell_in_txt}),噪声不计")

        if snap.buy_num_observed:
            ratio = snap.buy_num / max(snap.sell_num, 1)
            details["order_ratio"] = round(ratio, 3)
            # 偏离中性(1.0)越远越异常:求购远大于在售=需求旺;在售远大于求购=供应压力
            dev = abs(ratio - 1.0)
            got = _ramp(dev, 1.0, c.w_order_ratio)
            parts["order_ratio"] = round(got, 2)
            used.append(c.w_order_ratio)
            if ratio >= 1.5:
                reasons.append(f"求购/在售比 {ratio:.2f},需求偏强")
            elif ratio <= 0.3:
                reasons.append(f"求购/在售比 {ratio:.2f},供应偏多")
        else:
            details["order_ratio"] = None

    def _score_spread(self, history: Sequence[HistoryPoint], snap: MetricSnapshot,
                      now: datetime, parts: Dict[str, float], details: Dict[str, Any],
                      reasons: List[str], used: List[float]) -> None:
        """SpreadScore 0~15:买卖价差变化。

        V0.2.29 两处修正:
        1. **改为有符号** —— 只有「收窄」(交易意愿增强)计分;「扩大」是流动性走弱,
           只写原因文案不计分(旧实现取绝对值,扩大与收窄同分,最高分事件即由"价差扩大 30%"驱动)。
        2. **兜底口径改百分点** —— 批量查价源自算时用 `cur_sp - old_sp`(百分点差),
           不再做"价差的百分比变化"(旧口径在低价品上会产出 340% 这类尺度假信号)。
        """
        c = self._cfg
        chg: Optional[float] = snap.spread_chg
        basis = "pct"      # 榜单源:sell_buy_diff_chg_buff,单位 %
        if chg is None and snap.buy_price and snap.sell_price:
            # 批量查价源没有 API 价差变化字段,用历史 buy_price 自算
            p = self._past_point(history, 30, now)
            if p and p.buy_price and p.price:
                cur_sp = (snap.sell_price - snap.buy_price) / max(snap.buy_price, 1e-9) * 100
                old_sp = (p.price - p.buy_price) / max(p.buy_price, 1e-9) * 100
                chg = cur_sp - old_sp      # 百分点差(旧版是 _pct(cur_sp, old_sp))
                basis = "pp"
        details["spread_chg"] = chg
        details["spread_basis"] = basis if chg is not None else None
        if chg is None:
            return
        used.append(c.w_spread)          # 权重占位:扩大方向不计分,但避免归一化虚高
        full_at = c.spread_pct_full if basis == "pct" else c.spread_pp_full
        unit = "%" if basis == "pct" else "pp"
        if chg < 0:
            got = _ramp(chg, full_at, c.w_spread)
            parts["spread"] = round(got, 2)
        if chg <= -full_at * 0.5:
            reasons.append(f"买卖价差收窄 {abs(chg):.0f}{unit}(交易意愿增强)")
        elif chg >= full_at * 0.5:
            reasons.append(f"买卖价差扩大 {abs(chg):.0f}{unit}(流动性走弱,不计分)")

    def _score_trend(self, snap: MetricSnapshot, details: Dict[str, Any],
                     parts: Dict[str, float], reasons: List[str],
                     used: List[float]) -> None:
        """TrendScore 0~15:短中期方向一致性(5m/30m/1h/7d/30d)。

        V0.2.29:7日/30日基线先过合理性闸 —— 实测 ¥20 品出现 7日 +665% 的陈旧基线,
        却被当作"短中期方向一致(上涨)"把噪声顶进 L3。超阈值的基线不计入趋势,
        也不计入 `_direction` 的方向票(与 recommender 的 |rate_7|>=100 排除口径一致)。
        """
        c = self._cfg
        r7, r30 = snap.rate_7, snap.rate_30
        if r7 and abs(r7) > c.trend_baseline_max_7d:
            details["baseline_reject_7d"] = r7
            reasons.append(f"7日基线异常({r7:+.0f}%),不计入趋势")
            r7 = None
        if r30 and abs(r30) > c.trend_baseline_max_30d:
            details["baseline_reject_30d"] = r30
            reasons.append(f"30日基线异常({r30:+.0f}%),不计入趋势")
            r30 = None
        vals = [
            details.get("price_change_5m"),
            details.get("price_change_30m"),
            details.get("price_change_1h"),
            r7,
            r30,
        ]
        dirs = [1 if v and v > 0.5 else (-1 if v and v < -0.5 else 0) for v in vals]
        valid = [d for d in dirs if d != 0]
        # 至少 2 个指标有明确方向才谈"一致性":仅靠 1 个指标(如 rate_30 微涨)拿满分会让
        # 冷启动商品虚高(实测单指标即可刷满 15 分,直接把新品顶到 L3)
        if len(valid) < 2:
            return
        main = 1 if sum(valid) > 0 else -1
        same = sum(1 for d in valid if d == main)
        got = c.w_trend * same / len(valid)
        parts["trend"] = round(got, 2)
        used.append(c.w_trend)
        details["trend_aligned"] = f"{same}/{len(valid)}"
        if same >= 4:
            reasons.append(f"短中期方向一致({'上涨' if main > 0 else '下跌'} {same}/{len(valid)})")

    def _score_position(self, snap: MetricSnapshot, parts: Dict[str, float],
                        details: Dict[str, Any], reasons: List[str],
                        used: List[float]) -> None:
        """PositionScore 0~10:历史价格位置,用于区分低位启动与高位追涨。"""
        c = self._cfg
        pos7 = _pct(snap.price, snap.price_7) if snap.price_7 else None
        pos30 = _pct(snap.price, snap.price_30) if snap.price_30 else None
        details["pos_7"] = pos7
        details["pos_30"] = pos30
        # 异常值防护:历史参考价偏差超过 ±400%(即 5 倍以上)视为数据不可靠。
        # 实测 CSQAQ 榜单的 sell_price_7 偶发异常值(如 8799 元商品返回 31000 元参考价,
        # 导致 position 算出 +2707% 虚高满分),必须过滤,否则会把普通商品顶进 A/S 池。
        cand = [v for v in (pos7, pos30) if v is not None and abs(v) <= 400]
        if not cand:
            return
        pos = max(cand, key=abs)
        details["position"] = "低位" if pos < -8 else ("高位" if pos > 8 else "中位")
        got = _ramp(pos, 30.0, c.w_position)
        parts["position"] = round(got, 2)
        used.append(c.w_position)
        if pos <= -15:
            reasons.append(f"价格处近期低位({pos:+.0f}%)")
        elif pos >= 15:
            reasons.append(f"价格处近期高位({pos:+.0f}%)")

    # ---------- 方向与类型 ----------

    def _direction(self, snap: MetricSnapshot, details: Dict[str, Any]) -> str:
        """UP / DOWN / MIXED(PRD §18:指标矛盾时不得直接标强上涨)。

        V0.2.29:7日/30日基线超过合理性闸时不投方向票(与 `_score_trend` 同口径)。
        """
        up = down = 0
        pc = details.get("price_change_30m")
        if pc is not None:
            if pc > 0.5:
                up += 1
            elif pc < -0.5:
                down += 1
        bc, sc = details.get("buy_change"), details.get("sell_change")
        if bc is not None:
            if bc > 10:
                up += 1
            elif bc < -10:
                down += 1
        if sc is not None:
            if sc < -10:
                up += 1      # 在售减少 = 供应收缩 = 利涨
            elif sc > 10:
                down += 1    # 在售增加 = 供应压力 = 利跌
        for r, limit in ((snap.rate_7, self._cfg.trend_baseline_max_7d),
                         (snap.rate_30, self._cfg.trend_baseline_max_30d)):
            if not r or abs(r) > limit:
                continue
            if r > 2:
                up += 1
            elif r < -2:
                down += 1
        if up - down >= 2:
            return "UP"
        if down - up >= 2:
            return "DOWN"
        return "MIXED"

    def _classify(self, snap: MetricSnapshot, details: Dict[str, Any],
                  direction: str) -> str:
        """异动类型(PRD §19 / §21 组合表)。

        surge      突然拉升:价涨 + 求购增 + 在售减
        dump       突然跳水:价跌 + 供应增/需求降
        demand     需求增强:求购增 + 在售减 + 价格未明显动(潜在启动)
        supply     供应减少:在售明显下降
        divergence 上涨但供需背离:价涨 + 求购减 + 在售增(追高风险)
        stabilize  下跌后企稳:价跌但求购增/在售减(出现承接)
        """
        pc5 = details.get("price_change_5m")
        pc30 = details.get("price_change_30m")
        bc = details.get("buy_change")
        sc = details.get("sell_change")
        price_up = (pc30 is not None and pc30 > 1.0) or (pc5 is not None and pc5 > 1.0)
        price_dn = (pc30 is not None and pc30 < -1.0) or (pc5 is not None and pc5 < -1.0)
        buy_up = bc is not None and bc >= 15
        buy_dn = bc is not None and bc <= -15
        sell_dn = sc is not None and sc <= -15
        sell_up = sc is not None and sc >= 15

        if price_up and buy_dn and sell_up:
            return "divergence"       # 涨但供需反向 -> 追高风险
        if price_up and (buy_up or sell_dn):
            return "surge"
        if price_up:
            return "supply" if sell_dn else "normal"
        if price_dn and buy_up and sell_dn:
            return "stabilize"        # 跌但出现承接
        if price_dn and (buy_dn or sell_up):
            return "dump"
        if buy_up and sell_dn:
            return "demand"           # 价格未动但两边挤压 -> 潜在启动
        if sell_dn:
            return "supply"
        return "normal"

    def _resonance(self, parts: Dict[str, float], details: Dict[str, Any],
                   reasons: List[str]) -> None:
        """供需共振加成:求购增加与在售减少同时「强信号」,比单侧变化更强烈。

        2026-09-04:触发条件从百分比(≥15%)升级为双侧 book_signal==strong(百分比+绝对量+基线),
        避免 5→6 类弱信号参与共振加成。
        """
        bs, ss = details.get("buy_signal"), details.get("sell_signal")
        if bs != "strong" or ss != "strong":
            return
        base = sum(v for k, v in parts.items()
                   if k in ("buy_change", "sell_change", "order_ratio"))
        bonus = base * _RESONANCE_BONUS
        if bonus <= 0:
            return
        parts["resonance"] = round(bonus, 2)
        reasons.append("求购增加与在售减少同时出现,供需双侧共振")

    def _level(self, score: float) -> str:
        c = self._cfg
        if score >= c.level_extreme:
            return "L4"
        if score >= c.level_strong:
            return "L3"
        if score >= c.level_warning:
            return "L2"
        if score >= c.level_watch:
            return "L1"
        return "L0"

    @staticmethod
    def _reject(snap: "MetricSnapshot", reason: str) -> "AnomalyResult":
        """门槛拦截：返回 score=0 的 L0 结果并记录原因；池管理器再按原因
        区分真实质量失败、数据缺失和异动资格不足，决定保留或移除。"""
        return AnomalyResult(good_id=snap.good_id, name=snap.name,
                             score=0.0, level="L0", direction="MIXED",
                             anomaly_type="normal",
                             details={"price": snap.price, "sell_num": snap.sell_num,
                                      "buy_num": snap.buy_num, "rejected": reason,
                                      "buy_num_observed": snap.buy_num_observed})

    # ---- 近 N 日价格区间(V0.3.0:派发「高位」真实口径) ----
    # runner 的各扫描任务只对**本轮要评估的品**拉取区间(按品 TTL 缓存),
    # 这里只做 O(1) 查表;没有数据时返回 None,detect_phase 自动回退旧口径 pos_30。
    def _range_for(self, good_id: Any):
        if not self.range_map:
            return None
        try:
            return self.range_map.get(int(good_id or 0))
        except (TypeError, ValueError):
            return None

    # ---------- 主入口 ----------

    def evaluate(self, snap: MetricSnapshot,
                 history: Optional[Sequence[HistoryPoint]] = None,
                 now: Optional[datetime] = None) -> AnomalyResult:
        """计算单品异动评分。

        :param snap: 当前快照(真实 API 字段)
        :param history: item_listing_history 时序,按时间**正序**
        :param now: 当前时刻(便于测试注入)
        """
        now = now or datetime.now()
        history = history or []

        # ---- 质量门槛(治本:拦截无流动性/低价值噪声,而非单纯调高评分阈值) ----
        # 低价款(¥5 以下)与死盘(0 求购)在百分比口径下波动虚高,会把评分顶到 L2 刷屏;
        # 薄盘(在售侧总价值过低)即便挂单数达标也无真实交易深度。入口直接拦截不评分。
        if snap.price < self._cfg.min_price:
            return self._reject(snap, "min_price")
        # 先执行与求购深度无关的基础质量闸门。异动深度门槛不能短路这些检查，
        # 否则低求购成员会永远绕过薄盘、价差、离谱涨幅和高价噪声清理。
        if snap.buy_num_observed and snap.buy_num < self._cfg.min_buy_num:
            return self._reject(snap, "min_buy_num")
        if snap.sell_num < self._cfg.min_sell_num:
            # 在售挂单过少(默认 <3)说明没有真实交易市场:即便单价高、求购多,
            # 1 个挂单的 ¥500 品深度=¥500 也能过 min_depth,但本质是薄盘噪声。
            return self._reject(snap, "min_sell_num")
        if snap.sell_num * snap.price < self._cfg.min_depth:
            return self._reject(snap, "min_depth")
        if snap.buy_num_observed and snap.sell_num + snap.buy_num < self._cfg.min_listing:
            return self._reject(snap, "min_listing")
        # BUFF+悠悠流动性闸门。扫描入口已要求字段完整，这里保留同口径兜底。
        if (snap.buff_yyyp_complete
                and snap.sell_num < self._cfg.min_platform_sell):
            return self._reject(snap, "min_platform_sell")
        # 价差死盘门:售价/求购价超过阈值倍数 = 求购全是捡漏单(如求购价仅售价 1/6~1/27),
        # 售价是「挂着卖」的价格而非成交价,无真实流动性。正经流动品价差恒在 1.04~1.4x。
        # 求购价为 0 时无法判定(榜单源偶缺),跳过不误伤。
        if (snap.sell_price > 0 and snap.buy_price > 0
                and snap.sell_price / snap.buy_price > self._cfg.max_sell_buy_ratio):
            return self._reject(snap, "spread_too_wide")

        # 离谱涨幅门:CSQAQ 陈旧/错误基线会让薄盘品 1h 显示 +3231%(5m 不动、参考价错),
        # 这种在 CS2 几乎不可能真实发生,直接判数据故障拦截(不评分不推送)。
        # 仅当历史充足(能算出该窗口变化)才判定,缺历史则交给常规评分+置信度处理。
        pc30 = self._price_change(history, snap.price, 30, now)
        if pc30 is not None and abs(pc30) > self._cfg.implausible_30m:
            return self._reject(snap, "implausible_move_30m")
        pc60 = self._price_change(history, snap.price, 60, now)
        if pc60 is not None and abs(pc60) > self._cfg.implausible_1h:
            return self._reject(snap, "implausible_move_1h")
        # 价动量不动门:价格大幅波动(≥thin_noise_price_pct%)但盘口几乎未同步变化
        # (|盘口变化| < |价格变化|/3)=参考价塌方/薄盘噪声,非真实异动。
        # 例:格洛克蓝色裂纹 30m -51% 但在售 10→11(仅+10%)、求购 8→8(0%);
        #    Team Liquid 印花 30m -42% 但盘口 ±2% 以内。真实泵升必有盘口同步变化。
        # 盘口两侧均无历史时(数据不足)跳过,交给置信度折扣处理,不误拦。
        if pc30 is not None and abs(pc30) >= self._cfg.thin_noise_price_pct:
            bc30 = (self._num_change(history, snap.buy_num, 30, now, "buy")
                    if snap.buy_num_observed else None)
            sc30 = self._num_change(history, snap.sell_num, 30, now, "sell")
            sides = [v for v in (bc30, sc30) if v is not None]
            if sides and abs(pc30) >= 3.0 * max(abs(v) for v in sides):
                return self._reject(snap, "thin_price_noise")

        # ---- 资金周期四阶段检测(V0.2.0):建仓/启动/派发,并行独立 ----
        # 位置在降噪闸门之前:建仓=价格横盘+盘口真变化,派发=高位在售暴增,
        # 两者都是「盘口在动」的真实指纹,不应被 high_price_weak/stale(专拦盘口死水)误拦。
        # 假买墙过滤在 detect_phase 内部(求购增+价格跌+在售暴增=诱多,返回 None)。
        phase_sig: Optional[PhaseSignal] = None
        if self._cfg.phase_enable and snap.buy_num_observed:
            try:
                phase_window = int(getattr(self._cfg, "phase_window_minutes", 60))
                phase_sig = detect_phase(
                    snap, history, now, self._cfg,
                    # 2 小时总结快照只需“当前点+上一总结点”即可做一次窗口比较；
                    # 锚点本身仍会校验时间距离，不会把几分钟内的点冒充 2 小时变化。
                    min_points=(2 if phase_window >= 120 else None),
                    price_chg_60=(pc60 if phase_window == 60 else None),
                    window_minutes=phase_window,
                    # V0.3.0:近 N 日价格区间(派发「高位」真实口径),由 runner 定期刷新后
                    # 挂在 engine.range_map 上;缺失时 detect_phase 自动回退旧口径。
                    range_30d=self._range_for(snap.good_id),
                )
            except Exception:
                phase_sig = None   # 周期检测失败不阻塞常规评分

        # ---- 高价品流动性闸门(治本) ----
        # 高价收藏品(单价 ≥ high_price_tier)挂单数不少、但成交极稀,2~5% 的价格波动
        # 多为参考价微调而非真实异动。实测 M4A4 咆哮 FN(¥44300,在售94/求购15)30m -3.3%
        # 但求购 0%、在售仅 +2.2%(盘口死水),却反复触发 L2;而内格夫雷神之锤(求购 +20%)
        # 盘口真变化的品应保留。故对高价品单设更严格的盘口配合判定。
        if snap.price >= self._cfg.high_price_tier:
            # 1) 买盘稀薄:求购 < high_price_min_buy = 几乎没人愿意买,转手难(如双栖 ¥16800 仅 2 求购)
            if snap.buy_num_observed and snap.buy_num < self._cfg.high_price_min_buy:
                return self._reject(snap, "high_price_thin_buy")
            # 2)/3) 价动量不动 + 弱信号闸门:专拦「盘口死水」的高价品噪音。
            #    周期信号(建仓/启动/派发)本身就是盘口真变化的强指纹,豁免这两道闸,
            #    否则横盘吸筹的高价刀类会被 high_price_weak 永久拦截(V0.2.0)。
            if phase_sig is None:
                # 2) 价动量不动:价格在动(≥high_price_noise_pct%)但盘口两侧都未「真配合」
                #    → 参考价微调,拦。高价品成交稀、求购基数小(如咆哮 15 个),+1 个就是 +6.7%,
                #    百分比纯噪声 —— 真配合须「百分比 ≥ num_sig 且绝对量变化 ≥2 单」双满足。
                if (snap.buy_num_observed and pc30 is not None
                        and abs(pc30) >= self._cfg.high_price_noise_pct):
                    bc30 = (self._num_change(history, snap.buy_num, 30, now, "buy")
                            if snap.buy_num_observed else None)
                    sc30 = self._num_change(history, snap.sell_num, 30, now, "sell")
                    bp = self._past_point(history, 30, now)
                    buy_prev = bp.buy_num if bp and snap.buy_num_observed else None
                    sell_prev = bp.sell_num if bp else None

                    def _book_moved(pct, cur, prev) -> bool:
                        return (pct is not None and prev is not None
                                and abs(pct) >= self._cfg.high_price_num_sig
                                and abs(cur - prev) >= 2)

                    if (not _book_moved(bc30, snap.buy_num, buy_prev)
                            and not _book_moved(sc30, snap.sell_num, sell_prev)):
                        return self._reject(snap, "high_price_stale")
                # 3) 弱信号:价格几乎没动(5m/30m 都 <high_price_noise_pct%)——高价收藏品(如蝴蝶刀
                #    StatTrak 渐变之色 FN ¥14888)微动却靠价差/盘比等分量凑到 L2,纯噪音,拦。
                pc5 = self._price_change(history, snap.price, 5, now)
                pc5_quiet = pc5 is None or abs(pc5) < self._cfg.high_price_noise_pct
                if pc30 is not None and pc5_quiet and abs(pc30) < self._cfg.high_price_noise_pct:
                    return self._reject(snap, "high_price_weak")

        # 异动信号要求足够的求购深度。该门槛只决定本轮是否有资格参与异动评分/推送，
        # 放在全部真实质量检查之后，避免把其他质量失败掩盖成“求购不足”。
        if (snap.buy_num_observed
                and getattr(self._cfg, "min_anomaly_buy_num", 0) > 0
                and snap.buy_num < self._cfg.min_anomaly_buy_num):
            return self._reject(snap, "min_anomaly_buy_num")

        parts: Dict[str, float] = {}
        details: Dict[str, Any] = {
            "price": snap.price,
            "sell_num": snap.sell_num,
            "buy_num": snap.buy_num,
            "buy_num_observed": snap.buy_num_observed,
            "rate_7": snap.rate_7,
            "rate_30": snap.rate_30,
            "rate_1": snap.rate_1,
        }
        vol = normalize_turnover({
            "turnover_number": snap.transaction_volume,
            "period_at": snap.transaction_period_at,
        }, now=now)
        details["transaction_volume"] = vol.get("turnover_number")
        details["transaction_avg_price"] = snap.transaction_avg_price
        details["transaction_period_at"] = vol.get("turnover_period_at")
        details["transaction_volume_fresh"] = bool(
            vol.get("turnover_fresh") and snap.previous_transaction_volume is not None)
        details["previous_transaction_volume"] = snap.previous_transaction_volume
        details["transaction_volume_change"] = (
            turnover_change(snap.transaction_volume, snap.previous_transaction_volume)
            if details["transaction_volume_fresh"] else None
        )
        # 成交量只保存在 details 供主动分析参考；不参与评分、类型、
        # 原因或异动推送，核心异动口径仍是盘口与价格。
        details["volume_state"] = None
        reasons: List[str] = []
        used: List[float] = []      # 参与计算的权重(用于归一化)

        self._score_price(history, snap, now, parts, details, reasons, used)
        self._score_order(history, snap, now, parts, details, reasons, used)
        self._score_spread(history, snap, now, parts, details, reasons, used)
        self._score_trend(snap, details, parts, reasons, used)
        self._score_position(snap, parts, details, reasons, used)
        self._resonance(parts, details, reasons)

        # 归一化:只按可用分量折算到 100 分制(PRD §26:缺失不填 0)
        total_w = sum(used)
        earned = sum(parts.values())
        raw = (earned / total_w * 100.0) if total_w > 0 else 0.0
        # 置信度折扣:可用权重占比不足时按比例打折。
        # 否则历史稀薄的新品仅凭"5m 涨 3% + 趋势"就能冲到 74 分(L3),属严重误判。
        coverage = total_w / 100.0
        confidence = min(1.0, coverage / _CONFIDENCE_FULL_COVERAGE)
        details["coverage"] = round(coverage, 2)
        details["confidence"] = round(confidence, 2)
        score = round(max(0.0, min(100.0, raw * confidence)), 1)

        # ---- 周期信号落定(V0.2.0;V0.3.0 保底分改「加分制」) ----
        # 建仓/派发等阶段信号独立于五维评分——建仓期价格横盘会把 PriceScore(35 分)拉成 0,
        # 不给加分就永远到不了推送线。
        # V0.3.0:实测 209 条阶段事件里 208 条恰好等于保底分(建仓 120/121、启动 83/83、
        # 派发 5/5),评分被完全架空;而阶段信号的真实基础分只有 6~20 分(建仓 11.3 /
        # 启动 10.7 / 派发 6.4),所以「基础分不够就不保底」的硬闸门会连真信号一起杀光。
        # 改为加分制:`score = min(100, 基础分 + phase_bonus_*)` —— 基础分≈0 的噪声自然到
        # 不了推送线,基础分高的品还能升到 L3,评分重新参与取舍且没有悬崖。
        if phase_sig is not None:
            bonus = {
                "accum": self._cfg.phase_bonus_accum,
                "launch": self._cfg.phase_bonus_launch,
                "distribute": self._cfg.phase_bonus_distrib,
            }.get(phase_sig.phase, 0.0)
            base_score = score
            score = round(min(100.0, base_score + float(bonus or 0.0)), 1)
            details["phase_bonus"] = {"base": round(base_score, 1), "bonus": round(float(bonus or 0.0), 1)}
            atype_override = phase_sig.phase
            details["phase"] = {
                "phase": phase_sig.phase,
                "title": phase_sig.title,
                "icon": phase_sig.icon,
                "lines": phase_sig.lines,
                "reading": phase_sig.reading,
                "pos_pct": phase_sig.pos_pct,
            }
            # V0.3.38:启动卡要显示「¥旧 → ¥现」。必须放在 details **顶层**
            # (runner 用 d.get("price_from") 取,和 d.get("price") 同一个平面)。
            if phase_sig.price_from:
                details["price_from"] = phase_sig.price_from
            # V0.3.29:阶段事件的位置用**区间分位**判定(低位/中位/高位),覆盖引擎原先
            # 按「涨跌幅 ±8%」算的同名口径 —— 后者把「涨过又跌回来」误判成高位。
            if phase_sig.position:
                details["position"] = phase_sig.position
            reasons.extend(r for r in phase_sig.reasons if r)
        else:
            atype_override = None

        direction = self._direction(snap, details)
        atype = (atype_override
                 if atype_override
                 else (self._classify(snap, details, direction) if score >= self._cfg.level_watch else "normal"))

        return AnomalyResult(
            good_id=snap.good_id,
            name=snap.name,
            score=score,
            level=self._level(score),
            direction=direction,
            anomaly_type=atype,
            reasons=reasons,
            parts=parts,
            details=details,
        )
