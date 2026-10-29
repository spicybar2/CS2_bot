# -*- coding: utf-8 -*-
"""异动告警文案增强(PRD §34 扩展):量价解算 + 市场环境 + 展望。

数据源边界(继承 engine.py 铁律):
- CSQAQ 单品详情可提供 Steam turnover_number；仅在所属周期新鲜时用于量价解算，
  过期/缺失时才使用求购/在售盘口活跃度，绝不把盘口数量冒充成交量。
- 状态词如「背离/共振/承接」均为盘口语义,推送时附解释,避免歧义。
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, Optional

from bot.analyzer.price_action import analyze_price_action, format_price_action_block
from bot.anomaly import phase_rules
from bot.anomaly.market_sentiment import archive_sentiment   # V0.3.17 情绪按日归档
from bot.csqaq.client import CSQAQClient

logger = logging.getLogger(__name__)

# 市场环境缓存(5 分钟):get_current_data 每轮推送都调会撞限流,缓存降频。
_MARKET_CACHE: Dict[str, Any] = {"t": 0.0, "data": None}
_MARKET_TTL = 300.0

# 盘口变化的"显著"阈值(%):超过才算"增加/减少",否则视为持平
_NUM_SIG = 5.0
# 价格变化的"异动"阈值(%)
_PRICE_SIG = 1.0


def _state(price_chg: Optional[float], buy_chg: Optional[float],
           sell_chg: Optional[float]) -> Optional[str]:
    """根据价格方向 × 盘口方向判定状态(盘口语义,非成交量)。

    规则优先级:价动显著时看盘口配合;价不动时看供需挤压。
    """
    if price_chg is None:
        return None
    up = price_chg >= _PRICE_SIG
    dn = price_chg <= -_PRICE_SIG
    b_up = buy_chg is not None and buy_chg >= _NUM_SIG
    b_dn = buy_chg is not None and buy_chg <= -_NUM_SIG
    s_up = sell_chg is not None and sell_chg >= _NUM_SIG
    s_dn = sell_chg is not None and sell_chg <= -_NUM_SIG
    no_chg = (buy_chg is not None and -_NUM_SIG < buy_chg < _NUM_SIG
              and sell_chg is not None and -_NUM_SIG < sell_chg < _NUM_SIG)

    if up:
        if b_up and s_dn:
            return "供需共振：价升 + 求购增 + 在售减，买盘承接强"
        if b_dn:
            return "上涨背离：价升但求购减少，上涨缺乏承接，追高风险"
        if s_up:
            return "供应压力：价升但在售增加，上方抛压显现"
        if no_chg:
            return "价升但盘口未配合：警惕参考价异动/薄盘，可信度低"
        return "价格上行：盘口配合一般，关注后续"
    if dn:
        if b_up and s_dn:
            return "下跌承接：价跌但买盘承接、卖盘收缩，或现企稳"
        if b_dn and s_up:
            return "供需双弱：价跌 + 买盘撤 + 卖盘增，下跌共振"
        if no_chg:
            return "价跌但盘口未配合：疑似参考价异动/薄盘，可信度低"
        return "价格下行：盘口未见承接，谨慎"
    # 价稳:看供需挤压(需求异动特征)
    if b_up and s_dn:
        return "需求蓄力：价格未动但供需双侧挤压，潜在启动"
    if b_dn or s_up:
        return "供应压力：价格未动但在售增/求购减"
    return None


_OUTLOOK = {
    "surge": "若求购继续增加、在售持续收缩，拉升或延续；若在售快速回补则警惕冲高回落。",
    "dump": "若求购出现承接、在售停止增加，或现企稳；否则可能继续下探。",
    "demand": "价格未动但需求增强，若随后价格启动则确认启动，重点观察 30m-1h 窗口。",
    "supply": "在售收缩形成供给压力，若伴随求购增加，价格易涨难跌。",
    "divergence": "价升但买盘跟不上，追高风险较高，谨防回落。",
    "stabilize": "下跌后出现承接，若价格止跌回升则确认企稳。",
    "normal": "以盘口变化为准，观察后续方向性选择。",
}


def outlook(anomaly_type: str, state: Optional[str],
            mkt: Optional[Dict[str, Any]] = None) -> str:
    """预测:基础展望(按异动类型) + 状态修正 + 市场环境修正。

    :param mkt: fetch_market_snapshot 返回的结构化市场环境(index_chg/heat/up/down)
    """
    base = _OUTLOOK.get(anomaly_type, _OUTLOOK["normal"])
    if state and "背离" in state:
        base += "（当前呈背离状态，注意风险）"
    elif state and ("盘口未配合" in state or "孤立" in state):
        base += "（当前盘口未配合，可信度低）"
    # 结合市场环境(大盘方向/今日涨跌分布/热度)修正预测
    if mkt:
        env = []
        idx = mkt.get("index_chg")
        heat = mkt.get("heat")
        up, down = mkt.get("up"), mkt.get("down")
        up_type = anomaly_type in ("surge", "demand", "supply", "divergence")
        if idx is not None:
            if up_type and idx >= 0.3:
                env.append("大盘指数上涨，顺势")
            elif up_type and idx <= -0.3:
                env.append("大盘指数下跌，属逆势，谨防拖累")
            elif not up_type and idx <= -0.3:
                env.append("与大盘同向")
        if up is not None and down is not None and up_type:
            if down > up * 1.5:
                env.append("今日普跌，逆势品需谨慎")
            elif up > down * 1.5:
                env.append("今日普涨，顺势")
        if heat and up_type:
            if "高" in str(heat) or "贪" in str(heat):
                env.append("热度偏高，注意追高风险")
            elif "低" in str(heat) or "恐" in str(heat):
                env.append("热度偏低，或存超跌空间")
        if env:
            base += "（" + "、".join(env) + "）"
    return base


def _pct_text(v: Optional[float]) -> str:
    """百分比 -> 白话(增加/减少/持平)。"""
    if v is None:
        return "N/A"
    if v >= _NUM_SIG:
        return f"增加 {v:+.0f}%"
    if v <= -_NUM_SIG:
        return f"减少 {v:+.0f}%"
    return "持平"


def _prev_price(price: float, chg: Optional[float]) -> Optional[float]:
    """由变化百分比反推窗口前价格;异常值防护。"""
    if chg is None or abs(chg) > 200:
        return None
    return price / (1.0 + chg / 100.0)


def _fmt(v: Optional[float]) -> str:
    return "N/A" if v is None else f"{v:+.1f}%"


async def fetch_market_snapshot(csqaq_cfg: Any,
                                store: Any = None) -> Optional[Dict[str, Any]]:
    """获取市场环境(CSQAQ 首页指数),5 分钟缓存;失败返回 None 不阻塞推送。

    返回 {line, index_chg, heat, up, down}:
      line     - 展示文案(饰品指数/热度/今日涨跌分布)
      index_chg- 饰品指数当日涨跌%
      heat     - 市场热度标签(中等/高/低…)
      up/down  - 今日上涨/下跌饰品数量
    :param csqaq_cfg: CSQAQ 客户端配置(不是 AnomalyConfig——后者无 csqaq 属性)。
    :param store: 可选 SnapshotStore;给了就顺带按日归档情绪(V0.3.17,失败不影响推送)。
    """
    now = time.time()
    cached = _MARKET_CACHE
    if cached.get("data") and now - cached.get("t", 0) < _MARKET_TTL:
        return cached["data"]
    try:
        async with CSQAQClient(csqaq_cfg) as api:
            data = await api.get_current_data("init")
        d = data.get("data") or {}
        parts: list = []
        si = d.get("sub_index_data") or []
        main = next((x for x in si if x.get("name") == "饰品指数"), None)
        index_chg: Optional[float] = None
        if main:
            index_chg = float(main.get("chg_rate") or 0)
            parts.append(f"饰品指数 {index_chg:+.1f}%")
        g = d.get("greedy_status") or {}
        heat = g.get("label")
        if heat:
            parts.append(f"热度 {heat}")
        rd = d.get("rate_data") or {}
        up = int(rd["count_positive_1"]) if rd.get("count_positive_1") is not None else None
        down = int(rd["count_negative_1"]) if rd.get("count_negative_1") is not None else None
        if up is not None and down is not None:
            parts.append(f"今日 {up}涨/{down}跌")
        out = {"line": ("市场：" + " | ".join(parts)) if parts else None,
               "index_chg": index_chg, "heat": heat, "up": up, "down": down}
        _MARKET_CACHE["t"], _MARKET_CACHE["data"] = now, out
        # V0.3.17:顺带按日归档情绪(热度序列/多窗口涨跌分布/分类指数快照)。
        # 只在真正取数(非缓存命中)时写;失败只记 debug,绝不影响推送。
        await archive_sentiment(store, data)
        return out
    except Exception as exc:  # noqa: BLE001 失败静默,不影响推送
        logger.warning("[market] 获取市场环境失败: %s", exc)
        return None




def _format_phase_alert(r: Dict[str, Any], market: Optional[str] = None,
                        multi: Optional[Dict[str, Any]] = None) -> str:
    """周期阶段事件文案(V0.3.29)。

    标题/图标/尾注由 `(阶段, 位置)` 查 phase_rules 决定 —— 同一句「求购 +15.6%」
    在低位是「低位吸筹迹象」、在高位是「高位诱多预警」,不会出现报喜标题配利空正文。
    「判读 + 历史参照」整段由 detect_phase 按当时状态生成,这里只负责排版。
    """
    ptype = r.get('anomaly_type') or 'accum'
    pos = str(r.get('position') or '').strip()
    if pos in ('', '—', '-'):
        pos = ''            # 旧事件没有位置:标题与尾注回退到最保守的中位口径
    icon, title = phase_rules.headline(ptype, pos or '中位')
    price = float(r.get('price') or 0)
    lines = [
        f"{icon} {title}",
        '',
        r.get('name') or '',
        f"现价 ¥{price:,.0f}",
        '━━━━━━━━━━━━━━',
    ]
    block = str(r.get('reason') or '').strip()
    if block:
        lines.append(block)
    detail = str(r.get('detail') or '').strip()
    if detail:
        lines.append(detail)
    if multi and '在售' not in block:
        # 只在盘口块完全缺失时补一条在售合计,避免同一份数据说两遍。
        _MP = (("buff_sell_num", "BUFF"), ("yyyp_sell_num", "悠悠有品"))
        segs, total = [], 0
        for key, tag in _MP:
            v = int(multi.get(key) or 0)
            total += v
            if v > 0:
                segs.append(f"{tag} {v:,}")
        if segs:
            lines.append(f"在售量　{'  '.join(segs)}　合计 {total:,}")
    lines.append('━━━━━━━━━━━━━━')
    if pos:
        # 区间天数与分位已在「判读」第一句给全,这里只留档位,避免天数写死与配置不一致。
        lines.append(f"位置　{pos}")
    if market:
        lines.append(market)
    lines.append(phase_rules.footer(ptype, pos or '中位'))
    lines.append('仅供观察，不构成买卖建议')
    return '\n'.join(lines)


def format_row_alert(r: Dict[str, Any], market: Optional[str] = None,
                     mkt: Optional[Dict[str, Any]] = None,
                     multi: Optional[Dict[str, Any]] = None) -> str:
    """从DB事件行渲染增强版告警文案。"""
    level = r.get('level') or 'L2'
    ptype = r.get('anomaly_type') or 'normal'
    # ---- 资金周期四阶段(V0.2.0):专用文案(建仓/启动/派发,消化只进 /分析 不推送) ----
    if ptype in ("accum", "launch", "distribute"):
        return _format_phase_alert(r, market=market, multi=multi)
    icon = {'L1': '👀', 'L2': '⚠️', 'L3': '🔥', 'L4': '🔥'}.get(level, '⚠️')
    label = {'surge': '突然拉升', 'dump': '突然跳水', 'demand': '需求增强',
             'supply': '供应减少', 'divergence': '上涨供需背离',
             'stabilize': '下跌后企稳', 'normal': '异动'}.get(
                 r.get('anomaly_type') or 'normal', '异动')
    price = float(r.get('price') or 0)
    score = float(r.get('score') or 0)
    chg5 = r.get('price_change_5m')
    chg30 = r.get('price_change_30m')
    # V0.2.29:7d 显示改用新增的 rate_7 列(老事件该列为 0 → 回退 price_change_1d 兼容旧数据);
    # price_change_1d 从此只承载**真实近1日**(新事件缺该字段时写 NULL → 价量解算判"缺失",
    # 而不是被 7 日值冒充成"近1日",也不再默认成"持平")。
    rate1 = r.get('price_change_1d')
    rate7 = r.get('rate_7') or rate1
    bc, sc = r.get('buy_change'), r.get('sell_change')
    pos = r.get('position') or '中位'

    action = analyze_price_action(
        price=price,
        price_change_5m=chg5,
        price_change_30m=chg30,
        price_change_1d=rate1,
        rate_7=rate7,
        buy_change=bc,
        sell_change=sc,
        position=pos,
        market=mkt,
    )

    title = (f"{icon} 饰品{label}" if label.endswith('异动')  # normal→label='异动',勿再拼
             else f"{icon} 饰品{label}异动") + f" [{level}]"
    lines = [
        title,
        '',
        r.get('name') or '',
        f"当前价：¥{price:,.0f}",
        f"5m：{_fmt(chg5)}  30m：{_fmt(chg30)}  7d：{_fmt(rate7)}",
    ]
    if bc is not None or sc is not None:
        lines.append(f"求购：{_fmt(bc)}  在售：{_fmt(sc)}")
    lines.append('')
    lines.extend(action.get('lines') or [])
    lines.append('')
    # 方向取量价解算的价格三态(涨/跌/平),DB 行无 direction 列,勿用 r.direction(dict 会崩)
    price_dir = {'涨': '↑涨', '跌': '↓跌', '平': '→平'}.get(action.get('price_state'), '→平')
    lines.append(f"异动评分：{score:.0f}/100   位置：{pos}   方向：{price_dir}")
    if market:
        lines.append(market)
    if multi:
        # V0.3.12:由「BUFF+悠悠在售：BUFF 549 + 悠悠 512 ≈ 1,061」改为分平台并列,
        # 免得读成"两家相加的一个数"。变化量在 reason 的盘口块里逐平台给。
        _MP = (("buff_sell_num", "BUFF"), ("yyyp_sell_num", "悠悠有品"))
        segs = []
        total = 0
        for key, tag in _MP:
            v = int(multi.get(key) or 0)
            total += v
            if v > 0:
                segs.append(f"{tag} {v:,}")
        if segs:
            lines.append(f"在售：{'  '.join(segs)}  合计 {total:,}")
    lines.append(f"预测：{outlook(r.get('anomaly_type') or 'normal', _state(chg30, bc, sc), mkt)}")
    if r.get('reason'):
        lines += ['', '原因：', str(r['reason'])]
    lines += ['', '⚠️ 仅代表行情异动，不代表一定上涨。']
    return '\n'.join(lines)
