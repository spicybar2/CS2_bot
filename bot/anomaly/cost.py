# -*- coding: utf-8 -*-
"""成本门槛 · 观察模式(V0.3.27)

**要解决的问题**:机器人推送「异动」,但一笔倒货的真实成本是
「平台卖出费 2.5%(BUFF)/2.0%(悠悠)+ 提现 1%(银行卡最低 ¥2)+ 买卖价差」,
人民币品往返成本 3.5%~12.5%。而当前引擎对 ¥58 品 0.26% 的价格波动也计分 ——
扣费后是负收益。

**本地实测依据**(详见 docs/交易规则与池策略评估.md):
  · 池内买卖价差中位 4.2%;¥1000 以上 3.9%;¥50 以下 49%(无接盘)
  · 两平台对流动性商品只差 0.7%,跨平台套利已被抹平(费后中位 -2.7%)
  · 异动随机买入扣费持有:24h 中位 -4.00%/回本 13.4%,7 天 -6.54%/回本 6.2%
    (随机对照 -3.50%/18.2%)
  · 「相对 7 日中位价的折扣」与后续收益**单调相关**(低/中/高三档
    -6.65%/-5.28%/-3.48%,回本率 1.5%/4.5%/7.7%),但即使最高档仍亏 ——
    即折扣只能当**排序信号**,不能当**过线门槛**

**因此本模块职责只有两条**:
  1) 观察:对每条即将落库的异动算出「折后边际 = 折扣 - 往返成本」,写日志 + 落库;
  2) 影子追踪:被拦/未推的候选同样记录,供日后与 signal_outcomes 按 (good_id, ts)
     join,用真实数据回答「边际高的候选是不是真的更赚钱」。

`mode=observe` 时**不改变任何推送行为**;`mode=enforce` 为将来的拦截预留,
本版本只产出判定,不产生拦截动作。
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Sequence

PLATFORM_BUFF = "buff"
PLATFORM_YYYP = "yyyp"
PLATFORM_CN = {PLATFORM_BUFF: "BUFF", PLATFORM_YYYP: "悠悠"}

MODE_OBSERVE = "observe"
MODE_ENFORCE = "enforce"

# 判定结果
VERDICT_PASS = "pass"        # 折后边际达标
VERDICT_BLOCK = "block"      # 边际不足(将来 enforce 时会拦)
VERDICT_SKIP = "skip"        # 数据不足/离群,无法判定(不参与排序)


@dataclass
class CostGateConfig:
    """成本门槛配置。费用口径默认复用 config.yaml 的 `fees:` 段。"""

    enable: bool = True
    mode: str = MODE_OBSERVE
    buff_sell_fee_pct: float = 2.5      # BUFF 卖出费%
    yyyp_sell_fee_pct: float = 2.0      # 悠悠卖出费%
    withdraw_pct: float = 1.0           # 提现费率%
    withdraw_min_cny: float = 2.0       # 提现最低手续费(元)—— 小额品的成本主要来自这里
    min_edge_pct: float = 2.0           # 扣成本后至少要留的边际%
    baseline_days: int = 7              # 基准窗口:近 N 日日收盘的中位价
    baseline_min_days: int = 3          # 至少有 N 个日收盘才认基准(宁缺毋假)
    max_platform_gap_pct: float = 15.0  # 两平台价差超此值 = 单边无接盘,判离群
    max_book_spread_pct: float = 10.0   # 单平台要价-求购价差上限(超了买得进卖不出)
    min_sell_num: int = 400             # BUFF 在售下限(与 min_scan_sell_num 对齐)
    freshness_minutes: int = 90         # 报价新鲜度:超时的报价不算数

    @classmethod
    def from_mapping(cls, m: Mapping[str, Any],
                     buff_fee: Optional[float] = None,
                     yyyp_fee: Optional[float] = None) -> "CostGateConfig":
        """从 config.yaml 的 anomaly.cost_gate 构造;费用缺省继承全局 fees。"""
        cfg = cls()
        if not m:
            cfg.buff_sell_fee_pct = 2.5 if buff_fee is None else float(buff_fee)
            cfg.yyyp_sell_fee_pct = 2.0 if yyyp_fee is None else float(yyyp_fee)
            return cfg
        cfg.enable = bool(m.get("enable", cfg.enable))
        mode = str(m.get("mode", cfg.mode) or cfg.mode).strip().lower()
        cfg.mode = mode if mode in (MODE_OBSERVE, MODE_ENFORCE) else MODE_OBSERVE
        for key, cast in (("withdraw_pct", float), ("withdraw_min_cny", float),
                          ("min_edge_pct", float), ("max_platform_gap_pct", float),
                          ("max_book_spread_pct", float)):
            if key in m and m[key] is not None:
                setattr(cfg, key, cast(m[key]))
        for key in ("baseline_days", "baseline_min_days", "min_sell_num",
                    "freshness_minutes"):
            if key in m and m[key] is not None:
                setattr(cfg, key, int(m[key]))
        # 费用:显式配置优先,否则继承 fees 段
        if "buff_sell_fee_pct" in m and m["buff_sell_fee_pct"] is not None:
            cfg.buff_sell_fee_pct = float(m["buff_sell_fee_pct"])
        elif buff_fee is not None:
            cfg.buff_sell_fee_pct = float(buff_fee)
        if "yyyp_sell_fee_pct" in m and m["yyyp_sell_fee_pct"] is not None:
            cfg.yyyp_sell_fee_pct = float(m["yyyp_sell_fee_pct"])
        elif yyyp_fee is not None:
            cfg.yyyp_sell_fee_pct = float(yyyp_fee)
        return cfg


@dataclass
class PlatformQuote:
    """单个平台的一手报价 + 日收盘序列(基准价由 closes 现场取中位)。"""

    platform: str
    ask: float = 0.0            # 最新最低在售价
    bid: float = 0.0            # 最新最高求购价(0 = 无求购)
    sell_num: int = 0           # 在售数量(仅 BUFF 有该字段)
    ask_age_min: float = 1e9    # 报价距今分钟数
    closes: List[float] = field(default_factory=list)   # 近 N 日日收盘

    @property
    def baseline(self) -> float:
        return statistics.median(self.closes) if self.closes else 0.0

    @property
    def baseline_days(self) -> int:
        return len(self.closes)

    @property
    def spread_pct(self) -> float:
        """要价-求购价差%(无求购返回 0,由调用方另行判 no_bid)。"""
        if self.ask <= 0 or self.bid <= 0 or self.bid >= self.ask:
            return 0.0
        return (self.ask - self.bid) / self.ask * 100.0


@dataclass
class CostObservation:
    """一条候选的成本判定结果(同时也是落库行)。"""

    good_id: int
    ts: str
    name: str = ""
    pool: str = ""
    level: str = ""
    score: float = 0.0
    verdict: str = VERDICT_SKIP
    reason: str = ""
    buy_platform: str = ""
    buy_ask: float = 0.0
    buff_ask: float = 0.0
    yyyp_ask: float = 0.0
    gap_pct: float = 0.0
    baseline: float = 0.0
    baseline_days: int = 0
    sell_fee_pct: float = 0.0
    cost_pct: float = 0.0
    discount_pct: float = 0.0
    edge_pct: float = 0.0
    spread_pct: float = 0.0
    rank: int = 0
    push_status: str = ""

    def log_line(self) -> str:
        """单行日志:一眼看清「为什么放行/为什么拦」。"""
        base = (f"[COST] {self.verdict} {self.name} "
                f"买入={PLATFORM_CN.get(self.buy_platform, self.buy_platform or '-')}"
                f"¥{self.buy_ask:.2f}")
        if self.verdict == VERDICT_SKIP:
            return f"{base} 跳过:{self.reason}"
        return (f"{base} 基准¥{self.baseline:.2f}({self.baseline_days}日) "
                f"折扣{self.discount_pct:+.2f}% 成本{self.cost_pct:.2f}% "
                f"边际{self.edge_pct:+.2f}% 价差{self.spread_pct:.1f}% "
                f"平台差{self.gap_pct:.2f}% → {self.reason}")

    def as_row(self) -> Dict[str, Any]:
        return {
            "good_id": int(self.good_id or 0), "ts": self.ts, "name": self.name,
            "pool": self.pool, "level": self.level, "score": float(self.score or 0),
            "verdict": self.verdict, "reason": self.reason,
            "buy_platform": self.buy_platform, "buy_ask": self.buy_ask,
            "buff_ask": self.buff_ask, "yyyp_ask": self.yyyp_ask,
            "gap_pct": self.gap_pct, "baseline": self.baseline,
            "baseline_days": int(self.baseline_days or 0),
            "sell_fee_pct": self.sell_fee_pct, "cost_pct": self.cost_pct,
            "discount_pct": self.discount_pct, "edge_pct": self.edge_pct,
            "spread_pct": self.spread_pct, "rank": int(self.rank or 0),
            "push_status": self.push_status,
        }


def withdrawal_fee(price: float, cfg: CostGateConfig) -> float:
    """提现手续费(元):按比例,但不低于最低额。¥20 品这一项就占 10%。"""
    if price <= 0:
        return 0.0
    return max(float(cfg.withdraw_min_cny or 0), price * float(cfg.withdraw_pct) / 100.0)


def round_trip_cost_pct(price: float, sell_fee_pct: float, cfg: CostGateConfig) -> float:
    """往返成本%(买入无手续费,只算卖出费 + 提现)。"""
    if price <= 0:
        return 99.0
    return float(sell_fee_pct) + withdrawal_fee(price, cfg) / price * 100.0


def _quote_usable(q: Optional[PlatformQuote], cfg: CostGateConfig) -> str:
    """返回空串 = 可用,否则返回不可用原因。"""
    if q is None or q.ask <= 0:
        return "no_quote"
    if q.ask_age_min > float(cfg.freshness_minutes):
        return "stale_quote"
    if q.platform == PLATFORM_BUFF and int(cfg.min_sell_num or 0) > 0:
        if q.sell_num and q.sell_num < int(cfg.min_sell_num):
            return "thin_book"
    if q.bid > 0 and q.spread_pct > float(cfg.max_book_spread_pct):
        return "spread_wide"
    if q.baseline_days < int(cfg.baseline_min_days or 1):
        return "baseline_thin"
    return ""


def build_observation(good_id: int, ts: str, quotes: Mapping[str, PlatformQuote],
                      cfg: CostGateConfig, *,
                      name: str = "", pool: str = "", level: str = "",
                      score: float = 0.0, push_status: str = "") -> CostObservation:
    """对一条候选算出「折后边际」。任何数据不足都返回 skip 并写明原因。"""
    obs = CostObservation(good_id=int(good_id or 0), ts=ts, name=name, pool=pool,
                          level=level, score=float(score or 0),
                          push_status=push_status)
    buff = quotes.get(PLATFORM_BUFF)
    yyyp = quotes.get(PLATFORM_YYYP)
    obs.buff_ask = float(buff.ask) if buff else 0.0
    obs.yyyp_ask = float(yyyp.ask) if yyyp else 0.0

    # 1) 两平台价差:只做记录与离群判定(流动性商品实测中位仅 0.7%)
    if obs.buff_ask > 0 and obs.yyyp_ask > 0:
        lo = min(obs.buff_ask, obs.yyyp_ask)
        obs.gap_pct = abs(obs.buff_ask - obs.yyyp_ask) / lo * 100.0
        if obs.gap_pct > float(cfg.max_platform_gap_pct):
            obs.verdict = VERDICT_SKIP
            obs.reason = f"platform_gap_outlier({obs.gap_pct:.1f}%)"
            return obs

    # 2) 各平台可用性检查
    reasons: Dict[str, str] = {p: _quote_usable(q, cfg) for p, q in
                               ((PLATFORM_BUFF, buff), (PLATFORM_YYYP, yyyp))}
    usable = [p for p, reason in reasons.items() if not reason]
    if not usable:
        obs.verdict = VERDICT_SKIP
        detail = ",".join(f"{p}:{r}" for p, r in reasons.items() if r)
        obs.reason = f"no_usable_quote({detail or 'none'})"
        return obs

    # 3) 买入平台 = 有效平台里更便宜的那个(V0.3.27 口径修正:原先只看 BUFF 会高估成本)
    buy_platform = min(usable, key=lambda p: quotes[p].ask)
    q = quotes[buy_platform]
    obs.buy_platform = buy_platform
    obs.buy_ask = float(q.ask)
    obs.baseline = float(q.baseline)
    obs.baseline_days = int(q.baseline_days)
    obs.spread_pct = float(q.spread_pct)

    # 4) 往返成本与边际
    sell_fee = float(cfg.buff_sell_fee_pct if buy_platform == PLATFORM_BUFF
                     else cfg.yyyp_sell_fee_pct)
    obs.sell_fee_pct = sell_fee
    obs.cost_pct = round_trip_cost_pct(obs.buy_ask, sell_fee, cfg)
    if obs.baseline > 0:
        obs.discount_pct = (obs.baseline - obs.buy_ask) / obs.baseline * 100.0
    obs.edge_pct = obs.discount_pct - obs.cost_pct

    # 5) 判定:达标 / 未达标(未达标原因按优先级写清)
    if obs.edge_pct >= float(cfg.min_edge_pct):
        obs.verdict = VERDICT_PASS
        obs.reason = f"edge_ok(+{obs.edge_pct:.2f}%)"
    else:
        obs.verdict = VERDICT_BLOCK
        if obs.discount_pct <= 0:
            obs.reason = f"no_discount({obs.discount_pct:+.2f}%)"
        else:
            obs.reason = f"edge_below_min({obs.edge_pct:+.2f}%<{cfg.min_edge_pct:.1f}%)"
    return obs


def rank_observations(obs_list: Sequence[CostObservation]) -> List[CostObservation]:
    """按折后边际降序排名(只排可判定的,skip 不参与),返回原列表。"""
    ranked = [o for o in obs_list if o.verdict in (VERDICT_PASS, VERDICT_BLOCK)]
    ranked.sort(key=lambda o: o.edge_pct, reverse=True)
    for i, o in enumerate(ranked, start=1):
        o.rank = i
    return list(obs_list)


def summarize(obs_list: Sequence[CostObservation]) -> str:
    """一条汇总日志:本轮候选的放行/拦截/跳过数与边际中位。"""
    if not obs_list:
        return ""
    passed = sum(1 for o in obs_list if o.verdict == VERDICT_PASS)
    blocked = sum(1 for o in obs_list if o.verdict == VERDICT_BLOCK)
    skipped = sum(1 for o in obs_list if o.verdict == VERDICT_SKIP)
    edges = [o.edge_pct for o in obs_list if o.verdict in (VERDICT_PASS, VERDICT_BLOCK)]
    med = f"{statistics.median(edges):+.2f}%" if edges else "-"
    return (f"候选{len(obs_list)} 达标{passed} 未达标{blocked} 跳过{skipped} "
            f"边际中位{med}")
