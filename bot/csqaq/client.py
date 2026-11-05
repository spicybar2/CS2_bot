# -*- coding: utf-8 -*-
"""CSQAQ 数据开放 API 客户端。

官方文档: https://docs.csqaq.com
注册获取 ApiToken: https://csqaq.com/home (登录后点头像 -> 查看 ApiToken)
注意: 需在 CSQAQ 后台将本机/服务器出口 IP 加入白名单。
"""

import asyncio
import json
import logging
import os
import re
import socket
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlsplit

import aiohttp

from bot.config import CSQAQConfig

logger = logging.getLogger("bot.csqaq")

_IP_REBIND_LOCK: Optional[asyncio.Lock] = None
_IP_REBIND_UNTIL: float = 0.0
_IP_DRIFT_UNTIL: float = 0.0
_IP_LAST_DRIFT_IP: Optional[str] = None
_IP_REBIND_FAILS: int = 0          # 连续重绑失败次数(分级退避用,成功后清零)
_REBIND_SUCCESS_COOLDOWN: float = 120.0   # 成功后的常规冷却:防 30s 内连绑,又不拖慢下一次漂移修复
_TIMEOUT_REBIND_MIN_GAP: float = 300.0    # V0.3.19 35→300:钉 IP 后出口恒定,超时基本不再是"漂移",
                                          # 若还频繁超时说明是别的问题,不该每 35 秒重绑一次制造风暴
_TIMEOUT_REBIND_LAST: float = 0.0         # 上次"超时触发"的重绑**完成**时刻(单飞+限频用)
# V0.3.37:超时不再触发重绑,只累计次数;连续这么多次才判定"钉的节点真挂了"并换钉(不动白名单)。
_TIMEOUT_ROTATE_STREAK: int = 5
_TIMEOUT_STREAK: int = 0


def reset_timeout_streak() -> None:
    """请求成功一次就清零连续超时计数(V0.3.37)。"""
    global _TIMEOUT_STREAK
    _TIMEOUT_STREAK = 0


def timeout_state() -> Dict[str, Any]:
    return {"streak": _TIMEOUT_STREAK, "limit": _TIMEOUT_ROTATE_STREAK}


async def note_timeout(cfg: Optional["CSQAQConfig"], source: str = "") -> None:
    """V0.3.37:连续超时到阈值 → 换钉(只换服务器 IP,绝不改白名单)。

    为什么不再重绑:2026-11-04 复盘,出口已改成「按目标 IP 分流」,白名单(fixed IP)只对应
    钉住的那一条路,超时只是那个节点偶尔犯卡。11-03 16:00~11-04 10:00 的 15 次重绑全部
    来自超时,且 15 次都绑回同一个 IP —— 纯空转;更坏的情况是 bind 请求自己也要挑一条 WAN
    出去,万一走到别的出口,白名单就被改坏了(那才是真的制造 401)。
    """
    global _TIMEOUT_STREAK
    if cfg is None:
        return
    _TIMEOUT_STREAK += 1
    if _TIMEOUT_STREAK < _TIMEOUT_ROTATE_STREAK:
        return
    _TIMEOUT_STREAK = 0
    logger.warning("[csqaq.pin] 连续 %d 次超时(%s),判定钉的入口卡死,换钉",
                   _TIMEOUT_ROTATE_STREAK, source or "-")
    await note_pin_failure(cfg)


def _rebind_fail_backoff(cooldown_cap: float) -> float:
    """分级退避:失败第 1 次 120s,之后 240/480/600(cap)秒——连续失败自动放慢,给服务器风控解除留时间。"""
    return float(min(max(cooldown_cap, 120.0), 120.0 * (2 ** max(0, _IP_REBIND_FAILS - 1))))


def _ip_rebind_lock_obj() -> asyncio.Lock:
    global _IP_REBIND_LOCK
    if _IP_REBIND_LOCK is None:
        _IP_REBIND_LOCK = asyncio.Lock()
    return _IP_REBIND_LOCK


def _looks_like_ip_drift(body: str) -> bool:
    text = body or ""
    return (
        "环境IP" in text
        or "IP与账户所绑定" in text
        or "IP 不符" in text
        or "白名单" in text
    )


def _extract_env_ip(body: str) -> Optional[str]:
    m = re.search(r"环境IP[:：]\s*([\d.]+)", body or "")
    return m.group(1) if m else None


def _drift_pause_seconds(cfg: Optional[CSQAQConfig] = None) -> int:
    """V0.3.9:漂移期"暂停普通请求"的秒数。

    原来直接用 `rebind_cooldown_seconds`(600s)当暂停时长 —— 重绑一旦失败(如 bind 接口
    被 429),整机 **空转 10 分钟**(实测 15:29-15:59 就跳过了 17 次请求)。
    现在只暂停 120s:给单飞重绑留出完成时间,又不至于把采集停掉;
    重绑失败仍按 `rebind_cooldown_seconds` 做指数退避封顶,两者解耦。
    """
    return int(getattr(cfg, "drift_pause_seconds", 120) or 120)


def _mark_ip_drift(ip_hint: Optional[str], cooldown_seconds: int) -> None:
    global _IP_DRIFT_UNTIL, _IP_LAST_DRIFT_IP
    now = time.monotonic()
    _IP_DRIFT_UNTIL = max(_IP_DRIFT_UNTIL, now + float(cooldown_seconds))
    if ip_hint:
        _IP_LAST_DRIFT_IP = ip_hint


def _drift_cooldown_left() -> int:
    return max(0, int(_IP_DRIFT_UNTIL - time.monotonic()))


async def try_rebind_local_ip(
    cfg: CSQAQConfig,
    *,
    ip_hint: Optional[str] = None,
    source: str = "request",
    cooldown_seconds: int = 240,
    bypass_cooldown: bool = False,
) -> Tuple[bool, str]:
    """单飞重绑:同一漂移期只允许一个协程触发 bind_local_ip。

    冷却策略(V0.0.6 分级退避):
    - 成功 → 常规冷却 120s(防 30s 内连绑),失败计数清零——单次漂移快速自愈;
    - 失败 → 120s 起步、每次翻倍、封顶 cooldown_seconds(600):连续失败自动放慢,
      给服务器风控(429/挂起)解除留时间,避免把 bind 接口(官方 30s/次)打出死锁。

    V0.3.15 `bypass_cooldown`: **超时触发**的重绑可以越过冷却。
    理由(2026-10-13 实测):冷却的本意是防"漂移期频繁重绑被风控",但**超时**意味着
    连接已经被确认不可用(不是猜的),此时按 600 秒冷却压着不让重绑,等于把一次
    1 秒的抖动放大成整轮失败 —— 当天 **408 次** `超时后重绑未成(/info/get_rank_list): cooldown`,
    最终演变成 12:30–14:00 整整 90 分钟完全没采数据。
    绕过时仍受两道限制:①bind 接口自身 30s/次的限频窗口(下限取 35s,从**绑定完成**时刻
    起算,见函数末尾的 finally);②按连续失败次数逐级加长
    (35→70→140→280→300 封顶),所以不会形成重绑风暴。401 漂移路径**不变**,
    照旧尊重冷却(那是"确认是漂移",让退避起作用是对的)。
    """
    global _IP_REBIND_UNTIL, _IP_LAST_DRIFT_IP, _IP_DRIFT_UNTIL, _IP_REBIND_FAILS
    global _TIMEOUT_REBIND_LAST
    lock = _ip_rebind_lock_obj()
    if lock.locked():
        logger.debug(
            "source=csqaq event=ip_rebind_skip reason=inflight ip=%s source=%s",
            ip_hint or _IP_LAST_DRIFT_IP or "", source,
        )
        return False, "inflight"

    if bypass_cooldown:
        # V0.3.19:阶梯 300→600→1200→1800 秒(原来 35→70→140→280→300)。钉住服务器 IP 后出口恒定,
        # 超时基本不再是"漂移信号" → 不该每 35 秒重绑一次(那才是重绑风暴的来源)。
        gap = min(_TIMEOUT_REBIND_MIN_GAP * (2 ** max(0, _IP_REBIND_FAILS - 1)), 1800.0)
        since = time.monotonic() - _TIMEOUT_REBIND_LAST
        if _TIMEOUT_REBIND_LAST and since < gap:
            logger.debug(
                "source=csqaq event=ip_rebind_skip reason=timeout-gap wait=%.0fs "
                "ip=%s source=%s", gap - since, ip_hint or _IP_LAST_DRIFT_IP or "", source,
            )
            return False, f"timeout-gap({gap - since:.0f}s)"
        # 越过冷却:清掉锁让下面正常走(失败计数照旧累计 → 下次 gap 更长)
        _IP_REBIND_UNTIL = 0.0
    elif time.monotonic() < _IP_REBIND_UNTIL:
        logger.debug(
            "source=csqaq event=ip_rebind_skip reason=cooldown wait=%ss ip=%s source=%s",
            _drift_cooldown_left(), ip_hint or _IP_LAST_DRIFT_IP or "", source,
        )
        return False, "cooldown"

    async with lock:
        if time.monotonic() < _IP_REBIND_UNTIL:
            logger.debug(
                "source=csqaq event=ip_rebind_skip reason=cooldown wait=%ss ip=%s source=%s",
                _drift_cooldown_left(), ip_hint or _IP_LAST_DRIFT_IP or "", source,
            )
            return False, "cooldown"

        # V0.3.20:只有**真 401**(ip_hint 来自 401 响应体里的"环境IP")才算"漂移"、才进冷却期。
        # 超时触发的重绑(ip_hint 为空)不再标记漂移 —— 实测「近30天」两个排序会在服务端挂死 80 秒,
        # 每次超时都标记一次,一天下来几十次假冷却把榜单扫描整段跳过(2026-10-21 实测 27 次)。
        if ip_hint:
            _mark_ip_drift(ip_hint, _drift_pause_seconds(cfg))
        # V0.3.15:计时点放在**绑定完成后**(见下面 finally) —— 服务端的限频窗口
        # 「绑定IP频率限制为30s」是从**绑定成功那一刻**算的。上线实测抓到:
        # 16:07:01 发起、16:07:40 成功,16:07:53 再发起(距发起 52s 够了、距成功仅 13s)
        # → 服务端回「请求频率过快」白跑一趟。改成从完成时刻计时 + 下限 35s(>30s 窗口)。
        logger.warning(
            "source=csqaq event=ip_rebind_start ip=%s source=%s cooldown=%ss bypass=%s",
            ip_hint or _IP_LAST_DRIFT_IP or "", source, cooldown_seconds,
            "timeout" if bypass_cooldown else "no",
        )
        try:
            async with CSQAQClient(cfg) as api:
                bound, info = await api.bind_local_ip()
                if not bound:
                    _IP_REBIND_FAILS += 1
                    backoff = _rebind_fail_backoff(cooldown_seconds)
                    logger.warning(
                        "source=csqaq event=ip_rebind_failed ip=%s source=%s msg=%s "
                        "fails=%s next_retry_in=%.0fs",
                        ip_hint or _IP_LAST_DRIFT_IP or "", source, str(info)[:120],
                        _IP_REBIND_FAILS, backoff,
                    )
                    _IP_REBIND_UNTIL = time.monotonic() + backoff
                    return False, str(info)
                ok2, _ = await api.probe_ip_status(allow_drift_skip=False, allow_rebind=False)
            if ok2:
                _IP_DRIFT_UNTIL = 0.0
                _IP_REBIND_UNTIL = time.monotonic() + _REBIND_SUCCESS_COOLDOWN
                _IP_REBIND_FAILS = 0
                logger.info(
                    "source=csqaq event=ip_rebind_ok ip=%s source=%s msg=%s",
                    ip_hint or _IP_LAST_DRIFT_IP or "", source, str(info)[:120],
                )
                return True, str(info)
            logger.warning(
                "source=csqaq event=ip_rebind_probe_failed ip=%s source=%s msg=%s "
                "fails=%s next_retry_in=%.0fs",
                ip_hint or _IP_LAST_DRIFT_IP or "", source, str(info)[:120],
                _IP_REBIND_FAILS, _rebind_fail_backoff(cooldown_seconds),
            )
            _IP_REBIND_FAILS += 1
            _IP_REBIND_UNTIL = time.monotonic() + _rebind_fail_backoff(cooldown_seconds)
            return False, str(info)
        except Exception as exc:
            logger.exception(
                "source=csqaq event=ip_rebind_error ip=%s source=%s err=%s",
                ip_hint or _IP_LAST_DRIFT_IP or "", source, exc,
            )
            _IP_REBIND_FAILS += 1
            _IP_REBIND_UNTIL = time.monotonic() + _rebind_fail_backoff(cooldown_seconds)
            return False, str(exc)
        finally:
            # V0.3.15:从"绑定完成"时刻开始计下限(服务端 30s 窗口的起点)
            _TIMEOUT_REBIND_LAST = time.monotonic()

# 排行榜 filter 支持的排序选项（字符串与文档一致）
RANK_SORT_SELL_PRICE_DESC = "价格_售价_降序"
RANK_SORT_SELL_PRICE_ASC = "价格_售价_升序"
RANK_SORT_BUY_PRICE_DESC = "价格_求购价_降序"
RANK_SORT_BUY_PRICE_ASC = "价格_求购价_升序"

# 按涨跌幅排序（近7天/近30天，官方文档未全列，按命名规则推断；
# 若 fetch_loop 报"排序参数不合法"需以官方实际排序名替换）
RANK_SORT_DROP_7D = "价格_价格下降(百分比)_近7天"
RANK_SORT_RISE_7D = "价格_价格上升(百分比)_近7天"
RANK_SORT_DROP_30D = "价格_价格下降(百分比)_近30天"
RANK_SORT_RISE_30D = "价格_价格上升(百分比)_近30天"
RANK_SORT_DROP_1D = "价格_价格下降(百分比)_近1天"
RANK_SORT_RISE_1D = "价格_价格上升(百分比)_近1天"

# 常用磨损/类型筛选值（与文档一致）
EXTERIORS = ["崭新出厂", "略有磨损", "久经沙场", "破损不堪", "战痕累累"]


@dataclass
class RankItem:
    """排行榜返回的单条饰品行情数据。"""

    good_id: int
    name: str
    market_hash_name: str = ""   # 榜单若返回英文名则直接缓存,供 A/S 批量跟踪
    img: str = ""
    exterior: Optional[str] = None
    rarity: Optional[str] = None

    buff_sell_price: float = 0.0
    buff_buy_price: float = 0.0
    steam_sell_price: float = 0.0
    steam_buy_price: float = 0.0
    yyyp_sell_price: float = 0.0
    # V0.3.4: 悠悠求购价——榜单回包自带 yyyp_buy_price,与此前漏掉的 yyyp_buy_num 同类,
    # 双平台价格口径(全平台最高求购)需要它。
    yyyp_buy_price: float = 0.0

    buff_price_chg: float = 0.0          # BUFF 今日涨跌幅 %
    # V0.2.29:近1日涨跌幅。榜单回包若带 sell_price_rate_1 才有值;
    # 缺失时为 None(不得用 rate_7 冒充近1日,历史实现即因此把 7 日口径喂给了量价解算)。
    sell_price_rate_1: Optional[float] = None
    sell_price_rate_7: float = 0.0       # 7天涨跌幅 %
    sell_price_rate_30: float = 0.0      # 30天涨跌幅 %
    sell_price_rate_90: float = 0.0      # 90天涨跌幅 %
    sell_price_7: float = 0.0            # 7天前价格
    sell_price_30: float = 0.0           # 30天前价格

    buff_sell_num: int = 0               # BUFF 在售数量
    buff_buy_num: int = 0                # BUFF 求购数量
    steam_sell_num: int = 0              # Steam 在售数量
    steam_buy_num: int = 0               # Steam 求购数量
    yyyp_sell_num: int = 0               # 悠悠有品在售数量
    yyyp_buy_num: int = 0                # 悠悠有品求购数量(V0.2.24:榜单回包本来就有,此前未解析)

    sell_buy_diff_chg_buff: float = 0.0  # 售价减求购价变化（BUFF，%）

    # Steam 成交统计（单件详情接口提供；榜单接口通常为空）
    turnover_number: Optional[int] = None
    turnover_avg_price: Optional[float] = None
    period_at: Optional[str] = None

    raw: Dict[str, Any] = field(default_factory=dict)

    @property
    def main_price(self) -> float:
        """主参考价：优先 BUFF 售价，其次 Steam 售价。"""
        if self.buff_sell_price and self.buff_sell_price > 0:
            return float(self.buff_sell_price)
        return float(self.steam_sell_price)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "good_id": self.good_id,
            "name": self.name,
            "market_hash_name": self.market_hash_name,
            "img": self.img,
            "exterior": self.exterior,
            "rarity": self.rarity,
            "main_price": self.main_price,
            "buff_sell_price": self.buff_sell_price,
            "buff_buy_price": self.buff_buy_price,
            "steam_sell_price": self.steam_sell_price,
            "yyyp_sell_price": self.yyyp_sell_price,
            "buff_price_chg": self.buff_price_chg,
            "sell_price_rate_1": self.sell_price_rate_1,
            "sell_price_rate_7": self.sell_price_rate_7,
            "sell_price_rate_30": self.sell_price_rate_30,
            "sell_price_rate_90": self.sell_price_rate_90,
            "sell_price_7": self.sell_price_7,
            "sell_price_30": self.sell_price_30,
            "buff_sell_num": self.buff_sell_num,
            "buff_buy_num": self.buff_buy_num,
            "steam_sell_num": self.steam_sell_num,
            "yyyp_sell_num": self.yyyp_sell_num,
            "yyyp_buy_num": self.yyyp_buy_num,
            "sell_buy_diff_chg_buff": self.sell_buy_diff_chg_buff,
            "turnover_number": self.turnover_number,
            "turnover_avg_price": self.turnover_avg_price,
            "period_at": self.period_at,
        }


def _to_float(v: Any, default: float = 0.0) -> float:
    try:
        if v is None:
            return default
        return float(v)
    except (TypeError, ValueError):
        return default


def _to_int(v: Any, default: int = 0) -> int:
    try:
        if v is None:
            return default
        return int(v)
    except (TypeError, ValueError):
        return default


# ---- V0.3.8: 共享单连接会话(把出口 IP 钉住) ----
# 实测(probe_egress_ip_drift.py,2026-09-28):本机只有一条默认路由,但路由器/运营商
# **按 TCP 连接**做双 WAN 轮询 —— 20 次新建连接严格交替 14.145.31.44 / 14.145.65.164;
# 而**同一条 keep-alive 连接连打 12 次全程同一个 IP**;6 条并发则 3+3 劈成两个 IP。
# 结论:漂移是"每条连接"级别的 → 只要让 CSQAQ 流量只走一条长连接,出口 IP 就钉住了
# (白名单绑定一次即长期有效),而不是每条新连接都去赌另一条 WAN。
# 原来:每次 aiohttp.ClientSession() 都是新连接池(默认 limit=100),且 main.py 有 5 处
# `async with CSQAQClient(...)` 频繁新建会话 → 401/重绑风暴(今日 401 1901、重绑 1076)。
_SHARED_SESSION: Optional[aiohttp.ClientSession] = None
_SHARED_SESSION_LOCK: Optional[asyncio.Lock] = None
_PIN_LIMIT: int = 1            # 只允许 1 条连接 = 只落一条 WAN
_PIN_KEEPALIVE: float = 600.0  # 空闲 10 分钟才回收(默认 30s 太短,会被动换连接换 IP)

# ---- V0.3.19: 钉"目标服务器 IP"(治 IP 漂移的根) ----
# 实测(2026-10-21,probe_pin_feasible.py / probe_pin_feasible2.py):本机出口 IP 由**目标服务器
# IP** 决定(路由器/运营商按目标地址哈希选 WAN,同一目标恒定同一条出口)。而 api.csqaq.com 有
# 5 个 A 记录 → 5 条入口里 3 条走"未绑定出口"(401)、2 条走"已绑定出口"(200):
#     不钉:连续 8 次新连接只有 2 次成功(25%);钉住放行的那个:8/8、6/6 全通。
# V0.3.8 钉的是**连接**(limit=1 + 长 keepalive),但连接一断,重连就重新赌 DNS → 还是会漂。
# 这里钉的是 **DNS 解析结果**:解析器恒返回同一个服务器 IP → 每次重连都落同一条 WAN 出口。
_PIN_IP: Optional[str] = None              # 当前钉住的服务器 IP(None=不钉,走系统 DNS)
_PIN_CANDIDATES: List[str] = []            # 候选(第 1 个是成功过的,其余备用)
_PIN_FAIL_STREAK: int = 0                  # 连续失败计数
_PIN_FAIL_LIMIT: int = 3
_PIN_LAST_ROTATE: float = 0.0              # 上次换 IP 时刻(限频,防换 IP 也成风暴)
_PIN_ROTATE_MIN_GAP: float = 120.0
_PIN_SELECTED: bool = False                # 是否已做过选择(幂等)
_PIN_SELECT_LOCK: Optional[asyncio.Lock] = None
_PIN_CACHE_PATH: str = os.path.join("data", "csqaq_pin.json")
_PIN_VERIFY_PATH: str = "/goods/getPriceByMarketHashName"
_BIND_PATH: str = "/sys/bind_local_ip"
_PIN_VERIFY_NAME: str = "M4A4 | Griffin (Factory New)"
_PIN_PROBE_TIMEOUT: float = 12.0


def _pin_select_lock_obj() -> asyncio.Lock:
    """V0.3.19:选择锁 —— 启动时多个任务会同时首调 shared_session，不加锁会并发探测 4 次(实测 429)。"""
    global _PIN_SELECT_LOCK
    if _PIN_SELECT_LOCK is None:
        _PIN_SELECT_LOCK = asyncio.Lock()
    return _PIN_SELECT_LOCK


class _PinnedResolver(aiohttp.abc.AbstractResolver):
    """把 CSQAQ 域名钉到指定 IP;其它域名透传(防御性,正常用不到)。

    关键:**resolve 时读模块级 `_PIN_IP`**,所以换 IP 不需要重建 session/连接池。
    """

    def __init__(self, host: str, ip: Optional[str] = None):
        self._host = host
        self._ip = ip          # 显式指定(探测用);None = 跟随模块级 _PIN_IP
        self._fallback = aiohttp.resolver.DefaultResolver()

    async def resolve(self, host, port=0, family=socket.AF_INET):
        ip = self._ip or _PIN_IP
        if ip and host == self._host:
            return [{"hostname": host, "host": ip, "port": port or 443,
                     "family": socket.AF_INET, "proto": socket.IPPROTO_TCP, "flags": 0}]
        return await self._fallback.resolve(host, port, family)

    async def close(self):
        await self._fallback.close()


def _csqaq_host(cfg: Optional[CSQAQConfig]) -> str:
    try:
        host = urlsplit(str(getattr(cfg, "base_url", "") or "")).hostname
    except Exception:
        host = None
    return host or "api.csqaq.com"


def _load_pin_cache() -> List[str]:
    try:
        with open(_PIN_CACHE_PATH, "r", encoding="utf-8") as f:
            data = json.load(f) or {}
        return [str(x) for x in (data.get("candidates") or []) if x]
    except Exception:
        return []


def _save_pin_cache(candidates: List[str]) -> None:
    try:
        folder = os.path.dirname(_PIN_CACHE_PATH)
        if folder:
            os.makedirs(folder, exist_ok=True)
        tmp = _PIN_CACHE_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"candidates": candidates,
                       "saved_at": time.strftime("%Y-%m-%d %H:%M:%S")}, f, ensure_ascii=False)
        os.replace(tmp, _PIN_CACHE_PATH)
    except Exception as exc:
        logger.debug("[csqaq.pin] 写缓存失败(不影响运行): %s", exc)


def _dns_candidates(cfg: Optional[CSQAQConfig]) -> List[str]:
    host = _csqaq_host(cfg)
    try:
        infos = socket.getaddrinfo(host, 443, socket.AF_INET, socket.SOCK_STREAM)
    except Exception as exc:
        logger.debug("[csqaq.pin] 解析 %s 失败: %s", host, exc)
        return []
    out: List[str] = []
    for info in infos:
        ip = info[4][0]
        if ip not in out:
            out.append(ip)
    return out


async def _bind_via_ip(cfg: CSQAQConfig, ip: str) -> Optional[bool]:
    """用一条只走该 IP 的临时会话调用 /sys/bind_local_ip,把账号白名单绑到这条入口的出口上。

    返回 True=绑定成功、False=明确失败(401/参数错)、None=说不清(限频/超时)。
    这是"挑不到现成放行入口"时的兜底:bind 请求本身也走这条入口 → 绑的就是它的出口,必然自洽。
    """
    host = _csqaq_host(cfg)
    url = str(getattr(cfg, "base_url", "")).rstrip("/") + _BIND_PATH
    try:
        connector = aiohttp.TCPConnector(resolver=_PinnedResolver(host, ip), force_close=True)
        timeout = aiohttp.ClientTimeout(total=_PIN_PROBE_TIMEOUT)
        async with aiohttp.ClientSession(connector=connector, timeout=timeout) as sess:
            async with sess.post(url, json={},
                                 headers={"ApiToken": getattr(cfg, "api_token", "")}) as resp:
                body = await resp.text()
                if resp.status == 200:
                    try:
                        return (json.loads(body) or {}).get("code") == 200
                    except Exception:
                        return True
                return False if resp.status in (400, 401, 403) else None
    except Exception as exc:
        logger.debug("[csqaq.pin] 经 %s 绑定失败: %s", ip, exc)
        return None


async def _probe_pinned_ip(cfg: CSQAQConfig, ip: str) -> Optional[bool]:
    """用一条只走该 IP 的临时会话打一次轻接口,判定该入口的出口是否在白名单内。

    返回三态:**True**=放行(200)、**False**=明确不放行(401 IP 不符)、**None**=说不清
    (429 限频/超时/网络错)。限频绝不能被当成"这条入口不通" —— 否则选 IP 会挑错。
    """
    host = _csqaq_host(cfg)
    url = str(getattr(cfg, "base_url", "")).rstrip("/") + _PIN_VERIFY_PATH
    try:
        connector = aiohttp.TCPConnector(resolver=_PinnedResolver(host, ip), force_close=True)
        timeout = aiohttp.ClientTimeout(total=_PIN_PROBE_TIMEOUT)
        async with aiohttp.ClientSession(connector=connector, timeout=timeout) as sess:
            for attempt in (1, 2):
                async with sess.post(url, json={"marketHashNameList": [_PIN_VERIFY_NAME]},
                                     headers={"ApiToken": getattr(cfg, "api_token", "")}) as resp:
                    if resp.status == 200:
                        return True
                    if resp.status == 401:
                        return False
                    status = resp.status
                if status not in (429, 403, 503, 500, 502, 504):
                    return False          # 其它确定性错误:当成这条入口不可用
                if attempt == 1:
                    await asyncio.sleep(2.5)   # 限频类退避一次再判
            logger.debug("[csqaq.pin] 探测 %s 说不清(HTTP %s),不下结论", ip, status)
            return None
    except Exception as exc:
        logger.debug("[csqaq.pin] 探测 %s 失败(说不清): %s", ip, exc)
        return None


async def select_pin_ip(cfg: Optional[CSQAQConfig], *, force: bool = False) -> Optional[str]:
    """选一个"出口在白名单内"的服务器 IP 并钉住(幂等;force 才重选)。

    两段式:① 先逐个探测候选,第一个"现成就在白名单里"的胜出(不花 bind 额度);
    ② 都不放行 → 用第一条候选自己调一次 bind(请求走同一入口,出口必然自洽),复核后钉住。
    候选顺序:配置手填 > 上次成功的缓存 > 当前 DNS 的全部 A 记录。
    """
    global _PIN_IP, _PIN_CANDIDATES, _PIN_SELECTED, _PIN_FAIL_STREAK
    if cfg is None or not getattr(cfg, "pin_auto", True):
        _PIN_IP, _PIN_SELECTED = None, True
        return None
    if _PIN_SELECTED and not force:
        return _PIN_IP
    manual = str(getattr(cfg, "pin_server_ip", "") or "").strip()
    if manual:
        # 手填就直接钉,不探测(省一次请求,也避免限频把人工指定误判掉)
        _PIN_IP = manual
        _PIN_CANDIDATES = [manual] + [x for x in (_load_pin_cache() + _dns_candidates(cfg))
                                      if x and x != manual]
        _save_pin_cache(_PIN_CANDIDATES)
        _PIN_FAIL_STREAK = 0
        _PIN_SELECTED = True
        logger.info("[csqaq.pin] 按配置钉住服务器 IP %s", manual)
        return _PIN_IP
    cands: List[str] = []
    for ip in _load_pin_cache() + _dns_candidates(cfg):
        if ip and ip not in cands:
            cands.append(ip)
    if not cands:
        _PIN_IP, _PIN_SELECTED = None, True
        logger.warning("[csqaq.pin] 无候选 IP,回退系统 DNS")
        return None
    chosen: Optional[str] = None
    unclear: List[str] = []
    verdicts: List[str] = []
    for ip in cands:
        r = await _probe_pinned_ip(cfg, ip)
        verdicts.append(f"{ip}:{'通' if r is True else ('拒' if r is False else '?')}")
        if r is True:
            chosen = ip
            break
        if r is None:
            unclear.append(ip)
        await asyncio.sleep(0.6)      # 别把探测打成突发
    logger.info("[csqaq.pin] 入口探测 %s", " ".join(verdicts) or "-")

    if chosen is None and not unclear:
        # 没有任何现成入口的出口等于当前绑定 → 自己绑一次(bind 走同一条入口,出口必然自洽)
        first = cands[0]
        b = await _bind_via_ip(cfg, first)
        if b is True:
            r2 = await _probe_pinned_ip(cfg, first)
            if r2 is not False:
                chosen = first
                logger.info("[csqaq.pin] 已把白名单绑到入口 %s 的出口,并复核通过", first)
            else:
                logger.warning("[csqaq.pin] 经 %s 绑定后复核仍被拒,不钉", first)
        else:
            logger.warning("[csqaq.pin] 经 %s 绑定未成功(%s),不钉",
                           first, "被拒" if b is False else "不明确")

    if chosen is None and unclear:
        chosen = unclear[0]
        logger.warning("[csqaq.pin] 探测不明确(限频/超时),先暂钉 %s,连续失败会自动换", chosen)
    if chosen is None:
        _PIN_IP = None
        _PIN_SELECTED = True
        logger.warning("[csqaq.pin] 没有可用入口,回退系统 DNS(会照常出现 401 重绑)")
        return None
    _PIN_IP = chosen
    _PIN_CANDIDATES = [chosen] + [x for x in cands if x != chosen]
    _save_pin_cache(_PIN_CANDIDATES)
    logger.info("[csqaq.pin] 钉住服务器 IP %s(出口与白名单一致),备用 %s",
                chosen, ",".join(_PIN_CANDIDATES[1:]) or "-")
    _PIN_FAIL_STREAK = 0
    _PIN_SELECTED = True
    return _PIN_IP


def pin_state() -> Dict[str, Any]:
    """当前钉 IP 状态(日志/自检/测试用)。"""
    return {"enabled": _PIN_IP is not None, "ip": _PIN_IP,
            "candidates": list(_PIN_CANDIDATES), "fail_streak": _PIN_FAIL_STREAK}


def reset_pin_streak() -> None:
    """成功一次就清零连续失败计数。"""
    global _PIN_FAIL_STREAK
    _PIN_FAIL_STREAK = 0


async def note_pin_failure(cfg: Optional[CSQAQConfig]) -> None:
    """记一次失败;连续到阈值就换下一个候选 IP(120s 限频,防"换 IP"也成风暴)。"""
    global _PIN_FAIL_STREAK, _PIN_LAST_ROTATE, _PIN_IP, _PIN_CANDIDATES
    if _PIN_IP is None or cfg is None:
        return
    _PIN_FAIL_STREAK += 1
    if _PIN_FAIL_STREAK < _PIN_FAIL_LIMIT:
        return
    now = time.monotonic()
    if now - _PIN_LAST_ROTATE < _PIN_ROTATE_MIN_GAP:
        return
    _PIN_LAST_ROTATE = now
    old = _PIN_IP
    inconclusive = False
    for ip in _PIN_CANDIDATES:
        if ip == old:
            continue
        r = await _probe_pinned_ip(cfg, ip)
        if r is True:
            _PIN_IP = ip
            _PIN_CANDIDATES = [ip] + [x for x in _PIN_CANDIDATES if x != ip]
            _PIN_FAIL_STREAK = 0
            _save_pin_cache(_PIN_CANDIDATES)
            logger.warning("[csqaq.pin] 连续失败,换钉服务器 IP: %s → %s", old, ip)
            return
        if r is None:
            inconclusive = True
    _PIN_FAIL_STREAK = 0
    if inconclusive:
        # 限频/超时下探测不明确 → 保留当前钉的 IP,别把好好的选择丢掉
        logger.warning("[csqaq.pin] 换钉探测不明确(限频/超时),先保留 %s", old)
        return
    # V0.3.37:候选全 401 时**不再清空 pin**。
    # 出口已改成「按目标 IP 分流」,清空 pin = 让 5 个入口轮着出去,必然重新制造 401 风暴;
    # 钉着一个"探测不通"的入口,至少出口是确定的,还能靠重试熬过节点的间歇性卡顿。
    logger.warning("[csqaq.pin] 候选全部不通(401),保留当前 %s —— 退回系统 DNS 只会制造 401", old)

# V0.3.9:全局限速(跨实例共享)。
# 实测(2026-09-28 16:00-16:12):8 个任务同时启动的那一分钟出现 **30 次 429**,而随后
# 稳态只有 1~3 次/分钟;用 probe_429_vs_ip.py 在两个 WAN 上各打 10 次 /current_data
# **10/10 全部 200** → 既不是 IP 被风控、也不是配额用尽,而是**突发**(原来 `_throttle`
# 的 `_last_request_at` 是**每实例**的,5 个客户端各按 1 秒节流 → 瞬时并发 5 路起步;
# 再叠加 429 重试(每次尝试前也会节流但同样只在本实例内) → 放大 2 倍以上)。
# 改成全局时间戳后:所有实例、所有重试共用同一条最小间隔线。
_GLOBAL_THROTTLE_LOCK: Optional[asyncio.Lock] = None
_GLOBAL_LAST_REQUEST_AT: float = 0.0

# V0.3.10:全局单飞信号量(与我们自己的层排队,而不是堆在 aiohttp 连接池里)。
# 为什么必须要有:单连接(limit=1)意味着同时只有一条请求真的在飞,多余请求是**在
# aiohttp 连接池里等**;而 `ClientTimeout(total=15)` 的计时**包含排队时间** →
# 队头一条慢请求(15s 超时)会把后面排队的请求一起"等成"超时 ✗ —— 实测部署后
# 19 分钟里有 18 次超时,其中一部分正是这种**级联超时**(日志特征:`ip_rebind_start ip=` 为空,
# 即没有 401 的 ip_hint、纯粹由超时触发的重绑)。
# 用信号量把等待挪到我们自己的层里后:排队不计入超时,只有真挂住的请求才吃 15s,
# 也不会再拿"排队超时"去误触发重绑。
_GLOBAL_SEM: Optional[asyncio.Semaphore] = None


# ---- V0.3.21: 全局限流下沉到 client(把 max_req_per_min 的预算用在**每一次尝试**上) ----
# 起因(2026-10-21 14:35-14:39 实测):连续 5 分钟 `拉取=0`、只剩重试和 429 —— **自锁**。
# 原因:`max_req_per_min: 13` 只作用在异动扫描器的 `_TokenBucket` 上,而 client 这层只有
# `request_interval=1s` 的最小间隔(=60 次/分上限)。于是:① 盯盘/平台同步//分析 等非扫描调用
# 完全不受 13/分约束;② 一次失败重试 6 次 = 6 秒内 6 个请求,**重试绕过了令牌桶**;
# ③ 429 之后各自退避重试,谁都不肯先停 → 账号一直在惩罚窗口里,永远恢复不了。
# 现在:每次**尝试**(含重试)都必须先取一个令牌;429 触发**全局**指数惩罚(成功清零),
# 谁都不能越过惩罚期发请求 → 自锁被打破。
_RATE_RPM: float = 10.0            # 实测：11~13 次/分 仍会零星 429 → 安全线压到 10
_RATE_BURST_MAX: float = 5.0       # 突发上限：桶满(=10)时按 1 秒间隔连发 10 个也会被风控 → 最多攒 5 个
_RATE_TOKENS: float = 2.0          # 启动只给 2 个令牌:满桶会让启动瞬间连发 → 立刻 429
_RATE_UPDATED: float = time.monotonic()
_RATE_LOCK: Optional[asyncio.Lock] = None
_RATE_PENALTY: float = 0.0          # 429 惩罚(秒):10→20→40→80→120 封顶,成功后清零
_RATE_PENALTY_UNTIL: float = 0.0
_RATE_PENALTY_BASE: float = 10.0
_RATE_PENALTY_MAX: float = 120.0
_RATE_429_TOTAL: int = 0
_RATE_REQ_TS: List[float] = []
_RATE_429_TS: List[float] = []


def _rate_lock_obj() -> asyncio.Lock:
    global _RATE_LOCK
    if _RATE_LOCK is None:
        _RATE_LOCK = asyncio.Lock()
    return _RATE_LOCK


async def _rate_acquire(cfg: Any) -> None:
    """取一个请求令牌:先等 429 惩罚期结束,再按 max_req_per_min 匀速放行。"""
    global _RATE_TOKENS, _RATE_UPDATED, _RATE_RPM
    rpm = _RATE_RPM
    raw = getattr(cfg, "max_req_per_min", None) if cfg is not None else None
    if raw is not None:
        try:
            rpm = max(1.0, float(raw))
        except (TypeError, ValueError):
            rpm = _RATE_RPM
    if rpm != _RATE_RPM:
        _RATE_RPM = rpm
        _RATE_TOKENS = min(_RATE_TOKENS, rpm)
    async with _rate_lock_obj():
        while True:
            now = time.monotonic()
            if now < _RATE_PENALTY_UNTIL:
                await asyncio.sleep(min(5.0, _RATE_PENALTY_UNTIL - now))
                continue
            _RATE_TOKENS = min(min(_RATE_RPM, _RATE_BURST_MAX),
                              _RATE_TOKENS + (now - _RATE_UPDATED) * _RATE_RPM / 60.0)
            _RATE_UPDATED = now
            if _RATE_TOKENS >= 1.0:
                _RATE_TOKENS -= 1.0
                _RATE_REQ_TS.append(now)
                if len(_RATE_REQ_TS) > 600:
                    del _RATE_REQ_TS[:300]
                return
            await asyncio.sleep(max(0.05, (1.0 - _RATE_TOKENS) * 60.0 / _RATE_RPM))


def note_rate_limited() -> None:
    """收到 429:指数加大**全局**惩罚,让所有调用者一起退让(否则各自重试 = 自锁)。"""
    global _RATE_PENALTY, _RATE_PENALTY_UNTIL, _RATE_429_TOTAL
    _RATE_429_TOTAL += 1
    _RATE_429_TS.append(time.monotonic())
    if len(_RATE_429_TS) > 600:
        del _RATE_429_TS[:300]
    _RATE_PENALTY = min(_RATE_PENALTY_MAX,
                        _RATE_PENALTY * 2 if _RATE_PENALTY > 0 else _RATE_PENALTY_BASE)
    _RATE_PENALTY_UNTIL = time.monotonic() + _RATE_PENALTY
    logger.warning("[csqaq] 429 全局退避 %.0f 秒(累计 %d 次)", _RATE_PENALTY, _RATE_429_TOTAL)


def note_rate_ok() -> None:
    """请求成功:解除 429 惩罚。"""
    global _RATE_PENALTY
    _RATE_PENALTY = 0.0


def rate_stats() -> Dict[str, Any]:
    """本次运行的实际速率(60 秒窗口),用于验证"是否真的压在预算内"。"""
    now = time.monotonic()
    return {
        "rpm_60s": sum(1 for t in _RATE_REQ_TS if now - t <= 60.0),
        "budget": _RATE_RPM,
        "penalty_s": max(0.0, round(_RATE_PENALTY_UNTIL - now, 1)),
        "http429_total": _RATE_429_TOTAL,
        "http429_60s": sum(1 for t in _RATE_429_TS if now - t <= 60.0),
    }


def _global_sem_obj() -> asyncio.Semaphore:
    global _GLOBAL_SEM
    if _GLOBAL_SEM is None:
        _GLOBAL_SEM = asyncio.Semaphore(_PIN_LIMIT)
    return _GLOBAL_SEM


def _global_throttle_lock_obj() -> asyncio.Lock:
    global _GLOBAL_THROTTLE_LOCK
    if _GLOBAL_THROTTLE_LOCK is None:
        _GLOBAL_THROTTLE_LOCK = asyncio.Lock()
    return _GLOBAL_THROTTLE_LOCK


def _shared_session_lock_obj() -> asyncio.Lock:
    global _SHARED_SESSION_LOCK
    if _SHARED_SESSION_LOCK is None:
        _SHARED_SESSION_LOCK = asyncio.Lock()
    return _SHARED_SESSION_LOCK


async def shared_session(cfg: Optional[CSQAQConfig] = None) -> aiohttp.ClientSession:
    """进程级共享的 CSQAQ 会话(单连接)。

    V0.3.8:所有 CSQAQClient 实例共用它,保证**全程只有一条 TCP 连接** → 出口 IP 固定。
    注意:实例的 close() **不会**关闭它(否则短命的 `async with` 块会把雷达的连接关掉),
    进程退出时调用 close_shared_session()。
    """
    global _SHARED_SESSION
    # V0.3.19:首次建会话前先选一个"出口与白名单一致"的服务器 IP(在锁外做,别占着锁做 I/O)
    if cfg is not None and not _PIN_SELECTED:
        async with _pin_select_lock_obj():
            if not _PIN_SELECTED:          # 双检:并发首调时只让一个真去探测
                await select_pin_ip(cfg)
    async with _shared_session_lock_obj():
        if _SHARED_SESSION is None or _SHARED_SESSION.closed:
            connector = aiohttp.TCPConnector(
                limit=_PIN_LIMIT,
                limit_per_host=_PIN_LIMIT,
                keepalive_timeout=_PIN_KEEPALIVE,
                force_close=False,
                enable_cleanup_closed=True,
                # V0.3.19:钉住 DNS 解析结果 → 每次重连都落同一条 WAN 出口
                resolver=(_PinnedResolver(_csqaq_host(cfg)) if _PIN_IP else None),
            )
            total = float(getattr(cfg, "request_timeout", 15.0)) if cfg else 15.0
            _SHARED_SESSION = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=total),
                headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                                      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"},
                connector=connector,
            )
        return _SHARED_SESSION


async def close_shared_session() -> None:
    """进程收尾时关闭共享会话(测试与停机路径用)。"""
    global _SHARED_SESSION
    async with _shared_session_lock_obj():
        if _SHARED_SESSION is not None and not _SHARED_SESSION.closed:
            await _SHARED_SESSION.close()
        _SHARED_SESSION = None


class CSQAQClient:
    """CSQAQ API 客户端：带请求节流与重试。"""

    def __init__(self, config: CSQAQConfig):
        self._cfg = config
        self._session: Optional[aiohttp.ClientSession] = None
        self._last_request_at: float = 0.0

    @staticmethod
    def ip_drift_cooling() -> bool:
        """当前出口 IP 处于漂移/白名单冷却期时返回 True。"""
        return time.monotonic() < _IP_DRIFT_UNTIL

    @staticmethod
    def ip_drift_cooldown_left() -> int:
        return _drift_cooldown_left()

    async def __aenter__(self):
        await self._ensure_session()
        return self

    async def __aexit__(self, *args):
        await self.close()

    async def _ensure_session(self) -> aiohttp.ClientSession:
        # V0.3.8:改为取进程级共享单连接会话(不再每实例新建连接池 → 出口 IP 不再每次重赌)
        self._session = await shared_session(self._cfg)
        return self._session

    async def close(self):
        """释放本实例对会话的引用。

        V0.3.8:**绝不关闭共享会话** —— main.py 里有 5 处短命的
        `async with CSQAQClient(...)` 块,若它们把共享会话关掉,雷达侧的连接会被连带关闭,
        反而制造新的重连(=换 WAN=漂移)。共享会话由进程收尾统一关闭。
        """
        self._session = None

    async def _throttle(self):
        """按配置间隔节流请求，避免触发限频。

        V0.3.9:**全局限速**(跨实例共享时间戳)。原来是每实例一个
        `_last_request_at`,多个客户端会各自"1 秒一次"并发发车 → 启动/重试时成串
        触发 429(实测启动那一分钟 30 次 429)。现在所有实例、所有重试共用一条最小间隔线,
        再配合单连接,请求天然串行且平滑。
        """
        global _GLOBAL_LAST_REQUEST_AT
        # V0.3.21:先过全局 RPM 令牌桶(连重试一起管,非扫描调用也受约束)
        await _rate_acquire(self._cfg)
        async with _global_throttle_lock_obj():
            now = time.monotonic()
            wait = float(self._cfg.request_interval) - (now - _GLOBAL_LAST_REQUEST_AT)
            if wait > 0:
                await asyncio.sleep(wait)
            _GLOBAL_LAST_REQUEST_AT = time.monotonic()
            self._last_request_at = _GLOBAL_LAST_REQUEST_AT

    async def _post(self, path: str, payload: Dict[str, Any],
                    allow_drift_skip: bool = True,
                    skip_ip_rebind: bool = False,
                    max_attempts: Optional[int] = None,
                    backoff_429: float = 3.0) -> Dict[str, Any]:
        """POST 请求 + 重试。返回响应 JSON。

        :param max_attempts: 最大尝试次数,None=retry_times+3(默认 6 次)。
            bind_local_ip 等自身限频极严(30s/次)的接口必须传小值(2),防止短间隔轰炸触发服务器风控。
        :param backoff_429: 限频类(429/403/503)重试退避基数(秒)。bind 接口传 35(>官方 30s/次)。
        """
        session = await self._ensure_session()
        url = f"{self._cfg.base_url}{path}"
        headers = {
            "ApiToken": self._cfg.api_token,
            "Content-Type": "application/json",
        }
        last_err: Optional[Exception] = None
        if allow_drift_skip and time.monotonic() < _IP_DRIFT_UNTIL:
            raise RuntimeError(f"CSQAQ IP 漂移冷却中: {path}")
        attempts = max_attempts if max_attempts else self._cfg.retry_times + 3
        for attempt in range(1, attempts + 1):
            await self._throttle()
            status, body = 0, ""
            try:
                # V0.3.10:在**我们这层**排队(不计入 aiohttp 的 total 超时),避免队头级联超时
                async with _global_sem_obj():
                    async with session.post(url, json=payload, headers=headers) as resp:
                        body = await resp.text()
                        status = resp.status
                        if resp.status == 200:
                            try:
                                data = json.loads(body)
                            except Exception:
                                logger.warning("[csqaq] 响应非 JSON: %s", body[:120])
                                data = {}
                            if isinstance(data, dict) and data.get("code") == 200:
                                reset_pin_streak()      # V0.3.19:成功即清零连续失败计数
                                reset_timeout_streak()  # V0.3.37:成功即清零连续超时计数
                                note_rate_ok()          # V0.3.21:成功即解除 429 惩罚
                                return data
                            logger.warning("[csqaq] 接口返回异常: %s", data)
                            return data or {}
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                last_err = exc
                logger.warning("[csqaq] 请求失败(%s): %s (attempt %s)", path, exc, attempt)
                # V0.3.19:只有"连不上"才说明入口坏了 → 换钉。**超时不算**:实测超时多来自额度/排队,
                # 拿它换钉会让两个健康节点被来回摆(12:13 日志里 159→119→159 就是这么来的)。
                if isinstance(exc, aiohttp.ClientConnectorError):
                    await note_pin_failure(self._cfg)
                # V0.3.37:超时**不再重绑**。原逻辑(V0.3.9)是"超时就怀疑出口漂了,立刻重绑并重试",
                # 那是因为当时出口按「目标服务器 IP」恒定,超时≈落到了未绑定的 WAN 上。
                # 现在白名单只对应钉住的那条路,超时只是节点犯卡(实测 1.14.196.54 会挂 20s),
                # 重绑既救不了,还可能把 bind 请求自己甩到别的出口、把白名单改坏。
                # 改为:只累计连续超时,到阈值才换钉(换的是服务器 IP,不是白名单)。
                if isinstance(exc, asyncio.TimeoutError) and not skip_ip_rebind:
                    await note_timeout(self._cfg, path)
                # V0.3.9:最后一次尝试后面没有重试了,不再白睡(实测能省下 4.5s 的死等)
                if attempt < attempts:
                    await asyncio.sleep(attempt * 1.5)
                continue
            # ---- 连接与单飞名额都已释放;下面的分支要么 continue,要么抛 ----
            # 限频/风控: 指数退避后重试;401(IP 白名单不符)等直接失败,不重试烧请求
            logger.warning("[csqaq] HTTP %s @ %s (attempt %s)", status, path, attempt)
            if status == 401 and not skip_ip_rebind and _looks_like_ip_drift(body):
                ip_hint = _extract_env_ip(body)
                cd = int(getattr(self._cfg, "rebind_cooldown_seconds", 240))
                _mark_ip_drift(ip_hint, _drift_pause_seconds(self._cfg))
                # V0.3.19:401 说明当前服务器入口的出口不在白名单 → 先换入口,再谈重绑
                await note_pin_failure(self._cfg)
                rebind_ok, _ = await try_rebind_local_ip(
                    self._cfg, ip_hint=ip_hint, source=path, cooldown_seconds=cd)
                if rebind_ok:
                    continue
                raise RuntimeError(f"CSQAQ HTTP 401: {body[:200]}")
            if self._is_retryable_status(status):
                # V0.3.21:429/403/503 不是"这个接口/排序坏了",而是**全局限频** →
                # 触发全局指数惩罚,让所有调用者一起退让(实测各退各的会形成 429 自锁)
                note_rate_limited()
                await asyncio.sleep(attempt * backoff_429)
                continue
            raise RuntimeError(f"CSQAQ HTTP {status}: {body[:200]}")
        raise RuntimeError(f"CSQAQ 请求失败: {path}") from last_err

    @staticmethod
    def _is_retryable_status(status: int) -> bool:
        """状态码是否应指数退避重试。

        429(请求过多)/403/503(风控/网关)为限频类,退避后重试有意义;
        401(IP 与白名单不符)/4xx 参数错/5xx 服务错为确定性失败,重试只会烧额度,直接抛。
        """
        return status in (429, 403, 503)

    # ------------------------------------------------------------------
    # 公开接口
    # ------------------------------------------------------------------

    async def get_rank_list(
        self,
        page_index: int = 1,
        page_size: int = 100,
        sort: str = RANK_SORT_SELL_PRICE_DESC,
        extra_filter: Optional[Dict[str, Any]] = None,
    ) -> List[RankItem]:
        """获取排行榜数据（价格/涨跌幅/在售数量等）。

        :param sort: 排序选项（见 RANK_SORT_* 常量）
        :param extra_filter: 额外筛选，如 {"类型": ["匕首"], "磨损": ["崭新出厂"]}
        """
        flt: Dict[str, Any] = {"排序": [sort]}
        if extra_filter:
            flt.update(extra_filter)
        payload = {
            "page_index": page_index,
            "page_size": page_size,
            "show_recently_price": False,
            "filter": flt,
        }
        data = await self._post("/info/get_rank_list", payload)
        items: List[RankItem] = []
        d = data.get("data") or {}
        for row in d.get("data") or []:
            items.append(self._parse_rank_item(row))
        return items

    async def get_rank_items_all(
        self,
        page_size: int = 100,
        max_pages: int = 3,
        sorts: Optional[List[str]] = None,
        extra_filter: Optional[Dict[str, Any]] = None,
    ) -> List[RankItem]:
        """拉取多页/多排序排行榜数据并去重，作为本地分析样本。

        :param sorts: 要拉取的排序列表，默认按售价降序 + 求购价降序
        """
        if sorts is None:
            sorts = [RANK_SORT_SELL_PRICE_DESC, RANK_SORT_BUY_PRICE_DESC]
        seen: Dict[int, RankItem] = {}
        for sort in sorts:
            for page in range(1, max_pages + 1):
                try:
                    items = await self.get_rank_list(
                        page_index=page, page_size=page_size,
                        sort=sort, extra_filter=extra_filter,
                    )
                    for it in items:
                        seen[it.good_id] = it
                    logger.info("[csqaq] 已拉取 sort=%s page=%s items=%s 累计=%s",
                                sort, page, len(items), len(seen))
                    if len(items) < page_size:
                        break  # 已到末页
                except Exception as exc:  # 单页失败不中断整体
                    logger.error("[csqaq] 拉取失败 sort=%s page=%s: %s", sort, page, exc)
        return list(seen.values())

    async def get_price_by_market_hash_name(
        self,
        names: List[str],
        *,
        allow_drift_skip: bool = False,
        allow_rebind: bool = True,
    ) -> Dict[str, Dict[str, Any]]:
        """按 market_hash_name 批量查询价格（单次 ≤ 50 个）。

        返回 {market_hash_name: 行情数据}，行情数据含 name/buffSellPrice/steamSellPrice 等。

        V0.3.15:默认 **不再**在漂移冷却期跳过（原 True）。这是盯盘循环每一轮的核心请求，
        被强制跳过等于盯盘直接断档（实测 2026-10-13：漂移冷却 + 重绑退避导致 90 分钟
        完全没采数据、18 个自选品同步断档 121 分钟）。现在改成"照常试"：
        通得过就拿到数据，通不过会走 401→重绑的正常自愈链路。
        真正该在漂移期停的是**榜单**（get_rank_list，一次几十页，打了也是白打）。
        """
        if not names:
            return {}
        out: Dict[str, Dict[str, Any]] = {}
        for i in range(0, len(names), 50):
            chunk = names[i:i + 50]
            data = await self._post("/goods/getPriceByMarketHashName", {
                "marketHashNameList": chunk,
            }, allow_drift_skip=allow_drift_skip, skip_ip_rebind=not allow_rebind)
            # 真实返回结构: data.success 为 dict {marketHashName: row}，data.error 为未命中名称列表
            success = (data.get("data") or {}).get("success") or {}
            if isinstance(success, dict):
                for mhn, row in success.items():
                    if isinstance(row, dict) and mhn:
                        out[mhn] = row
        return out

    async def get_good_info(self, good_id: int, *, allow_drift_skip: bool = False) -> Dict[str, Any]:
        """获取单件饰品详情(返回 goods_info,含英文名 market_hash_name)。

        该接口同样受 CSQAQ IP 白名单约束；401 漂移时沿用批量查价的
        自动重绑逻辑，成功后从头重试，避免异动复核/全平台同步长期失效。

        V0.3.15:漂移冷却期默认**不再**直接抛错跳过（原硬门控）—— 这个接口是盯盘
        "补全分平台盘口"和 /分析 取详情用的，跳过它就等于停采。改成照常试 + 401 自愈。
        """
        if allow_drift_skip and self.ip_drift_cooling():
            raise RuntimeError(
                f"CSQAQ IP 漂移冷却中: /info/good (剩余{self.ip_drift_cooldown_left()}秒)"
            )
        session = await self._ensure_session()
        url = f"{self._cfg.base_url}/info/good"
        headers = {"ApiToken": self._cfg.api_token}
        last_err: Optional[Exception] = None
        for attempt in range(1, self._cfg.retry_times + 3):
            await self._throttle()
            status, body = 0, ""
            try:
                # V0.3.10:排队挪到我们这层,避免单连接的队头级联超时
                async with _global_sem_obj():
                    async with session.get(url, params={"id": good_id}, headers=headers) as resp:
                        body = await resp.text()
                        status = resp.status
                        if resp.status == 200:
                            data = json.loads(body)
                            gi = (data.get("data") or {}).get("goods_info") or {}
                            reset_timeout_streak()   # V0.3.40:成功即清零连续超时
                            return gi or {}
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                last_err = exc
                logger.warning("[csqaq] GET 请求失败(/info/good): %s (attempt %s)", exc, attempt)
                # V0.3.40:与 _post 统一 —— 只有"连不上"才换钉;超时只累计,连续 5 次才换钉。
                # (V0.3.37 只改了 POST,漏了这里和 /current_data,11-04~11-05 仍在
                #  bypass=timeout 重绑 3 次,等于老问题没除根。)
                if isinstance(exc, aiohttp.ClientConnectorError):
                    await note_pin_failure(self._cfg)
                if isinstance(exc, asyncio.TimeoutError):
                    await note_timeout(self._cfg, "/info/good")
                if attempt < self._cfg.retry_times + 3:
                    await asyncio.sleep(attempt * 1.5)
                continue
            # ---- 连接与单飞名额都已释放 ----
            # 限频/风控: 指数退避后重试
            logger.warning("[csqaq] GET HTTP %s @ /info/good (attempt %s)", status, attempt)
            if status == 401 and _looks_like_ip_drift(body):
                ip_hint = _extract_env_ip(body)
                cd = int(getattr(self._cfg, "rebind_cooldown_seconds", 600))
                _mark_ip_drift(ip_hint, _drift_pause_seconds(self._cfg))
                rebind_ok, info = await try_rebind_local_ip(
                    self._cfg, ip_hint=ip_hint, source="/info/good",
                    cooldown_seconds=cd)
                if rebind_ok:
                    # 重绑成功后重新探测/请求；不要在旧 session 上继续假设白名单已生效。
                    continue
                raise RuntimeError(f"CSQAQ HTTP 401: {body[:200]}")
            if status in (429, 403, 503):
                await asyncio.sleep(attempt * 3.0)
                continue
            raise RuntimeError(f"CSQAQ HTTP {status}")
        raise RuntimeError("CSQAQ 请求失败: /info/good") from last_err

    # 探测用固定英文名:getPriceByMarketHashName 强制校验账户绑定 IP(401 时返回当前环境IP),
    # 而 get_rank_list 不校验 IP——旧探针用它会在漂移时误报 ok,导致自动绑定不触发。
    _PROBE_MARKET_HASH_NAME = "M4A4 | Griffin (Factory New)"

    async def probe_ip_status(
        self,
        *,
        allow_drift_skip: bool = True,
        allow_rebind: bool = True,
    ) -> Tuple[bool, Optional[str]]:
        """探测 CSQAQ IP 白名单状态。

        用 getPriceByMarketHashName 探测:该接口强制校验账户绑定 IP,
        IP 漂移时返回 401(含"环境IP:x.x.x.x"),能真实反映"单品实时查价"链路是否可用。
        返回 (是否匹配, 当前出口IP)。IP 未绑定/漂移时返回 (False, 当前IP)。
        """
        try:
            await self.get_price_by_market_hash_name(
                [self._PROBE_MARKET_HASH_NAME],
                allow_drift_skip=allow_drift_skip,
                allow_rebind=allow_rebind,
            )
            return True, None
        except Exception as exc:
            m = re.search(r"环境IP:([\d.]+)", str(exc))
            return False, m.group(1) if m else None

    async def bind_local_ip(self) -> Tuple[bool, str]:
        """自动绑定当前出口 IP 到 CSQAQ 白名单（官方「绑定本机白名单IP」接口）。

        官方说明: 为当前请求的 API_TOKEN 绑定本机的 IP 地址, 适用于非固定 IP
        / NAT 双出口漂移场景; 频率限制 30 秒/次（ip_check_loop 探测间隔 120 秒,
        天然满足）。
        返回 (是否成功, 服务端提示信息, 形如"绑定IP更新成功,当前绑定IP为:x.x.x.x")。
        """
        try:
            data = await self._post("/sys/bind_local_ip", {},
                                    allow_drift_skip=False,
                                    skip_ip_rebind=True,
                                    max_attempts=2,       # 官方限频 30s/次,6 连发会触发服务器风控(429→挂起)
                                    backoff_429=35.0)     # 429 后退避 >30s 再试,尊重限频窗口
            raw = data.get("data") or ""
            msg = str(raw)
            logger.info("[csqaq] 自动绑定 IP 结果: %s", msg)
            # V0.3.15:原来这里是**无条件** return True —— 从不看服务端提示。
            # 代价(上线后核验抓到):服务端回「请求频率过快，绑定IP频率限制为30s」时
            # 也被判成"重绑成功",调用方于是白重试一次,日志里出现
            # `event=ip_rebind_ok ... msg=请求频率过快` 这种自相矛盾的组合。
            # 现在:命中失败字样 → 返回 False(交给调用方退避);其余情况仍返回 True,
            # 由调用方紧跟的 probe_ip_status 做**最终**判定(不依赖文案格式)。
            for bad in ("频率过快", "限制", "失败", "错误", "不符", "频繁"):
                if bad in msg:
                    logger.warning("[csqaq] 自动绑定 IP 未生效(%s): %s", bad, msg[:120])
                    return False, msg
            return True, msg
        except Exception as exc:
            logger.warning("[csqaq] 自动绑定 IP 失败: %s", exc)
            return False, str(exc)

    async def get_current_data(self, type: str = "init",
                               *, allow_drift_skip: bool = False) -> Dict[str, Any]:
        """获取 CSQAQ 首页指数数据（饰品指数/分类指数/在线人数/卡价等）。

        :param type: init(日线) | hours(时线) | kline(K线) | lease(租赁数据)
        返回 data 字段含:
          sub_index_data  - 23 个分类指数（market_index/chg_rate/open/high/low...）
          online_number   - 当前在线/今日峰值/本月峰值
          card_price      - Steam 美金卡价历史
          rate_data       - 涨跌分布统计
          greedy_status   - 市场热度

        V0.3.15:漂移冷却期默认**不再**直接跳过。它是异动推送卡片「市场环境」那一行的来源
        （实测 2026-10-13 12:30 日志:`[market] 获取市场环境失败: CSQAQ IP 漂移冷却中`），
        跳过就少一行信息。只有**榜单**在漂移期该停。
        """
        if allow_drift_skip and self.ip_drift_cooling():
            raise RuntimeError(
                f"CSQAQ IP 漂移冷却中: /current_data (剩余{self.ip_drift_cooldown_left()}秒)"
            )
        session = await self._ensure_session()
        url = f"{self._cfg.base_url}/current_data"
        headers = {"ApiToken": self._cfg.api_token}
        last_err: Optional[Exception] = None
        for attempt in range(1, self._cfg.retry_times + 3):
            await self._throttle()
            status, body = 0, ""
            try:
                # V0.3.10:排队挪到我们这层,避免单连接的队头级联超时
                async with _global_sem_obj():
                    async with session.get(url, params={"type": type}, headers=headers) as resp:
                        body = await resp.text()
                        status = resp.status
                        if resp.status == 200:
                            reset_timeout_streak()   # V0.3.40
                            return json.loads(body)
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                last_err = exc
                logger.warning("[csqaq] GET 请求失败(/current_data): %s (attempt %s)", exc, attempt)
                # V0.3.40:同上 —— 超时不再重绑,只累计
                if isinstance(exc, aiohttp.ClientConnectorError):
                    await note_pin_failure(self._cfg)
                if isinstance(exc, asyncio.TimeoutError):
                    await note_timeout(self._cfg, "/current_data")
                if attempt < self._cfg.retry_times + 3:
                    await asyncio.sleep(attempt * 1.5)
                continue
            # ---- 连接与单飞名额都已释放 ----
            logger.warning("[csqaq] GET HTTP %s @ /current_data (attempt %s)",
                           status, attempt)
            if status == 401 and _looks_like_ip_drift(body):
                ip_hint = _extract_env_ip(body)
                cd = int(getattr(self._cfg, "rebind_cooldown_seconds", 600))
                _mark_ip_drift(ip_hint, _drift_pause_seconds(self._cfg))
                rebind_ok, _ = await try_rebind_local_ip(
                    self._cfg, ip_hint=ip_hint, source="/current_data",
                    cooldown_seconds=cd)
                if rebind_ok:
                    continue
                raise RuntimeError(f"CSQAQ HTTP 401: {body[:200]}")
            if status in (429, 403, 503):
                await asyncio.sleep(attempt * 3.0)
                continue
            raise RuntimeError(f"CSQAQ HTTP {status}: {body[:200]}")
        raise RuntimeError("CSQAQ 请求失败: /current_data") from last_err

    async def get_goods_template(self, page_index: int = 1, page_size: int = 100) -> Dict[str, Any]:
        """获取饰品模板数据（全站饰品 ID/名称映射，企业权限接口，普通账号可能无权限）。

        普通账号降级方案：直接用 get_rank_list 拿数据即可，本接口仅用于可选扩展。
        """
        return await self._post("/goods/get_goods_template", {
            "page_index": page_index,
            "page_size": page_size,
        })

    # ------------------------------------------------------------------
    # 内部工具
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_rank_item(row: Dict[str, Any]) -> RankItem:
        return RankItem(
            good_id=_to_int(row.get("id")),
            name=str(row.get("name") or ""),
            market_hash_name=str(row.get("market_hash_name") or row.get("marketHashName") or ""),
            img=str(row.get("img") or ""),
            exterior=row.get("exterior_localized_name"),
            rarity=row.get("rarity_localized_name"),
            buff_sell_price=_to_float(row.get("buff_sell_price")),
            buff_buy_price=_to_float(row.get("buff_buy_price")),
            steam_sell_price=_to_float(row.get("steam_sell_price")),
            steam_buy_price=_to_float(row.get("steam_buy_price")),
            yyyp_sell_price=_to_float(row.get("yyyp_sell_price")),
            yyyp_buy_price=_to_float(row.get("yyyp_buy_price")
                                     if row.get("yyyp_buy_price") is not None
                                     else row.get("yyypBuyPrice")),
            buff_price_chg=_to_float(row.get("buff_price_chg")),
            sell_price_rate_1=(_to_float(row.get("sell_price_rate_1"))
                               if row.get("sell_price_rate_1") is not None else None),
            sell_price_rate_7=_to_float(row.get("sell_price_rate_7")),
            sell_price_rate_30=_to_float(row.get("sell_price_rate_30")),
            sell_price_rate_90=_to_float(row.get("sell_price_rate_90")),
            sell_price_7=_to_float(row.get("sell_price_7")),
            sell_price_30=_to_float(row.get("sell_price_30")),
            buff_sell_num=_to_int(row.get("buff_sell_num")),
            buff_buy_num=_to_int(row.get("buff_buy_num")),
            steam_sell_num=_to_int(row.get("steam_sell_num")),
            steam_buy_num=_to_int(row.get("steam_buy_num")),
            yyyp_sell_num=_to_int(row.get("yyyp_sell_num") if row.get("yyyp_sell_num") is not None
                                  else row.get("yyypSellNum")),
            # V0.2.24: 悠悠求购——榜单回包自带 yyyp_buy_num,此前未解析被丢弃;
            # 求购侧要统一成 BUFF+悠悠 口径必须依赖它(批量查价接口无任何 buy 字段)。
            yyyp_buy_num=_to_int(row.get("yyyp_buy_num") if row.get("yyyp_buy_num") is not None
                                 else row.get("yyypBuyNum")),
            sell_buy_diff_chg_buff=_to_float(row.get("sell_buy_diff_chg_buff")),
            turnover_number=(_to_int(row.get("turnover_number"))
                             if row.get("turnover_number") is not None else None),
            turnover_avg_price=(_to_float(row.get("turnover_avg_price"))
                                if row.get("turnover_avg_price") is not None else None),
            period_at=row.get("period_at"),
            raw=row,
        )
