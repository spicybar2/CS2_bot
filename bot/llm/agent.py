# -*- coding: utf-8 -*-
"""CS2 饰品行情 AI 智能体。

自然语言 -> LLM 理解意图 -> 调用工具查真实数据(CSQAQ/SQLite) -> LLM 组织自然语言回复。
工具复用现有模块:SnapshotStore / Recommender / CSQAQClient / commands 格式化。
"""

import asyncio
import json
import logging
import time
from collections import deque
from typing import Any, Deque, Dict, List, Optional

from bot.analyzer.recommender import Recommender
from bot.config import AppConfig
from bot.csqaq.client import CSQAQClient
from bot.csqaq.price_lookup import answer_price, ensure_default_wear, fetch_live, format_price_card
from bot.csqaq.resolver import resolve_prices
from bot.csqaq.skins import zh_to_en_candidates
from bot.llm.client import LLMClient
from bot.qqbot import commands as cmds
from bot.steamdt.client import SteamDTClient
from bot.storage.db import SnapshotStore

logger = logging.getLogger("agent")

SYSTEM_PROMPT = """你是 CS2 饰品市场行情助手,在 CS2 玩家 QQ 群里回答饰品价格、涨跌幅、选品推荐、市场播报等问题。
风格:
- 说话尖锐毒辣、犀利直接,像个懂行的老 CS 玩家,敢吐槽敢调侃,语气带点刻薄和幽默,别端着,别用敬语。
- 可以毒舌(比如价格离谱就直说"这价谁买谁冤种",数据拉胯就吐槽),但禁止人身攻击、禁止脏话;数据必须真实,不能为了毒舌编数字。
规则:
1. 所有价格、涨跌幅、排名数据必须通过调用工具获取真实数据,禁止凭记忆编造任何数字。
2. 回复要口语化,直接给出答案本身,不要输出 Markdown 表格,控制在 250 字以内。
3. 禁止向用户展示任何接口名、工具名、斜杠命令、策略名或参数名(如 search_price、/推荐、low_7d、7天热点 等),也不要解释"我调用了什么工具"。工具返回的内容只是你的数据来源,不能直接复述工具输出的原文。
   - 纯查价类问题(用户明确只要价格,如「XX多少钱」「XX价格多少」「XX什么价」「XX报价」「查一下XX」):把 search_price 工具返回的每一行价格数据**原样**呈现给用户,不要改写成"现在的价格是…BUFF售,Steam售价…"这种带前缀的绕话,也不要只甩一个数字。每行必须列出所有平台价格:参考价、BUFF售/求购、Steam售、悠悠有品售、在售件数、7日/今日涨跌(工具给到什么就贴什么,没给的不编)。直接贴价格即可。
   - 搭配/咨询/推荐类问题(如「XX配什么手套」「XX怎么看」「推荐个XX」):必须用自己的话重新组织转述,尤其禁止直接粘贴「| 参考价 ¥xxx | BUFF售 ¥xxx」这种工具行格式,要总结成自然语言价格区间(如"专业手套最低 ¥2.6 万起、运动手套 ¥7.9 万起")。
4. 查询不到或数据为空时,直接说「没查到」,不要推测原因(如太稀有、冷门、市场如何),不要编造市场判断;可以吐槽数据不全,但别用客服腔,别假装有数据。
5. 用户指定了饰品类型或名称(如 蝴蝶刀、AK-47、爪子刀、手套)时,推荐/查询结果必须与该类型匹配;没有匹配就如实说,别拿其他类型充数,建议替代品时也要同类(如刀皮查不到就建议其他刀皮,不要推枪皮)。
6. 涉及选品推荐时,末尾提示「数据仅供参考,投资有风险」。
7. 用户可以追问,记住当前对话上下文。
8. 你回答 CS2 饰品相关的一切问题,分两类处理:
   - 硬行情类(价格/涨跌/排名/推荐/播报):必须用工具查真实数据,禁止编造数字(规则1)。
   - 经验/科普/搭配咨询类(如「科隆贴纸怎么看」「XX皮肤配什么手套好」「磨损区间怎么选」「什么箱子值开」):涉及具体饰品名称时,优先调用 search_price 拉取该饰品/CSQAQ 真实行情(当前价、近期涨跌)来辅助回答,结尾标注「(实时行情来自 CSQAQ)」;纯属审美/搭配原则(不涉及具体单品价格,如「什么手套百搭」)可只用常识给配色/风格建议,结尾标注「(经验之谈,非实时数据)」;查不到数据时老实说没查到,别编。
   - 与 CS2 饰品完全无关的话题(编程、新闻、天气、闲聊等)仍直接拒绝,提示「只聊饰品行情」,可带点毒舌。
9. 用户问"怎么看/配什么/值不值/好不好"等咨询类问题时,先想清楚是否涉及具体饰品;涉及具体单品(如「血腥运动」「科隆2014」)就先调 search_price 拿真实行情再给判断,纯原则类(如「什么手套百搭」)才用常识直接答,别绕回"请查价格"的客服腔。
10. 严禁编造任何饰品/手套/皮肤的具体名称、英文名或款式代号(如不能凭空说「死亡之舞」「夜魔」这类具体皮肤名)。举例只能用通用大类(如「专业手套」「运动手套」「红黑配色」),且必须说明「具体款式以实际查价为准」;不确定具体名字时宁可不说,绝不杜撰。
11. 搭配类问题(如「XX皮肤配什么手套好」「XX枪配什么手套」):必须同时调用多次 search_price 拉真实数据——先查该皮肤/枪本身(如 keyword="血腥运动"),再分别查几类主流手套大类(keyword 依次用 "专业手套"、"运动手套"、"摩托手套"、"驾驶手套" 等通用类名,不要拼组合词),把各自的真实在售价/求购价/近期涨跌都拿到。然后基于"真实价格区间 + 配色/风格常识"给建议(例如"血腥运动红黑配色,搭专业手套或运动手套都不违和,专业手套最低 ¥X 起、运动手套 ¥Y 起,预算够就上"),严禁只凭记忆给具体款式名。若某类手套查不到数据,老实说没查到,别编。"""

# OpenAI function calling 工具 schema
TOOLS: List[Dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "search_price",
            "description": "按饰品名关键词查询 CS2 饰品当前价格与 7 日/今日涨跌幅(本地快照,支持模糊匹配)。可指定价格区间过滤",
            "parameters": {
                "type": "object",
                "properties": {
                    "keyword": {"type": "string", "description": "饰品名关键词,如 爪子刀 或 蝴蝶刀 渐变之色"},
                    "min_price": {"type": "number", "description": "可选。价格下限(元),如用户说7000左右则传 6500"},
                    "max_price": {"type": "number", "description": "可选。价格上限(元),如用户说7000左右则传 7500"},
                    "limit": {"type": "integer", "description": "返回条数,默认 3"},
                },
                "required": ["keyword"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "recommend",
            "description": "获取选品推荐。策略:low_7d=7天低位, dip_7d=7天超跌, hot_7d=7天热点, liquid=好出手, day_move=今日异动。可选 keyword 按饰品名/类型过滤(如 蝴蝶刀、匕首、AK-47、手套),仅返回名称包含该词的条目",
            "parameters": {
                "type": "object",
                "properties": {
                    "strategy": {"type": "string", "enum": ["low_7d", "dip_7d", "hot_7d", "liquid", "day_move"]},
                    "keyword": {"type": "string", "description": "可选。返回结果的名称必须包含该关键词(如 蝴蝶刀)。用户指定了类型时必须传"},
                },
                "required": ["strategy"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "daily_report",
            "description": "获取 CS 饰品市场每日播报(大盘指数/涨跌分布/在线人数/分类指数/卡价)",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "rank",
            "description": "获取 7 日涨跌幅榜单(up=涨幅榜, down=跌幅榜)",
            "parameters": {
                "type": "object",
                "properties": {
                    "direction": {"type": "string", "enum": ["up", "down"]},
                },
                "required": ["direction"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "status",
            "description": "查看数据采集状态(最近快照时间/饰品样本数/采集周期)",
            "parameters": {"type": "object", "properties": {}},
        },
    },
]

_MAX_TOOL_RESULT = 1500  # 单次工具结果回传给 LLM 的最大字符数
_MAX_ROUNDS = 5          # 单条消息最多对话轮数(搭配类可能调3次search_price+1轮转述,需留足)

# 内部术语泄漏黑名单:AI 回复中出现这些词说明把工具输出原样复述了,直接兜底替换
_LEAK_PATTERNS = [
    "search_price", "daily_report", "recommend", "status", "rank",
    "low_7d", "dip_7d", "hot_7d", "day_move", "liquid",
    "策略:", "策略：", "接口", "工具调用", "function calling", "参数",
    "keyword=", "无匹配:",
]
_LEAK_FALLBACK = "抱歉,我暂时没找到合适的信息,换个问法试试?比如直接说饰品名「蝴蝶刀多少钱」,或问「有什么推荐」~"

# 话题门控:改为「黑名单」模式 —— 默认放行所有消息给 LLM 自行判断,
# 仅当命中明显与 CS2 饰品无关的「强无关词」时才直接拒绝,不调 LLM。
# 这样「科隆贴纸怎么看」「XX配什么手套好」等经验/科普/搭配咨询能稳过,
# 不会因换个说法没命中关键词而被误杀(旧逻辑是白名单,容易漏)。
OFF_TOPIC_HARD = [
    "写代码", "编程", "python", "java", "bug", "报错", "天气", "今天几度",
    "新闻", "股票", "基金", "比特币", "吃饭", "中午吃", "下午茶", "睡觉",
    "作业", "考试", "数学", "英语", "物理", "语文", "公司", "工资", "老板",
    "放假", "旅游", "攻略", "怎么去", "几点了", "现在几点", "你是谁", "你多大了",
    "讲个笑话", "讲个故事", "唱首歌", "翻译", "帮我写", "帮我做",
]
OFF_TOPIC_FALLBACK = "打住打住,我是 CS2 饰品行情机器人,只聊饰品价格、涨跌、推荐、搭配这些,别的话题我可不接话。想查啥直接报饰品名,比如「蝴蝶刀多少钱」。"


class Session:
    """单个会话(按群/用户)的短期上下文。"""

    __slots__ = ("history", "updated")

    def __init__(self):
        self.history: Deque[Dict[str, str]] = deque(maxlen=20)
        self.updated: float = time.time()


class CS2Agent:
    """CS2 行情智能体:意图理解 + 工具调用 + 自然语言回复。"""

    def __init__(self, cfg: AppConfig, store: SnapshotStore,
                 recommender: Recommender, llm: Optional[LLMClient] = None,
                 steamdt: Optional[SteamDTClient] = None):
        self._cfg = cfg
        self._store = store
        self._rec = recommender
        self._llm = llm or LLMClient(cfg.llm)
        # 共享的 SteamDT 大盘客户端(与 main.py 同一实例,复用其缓存与限频退避状态,
        # 绝不能每调用新建,否则退避状态丢失→重复打网络→白烧免费 Key 额度)
        self._steamdt = steamdt
        self._sessions: Dict[str, Session] = {}
        self._lock = asyncio.Lock()
        # 纯查价/查价工具产生的走势图路径,按会话 key 暂存,由 bot 层取出发送图片
        self._pending_charts: Dict[str, str] = {}

    @property
    def enabled(self) -> bool:
        return self._llm.enabled

    async def close(self):
        await self._llm.close()

    def pop_chart(self, session_key: str) -> Optional[str]:
        """取出并清空该会话暂存的走势图路径(线程/协程安全:按 session_key 隔离)。"""
        return self._pending_charts.pop(session_key, None)

    # ------------------------------------------------------------------
    # 会话管理
    # ------------------------------------------------------------------

    def _session_for(self, key: str) -> Session:
        now = time.time()
        s = self._sessions.get(key)
        if s is None or now - s.updated > self._cfg.llm.history_ttl:
            s = Session()
            self._sessions[key] = s
        s.updated = now
        if len(self._sessions) > 200:  # 防止内存无限增长,简单清一轮
            for k in list(self._sessions):
                if now - self._sessions[k].updated > self._cfg.llm.history_ttl:
                    self._sessions.pop(k, None)
        return s

    # ------------------------------------------------------------------
    # 对外入口
    # ------------------------------------------------------------------

    async def handle(self, text: str, session_key: str = "default") -> str:
        """处理一条自然语言消息,返回回复文本。"""
        self._last_session_key = session_key
        # 话题门控:与 CS2 饰品行情无关的话题直接拒绝,不调 LLM
        if self._is_off_topic(text, session_key):
            return OFF_TOPIC_FALLBACK
        # 纯查价:固定格式直答,完全绕开 LLM,杜绝自然语言夹带(用户硬性要求)
        if self._is_pure_price_query(text):
            sess = self._session_for(session_key)
            sess.history.append({"role": "user", "content": text})
            try:
                async with CSQAQClient(self._cfg.csqaq) as api:
                    reply, chart = await answer_price(api, self._store, text, steamdt=self._steamdt)
            except Exception as exc:
                logger.exception("[agent] 纯查价失败: %s", exc)
                reply, chart = "机器人维护中，请稍后再试", None
            if chart:
                self._pending_charts[session_key] = chart
            sess.history.append({"role": "assistant", "content": reply})
            return reply
        sess = self._session_for(session_key)
        messages: List[Dict[str, str]] = [{"role": "system", "content": SYSTEM_PROMPT}]
        messages.extend(list(sess.history)[-self._cfg.llm.history_len:])
        messages.append({"role": "user", "content": text})
        sess.history.append({"role": "user", "content": text})

        try:
            reply = await self._run_loop(messages, allow_raw=self._is_pure_price_query(text))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception("AI 处理失败: %s", exc)
            reply = "AI 暂时开小差了,试试 /推荐 或 /播报 查看行情 📡"
        reply = self._sanitize_reply(reply, allow_raw=self._is_pure_price_query(text))
        sess.history.append({"role": "assistant", "content": reply})
        return reply

    @staticmethod
    def _is_off_topic(text: str, session_key: str) -> bool:
        """判断消息是否与 CS2 饰品明显无关(黑名单模式)。

        - 命中强无关词 -> 无关(直接拒绝,不调 LLM)
        - 短消息(<=6 字,可能是指代追问,如「那它呢」) -> 放行
        - 其余(含所有饰品咨询/经验/搭配类) -> 放行,交给 LLM 自行判断
        """
        if not text:
            return False
        if any(k in text for k in OFF_TOPIC_HARD):
            return True
        if len(text.strip()) <= 3:  # 短指代(「那它呢」「这个呢」)放行
            return False
        return False

    # 纯查价意图关键词:命中即视为"用户只要价格数字",允许 AI 直接裸贴价格行
    _PRICE_QUERY_HINTS = [
        "多少钱", "价格多少", "什么价", "报价", "售价", "多少钱啊", "啥价",
        "多少钱一个", "价格", "查价", "查一下", "多少钱呀", "现在多少钱",
        "现价", "市价", "值多少钱", "多少钱收", "多少价位",
    ]

    @staticmethod
    def _is_pure_price_query(text: str) -> bool:
        """判断是否为纯查价意图:用户明确只要价格数字,无需搭配/推荐等衍生建议。

        命中价格关键词即视为纯查价(允许 AI 直接裸贴价格数据行);
        含搭配/咨询类信号(配/搭/怎么看/推荐/值不值)则不是纯查价。
        """
        if not text:
            return False
        has_price_hint = any(k in text for k in CS2Agent._PRICE_QUERY_HINTS)
        has_advice_hint = any(k in text for k in ("配", "搭", "怎么看", "推荐", "值不值", "好不好", "值得", "入手", "买不买"))
        # 同时含价格词+搭配词(如"血腥运动配什么手套多少钱")按搭配类处理,需转述
        if has_price_hint and not has_advice_hint:
            return True
        return False

    @staticmethod
    def _sanitize_reply(text: str, allow_raw: bool = False) -> str:
        """兜底:若 AI 把工具/策略等内部术语泄漏给用户,替换为自然文案。

        注:裸奔粘贴工具数据格式(「| 参考价」「BUFF售」)已由 _run_loop 的
        "转述补刀"逻辑处理(追加一轮让模型转述),此处不再硬替换,避免双重处理。
        allow_raw=True 时(纯查价意图)跳过裸奔格式拦截,仅拦内部术语泄漏。
        """
        if not text:
            return text
        for p in _LEAK_PATTERNS:
            if p in text:
                logger.warning("[agent] 回复泄漏内部术语(%s),已兜底: %s", p, text[:80])
                return _LEAK_FALLBACK
        # 纯查价意图允许直接裸贴价格行,不做格式拦截
        if allow_raw:
            return text
        return text

    async def _run_loop(self, messages: List[Dict[str, Any]], allow_raw: bool = False) -> str:
        """与 LLM 多轮对话:直到无工具调用或轮次用尽。"""
        for _ in range(_MAX_ROUNDS):
            resp = await self._llm.chat(messages, tools=TOOLS)
            choices = resp.get("choices") or []
            if not choices:
                return "AI 返回异常,请稍后再试。"
            msg = choices[0].get("message") or {}
            content = msg.get("content") or ""
            tool_calls = msg.get("tool_calls") or []
            if not tool_calls:
                # 最终回复若仍是裸奔粘贴的工具数据格式,且非纯查价意图,追加一轮"转述指令"让模型用自然语言总结
                if (not allow_raw) and ("| 参考价" in content or ("BUFF售" in content and "|" in content)):
                    logger.warning("[agent] 检测到裸奔工具数据,追加转述轮")
                    messages.append({"role": "user", "content":
                        "你刚才直接粘贴了工具返回的原始数据行,这是不允许的。请用自然语言总结以上所有数据,"
                        "直接给用户的搭配建议和真实价格区间(如「专业手套最低 ¥2.6 万起、运动手套 ¥7.9 万起」),"
                        "禁止出现「| 参考价」「BUFF售」这类格式行。"})
                    continue
                return content

            # 记录 assistant 的工具调用,并把结果逐个回传
            messages.append({
                "role": "assistant",
                "content": content or None,
                "tool_calls": tool_calls,
            })
            for tc in tool_calls:
                fn = tc.get("function") or {}
                name = fn.get("name") or ""
                try:
                    args = json.loads(fn.get("arguments") or "{}")
                except json.JSONDecodeError:
                    args = {}
                result = await self._exec_tool(name, args)
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.get("id") or "",
                    "content": result,
                })
        # 轮次用尽兜底:取最后一条消息文本
        for m in reversed(messages):
            if m.get("role") in ("assistant", "tool") and m.get("content"):
                return str(m["content"])[:800]
        return "处理超时,请再问一次。"

    # ------------------------------------------------------------------
    # 工具执行(全部复用现有模块)
    # ------------------------------------------------------------------

    async def _exec_tool(self, name: str, args: Dict[str, Any]) -> str:
        try:
            if name == "search_price":
                return await self._tool_search_price(args, session_key=self._last_session_key)
            if name == "recommend":
                return await self._tool_recommend(args)
            if name == "daily_report":
                return await self._tool_daily_report()
            if name == "rank":
                return await self._tool_rank(args)
            if name == "status":
                return await self._tool_status()
            return f"未知工具: {name}"
        except Exception as exc:
            logger.exception("[agent] 工具 %s 执行失败: %s", name, exc)
            return f"工具 {name} 执行失败: {exc}"

    async def _tool_search_price(self, args: Dict[str, Any], session_key: str = "default") -> str:
        """实时查价工具(不走本地快照):词典/索引解析英文名 -> get_good_info 全字段。

        未指定磨损时默认崭新出厂;返回全平台价格卡片(BUFF/UU/Steam/挂刀率/近7天)。
        若生成了走势图,按会话 key 暂存,由 bot 层作为图片发送。
        """
        keyword = str(args.get("keyword") or "").strip()
        if not keyword:
            return "缺少查询关键词"
        kw = ensure_default_wear(keyword)
        try:
            async with CSQAQClient(self._cfg.csqaq) as api:
                data = await fetch_live(api, self._store, kw)
        except Exception as exc:
            logger.warning("[agent] 实时查价「%s」失败: %s", keyword, exc)
            return f"机器人维护中，请稍后再试"
        if not data:
            return f"CSQAQ 实时查询未找到「{keyword}」,确认下名称,或到 csqaq.com 网页搜索"
        card = format_price_card(data)
        trend = ""
        try:
            from bot.csqaq.price_lookup import render_trend
            good_id = int(data.get("goodId") or data.get("good_id") or 0)
            name = (data.get("name") or data.get("_market_hash_name") or "")
            current = float(data.get("buffSellPrice") or data.get("buff_sell_price") or 0)
            chart_path, trend = await render_trend(self._store, good_id, name, current, data, steamdt=self._steamdt)
            if chart_path:
                self._pending_charts[session_key] = chart_path
        except Exception:
            trend = ""
        return card + ("\n" + trend if trend else "")

    async def _live_search(self, keyword: str) -> str:
        """实时兜底:词典候选 -> 全量索引(中文名匹配+详情取英文名) -> 查价。

        覆盖 CSQAQ 收录的全部饰品,不依赖词典覆盖。
        """
        try:
            async with CSQAQClient(self._cfg.csqaq) as api:
                result = await resolve_prices(api, self._store, keyword)
        except Exception as exc:
            logger.warning("[agent] 实时查询「%s」失败: %s", keyword, exc)
            return f"本地快照暂未收录「{keyword}」,实时查询失败(可能数据源IP白名单未绑定,可到 csqaq.com 绑定后重试)"
        if not result:
            return f"CSQAQ 实时查询未找到「{keyword}」,确认下名称是不是记错了,或到 csqaq.com 网页搜索"
        lines = []
        for mhn, row in list(result.items())[:5]:
            name = row.get("name") or mhn
            buff_sell = float(row.get("buffSellPrice") or 0)
            buff_buy = float(row.get("buffBuyPrice") or 0)
            steam_sell = float(row.get("steamSellPrice") or 0)
            yyyp = float(row.get("yyypSellPrice") or row.get("yyyp_sell_price") or 0)
            buff_sell_num = int(row.get("buffSellNum") or 0)
            lines.append(
                f"{name}\n"
                f"  BUFF售 ¥{buff_sell:,.2f} 求购 ¥{buff_buy:,.2f} 在售{buff_sell_num}件"
                f"{f' | Steam售 ¥{steam_sell:,.2f}' if steam_sell else ''}"
                f"{f' | 悠悠有品 ¥{yyyp:,.2f}' if yyyp else ''}"
            )
        return "\n".join(lines)[:_MAX_TOOL_RESULT]

    async def _tool_recommend(self, args: Dict[str, Any]) -> str:
        strategy = str(args.get("strategy") or "low_7d")
        keyword = str(args.get("keyword") or "").strip()
        rows = await self._store.async_query_latest(limit=2000)
        if not rows:
            return "数据采集中,请 5 分钟后再试"
        result = self._rec.recommend(rows, strategy)
        items = result.items[:10]
        if keyword:
            items = [it for it in items if keyword in it.name]
            if not items:
                # 纯结构化数据,避免 AI 复述出内部术语;是否换查价方式由 LLM 自行决定
                return f"无匹配:策略={result.strategy_label},关键词={keyword},推荐列表为空"
        if not items:
            return "该策略暂无符合条件的推荐"
        lines = [f"策略: {result.strategy_label}"]
        for i, it in enumerate(items[:5], 1):
            lines.append(it.format_line(i))
        return "\n".join(lines)[:_MAX_TOOL_RESULT]

    async def _tool_daily_report(self) -> str:
        # 统一为 SteamDT 大盘卡 + CSQAQ 市场全景(与 /大盘 /播报 一致,单源不混比)
        if self._steamdt is None:
            return "当前大盘数据源未配置(SteamDT)"
        try:
            broad = await self._steamdt.get_broad_index()
        except Exception as exc:
            logger.warning("[agent] 播报 SteamDT 大盘失败: %s", exc)
            return "机器人维护中，请稍后再试"
        # CSQAQ 市场全景(标注来源,失败不影响大盘卡)
        csqaq_data = None
        try:
            async with CSQAQClient(self._cfg.csqaq) as api:
                csqaq_data = await api.get_current_data(type="init")
        except Exception as exc:
            logger.warning("[agent] 播报 CSQAQ 全景失败: %s", exc)
        text = cmds.format_broad_full(broad, csqaq_data)
        # 走势图(与手动/定时一致,暂存由 bot 层发出)
        hist = broad.get("history") or []
        if len(hist) >= 2:
            try:
                import datetime
                from bot.csqaq.chart import make_trend_png
                prices = [v for _, v in hist]
                labels = [datetime.datetime.fromtimestamp(ts).strftime("%m-%d %H") for ts, _ in hist]
                chart = make_trend_png(prices, "CS2 大盘指数", prices[-1], labels=labels)
                if chart:
                    self._pending_charts[self._last_session_key] = chart
            except Exception:
                pass
        return text[:_MAX_TOOL_RESULT]

    async def _tool_rank(self, args: Dict[str, Any]) -> str:
        # 与 /排行 /推荐 统一:实时官方榜 + format_recommend
        direction = "down" if str(args.get("direction") or "") == "down" else "up"
        strategy = "dip_7d" if direction == "down" else "hot_7d"
        try:
            async with CSQAQClient(self._cfg.csqaq) as api:
                result = await self._rec.recommend_realtime(api, strategy)
        except Exception as exc:
            logger.exception("[agent] 实时排行失败: %s", exc)
            return "机器人维护中，请稍后再试"
        return cmds.format_recommend(result, None)[:_MAX_TOOL_RESULT]

    async def _tool_status(self) -> str:
        fetch_time = await self._store.async_last_fetch_time()
        latest = await self._store.async_query_latest(limit=1)
        return cmds.format_status(fetch_time, len(latest) if fetch_time else 0)
