# -*- coding: utf-8 -*-
"""QQ 群消息指令解析与回复文本格式化。"""

import re
from typing import Any, Dict, List, Optional, Tuple

from bot.analyzer.price_action import analyze_price_action, format_price_action_block
from bot.analyzer.recommender import RecommendResult, Recommendation
from bot.config import QQConfig

# 策略别名 -> 内部标识
STRATEGY_ALIASES = {
    "7天低位": "low_7d", "7天最低": "low_7d", "低位": "low_7d", "low": "low_7d",
    "7天超跌": "dip_7d", "超跌": "dip_7d", "dip": "dip_7d", "跌": "dip_7d",
    "7天热点": "hot_7d", "热点": "hot_7d", "hot": "hot_7d", "涨": "hot_7d",
    "好出手": "liquid", "流动性": "liquid", "liquid": "liquid",
    "今日异动": "day_move", "异动": "day_move", "day": "day_move",
}


def strip_at(content: str) -> str:
    """去掉消息中的 @机器人 片段。"""
    # QQ 富文本中 @ 以 <@!id> 或 <@id> 形式存在
    cleaned = re.sub(r"<@!?\d+>", " ", content)
    return cleaned.strip()


# 自然语言意图关键词（本地意图识别）
_INTENT_HELP = ["帮助", "菜单", "你能做什么", "怎么用", "指令", "有哪些功能"]
_INTENT_PRICE = ["价格", "行情", "多少钱", "多少米", "什么价", "价格怎么样", "帮我看看", "看看", "查一下", "查查", "搜一下", "找一下", "值多少"]
_INTENT_RANK = ["排行", "涨跌榜", "排行榜", "涨幅榜", "跌幅榜", "哪些涨", "哪些跌", "涨得", "跌得", "涨了", "跌了"]
_INTENT_STATUS = ["状态", "数据源", "数据情况", "连接正常吗", "在线吗", "活着吗"]
_INTENT_REPORT = ["播报", "大盘", "指数", "市场日报", "今日播报", "市场情况"]
_INTENT_RECOMMEND = ["推荐", "选品", "买什么", "入什么", "值得买", "买啥", "入手", "有什么好"]
# 异动监控意图(纯文本等价 /异动 命令,避免被兜底丢给 AI 闲聊导致卡顿)
_INTENT_ANOMALY = ["异动", "扫货", "大商", "在售骤降", "异动榜"]
# 语气词/无意义词，用于从自然语言中剥离出饰品名
_NOISE_WORDS = ["帮我", "麻烦", "一下", "看看", "查一下", "查查", "搜一下", "找一下",
                "怎么样", "如何", "是啥", "是什么", "价格", "行情", "多少钱", "多少米",
                "值多少", "的", "了", "啊", "呢", "呀", "吗", "哦", "哈", "请问", "问一下"]


def extract_skin_keyword(text: str) -> str:
    """从自然语言中提取饰品查询关键词。"""
    t = text
    for w in _NOISE_WORDS:
        t = t.replace(w, " ")
    t = re.sub(r"[\s,，。？?！!、]+", " ", t).strip()
    return t


def parse_message(raw_content: str, qq_cfg: QQConfig) -> Tuple[str, Optional[str]]:
    """解析群消息，返回 (command, arg)。

    command: help | recommend | price | rank | status | chat | unknown
    - 斜杠指令优先（严格路由，回实时数据）
    - 自然语言走意图识别（price/recommend/rank 也会回实时数据）
    - 无法识别的闲聊归 chat
    """
    content = strip_at(raw_content).strip()
    if not content:
        return "unknown", None

    # ---- 斜杠指令（优先，严格路由） ----
    if content.startswith("/"):
        parts = content[1:].strip().split(maxsplit=1)
        cmd = parts[0].lower()
        arg = parts[1] if len(parts) > 1 else ""
        if cmd in ("help", "帮助", "?", "菜单"):
            return "help", None
        if cmd in ("推荐", "recommend", "rec", "选品"):
            return "recommend", arg or None
        if cmd in ("价格", "price", "行情"):
            return "price", arg or None
        if cmd in ("排行", "rank", "涨跌榜"):
            return "rank", arg or None
        if cmd in ("状态", "status", "数据"):
            return "status", None
        if cmd in ("播报", "指数", "日报", "大盘", "report", "market"):
            return "report", None
        if cmd in ("关注", "盯", "盯盘", "watch", "watch_add"):
            return "watch", arg or None
        if cmd in ("取消关注", "取关", "unwatch", "watch_del"):
            return "unwatch", arg or None
        if cmd in ("我的关注", "关注列表", "watches", "watchlist"):
            return "watches", None
        if cmd in ("公告", "announce", "广播", "notify"):
            return "announce", arg or None
        if cmd in ("功能", "feature", "开关"):
            return "feature", arg or None
        if cmd in ("功能开放", "featureopen", "开放功能", "权限"):
            return "featureopen", arg or None
        if cmd in ("分析", "analysis", "状态分析"):
            return "analysis", arg or None
        if cmd in ("异动", "anomaly", "扫货", "大商"):
            return "anomaly", normalize_anomaly_arg(arg)
        return "chat", arg or None

    # ---- 自然语言意图识别 ----
    low = content.lower()
    for kw in _INTENT_HELP:
        if kw in content:
            return "help", None
    for kw in _INTENT_STATUS:
        if kw in content:
            return "status", None
    for kw in _INTENT_REPORT:
        if kw in content:
            return "report", None
    # 推荐意图优先于排行（避免"超跌了"被"跌了"抢判为排行）
    for kw in _INTENT_RECOMMEND:
        if kw in content:
            for alias in STRATEGY_ALIASES:
                if alias in content:
                    return "recommend", alias
            return "recommend", None
    # 异动监控意图(必须在 recommend 之后, 以免 "推荐 异动" 被抢; "今日异动" 仍走选品榜)
    for kw in _INTENT_ANOMALY:
        if kw in content:
            if "今日" in content and "异动" in content:
                return "recommend", "day_move"
            return "anomaly", None
    for kw in _INTENT_PRICE:
        if kw in content:
            keyword = extract_skin_keyword(content)
            return "price", keyword or None
    for kw in _INTENT_RANK:
        if kw in content:
            direction = "down" if ("跌" in content or "跌得" in content or "跌了" in content) else "up"
            return "rank", direction
    # 兜底：交给闲聊处理
    return "chat", content


_ANOMALY_LABEL = {
    "surge": "突然拉升", "dump": "突然跳水", "demand": "需求增强",
    "supply": "供应减少", "divergence": "供需背离", "stabilize": "下跌企稳",
    "scan": "疑似扫货", "restock": "批量下架", "fake": "假动作",
}
_ANOMALY_ICON = {
    "surge": "🚀", "dump": "🔻", "demand": "🟣", "supply": "🟠",
    "divergence": "🟡", "stabilize": "🔵",
    "scan": "🟢", "restock": "🔴", "fake": "⚠️",
}
_KIND_TITLE = {
    None: "异动榜", "strong": "强异动榜(L3+)", "up": "上涨异动榜",
    "down": "下跌异动榜", "demand": "需求/供应异动榜", "low": "低位异动榜",
}


_ANOMALY_ARG_MAP = {
    "强": "strong", "strong": "strong",
    "上涨": "up", "up": "up", "涨": "up",
    "下跌": "down", "down": "down", "跌": "down",
    "需求": "demand", "demand": "demand", "供需": "demand", "供应": "demand",
    "低位": "low", "low": "low",
    "状态": "status", "status": "status",
}


def normalize_anomaly_arg(arg: Optional[str]) -> Optional[str]:
    """/异动 子命令归一化 -> strong/up/down/demand/low/status。"""
    if not arg:
        return None
    a = str(arg).strip().lower()
    for kw, val in _ANOMALY_ARG_MAP.items():
        if kw in a:
            return val
    return None


def _direction_of(r: Dict[str, Any]) -> str:
    """从 30m 价格变化推断方向(DB 未单列 direction 字段)。"""
    try:
        v = float(r.get("price_change_30m") or 0)
    except (TypeError, ValueError):
        return "MIXED"
    if v > 0.5:
        return "UP"
    if v < -0.5:
        return "DOWN"
    return "MIXED"


def filter_anomalies(rows: List[Dict[str, Any]],
                     kind: Optional[str] = None) -> List[Dict[str, Any]]:
    """按 /异动 子命令过滤:strong/up/down/demand/low。

    只保留异动雷达的评分事件(score>0)。旧版「在售骤降」监控写入的
    scan/restock/fake 事件 score=0 且不带 position/price_change 等雷达字段,
    若混入会把雷达榜淹没(实测 37 条里 35 条是这类旧事件,且重复 5~7 遍),
    同时导致 demand/低位 等分类必然命中 0。
    """
    rows = [r for r in rows if float(r.get("score") or 0) > 0]
    if not kind:
        return list(rows)
    if kind == "strong":
        return [r for r in rows if (r.get("level") or "") in ("L3", "L4")]
    if kind == "up":
        return [r for r in rows if _direction_of(r) == "UP"]
    if kind == "down":
        return [r for r in rows if _direction_of(r) == "DOWN"]
    if kind == "demand":
        return [r for r in rows if (r.get("anomaly_type") or "") in ("demand", "supply")]
    if kind == "low":
        return [r for r in rows if r.get("position") == "低位"]
    return list(rows)




def format_anomaly_report(rows: List[Dict[str, Any]], top_n: int = 10,
                          kind: Optional[str] = None) -> str:
    """格式化异动榜(/异动 命令回复)。
    兼容两类事件:异动雷达的评分事件(score>0)与旧版在售降/出货事件(score=0)。
    """
    if not rows:
        tip = (
            "监控池 = 三层池B(全量发现,180分钟一圈) / A(活跃,2分钟) / S(超级,30秒)\n"
            "只有评分达到 L2 预警(>=55)的异动才会入库并推送，L0/L1 属观察级不打扰。"
        )
        if kind:
            return f"📥 近24小时暂无符合《{_KIND_TITLE.get(kind, kind)}》的异动。\n{tip}"
        return f"📥 近24小时暂无饰品异动。\n{tip}"

    rows = sorted(rows, key=lambda r: (float(r.get('score') or 0), r.get('ts') or ''), reverse=True)
    title = _KIND_TITLE.get(kind, '异动榜')
    lines = [f"📊 近24h {title}（共 {len(rows)} 条，按评分倒序）\n"]

    for i, r in enumerate(rows[:top_n], 1):
        at = r.get('anomaly_type') or 'fake'
        icon = _ANOMALY_ICON.get(at, '⚠️')
        label = _ANOMALY_LABEL.get(at, '异动')
        name = r.get('name') or '?'
        score = float(r.get('score') or 0)

        if score > 0:
            level = r.get('level') or 'L1'
            price = float(r.get('price') or 0)
            pos = r.get('position') or ''
            pos_txt = f'  {pos}' if pos else ''
            action = analyze_price_action(
                price=price,
                price_change_5m=r.get('price_change_5m'),
                price_change_30m=r.get('price_change_30m'),
                price_change_1d=r.get('price_change_1d'),
                # V0.2.29:7 日涨跌取新增 rate_7 列(老事件为 0 → 回退 1d 列兼容),
                # 不再把同一个值同时当"近1日"和"7日"传给量价解算。
                rate_7=r.get('rate_7') or r.get('price_change_1d'),
                buy_change=r.get('buy_change'),
                sell_change=r.get('sell_change'),
                position=pos,
            )
            lines.append(
                f"{i}. {icon} {label} [{level}] {score:.0f}分{pos_txt}\n"
                f"   {name}\n"
                f"   ¥{price:,.0f}  5m {_pct(r.get('price_change_5m'))}  30m {_pct(r.get('price_change_30m'))}\n"
                f"   求购 {_pct(r.get('buy_change'))}  在售 {_pct(r.get('sell_change'))}"
            )
            lines.append(f"   量价 {action['summary']}")
            lines.append(f"   {action['lines'][-1]}")
        else:
            sp = r.get('sell_prev') or 0
            sc = r.get('sell_cur') or 0
            ad = int(r.get('abs_drop') or 0)
            w = r.get('window_min') or 0
            abs_txt = f"（少 {ad} 件）" if ad else ''
            lines.append(
                f"{i}. {icon} {label} {name}\n"
                f"   在售 {sp}→{sc}，跌幅 {r.get('drop_pct') or 0}%{abs_txt}（{w}分钟内）"
            )
    lines.append("\n⚠️ 仅代表行情异动，不代表一定上涨。")
    return '\n'.join(lines)
def _pct(v: Any) -> str:
    if v is None:
        return "N/A"
    try:
        return f"{float(v):+.1f}%"
    except (TypeError, ValueError):
        return "N/A"


def format_anomaly_status(counts: Dict[str, int], latest_ts: Optional[str],
                          api_ok: bool = True, recent_cnt: int = 0) -> str:
    """格式化 /异动 状态 面板(PRD V1.0:三层池规模与健康度)。"""
    api_txt = "正常" if api_ok else "异常(限频或网络问题)"
    return (
        "🛰 异动雷达状态\n\n"
        f"B 池（全量发现）：{counts.get('B', 0)} 个\n"
        f"A 池（活跃监控）：{counts.get('A', 0)} 个\n"
        f"S 池（超级异动）：{counts.get('S', 0)} 个\n\n"
        f"最近一次扫描：{latest_ts or '暂无'}\n"
        f"API 状态：{api_txt}\n"
        f"近 24h 发现异动：{recent_cnt} 条"
    )


def resolve_strategy(arg: Optional[str]) -> str:
    """把用户输入的策略文本解析为内部策略 ID。"""
    if not arg:
        return "low_7d"
    for alias, sid in STRATEGY_ALIASES.items():
        if alias in arg:
            return sid
    return "low_7d"


def _fmt_money(v: float) -> str:
    return f"¥{v:,.2f}" if v > 0 else "-"


# ----------------------------------------------------------------------
# 回复文本
# ----------------------------------------------------------------------

def format_recommend(result: RecommendResult, data_time: Optional[str] = None) -> str:
    if not result.items:
        return "暂无符合条件的推荐，换个策略试试？(如 /推荐 超跌)"
    lines = [
        f"🔍 选品推荐 · {result.strategy_label}",
        f"数据时间: {data_time or '--'}\n",
    ]
    for i, it in enumerate(result.items, 1):
        lines.append(it.format_line(i))
    lines.append("\n⚠️ 数据仅供参考，投资有风险。回复 /推荐 超跌 等可切换策略")
    return "\n".join(lines)


def format_price(rows: List[Dict[str, Any]]) -> str:
    if not rows:
        return "没有找到相关饰品，试试 /价格 爪子刀"
    lines = ["💰 饰品行情"]
    for r in rows[:5]:
        name = r["name"]
        price = float(r.get("main_price") or 0)
        sell = float(r.get("buff_sell_price") or 0)
        buy = float(r.get("buff_buy_price") or 0)
        rate_7 = float(r.get("sell_price_rate_7") or 0)
        rate_d = float(r.get("buff_price_chg") or 0)
        lines.append(
            f"\n{name}"
            f"\n  参考价 {_fmt_money(price)} | BUFF售 {_fmt_money(sell)} / 求购 {_fmt_money(buy)}"
            f"\n  7日 {'▲' if rate_7 >= 0 else '▼'}{abs(rate_7):.1f}% | "
            f"今日 {'+' if rate_d >= 0 else ''}{rate_d:.1f}%"
        )
    return "\n".join(lines)


def format_rank(items: List[Recommendation], direction: str, data_time: Optional[str] = None) -> str:
    if not items:
        return "暂无数据，稍后再试"
    title = "7日涨幅榜 📈" if direction == "up" else "7日跌幅榜 📉"
    lines = [f"{title}  (数据: {data_time or '--'})"]
    for i, it in enumerate(items, 1):
        arrow = "▲" if it.rate_7d >= 0 else "▼"
        lines.append(
            f"{i}. {it.name}  {_fmt_money(it.price)}  {arrow}{abs(it.rate_7d):.1f}%"
        )
    return "\n".join(lines)


def format_help() -> str:
    return (
        "🤖 倒货波 · CS2 饰品行情指令\n"
        "（群里 @我 后输入）\n\n"
        "【1·大盘 & 播报】\n"
        "▪ /大盘      大盘指数(SteamDT)+走势图\n"
        "▪ /播报      今日市场播报\n\n"
        "【2·价格查询】\n"
        "▪ /价格 饰品名   单价/求购/在售(原始数据)\n\n"
        "【3·排行榜】\n"
        "▪ /排行 涨|跌   7日涨跌幅榜\n\n"
        "【4·选品推荐】\n"
        "▪ /推荐 [超跌|热点|好出手|低位|异动]   实时选品\n\n"
        "【5·异动雷达】⭐\n"
        "▪ /异动            近24h异动榜(按评分倒序)\n"
        "▪ /异动 强         只看强异动(L3以上)\n"
        "▪ /异动 上涨|下跌   按方向筛选\n"
        "▪ /异动 需求|低位   需求异动 / 低位启动\n"
        "▪ /异动 状态        B/A/S 三层池规模与健康度\n"
        "  三层池：B全量发现(约1.5万件) → A活跃(2分钟) → S超级(30秒)\n"
        "  达到预警等级自动播报VIP群，免费群暂未开放\n\n"
        "【盯盘】\n"
        "▪ /关注 饰品名 [目标价|5%|7日涨X%|7日跌X%|在售降X%]\n"
        "▪ /取消关注 饰品名   /我的关注\n\n"
        "【其他】\n"
        "▪ /状态      采集状态   ▪ /帮助      本帮助\n\n"
        "私聊我可用：\n"
        "▪ /公告 <群号> <内容>   群公告(仅管理员)\n\n"
        "直接说人话也行：\n"
        "▪ 大盘怎么样 / 帮我看看 爪子刀\n"
        "▪ 哪些跌得最惨 / 蝴蝶刀配什么手套"
    )


def format_chat_fallback() -> str:
    """本地意图识别未命中时的兜底回复。"""
    return (
        "我没太听懂你说的～ 我是 CS2 饰品行情机器人，可以这样问我：\n"
        "▪ 帮我看看 爪子刀\n"
        "▪ /推荐 超跌\n"
        "▪ 哪些涨得多\n"
        "▪ /帮助 查看全部指令"
    )


def format_status(fetch_time: Optional[str], item_count: int) -> str:
    return (
        "📡 数据采集状态\n"
        f"最新快照: {fetch_time or '尚未抓取'}\n"
        f"饰品样本: {item_count} 条\n"
        "采集周期: 15 分钟"
    )


# ----------------------------------------------------------------------
# 自选盯盘
# ----------------------------------------------------------------------

def parse_watch_arg(arg: str) -> Tuple[str, Optional[float], Optional[float], str, float]:
    """解析 /关注 参数:返回 (饰品名, 阈值%, 目标价, alert_type, param)。

    alert_type / param:
      - 'price'      阈值%(param 无关) 或 目标价(param 无关)
      - 'pct7d_up'   7日涨幅超 param%
      - 'pct7d_down' 7日跌幅超 param%
      - 'supply'     在售件数降幅超 param%

    识别规则(优先级从高到低):
      1. "7日涨/升 X%" / "7日跌/降 X%"  -> pct7d_up / pct7d_down
      2. "在售降/跌 X%"               -> supply
      3. 裸数字带 %                   -> price 阈值
      4. 裸数字 / "到/涨到/跌到 数字"  -> price 目标价
    """
    arg = (arg or "").strip()
    if not arg:
        return "", None, None, "price", 0.0
    # 1) 7日涨/跌
    m = re.search(r"7\s*日\s*(涨|升|跌|降)\s*(\d{1,3})\s*%", arg)
    if m:
        direction = "up" if m.group(1) in ("涨", "升") else "down"
        name = re.sub(r"7\s*日\s*(涨|升|跌|降)\s*\d{1,3}\s*%", "", arg).strip() or arg
        return name, None, None, f"pct7d_{direction}", float(m.group(2))
    # 2) 在售降/跌 X%
    m = re.search(r"在售\s*(跌|降|减少)?\s*(\d{1,3})\s*%", arg)
    if m:
        name = re.sub(r"在售\s*(跌|降|减少)?\s*\d{1,3}\s*%", "", arg).strip() or arg
        return name, None, None, "supply", float(m.group(2))
    # 2.5) 周期监测(V0.2.3):资金周期四阶段(建仓/启动/派发)推送,@ 关注者
    m = re.search(r"(周期监测|资金周期|四阶段|建仓监测|周期)", arg)
    if m:
        name = arg.replace(m.group(1), "").strip()
        return name, None, None, "cycle", 0.0
    # 3) / 4) 价格阈值 / 目标价
    m = re.match(r"^(.*?)\s*(到|涨到|跌到)?\s*(\d{1,7})\s*(?:元|块钱|块)?\s*(%?)\s*$", arg)
    if m and m.group(1).strip():
        name = m.group(1).strip()
        num = float(m.group(3))
        if m.group(4) == "%":
            return name, max(1.0, min(num, 100.0)), None, "price", 0.0
        return name, None, num, "price", 0.0
    return arg, None, None, "price", 0.0


def watch_should_alert(base_price: float, cur_price: float,
                       threshold: Optional[float], target_price: Optional[float],
                       alert_type: str = "price", param: float = 0.0,
                       rate_7: Optional[float] = None,
                       sell_num: Optional[int] = None,
                       last_sell_num: Optional[int] = None) -> Tuple[bool, str]:
    """盯盘触发判断:返回 (是否提醒, 原因文案)。按 alert_type 分派。

    - price: 价格涨跌幅超 threshold% 或 穿越 target_price
    - pct7d_up/down: 7日涨/跌幅绝对值超 param%(rate_7 由调用方提供)
    - supply: 全平台在售件数较上次下降超 param%(sell_num/last_sell_num 由调用方提供)
    """
    if alert_type in ("pct7d_up", "pct7d_down"):
        if rate_7 is None:
            return False, ""
        if alert_type == "pct7d_up" and rate_7 >= param:
            return True, f"7日涨幅 {rate_7:+.1f}%"
        if alert_type == "pct7d_down" and rate_7 <= -param:
            return True, f"7日跌幅 {rate_7:+.1f}%"
        return False, ""
    if alert_type == "supply":
        if last_sell_num and last_sell_num > 0 and sell_num is not None and sell_num >= 0:
            if last_sell_num - sell_num >= last_sell_num * param / 100.0:
                drop = (last_sell_num - sell_num) / last_sell_num * 100
                return True, f"全平台在售 {last_sell_num}→{sell_num}件(-{drop:.0f}%)"
        return False, ""
    # price(默认)
    if base_price <= 0 or cur_price <= 0:
        return False, ""
    chg = (cur_price - base_price) / base_price * 100
    if threshold and abs(chg) >= threshold:
        return True, f"涨跌 {chg:+.1f}%"
    if target_price and base_price != target_price:
        if (cur_price - target_price) * (base_price - target_price) <= 0:
            direction = "📈 涨到" if cur_price >= target_price else "📉 跌到"
            return True, f"{direction}目标价 ¥{target_price:,.2f}(现 ¥{cur_price:,.2f})"
    return False, ""


def _watch_cond_text(alert_type: str, param: float,
                     threshold: Optional[float], target_price: Optional[float]) -> str:
    if alert_type == "pct7d_up":
        return f"7日涨超 {param:.0f}% 提醒"
    if alert_type == "pct7d_down":
        return f"7日跌超 {param:.0f}% 提醒"
    if alert_type == "supply":
        return f"全平台在售降超 {param:.0f}% 提醒"
    if alert_type == "cycle":
        return "资金周期四阶段监测(建仓/启动/派发,每 15 分钟采样,命中即 @ 你)"
    if target_price:
        return f"到价 ¥{target_price:,.2f} 提醒"
    return f"涨跌超 {threshold or 10:.0f}% 提醒"


def _platform_label(scope: str) -> str:
    return {"yyyp": "悠悠有品", "buff": "BUFF", "steam": "Steam"}.get(scope or "", "")


def _price_pair_line(buff_price: Optional[float], yyyp_price: Optional[float]) -> str:
    """'平台对照: BUFF ¥x | 悠悠有品 ¥y'(缺失的平台不显示)。"""
    parts = []
    if buff_price and buff_price > 0:
        parts.append(f"BUFF ¥{buff_price:,.2f}")
    if yyyp_price and yyyp_price > 0:
        parts.append(f"悠悠有品 ¥{yyyp_price:,.2f}")
    return ("平台对照: " + " | ".join(parts)) if parts else ""


def format_watch_added(zh_name: str, base_price: float,
                       threshold: Optional[float], target_price: Optional[float],
                       alert_type: str = "price", param: float = 0.0,
                       buff_price: Optional[float] = None,
                       yyyp_price: Optional[float] = None,
                       scope: str = "") -> str:
    cond = _watch_cond_text(alert_type, param, threshold, target_price)
    label = _platform_label(scope)
    head = f"基准价 ¥{base_price:,.2f}" + (f"（{label}）" if label else "")
    pair = _price_pair_line(buff_price, yyyp_price)
    return (
        f"🔔 已关注「{zh_name}」\n"
        f"{head} | {cond}\n"
        + (f"{pair}\n" if pair else "")
        + "（发送 /取消关注 或 /我的关注 管理）\n"
        "支持类型: 目标价 / 涨跌X% / 7日涨X% / 7日跌X% / 在售降X% / 周期监测\n"
        "（周期监测: 关注后每 15 分钟采样,约 1 小时后开始四阶段判定,命中即 @ 你）"
    )


def format_watch_removed(zh_name: str) -> str:
    return f"✅ 已取消关注「{zh_name}」"


def format_watch_list(rows: List[Dict[str, Any]]) -> str:
    if not rows:
        return "📭 你还没有关注任何饰品\n使用 /关注 饰品名 [目标价|涨跌X%|7日涨X%|7日跌X%|全平台在售降X%] 开始盯盘"
    lines = ["🔔 我的盯盘列表"]
    for i, r in enumerate(rows, 1):
        at = r.get("alert_type") or "price"
        p = float(r.get("param") or 0)
        if at == "pct7d_up":
            cond = f"7日涨超 {p:.0f}%"
        elif at == "pct7d_down":
            cond = f"7日跌超 {p:.0f}%"
        elif at == "supply":
            cond = f"全平台在售降超 {p:.0f}%"
        elif at == "cycle":
            cond = "四阶段周期监测"
        elif r.get("target_price"):
            cond = f"目标 ¥{float(r['target_price']):,.2f}"
        else:
            cond = f"阈值 {float(r.get('threshold') or 10):.0f}%"
        base = f"基准 ¥{float(r['base_price']):,.2f}" if r.get("base_price") else ""
        _bl = _platform_label(str(r.get("base_price_scope") or ""))
        if base and _bl:
            base += f"（{_bl}）"
        lines.append(f"{i}. {r['zh_name']} | {cond} | {base}")
    lines.append("\n用 /取消关注 饰品名 可取消")
    return "\n".join(lines)[:1200]


def format_watch_alert(zh_name: str, base_price: float, cur_price: float, reason: str,
                       buff_price: Optional[float] = None,
                       yyyp_price: Optional[float] = None) -> str:
    pair = _price_pair_line(buff_price, yyyp_price)
    return (
        f"🔔 你关注的「{zh_name}」变动提醒\n"
        f"基准价 ¥{base_price:,.2f} → 现价 ¥{cur_price:,.2f}\n"
        + (f"{pair}\n" if pair else "")
        + f"{reason} | 已按现价更新基准"
    )


# ----------------------------------------------------------------------
# 大盘指数盯盘(index_watch,触发源 = SteamDT 大盘指数)
# ----------------------------------------------------------------------

def parse_index_watch_arg(arg: str) -> Tuple[Optional[str], float]:
    """解析 /关注 大盘 ... 的参数:返回 (alert_type, param) 或 (None, 0) 解析失败。

    alert_type:
      - 'idx_below'    跌破点位(param 为指数点位,如 2000)
      - 'idx_above'    突破点位(param 为指数点位,如 2100)
      - 'idx_drop_pct' 当日跌幅超 param%
      - 'idx_rise_pct' 当日涨幅超 param%
    识别规则(优先级从高到低):
      1. "跌破/跌穿/低于/小于 N"(无 %) -> idx_below
      2. "突破/超过/高于/大于 N"(无 %) -> idx_above
      3. "跌/降 X%"                    -> idx_drop_pct
      4. "涨/升 X%"                    -> idx_rise_pct
    """
    arg = (arg or "").strip()
    if not arg:
        return None, 0.0
    # 点位阈值(必须无 %):避免 "跌3%" 被误判为跌破 3
    if "%" not in arg:
        m = re.search(r"(跌破|跌穿|低于|小于)\s*(\d{2,7})", arg)
        if m:
            return "idx_below", float(m.group(2))
        m = re.search(r"(突破|超过|高于|大于)\s*(\d{2,7})", arg)
        if m:
            return "idx_above", float(m.group(2))
    # 百分比
    m = re.search(r"(跌|降)\s*(\d{1,3})\s*%", arg)
    if m:
        return "idx_drop_pct", float(m.group(2))
    m = re.search(r"(涨|升)\s*(\d{1,3})\s*%", arg)
    if m:
        return "idx_rise_pct", float(m.group(2))
    return None, 0.0


def index_should_alert(alert_type: str, param: float,
                       idx_val: float, ratio_pct: float) -> Tuple[bool, str]:
    """大盘盯盘触发判断:返回 (是否提醒, 原因文案)。

    idx_val: 当前大盘指数; ratio_pct: 当日涨跌百分比(如 0.66 表示 +0.66%)。
    """
    if alert_type == "idx_drop_pct":
        if ratio_pct <= -param:
            return True, f"当日跌幅 {ratio_pct:+.2f}%"
    elif alert_type == "idx_rise_pct":
        if ratio_pct >= param:
            return True, f"当日涨幅 {ratio_pct:+.2f}%"
    elif alert_type == "idx_below":
        if idx_val <= param:
            return True, f"指数 {idx_val:,.2f} 跌破 {param:,.0f}"
    elif alert_type == "idx_above":
        if idx_val >= param:
            return True, f"指数 {idx_val:,.2f} 突破 {param:,.0f}"
    return False, ""


def _index_watch_cond_text(alert_type: str, param: float) -> str:
    if alert_type == "idx_drop_pct":
        return f"当日跌超 {param:.0f}% 提醒"
    if alert_type == "idx_rise_pct":
        return f"当日涨超 {param:.0f}% 提醒"
    if alert_type == "idx_below":
        return f"跌破 {param:,.0f} 提醒"
    if alert_type == "idx_above":
        return f"突破 {param:,.0f} 提醒"
    return "大盘提醒"


def format_broad_index(bi: Dict[str, Any]) -> str:
    """SteamDT 大盘指数卡(头条,单源)。来源已由标题「(SteamDT)」标注,末尾不重复来源行。"""
    import datetime
    idx = float(bi.get("index") or 0)
    ratio = float(bi.get("diff_ratio_pct") or 0)
    upd = ""
    try:
        ts = int(bi.get("update_time") or 0)
        if ts:
            upd = datetime.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")
    except Exception:
        pass
    lines = ["📊 CS2 大盘指数（SteamDT）", "━━━━━━━━━━━━━━"]
    lines.append(f"📍 当前指数：{idx:,.2f} {_fmt_arrow(ratio)}")
    lines.append(f"📈 较昨日：{float(bi.get('diff_yesterday') or 0):+.2f} ({ratio:+.2f}%)")
    if upd:
        lines.append(f"🕒 更新：{upd}")
    return "\n".join(lines)


def format_broad_panorama(data: Dict[str, Any]) -> str:
    """CSQAQ 市场全景(大盘头条以外的市场数据)。

    与 SteamDT 大盘指数分开展示并标注「CSQAQ」来源:两源算法/权重不同,数值不可直接比,
    故开盘/收盘(~1500 量级)等不与 SteamDT 当前指数(829 量级)混为同一张"大盘指数"卡。
    包含:开盘/收盘/最高/最低、在线人数(今日/本月峰值)、Steam 卡价、今日涨跌分布、
    市场热度、饰品分类指数。
    """
    d = data.get("data") or {}
    subs = d.get("sub_index_data") or []
    if not subs:
        return ""
    subs_by_name = {s.get("name"): s for s in subs}
    main = subs_by_name.get("饰品指数") or subs[0]

    lines = ["━━━━━━━━━━━━━━", "📋 市场全景 · CSQAQ"]
    lines.append(f"⚖️ 开盘：{main.get('open', 0):,.2f} | 收盘：{main.get('close', 0):,.2f}")
    lines.append(f"🔺 最高：{main.get('high', 0):,.2f} | 🔻 最低：{main.get('low', 0):,.2f}")

    onl = d.get("online_number") or {}
    if onl:
        lines.append(f"👥 在线人数：{onl.get('current_number', 0):,}"
                     f"（今日峰值 {onl.get('today_peak', 0):,}，"
                     f"本月峰值 {onl.get('month_peak', 0):,}）")

    cards = d.get("card_price") or []
    if cards:
        latest = cards[-1]
        lines.append(f"💳 Steam 美金卡价：{latest.get('card_price', 0):.1f} 元")

    rate = d.get("rate_data") or {}
    rate_line = _rate_line(rate)   # V0.3.17:1/7/30 日多窗口(原来只给今日)
    if rate_line:
        lines.append(rate_line)

    gs = d.get("greedy_status") or {}
    if gs.get("label"):
        lines.append(f"🔥 市场热度：{gs.get('label')}")

    lines.append("📊 饰品分类指数")
    for i, name in enumerate(_report_index_names(subs_by_name), 1):
        s = subs_by_name.get(name)
        if s:
            lines.append(
                f"{i}. {name}：{s.get('market_index', 0):,.2f} "
                f"({s.get('chg_rate', 0):+.2f}%) {_fmt_arrow(s.get('chg_rate', 0))}"
            )
    return "\n".join(lines)


def format_broad_full(bi: Dict[str, Any], data: Optional[Dict[str, Any]] = None) -> str:
    """统一大盘播报:SteamDT 指数卡(头条) + CSQAQ 市场全景(下方,标注来源)。

    大盘指数只认 SteamDT 单源(两源算法/权重不同,数值不可直接比);
    市场全景(开盘/收盘/在线/卡价/分类指数等)来自 CSQAQ,单独标注来源,不构成双源指数。
    """
    lines = format_broad_index(bi).split("\n")
    if data:
        pano = format_broad_panorama(data)
        if pano:
            lines.extend(pano.split("\n"))
            lines.append("━━━━━━━━━━━━━━")
            lines.append("数据来源：大盘指数 SteamDT.COM ｜ 市场全景 CSQAQ.COM ｜ 仅供参考，投资有风险")
            return "\n".join(lines)
    # 仅 SteamDT(CSQAQ 全景不可用)
    lines.append("数据来源：SteamDT.COM ｜ 仅供参考，投资有风险")
    return "\n".join(lines)


def format_index_watch_added(alert_type: str, param: float) -> str:
    cond = _index_watch_cond_text(alert_type, param)
    return (
        f"🔔 已关注「大盘指数」\n"
        f"{cond}\n"
        "触发源：SteamDT 大盘指数\n"
        "（发送 /取消关注大盘 管理）"
    )


def format_index_watch_list(rows: List[Dict[str, Any]]) -> str:
    if not rows:
        return "📭 你还没有关注大盘指数"
    lines = ["📊 我的大盘盯盘"]
    for i, r in enumerate(rows, 1):
        at = r.get("alert_type") or ""
        p = float(r.get("param") or 0)
        lines.append(f"{i}. 大盘指数 | {_index_watch_cond_text(at, p)}")
    lines.append("\n用 /取消关注大盘 可取消")
    return "\n".join(lines)


def format_index_watch_alert(alert_type: str, param: float, bi: Dict[str, Any]) -> str:
    idx = float(bi.get("index") or 0)
    ratio = float(bi.get("diff_ratio_pct") or 0)
    return (
        f"🔔 你关注的大盘指数变动提醒\n"
        f"当前指数：{idx:,.2f} ({ratio:+.2f}%)\n"
        f"触发条件：{_index_watch_cond_text(alert_type, param)}\n"
        f"数据来源：SteamDT.COM"
    )


def format_watch_intro(tier: str = "none", group_openid: str = "",
                       max_per_user: int = 1) -> str:
    """盯盘使用说明 + 权限提示。tier: vip / free / none。"""
    if tier == "free":
        head = "✅ 本群已开启盯盘功能（免费群）"
        foot = (f"⚠️ 免费群限制：每用户最多 {max_per_user} 个盯盘项\n"
                f"💡 升级会员群可解锁：不限量盯盘 + 大盘指数盯盘")
    elif tier == "vip":
        head = "👑 会员群盯盘（不限量）"
        foot = "大盘指数盯盘：/关注 大盘 跌3% ｜ /关注 大盘 突破2000"
    else:
        head = "🔒 盯盘功能为会员群专属"
        foot = (f"如需在本群开通，请联系管理员加入白名单。\n"
                f"本群 openid：{group_openid or '未知'}（发给管理员即可写入配置）")
    usage = (
        "使用方式：\n"
        "/关注 饰品名 [目标价|涨跌X%|7日涨X%|7日跌X%|在售降X%]\n"
        "  例：/关注 蝴蝶刀 大马士革钢 4500\n"
        "      /关注 蝴蝶刀 大马士革钢 15%\n"
        "      /关注 蝴蝶刀 大马士革钢 7日跌5%\n"
        "      /关注 蝴蝶刀 大马士革钢 在售降30%\n"
        "/取消关注 饰品名   ｜   /我的关注 查看列表"
    )
    return f"{head}\n{usage}\n{foot}"


def format_watch_limit_reached(max_per_user: int) -> str:
    return (f"⚠️ 你已达免费群上限：每用户最多 {max_per_user} 个盯盘项。\n"
            f"先 /取消关注 一个，或升级会员群解锁不限量盯盘。")


def format_index_watch_vip_only() -> str:
    return ("📈 大盘指数盯盘为会员群专属功能。\n"
            "免费群暂不支持，升级会员群即可使用（/关注 大盘 跌3% 等）。")


# ----------------------------------------------------------------------
# 每日市场播报
# ----------------------------------------------------------------------

# 内置每日一言（en/zh），按日期轮换
_DAILY_QUOTES = [
    ("Rain awakens the seeds sleeping in your heart.", "雨声唤醒你心中沉睡的种子。"),
    ("Fortune favors the prepared mind.", "机会偏爱有准备的人。"),
    ("Patience is a trader's best armor.", "耐心是交易员最好的铠甲。"),
    ("Buy when others are fearful.", "别人恐惧时我贪婪。"),
    ("Trends are your friends, until they end.", "趋势是你的朋友，直到它结束。"),
    ("Know what you hold, and why.", "清楚你持有的是什么，以及为什么。"),
    ("A stop-loss saves a portfolio.", "一次止损，保全整个账户。"),
    ("The market always pays for discipline.", "市场总会奖励纪律。"),
    ("Don't chase pumps, ride waves.", "别追高，顺势而为。"),
    ("Liquidity is the king.", "流动性为王。"),
]

# 播报展示的分类指数（按网站顺序）
_REPORT_INDEXES = [
    "饰品指数", "租赁指数", "百元主战", "探员指数", "原皮指数",
    "红皮指数", "千战指数", "武库指数", "贴纸指数", "匕首指数",
    "手套指数", "挂件指数", "收藏品",
]

# V0.3.17 原为「盯盘重点 3 个」(三代手套/多普勒/伽玛多普勒);
# V0.3.18(老大 2026-10-14 看完测试群实发后改口):分类指数**全量铺开**——
# 基础 13 个保持原顺序在前,回包里其余的按接口顺序接排(2026-10-14 实测共 23 个)。
# 规则不写死名字:CSQAQ 以后新增分类会自动带上,不用再改代码。


def _ratio_tag(up: int, down: int) -> str:
    """涨跌家数的方向标签(偏多/偏空/均衡)。

    阈值放宽到 1.2/0.8(异动推送里用的是 1.5 倍口径):这里只是一句话概括,
    太灵敏会天天翻转,反而看不清趋势。
    """
    if not down:
        return "偏多" if up else "--"
    r = up / down
    return "偏多" if r > 1.2 else ("偏空" if r < 0.8 else "均衡")


def _rate_line(rate: Dict[str, Any]) -> str:
    """V0.3.17 多窗口涨跌分布行(1/7/30 日);无数据返回空串。

    口径(2026-10-14 用真实回包核实):各窗口三态之和**恒等**(实侧 26,090)=
    同一批商品在不同时间窗下的涨跌家数,不是累计量 → 窗口之间可直接比。
    旧文案只给 1 天,看不出「今日跌」是急跌还是中期下行的一部分。
    """
    def _i(v: Any) -> Optional[int]:
        if v is None:
            return None
        try:
            return int(v)
        except (TypeError, ValueError):
            return None

    u1, d1 = _i(rate.get("count_positive_1")), _i(rate.get("count_negative_1"))
    if u1 is None and d1 is None:
        return ""
    u1, d1 = u1 or 0, d1 or 0
    if not (u1 or d1 or (_i(rate.get("count_zero_1")) or 0)):
        return ""
    parts = [f"今 {u1}/{d1} {_ratio_tag(u1, d1)}"]
    for w in (7, 30):
        uw, dw = _i(rate.get(f"count_positive_{w}")), _i(rate.get(f"count_negative_{w}"))
        if uw is None or dw is None:
            continue
        parts.append(f"{w}日 {uw}/{dw} {_ratio_tag(uw, dw)}")
    return "📊 涨跌分布(涨/跌)：" + " ｜ ".join(parts)


def _report_index_names(subs_by_name: Dict[str, Any]) -> List[str]:
    """卡片渲染哪些分类指数:基础 13 个(原顺序在前) + 回包里其余全部(V0.3.18)。

    不写死名单 → CSQAQ 以后新增分类自动带上,不用改代码;回包缺某类时它压根不在
    subs_by_name 里,也就不会渲染成空行。subs_by_name 由调用方按回包顺序构造(dict 保插入序),
    所以附加部分与接口顺序一致。名字为空/缺 name 的脏项直接跳过(否则会渲染出 "None")。
    """
    return list(_REPORT_INDEXES) + [n for n in subs_by_name
                                    if isinstance(n, str) and n.strip()
                                    and n not in _REPORT_INDEXES]


def _pick_quote() -> Dict[str, str]:
    import datetime
    idx = datetime.date.today().toordinal() % len(_DAILY_QUOTES)
    en, zh = _DAILY_QUOTES[idx]
    return {"en": en, "zh": zh}


def _fmt_arrow(v: float) -> str:
    return "🔴" if v >= 0 else "🟢"


def format_daily_report(data: Dict[str, Any], broad_index: Optional[Dict[str, Any]] = None) -> str:
    """生成「CS 饰品市场·每日播报」文本。

    data: CSQAQ current_data 接口返回的完整 JSON(分类指数/在线/卡价等仍来自 CSQAQ)。
    broad_index: 可选,SteamDT 大盘指数归一化结果;提供时头条「饰品指数/今日变动」改用
        SteamDT(两源算法不同,不混比),并在来源行标注双源。
    """
    d = data.get("data") or {}
    subs = d.get("sub_index_data") or []
    if not subs:
        return "机器人维护中，请稍后再试。"
    subs_by_name = {s.get("name"): s for s in subs}
    main = subs_by_name.get("饰品指数") or subs[0]

    lines = ["📊 CS饰品市场·每日播报"]
    lines.append("━━━━━━━━━━━━━━")
    if broad_index:
        # 大盘指数以 SteamDT 为准(与 CSQAQ 算法/权重不同,数值不可直接比,故不混比)
        lines.append(f"📍 饰品指数(SteamDT)：{broad_index['index']:,.2f} {_fmt_arrow(broad_index['diff_ratio_pct'])}")
        lines.append(f"📈 今日变动(SteamDT)：{broad_index['diff_yesterday']:+.2f} ({broad_index['diff_ratio_pct']:+.2f}%)")
    else:
        lines.append(f"📍 饰品指数：{main.get('market_index', 0):,.2f} {_fmt_arrow(main.get('chg_rate', 0))}")
        lines.append(f"📈 今日变动：{main.get('chg_num', 0):+.2f} ({main.get('chg_rate', 0):+.2f}%)")
    lines.append(f"⚖️ 开盘：{main.get('open', 0):,.2f} | 收盘：{main.get('close', 0):,.2f}")
    lines.append(f"🔺 最高：{main.get('high', 0):,.2f} | 🔻 最低：{main.get('low', 0):,.2f}")

    onl = d.get("online_number") or {}
    if onl:
        lines.append("----------------------------")
        lines.append(f"👥 在线人数：{onl.get('current_number', 0):,}")
        lines.append(f"🔥 今日峰值：{onl.get('today_peak', 0):,}")
        lines.append(f"📅 本月峰值：{onl.get('month_peak', 0):,}")

    cards = d.get("card_price") or []
    if cards:
        latest = cards[-1]
        lines.append("----------------------------")
        lines.append(f"💳 Steam美金卡价：{latest.get('card_price', 0):.1f} 元")

    rate = d.get("rate_data") or {}
    rate_line = _rate_line(rate)   # V0.3.17:1/7/30 日多窗口(与 /大盘 全景段同款)
    if rate_line:
        lines.append(rate_line)

    gs = d.get("greedy_status") or {}
    if gs.get("label"):
        lines.append(f"🔥 市场热度：{gs.get('label')}")

    lines.append("━━━━━━━━━━━━━━")
    lines.append("📋 饰品分类指数")
    for i, name in enumerate(_report_index_names(subs_by_name), 1):
        s = subs_by_name.get(name)
        if s:
            lines.append(
                f"{i}. {name}: {s.get('market_index', 0):,.2f} "
                f"({s.get('chg_rate', 0):+.2f}%) {_fmt_arrow(s.get('chg_rate', 0))}"
            )

    q = _pick_quote()
    lines.append("----------------------------")
    lines.append("📜 每日一言：")
    lines.append(f"「{q['en']}」")
    lines.append(f"— {q['zh']}")

    updated = str(main.get("updated_at") or "")
    lines.append("━━━━━━━━━━━━━━")
    lines.append(f"🕒 数据时间：{updated[:16]}")
    if broad_index:
        lines.append("数据来源：饰品指数 SteamDT.COM ｜ 分类/在线/卡价 CSQAQ.COM ｜ 仅供参考")
    else:
        lines.append("数据来源：CSQAQ.COM ｜ 仅供参考，投资有风险")
    return "\n".join(lines)
