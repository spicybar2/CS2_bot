# -*- coding: utf-8 -*-
"""选品推荐引擎：基于快照数据，按多策略产出推荐。

策略说明（基于 CSQAQ 返回字段）：
  1. 7天最低价策略  - 当前价接近近7日最低，处于低位区域
  2. 超跌反弹策略    - 7天跌幅显著，存在修复预期
  3. 热点异动策略    - 7天涨幅显著，资金关注度高
  4. 好出手策略      - BUFF 售价与求购价差极小（流动性好、套利空间清晰）
  5. 今日异动策略    - 当日涨跌幅显著
"""

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from bot.config import RecommendConfig
from bot.csqaq.market_counts import normalize_book

logger = logging.getLogger("analyzer")

# 策略标识
STRATEGY_LOW_7D = "low_7d"        # 7天最低价
STRATEGY_DIP_7D = "dip_7d"        # 7天超跌
STRATEGY_HOT_7D = "hot_7d"        # 7天热点
STRATEGY_LIQUID = "liquid"        # 好出手
STRATEGY_DAY_MOVE = "day_move"    # 今日异动


@dataclass
class Recommendation:
    good_id: int
    name: str
    price: float
    price_7d: float
    rate_7d: float
    rate_today: float
    strategy: str
    strategy_label: str
    note: str = ""
    sort_value: float = 0.0   # V0.2.29:策略排序键(数值直传,别再从 note 文案反解)
    # V0.3.12:分平台盘口(在售/求购 各显 BUFF 与悠悠有品)。note 此前从不被渲染,
    # 所以"在售 N 件"这类只取 BUFF 的说明实际没被看到;改用 book 由 format_line 输出。
    book: str = ""

    def format_line(self, rank: int) -> str:
        arrow = "▲" if self.rate_7d >= 0 else "▼"
        out = (
            f"{rank}. {self.name}\n"
            f"   价 ¥{self.price:.2f} | 7日{arrow}{abs(self.rate_7d):.1f}%"
            f" | 今日{'+' if self.rate_today >= 0 else ''}{self.rate_today:.1f}%"
        )
        if self.book:
            out += f"\n   {self.book}"
        return out


@dataclass
class RecommendResult:
    strategy: str
    strategy_label: str
    items: List[Recommendation] = field(default_factory=list)


class Recommender:
    """从快照行（dict）生成推荐。"""

    def __init__(self, config: RecommendConfig):
        self._cfg = config

    # ------------------------------------------------------------------
    # 过滤
    # ------------------------------------------------------------------

    def _base_filter(self, rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        out = []
        for r in rows:
            price = float(r.get("main_price") or 0)
            if price < self._cfg.min_price:
                continue
            # 价格上限过滤：剔掉手套/刀/胶囊/签名等高价低频商品
            if price > self._cfg.max_price:
                continue
            rate_7 = float(r.get("sell_price_rate_7") or 0)
            # 7天涨跌幅为 0 或缺失 → 数据不可用
            if abs(rate_7) < 0.01 and not r.get("sell_price_7"):
                continue
            out.append(r)
        return out

    # ------------------------------------------------------------------
    # 各策略
    # ------------------------------------------------------------------

    def low_7d(self, rows: List[Dict[str, Any]]) -> List[Recommendation]:
        """7天最低价：当前价相对7天前价格跌得最多（跌到低位），且今日未继续大涨。"""
        items = []
        for r in self._base_filter(rows):
            price_7 = float(r.get("sell_price_7") or 0)
            price = float(r.get("main_price") or 0)
            if price_7 <= 0 or price <= 0:
                continue
            rate_7 = float(r.get("sell_price_rate_7") or 0)
            if not (-30 <= rate_7 < 0):  # 需明显低于7天前，且跌幅不过分夸张（排除异常数据）
                continue
            items.append(Recommendation(
                good_id=int(r["good_id"]), name=r["name"], price=price,
                price_7d=price_7, rate_7d=rate_7,
                rate_today=float(r.get("buff_price_chg") or 0),
                strategy=STRATEGY_LOW_7D, strategy_label="7天低位",
                note="当前价已跌至7日低位",
                book=self._book_note(r),
            ))
        items.sort(key=lambda x: x.rate_7d)  # 跌幅最大（最低位）优先
        return items[: self._cfg.top_n]

    def dip_7d(self, rows: List[Dict[str, Any]]) -> List[Recommendation]:
        """7天超跌 = 七天涨跌幅排行榜，但只推"流动性好、好出手"的品种。

        三道闸（确保推出来的是能买卖的超跌品，而不是暴跌的冷门孤品）：
          - 价格护栏：main_price 在 [min_price, max_price]（_base_filter 处理）
          - 跌幅区间：只看下跌品种（rate_7 < 0），排除 +0（涨）/ -100% 哨兵（数据缺失）
          - 流动性门槛：BUFF/Steam 在售+求购合计 >= min_liquidity（默认 50 单），
            低于门槛的冷门品直接跳过（这才是"好出手"的真实过滤）
        通过三道闸后按 7天跌幅降序取 top_n。
        """
        items = []
        for r in self._base_filter(rows):
            rate_7 = float(r.get("sell_price_rate_7") or 0)
            # 只看下跌品种；排除 -100% 哨兵值（7天前价格缺失/数据异常）
            if rate_7 >= 0 or rate_7 <= -99:
                continue
            # 流动性门槛：BUFF/悠悠在售+求购总数(V0.2.29 统一走 _liquidity,剔除 Steam 脏求购字段)
            liquidity = self._liquidity(r)
            if liquidity < self._cfg.min_liquidity:
                continue
            items.append(Recommendation(
                good_id=int(r["good_id"]), name=r["name"],
                price=float(r.get("main_price") or 0),
                price_7d=float(r.get("sell_price_7") or 0),
                rate_7d=rate_7,
                rate_today=float(r.get("buff_price_chg") or 0),
                strategy=STRATEGY_DIP_7D, strategy_label="7天超跌",
                note=f"流动性 {liquidity} 单",
                book=self._book_note(r),
            ))
        items.sort(key=lambda x: x.rate_7d)  # 跌幅最大（最负）在前
        return items[: self._cfg.top_n]

    def hot_7d(self, rows: List[Dict[str, Any]]) -> List[Recommendation]:
        """7天热点：近7日涨幅显著且仍在放量。

        与 SteamDT「在售价涨幅榜」对齐：剔除噪音小件(分母极小的暴涨件/印花/挂件)
        和几乎不成交的冷门品,只保留有真实在售+求购成交量的主流枪皮/手套/刀。
        门槛:7天涨幅 >= 8% 且 < 100%(排除爆款式异常),流动性 >= min_liquidity(同超跌)。
        """
        items = []
        for r in self._base_filter(rows):
            rate_7 = float(r.get("sell_price_rate_7") or 0)
            if rate_7 < 8:
                continue
            # 排除爆款式异常(分母极小的印花/挂件涨幅动辄几千%,无参考价值)
            if rate_7 >= 100:
                continue
            # 流动性门槛:与超跌对齐,过滤几乎不成交的暴涨件(V0.2.29 统一走 _liquidity,剔除 Steam 脏求购)
            liquidity = self._liquidity(r)
            if liquidity < self._cfg.min_liquidity:
                continue
            sell_num = int(r.get("buff_sell_num") or 0)
            items.append(Recommendation(
                good_id=int(r["good_id"]), name=r["name"],
                price=float(r.get("main_price") or 0),
                price_7d=float(r.get("sell_price_7") or 0),
                rate_7d=rate_7,
                rate_today=float(r.get("buff_price_chg") or 0),
                strategy=STRATEGY_HOT_7D, strategy_label="7天热点",
                note=f"流动 {liquidity} 单 / 在售 {sell_num} 件",
                book=self._book_note(r),
            ))
        items.sort(key=lambda x: x.rate_7d, reverse=True)
        return items[: self._cfg.top_n]

    def liquid(self, rows: List[Dict[str, Any]]) -> List[Recommendation]:
        """好出手：BUFF 售价与求购价差额最小（买入后可快速卖出）。

        V0.2.29:排序键不再从展示文案里反解(`note.split()[-1]`),改为直接携带数值。
        """
        items = []
        for r in self._base_filter(rows):
            sell = float(r.get("buff_sell_price") or 0)
            buy = float(r.get("buff_buy_price") or 0)
            if sell <= 0 or buy <= 0:
                continue
            diff_pct = (sell - buy) / sell * 100  # 差额占售价百分比，越小越好出手
            if diff_pct > 3.0:
                continue  # 价差过大，出手困难
            rate_7 = float(r.get("sell_price_rate_7") or 0)
            items.append(Recommendation(
                good_id=int(r["good_id"]), name=r["name"],
                price=sell, price_7d=float(r.get("sell_price_7") or 0),
                rate_7d=rate_7,
                rate_today=float(r.get("buff_price_chg") or 0),
                strategy=STRATEGY_LIQUID, strategy_label="好出手",
                note=f"售价/求购差 {diff_pct:.1f}%",
                book=self._book_note(r),
                sort_value=diff_pct,
            ))
        items.sort(key=lambda x: x.sort_value)
        return items[: self._cfg.top_n]

    def day_move(self, rows: List[Dict[str, Any]]) -> List[Recommendation]:
        """今日异动：当日涨跌幅显著（排除7天趋势影响）。"""
        items = []
        for r in self._base_filter(rows):
            chg = float(r.get("buff_price_chg") or 0)
            if abs(chg) < 3.0:
                continue
            items.append(Recommendation(
                good_id=int(r["good_id"]), name=r["name"],
                price=float(r.get("main_price") or 0),
                price_7d=float(r.get("sell_price_7") or 0),
                rate_7d=float(r.get("sell_price_rate_7") or 0),
                rate_today=chg,
                strategy=STRATEGY_DAY_MOVE, strategy_label="今日异动",
                note="上涨" if chg > 0 else "下跌",
                book=self._book_note(r),
            ))
        items.sort(key=lambda x: x.rate_today, reverse=True)
        return items[: self._cfg.top_n]

    # ------------------------------------------------------------------
    # 统一入口
    # ------------------------------------------------------------------

    STRATEGIES = {
        "low_7d": ("7天低位", low_7d),
        "dip_7d": ("7天超跌", dip_7d),
        "hot_7d": ("7天热点", hot_7d),
        "liquid": ("好出手", liquid),
        "day_move": ("今日异动", day_move),
    }

    # ------------------------------------------------------------------
    # 实时官方榜单（不再依赖本地快照）
    # ------------------------------------------------------------------

    async def recommend_realtime(
        self, api: "CSQAQClient", strategy: str = "low_7d"
    ) -> RecommendResult:
        """实时从 CSQAQ 官方排行榜取数，直接对齐网页榜单（全市场覆盖，不依赖本地快照）。

        支持的策略:
          - low_7d / dip_7d -> 官方 7天跌幅榜
          - hot_7d          -> 官方 7天涨幅榜
          - day_move        -> 官方近1天涨/跌榜合并，按今日涨跌幅排序
          - liquid          -> 广样本按流动性(在售+求购)排序，好出手
        """
        from bot.csqaq.client import (
            RANK_SORT_DROP_7D, RANK_SORT_RISE_7D,
            RANK_SORT_DROP_1D, RANK_SORT_RISE_1D,
            RANK_SORT_SELL_PRICE_DESC,
        )

        label, _ = self.STRATEGIES.get(strategy, self.STRATEGIES["low_7d"])

        try:
            if strategy in ("low_7d", "dip_7d"):
                # V0.2.29:两个策略此前共用一个分支(输出完全相同的品,只有标题不同)。
                # 现在按**本地策略的同款口径**拆分:
                #   low_7d(7天低位) = -30% ≤ rate_7 < 0,不加流动性闸(本地亦无);
                #   dip_7d(7天超跌) = -99% < rate_7 < 0 且流动性 ≥ min_liquidity(本地有闸)。
                raw = await api.get_rank_items_all(
                    page_size=100, max_pages=10, sorts=[RANK_SORT_DROP_7D])
                if strategy == "low_7d":
                    return self._build_rank_recs(raw, strategy, label, sort_key="rate_7d",
                                                 reverse=False, rate_range=(-30.0, 0.0),
                                                 min_liquidity=0)
                return self._build_rank_recs(raw, strategy, label, sort_key="rate_7d",
                                             reverse=False, rate_range=(-99.0, 0.0),
                                             min_liquidity=self._cfg.min_liquidity)
            if strategy == "hot_7d":
                raw = await api.get_rank_items_all(
                    page_size=100, max_pages=10, sorts=[RANK_SORT_RISE_7D])
                # 与本地 hot_7d 同口径:8% ≤ rate_7 < 100%(排除分母极小的暴涨噪声)
                return self._build_rank_recs(raw, strategy, label, sort_key="rate_7d",
                                             reverse=True, rate_range=(8.0, 100.0))
            if strategy == "day_move":
                drop = await api.get_rank_items_all(
                    page_size=100, max_pages=5, sorts=[RANK_SORT_DROP_1D])
                rise = await api.get_rank_items_all(
                    page_size=100, max_pages=5, sorts=[RANK_SORT_RISE_1D])
                return self._build_day_move_recs(drop + rise, label)
            if strategy == "liquid":
                raw = await api.get_rank_items_all(
                    page_size=100, max_pages=3, sorts=[RANK_SORT_SELL_PRICE_DESC])
                return self._build_liquid_recs(raw, label)
        except Exception as exc:
            logger.exception("[recommender] 实时榜单拉取失败 %s: %s", strategy, exc)
        return RecommendResult(strategy=strategy, strategy_label=label, items=[])

    # ---- 实时榜单 -> 推荐项 的过滤/构建 ----

    def _pass_common(self, r: Dict[str, Any]) -> bool:
        """通用护栏:价格区间 / 排除 -100% 异常 / 排除容器类(无磨损且非印花)。"""
        price = float(r.get("main_price") or 0)
        if price < self._cfg.min_price or price > self._cfg.max_price:
            return False
        rate_7 = float(r.get("sell_price_rate_7") or 0)
        if abs(rate_7) >= 100:
            return False
        name = r.get("name", "")
        exterior = r.get("exterior")
        # 对齐网页「价格榜」默认视图:只保留印花 / 有磨损皮肤,
        # 排除胶囊、纪念包、通行证、挂件等容器/工具类
        if "印花" not in name and not exterior:
            return False
        return True

    def _book_note(self, r: Dict[str, Any]) -> str:
        """V0.3.12:分平台盘口一行(在售 BUFF x/悠悠 y　求购 BUFF a/悠悠 b)。

        字段不全(如实时榜单缺悠悠求购)时返回空串 —— 宁可不显示,也不显示半截数据
        或把 BUFF 单平台数冒充两平台合计(旧 note 文案就是这个问题,且从没被渲染过)。
        """
        pb = normalize_book(r)
        if not pb:
            return ""
        return (f"在售 BUFF {pb['buff_sell']:,}/悠悠有品 {pb['yyyp_sell']:,}"
                f"　求购 BUFF {pb['buff_buy']:,}/悠悠有品 {pb['yyyp_buy']:,}")

    def _liquidity(self, r: Dict[str, Any]) -> int:
        """可交易流动性(单数):BUFF/悠悠在售+求购 + Steam 在售。

        V0.2.29:`steam_buy_num` 剔除 —— 项目自己已在 market_counts/cycle 判定
        "Steam 无求购机制,该字段是脏数据"(实测狮鹫 5130、手套武器箱约 47 万),
        计入合计会把 min_liquidity 门槛虚高放宽,让冷门品混过。
        """
        return (
            int(r.get("buff_sell_num") or 0)
            + int(r.get("buff_buy_num") or 0)
            + int(r.get("steam_sell_num") or 0)
            + int(r.get("yyyp_sell_num") or 0)
            + int(r.get("yyyp_buy_num") or 0)
        )

    def _build_rank_recs(self, raw_items, strategy, label, sort_key, reverse,
                         rate_range: Optional[Tuple[float, float]] = None,
                         min_liquidity: Optional[int] = None):
        """实时榜单 → 推荐项。

        :param rate_range: 7 日涨跌幅取值区间 (下限, 上限),None 表示不过滤。
            V0.2.29:实时路径此前不给 low_7d/dip_7d 任何涨跌幅过滤(两者输出完全相同),
            现在按区间拆分,与本地策略定义对齐。
        :param min_liquidity: 流动性门槛;None 用配置默认值,0 表示不加闸(与本地 low_7d 一致)。
        """
        need_liq = self._cfg.min_liquidity if min_liquidity is None else int(min_liquidity)
        recs = []
        for it in raw_items:
            r = it.to_dict()
            if not self._pass_common(r):
                continue
            rate_7 = float(r.get("sell_price_rate_7") or 0)
            if rate_range is not None and not (rate_range[0] <= rate_7 < rate_range[1]):
                continue
            liquidity = self._liquidity(r)
            if need_liq > 0 and liquidity < need_liq:
                continue
            recs.append(Recommendation(
                good_id=int(r["good_id"]), name=r["name"],
                price=float(r.get("main_price") or 0),
                price_7d=float(r.get("sell_price_7") or 0), rate_7d=rate_7,
                rate_today=float(r.get("buff_price_chg") or 0),
                strategy=strategy, strategy_label=label,
                note=f"流动 {liquidity} 单",
                book=self._book_note(r),
            ))
        recs.sort(key=lambda x: getattr(x, sort_key), reverse=reverse)
        return RecommendResult(strategy=strategy, strategy_label=label, items=recs[: self._cfg.top_n])

    def _build_day_move_recs(self, raw_items, label):
        recs = []
        for it in raw_items:
            r = it.to_dict()
            if not self._pass_common(r):
                continue
            if self._liquidity(r) < self._cfg.min_liquidity:
                continue
            today = float(r.get("buff_price_chg") or 0)
            recs.append(Recommendation(
                good_id=int(r["good_id"]), name=r["name"],
                price=float(r.get("main_price") or 0),
                price_7d=float(r.get("sell_price_7") or 0), rate_7d=today,
                rate_today=today, strategy="day_move", strategy_label=label,
                note=f"今日 {today:+.1f}% / 流动 {self._liquidity(r)} 单",
                book=self._book_note(r),
            ))
        recs.sort(key=lambda x: abs(x.rate_today), reverse=True)
        return RecommendResult(strategy="day_move", strategy_label=label, items=recs[: self._cfg.top_n])

    def _build_liquid_recs(self, raw_items, label):
        tmp = []
        for it in raw_items:
            r = it.to_dict()
            if not self._pass_common(r):
                continue
            liq = self._liquidity(r)
            if liq < self._cfg.min_liquidity:
                continue
            rec = Recommendation(
                good_id=int(r["good_id"]), name=r["name"],
                price=float(r.get("main_price") or 0),
                price_7d=float(r.get("sell_price_7") or 0),
                rate_7d=float(r.get("sell_price_rate_7") or 0),
                rate_today=float(r.get("buff_price_chg") or 0),
                strategy="liquid", strategy_label=label,
                note=f"流动 {liq} 单",
                book=self._book_note(r),
            )
            tmp.append((liq, rec))
        tmp.sort(key=lambda x: x[0], reverse=True)
        return RecommendResult(strategy="liquid", strategy_label=label,
                               items=[t[1] for t in tmp[: self._cfg.top_n]])

    def recommend(self, rows: List[Dict[str, Any]], strategy: str = "low_7d") -> RecommendResult:
        label, fn = self.STRATEGIES.get(strategy, self.STRATEGIES["low_7d"])
        items = fn(self, rows)
        return RecommendResult(strategy=strategy, strategy_label=label, items=items)

    def all_strategies(self, rows: List[Dict[str, Any]]) -> List[RecommendResult]:
        return [self.recommend(rows, k) for k in self.STRATEGIES]
