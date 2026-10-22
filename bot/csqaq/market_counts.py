# -*- coding: utf-8 -*-
"""多平台盘口数量归一化。

CSQAQ 不同接口的字段命名不同（camelCase / snake_case），统一在这里
处理，避免各业务路径各自漏加平台或误把 BUFF 数量当成全平台数量。
"""

from __future__ import annotations

from typing import Any, Dict, Optional, Tuple


_PLATFORMS = ("buff", "steam", "yyyp", "c5", "igxe", "eco")
_LIQUIDITY_PLATFORMS = ("buff", "yyyp")


def _value(data: Dict[str, Any], platform: str, side: str) -> Optional[int]:
    # V0.2.11: Steam 市场没有求购机制,`steam_buy_num` 是脏数据
    # (实测 2026-09-07: 狮鹫 5130 / MAG-7 17551 / 手套武器箱约 47 万"求购",
    # 全为不可能的值),求购合计必须剔除,否则污染全平台口径。
    if platform == "steam" and side == "buy":
        return None
    for key in (
        f"{platform}_{side}_num",
        f"{platform}{side.title()}Num",
        f"{platform}{'Buy' if side == 'buy' else 'Sell'}Num",
    ):
        if key in data and data.get(key) is not None:
            try:
                return max(0, int(float(data.get(key) or 0)))
            except (TypeError, ValueError):
                return 0
    return None


def aggregate_market_counts(data: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """返回六个平台合计及各平台明细。

    ``*_available`` 表示是否至少读到一个平台字段；未提供的平台按 0 计，
    与现有全平台展示逻辑一致。调用方可据此决定是否把历史点用于比较。
    """
    d = data or {}
    out: Dict[str, Any] = {"buy_num": 0, "sell_num": 0,
                           "buy_available": False, "sell_available": False,
                           "buy_complete": True, "sell_complete": True}
    for platform in _PLATFORMS:
        buy = _value(d, platform, "buy")
        sell = _value(d, platform, "sell")
        # steam 的 buy 字段为脏数据(V0.2.11 剔除,Steam 无求购机制):置 None,
        # 且完整性判定同步排除——否则 buy_complete 永远 False,周期检测永久跳过
        buy_structural_missing = (platform == "steam")
        out[f"{platform}_buy_num"] = None if buy_structural_missing else buy
        out[f"{platform}_sell_num"] = sell
        if buy is not None:
            out["buy_num"] += buy
            out["buy_available"] = True
        elif not buy_structural_missing:
            out["buy_complete"] = False
        if sell is not None:
            out["sell_num"] += sell
            out["sell_available"] = True
        else:
            out["sell_complete"] = False
    return out


def aggregate_buff_yyyp_sell(data: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """返回 BUFF+悠悠有品在售合计及完整性。

    两个平台字段必须同时存在才算完整；缺失不能按 0 处理。该口径专供
    异动雷达的流动性过滤、评分和推送复核使用。
    """
    d = data or {}
    values = {platform: _value(d, platform, "sell")
              for platform in _LIQUIDITY_PLATFORMS}
    complete = all(value is not None for value in values.values())
    return {
        "buff_sell_num": values["buff"],
        "yyyp_sell_num": values["yyyp"],
        "sell_num": (sum(int(value or 0) for value in values.values())
                     if complete else None),
        "sell_complete": complete,
    }


# ---------------------------------------------------------------------------
# V0.3.4: 价格的双平台口径(BUFF+悠悠)—— 数量早就统一了,价格此前只有 BUFF
# ---------------------------------------------------------------------------
# 实测(2026-09-28,榜单 597 品,`probe_buff_vs_yyyp_price.py`):
#   * 97% 的品两平台售价都有(578/597),只有 BUFF 13、只有悠悠 5;
#   * 两侧报价**基本五五开**(悠悠更低 302 / BUFF 更低 244 / 相同 32),所以"取 min 当现价"
#     是实质改善而不是偏向某一家;
#   * 但偏差分布极宽,且**与价位强相关**:¥<1 档中位 -20%(分币级离散天然如此)、±2% 内仅 11%;
#     ≥¥5 才收敛(¥5-20 中位 +1.6%、¥100-1k 中位 +0.6%)。
# 因此合成必须:① 设**价位分档闸**(只挡真正离谱的报价,不是把正常差异当异常);
#              ② 设 **¥5 下限**(低价品的"市场价"本就不唯一,序列基准继续用 BUFF,
#                 避免分币级 min() 切换制造假异动)。
# 门槛覆盖率实测: 分档闸+¥5 下限 → 可合成 158/258(61%),通过样本偏差 p10 -4.6% / p90 +5.0%,
# 中位 -0.09%;而只用分档闸时 p10 -40%、|差|>5% 占 51%(太脏)。
# **超闸不是噪声而是情报**:两平台价差本身就是倒货机会,故两个平台原值都要落库,供展示与后续信号使用。
PRICE_SYNTH_MIN = 5.0
PRICE_GATE_BANDS = (
    # (下界含, 上界不含, 允许的最大 |悠悠/BUFF-1| %)
    (5.0, 20.0, 25.0),
    (20.0, 100.0, 15.0),
    (100.0, 1000.0, 10.0),
    (1000.0, float("inf"), 8.0),
)


def _price(data: Dict[str, Any], platform: str, side: str) -> Optional[float]:
    """取某平台某侧的价格;缺失或非正数返回 None(0 不能当"免费"用)。"""
    for key in (f"{platform}_{side}_price",
                f"{platform}{'Buy' if side == 'buy' else 'Sell'}Price"):
        if key in data and data.get(key) is not None:
            try:
                value = float(data.get(key) or 0)
            except (TypeError, ValueError):
                return None
            return value if value > 0 else None
    return None


def price_gate_limit(price: Optional[float]) -> Optional[float]:
    """按价位返回允许的两平台偏差上限(%);低于合成下限返回 None(不合成)。"""
    if price is None or price < PRICE_SYNTH_MIN:
        return None
    for low, high, limit in PRICE_GATE_BANDS:
        if low <= price < high:
            return limit
    return PRICE_GATE_BANDS[-1][2]


def _combine(buff: Optional[float], yyyp: Optional[float], *, side: str) -> Dict[str, Any]:
    """合成单侧价格。售价取 min(哪里买更便宜)、求购取 max(哪里卖更值),都带分档闸。

    scope 语义(存进 `total_*_price_scope`,消费者据此决定能不能把该点当合成口径):
      ``buff_yyyp``     两平台都在且偏差在闸内 → 已合成,可信
      ``below_floor``   价位低于合成下限(¥5)→ 不合成(低价品两平台价差天然巨大)
      ``buff_conflict`` 两平台都在但偏差超闸 → 退回 BUFF,偏差本身是套利线索
      ``buff_only``     只有 BUFF 价(悠悠缺字段)
      ``yyyp_only``     只有悠悠价(罕见,CSQAQ 对个别品不返回 BUFF)
      ``None``          两侧都没有 → 不可判,不能当 0
    """
    dev = None
    if buff and yyyp:
        dev = (yyyp / buff - 1.0) * 100.0
        limit = price_gate_limit(buff)
        if limit is None:
            return {"price": None, "scope": "below_floor", "dev_pct": dev}
        if abs(dev) <= limit:
            return {"price": min(buff, yyyp) if side == "sell" else max(buff, yyyp),
                    "scope": "buff_yyyp", "dev_pct": dev}
        return {"price": None, "scope": "buff_conflict", "dev_pct": dev}
    if buff:
        return {"price": None, "scope": "buff_only", "dev_pct": None}
    if yyyp:
        return {"price": None, "scope": "yyyp_only", "dev_pct": None}
    return {"price": None, "scope": None, "dev_pct": None}


def aggregate_buff_yyyp_prices(data: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """返回 BUFF+悠悠有品的两侧价格、合成价与口径标记。

    与 :func:`aggregate_buff_yyyp_sell` 同规矩:**缺平台不能按 0 处理**。
    ``sell_price``/``buy_price`` 为 None 表示"不可判或不该合成",消费者必须回退到
    单平台口径(`buff_sell_price`/`buff_buy_price`),不得把 None 当 0。
    """
    d = data or {}
    buff_sell = _price(d, "buff", "sell")
    yyyp_sell = _price(d, "yyyp", "sell")
    buff_buy = _price(d, "buff", "buy")
    yyyp_buy = _price(d, "yyyp", "buy")
    sell = _combine(buff_sell, yyyp_sell, side="sell")
    buy = _combine(buff_buy, yyyp_buy, side="buy")
    return {
        "buff_sell_price": buff_sell,
        "yyyp_sell_price": yyyp_sell,
        "sell_price": sell["price"],
        "sell_scope": sell["scope"],
        "sell_dev_pct": sell["dev_pct"],
        "buff_buy_price": buff_buy,
        "yyyp_buy_price": yyyp_buy,
        "buy_price": buy["price"],
        "buy_scope": buy["scope"],
        "buy_dev_pct": buy["dev_pct"],
    }


# 快照行的双平台价格列:各写入路径统一用 `**dual_price_columns(raw)` 展开。
DUAL_PRICE_COLUMNS = ("yyyp_sell_price", "yyyp_buy_price", "total_sell_price",
                      "total_buy_price", "total_sell_price_scope", "total_buy_price_scope")


def dual_price_columns(data: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """把原始数据里的双平台价格整理成快照表列(V0.3.4)。

    缺平台时 ``yyyp_*`` 写 0(与既有 `buff_buy_price` 缺省风格一致),
    合成价写 None(**不能写 0** —— 0 会被下游当成免费)。
    """
    p = aggregate_buff_yyyp_prices(data)
    return {
        "yyyp_sell_price": float(p["yyyp_sell_price"] or 0),
        "yyyp_buy_price": float(p["yyyp_buy_price"] or 0),
        "total_sell_price": p["sell_price"],
        "total_buy_price": p["buy_price"],
        "total_sell_price_scope": p["sell_scope"],
        "total_buy_price_scope": p["buy_scope"],
    }


# ---- V0.3.23: 盯盘主用价(悠悠有品优先) ----
# 老大要求「/关注 同时列出 BUFF 与悠悠,基准以悠悠为主」。注意:老关注记录的基准是 BUFF 价,
# 直接切换会让第一轮误报,所以 watchlist 增加 base_price_scope 列做一次性重基准(见 main.py)。
WATCH_PRIMARY_PLATFORM = "yyyp"
PLATFORM_LABELS = {"yyyp": "悠悠有品", "buff": "BUFF", "steam": "Steam"}


def platform_sell_price(data: Optional[Dict[str, Any]], platform: str) -> Optional[float]:
    """取某平台的卖一价;缺失或非正数返回 None。"""
    if not data:
        return None
    return _price(data, platform, "sell")


def watch_price_pair(data: Optional[Dict[str, Any]]) -> Tuple[float, float]:
    """返回 (BUFF 卖价, 悠悠卖价),缺失的记 0。用于展示"两个价都列出来"。"""
    buff = platform_sell_price(data, "buff") or 0.0
    yyyp = platform_sell_price(data, "yyyp") or 0.0
    return float(buff), float(yyyp)


def watch_primary_price(data: Optional[Dict[str, Any]],
                        prefer: str = WATCH_PRIMARY_PLATFORM) -> Tuple[float, str]:
    """盯盘主用价:悠悠有品 → BUFF → Steam,取第一个有效价。

    返回 ``(价格, 平台标签)``;全缺返回 ``(0.0, "")``。基准与现价**必须用同一个函数**,
    否则会出现"基准按悠悠、现价按 BUFF"的错位比较。
    """
    order = [prefer] + [p for p in ("buff", "yyyp", "steam") if p != prefer]
    for plat in order:
        value = platform_sell_price(data, plat)
        if value:
            return float(value), plat
    return 0.0, ""


def cross_spread_pct(data: Optional[Dict[str, Any]]) -> Optional[float]:
    """两平台价差(悠悠售价÷BUFF售价−1,%)。正=悠悠更贵(去悠悠卖/BUFF 买更划算)。

    这不是噪声而是**倒货机会**:实测有超闸样本(如 +1464%、-95%),故单独提供,
    不参与合成价,只在展示与后续信号里用。
    """
    p = aggregate_buff_yyyp_prices(data)
    if p["buff_sell_price"] and p["yyyp_sell_price"]:
        return (p["yyyp_sell_price"] / p["buff_sell_price"] - 1.0) * 100.0
    return None


def rank_dual_price_columns(item: Any) -> Dict[str, Any]:
    """榜单对象(RankItem)版本:优先用 `.raw` 原始行,缺字段时用解析后的属性兜底。

    榜单路径的 RankItem 只有部分字段被解析(如悠悠求购价是 V0.3.4 才加的),
    用 raw 能拿到接口返回的全部价格,避免"接口给了但代码没接"再次发生。
    """
    raw = dict(getattr(item, "raw", None) or {})
    for attr in ("buff_sell_price", "buff_buy_price", "yyyp_sell_price", "yyyp_buy_price"):
        if not raw.get(attr):
            value = getattr(item, attr, None)
            if value:
                raw[attr] = value
    return dual_price_columns(raw)


# ---------------------------------------------------------------------------
# V0.3.12: 盘口数量的**分平台展示**(BUFF / 悠悠有品 各显示变化量与当前值)
# ---------------------------------------------------------------------------
# 需求:推送里原来只有「求购(BUFF+悠悠) 52→59(+7/+13.5%)」这种合计口径,看不出是哪一家
# 在动。合计口径对判定是对的(两平台合并才反映真实盘口),但对**读的人**不够 —— 需要
# 「BUFF +5 个(52→57)  悠悠有品 +2 个(2→4)」这样的拆解。故只改展示层,判定逻辑一行不动。
#
# 数据可得性(2026-10-10 实测):
#   · 快照/详情回包: buff_sell_num / buff_buy_num / yyyp_buy_num 直接有;
#   · item_listing_history: 有 buff_sell_num / buff_buy_num / yyyp_buy_num / total_sell_num
#     + total_sell_scope,但**没有 yyyp_sell_num 列** → 悠悠在售只能反推。
PLATFORM_ORDER = (("buff", "BUFF"), ("yyyp", "悠悠有品"))


def _as_int(value: Any) -> Optional[int]:
    if value is None:
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def normalize_book(src: Any) -> Optional[Dict[str, int]]:
    """把任意来源(DB 行 / MetricSnapshot / HistoryPoint / API 回包)归一化成四平台盘口。

    返回 ``{"buff_sell","yyyp_sell","buff_buy","yyyp_buy"}``;任一平台缺字段返回 None
    (调用方回退到合计文案 —— 缺失绝不用 0 冒充)。

    悠悠在售的反推保护(必须保留否则会显示错数):
      · 优先直接读 ``yyyp_sell_num``(快照 / 详情回包有该字段);
      · 历史行没有该列,只有 ``total_sell_num`` 合计 → **必须**
        ``total_sell_scope == "buff_yyyp"`` 才允许 ``合计 − BUFF`` 反推;
        旧数据里 total 可能是单平台值或缺失,直接相减会得到错值/负数。
    """
    if src is None:
        return None
    get = src.get if isinstance(src, dict) else (lambda k: getattr(src, k, None))
    buff_sell = _as_int(get("buff_sell_num"))
    yyyp_sell = _as_int(get("yyyp_sell_num"))
    if yyyp_sell is None:
        total = _as_int(get("total_sell_num"))
        if (total is not None and buff_sell is not None
                and get("total_sell_scope") == "buff_yyyp"):
            diff = total - buff_sell
            yyyp_sell = diff if diff >= 0 else None
    buff_buy = _as_int(get("buff_buy_num"))
    yyyp_buy = _as_int(get("yyyp_buy_num"))
    if None in (buff_sell, yyyp_sell, buff_buy, yyyp_buy):
        return None
    return {"buff_sell": buff_sell, "yyyp_sell": yyyp_sell,
            "buff_buy": buff_buy, "yyyp_buy": yyyp_buy}


def book_split_pairs(base: Optional[Dict[str, int]], cur: Optional[Dict[str, int]],
                     side: str) -> Optional[list]:
    """返回 ``[(label, base, cur), ...]``(两平台各一段);缺数据返回 None。

    :param side: ``"sell"``(在售) 或 ``"buy"``(求购)
    """
    if not base or not cur:
        return None
    out = []
    for plat, label in PLATFORM_ORDER:
        key = f"{plat}_{side}"
        b, c = base.get(key), cur.get(key)
        if b is None or c is None:
            return None
        out.append((label, b, c))
    return out


def book_split_line(base: Optional[Dict[str, int]], cur: Optional[Dict[str, int]],
                    side: str, prefix: str = "") -> Optional[str]:
    """生成分平台一行:「在售：BUFF -3 个(567→564)  悠悠有品 +4 个(512→516)」。

    变化量在前、当前值在后(括号里 base→cur),这是用户明确要的格式。
    """
    pairs = book_split_pairs(base, cur, side)
    if not pairs:
        return None
    name = "在售" if side == "sell" else "求购"
    segs = [f"{label} {c - b:+d} 个({b:,}→{c:,})" for label, b, c in pairs]
    return f"{prefix}{name}：{'  '.join(segs)}"


def book_split_inline(base: Optional[Dict[str, int]], cur: Optional[Dict[str, int]],
                      side: str) -> Optional[str]:
    """紧凑版(嵌在 reason 句子里用):「BUFF +5/悠悠有品 +2」。"""
    pairs = book_split_pairs(base, cur, side)
    if not pairs:
        return None
    return "/".join(f"{label} {c - b:+d}" for label, b, c in pairs)
