"""异动雷达调度器(PRD V1.0 §28-30):七个彼此独立的异步任务 + 全局限流。

任务:
    anomaly_a_scan   A 活跃池,多维度榜单前 N 页,10 分钟一轮(发现/升级)
    anomaly_a_track A 活跃池,批量查价跟踪池内成员,3 分钟一轮(短线时序)
    anomaly_b_scan   B 全量池,每轮深翻 1 个 sort,10 轮=180 分钟一圈(长尾发现)
    anomaly_s_scan   S 超级池,批量查价,30 秒一轮(高频确认)
    anomaly_cleanup  清理过期池成员与历史事件
    anomaly_push     聚合并推送待发异动(分级冷却去重)
    anomaly_platform_sync  分片补全 BUFF+悠悠在售数量

设计要点:
- 任一任务异常都被捕获并记录,不影响其他任务与机器人主流程(PRD §28)
- 全部 API 请求统一经过 _TokenBucket 限流,并复用现有 CSQAQ client(不绕过其重试)
- B 池保持独立深翻节奏;A/S 高频请求统一经过令牌桶排队,不改变 B 的发现周期
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from bot.anomaly.engine import AnomalyEngine, HistoryPoint, MetricSnapshot
from bot.anomaly.cost import (CostObservation, PlatformQuote, VERDICT_BLOCK,
                              VERDICT_PASS, VERDICT_SKIP, build_observation,
                              rank_observations, summarize)
from bot.anomaly.pool import POOL_A, POOL_B, POOL_S, WatchPoolManager
from bot.config import AnomalyConfig
from bot.csqaq.client import (
    RANK_SORT_BUY_PRICE_ASC,
    RANK_SORT_BUY_PRICE_DESC,
    RANK_SORT_DROP_1D,
    RANK_SORT_DROP_7D,
    RANK_SORT_DROP_30D,
    RANK_SORT_RISE_1D,
    RANK_SORT_RISE_7D,
    RANK_SORT_RISE_30D,
    RANK_SORT_SELL_PRICE_ASC,
    RANK_SORT_SELL_PRICE_DESC,
)
from bot.csqaq.market_counts import (aggregate_buff_yyyp_sell, dual_price_columns,
                                     rank_dual_price_columns)
from bot.storage.db import SnapshotStore

logger = logging.getLogger(__name__)

# 榜单排序维度:覆盖 高价/低价/高求购/低求购/1日涨跌/7日涨跌/30日涨跌
RANK_SORTS: List[str] = [
    RANK_SORT_SELL_PRICE_DESC,
    RANK_SORT_SELL_PRICE_ASC,
    RANK_SORT_BUY_PRICE_DESC,
    RANK_SORT_BUY_PRICE_ASC,
    RANK_SORT_RISE_1D,
    RANK_SORT_DROP_1D,
    RANK_SORT_RISE_7D,
    RANK_SORT_DROP_7D,
    RANK_SORT_RISE_30D,
    RANK_SORT_DROP_30D,
]

# V0.3.5:按"百分比"排序的 6 路榜单。实测(`probe_b_scan_usable_pages.py`)这类榜被分币级
# 印花/贴纸霸榜:每页 100 条里,能同时过「在售 ≥400」与「价 ≥¥5」两道闸的只有 0~1 条,
# 深翻 25 页的期望收益 ≈ 0~2 个可评分品(拦截原因 80% 是 thin、10~27% 是 price);
# 而 4 路价格榜每页稳定有 3~8 条可评分品,深翻依然有价值。故页数按类分档 + 提前止损。
RANK_SORTS_PCT: List[str] = [
    RANK_SORT_RISE_1D,
    RANK_SORT_DROP_1D,
    RANK_SORT_RISE_7D,
    RANK_SORT_DROP_7D,
    RANK_SORT_RISE_30D,
    RANK_SORT_DROP_30D,
]


def pages_for_sort(sort: str, cfg: Any) -> int:
    """该 sort 本轮翻多少页(×100 件/页)。百分比榜用单独页数,其余用深翻页数。"""
    if sort in RANK_SORTS_PCT:
        return max(1, int(getattr(cfg, "b_pages_pct_sort", 5) or 5))
    return max(1, int(getattr(cfg, "b_pages_per_sort", 25) or 25))


# ---- V0.3.20: 排序级熔断 ----
# 实测(2026-10-21):`价格_价格下降(百分比)_近30天` / `价格_价格上升(百分比)_近30天` 会在服务端**挂死**
# (80 秒级无响应、6 轮重试全超时),而同一条连接、同一个出口上其它 8 个排序只要 1.2~4.3 秒 ——
# 不是 IP 漂移、也不是我们网络的问题,是这两个排序本身在服务端不可用。
# 危害:① 每次挂死把整轮扫描拖近 90 秒;② 超时路径会被误当成漂移触发假冷却(见 client 里 V0.3.20)。
# 策略(V0.3.20 起):某个排序失败(401/429/白名单这类全局原因除外)就熔断,期间轮转自动跳过。
# 熔断时长**阶梯化**(V0.3.22):30 分钟 → 2 小时 → 6 小时封顶。
# 为什么:hung 排序每 30 分钟被"到期重试"一次,每次白烧 ~90 秒(166 分钟里白烧 12 分钟);
# 而"暂时抖一下"的排序又能很快恢复 —— 阶梯让偶发失败代价小、长期挂死代价趋近于零。
_SORT_UNHEALTHY_UNTIL: Dict[str, float] = {}
_SORT_FAILS: Dict[str, int] = {}
SORT_UNHEALTHY_COOLDOWN: float = 1800.0      # 第一次熔断 30 分钟
SORT_UNHEALTHY_STEP: float = 4.0             # 之后每次再失败 ×4
SORT_UNHEALTHY_MAX: float = 21600.0          # 封顶 6 小时

# V0.3.24:熔断状态落盘。进程重启会把内存里的熔断全清掉,导致每次重启都要重付一遍
# 那两个挂死排序的代价(2 个排序 × 6 次重试 ≈ 3 分钟 + 12 个 429)。实测今天重启 3 次
# 白烧 9 分钟。落盘后重启直接跳过已知挂死的排序,阶梯次数也一并继承。
SORT_BREAKER_STATE_PATH: str = "data/sort_breaker.json"
_SORT_STATE_LOADED = False
_SORT_STATE_SAVE_WARNED = False


def _state_path() -> Path:
    return Path(SORT_BREAKER_STATE_PATH)


def _ensure_state_loaded() -> None:
    """首次使用时把磁盘上的熔断状态读进内存(单调时钟在重启后不可比,故存墙钟换算)。"""
    global _SORT_STATE_LOADED
    if _SORT_STATE_LOADED:
        return
    _SORT_STATE_LOADED = True
    path = _state_path()
    if not path.exists():
        return
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning("[ANOMALY] 熔断状态文件损坏,忽略: %s (%s)", path, exc)
        return
    now_epoch = time.time()
    now_mono = time.monotonic()
    restored = 0
    for sort, info in (data.get("sorts") or {}).items():
        try:
            fails = int(info.get("fails") or 0)
            until_epoch = float(info.get("until_epoch") or 0)
        except (TypeError, ValueError):
            continue
        if fails > 0:
            _SORT_FAILS[sort] = fails
        remain = until_epoch - now_epoch
        if remain > 0:
            _SORT_UNHEALTHY_UNTIL[sort] = now_mono + remain
            restored += 1
    if restored or _SORT_FAILS:
        logger.info("[ANOMALY] 已恢复排序熔断状态: %d 个排序仍在熔断期,失败计数 %d 项",
                    restored, len(_SORT_FAILS))


def _save_state() -> None:
    """把熔断状态原子写入磁盘;失败只告警一次,绝不影响扫描主流程。"""
    global _SORT_STATE_SAVE_WARNED
    now_epoch = time.time()
    now_mono = time.monotonic()
    sorts: Dict[str, Dict[str, Any]] = {}
    for sort in set(_SORT_FAILS) | set(_SORT_UNHEALTHY_UNTIL):
        remain = _SORT_UNHEALTHY_UNTIL.get(sort, 0.0) - now_mono
        if remain <= 0 and not _SORT_FAILS.get(sort):
            continue
        sorts[sort] = {"fails": int(_SORT_FAILS.get(sort, 0)),
                       "until_epoch": (now_epoch + remain) if remain > 0 else 0.0}
    path = _state_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps({"version": 1, "sorts": sorts}, ensure_ascii=False, indent=1),
                       encoding="utf-8")
        os.replace(tmp, path)                  # 原子替换:崩溃也不会留下半个文件
    except Exception as exc:
        if not _SORT_STATE_SAVE_WARNED:
            _SORT_STATE_SAVE_WARNED = True
            logger.warning("[ANOMALY] 熔断状态落盘失败(不影响扫描): %s", exc)


def reset_sort_breaker_state(clear_file: bool = False) -> None:
    """清空内存熔断状态(测试/手工复位用);clear_file=True 时连磁盘文件一起删。"""
    global _SORT_STATE_LOADED, _SORT_STATE_SAVE_WARNED
    _SORT_UNHEALTHY_UNTIL.clear()
    _SORT_FAILS.clear()
    _SORT_STATE_LOADED = True                  # 复位后不再自动回读旧文件
    _SORT_STATE_SAVE_WARNED = False
    if clear_file:
        try:
            _state_path().unlink(missing_ok=True)
        except Exception:
            pass


def sort_cooldown(fails: int) -> float:
    """第 fails 次失败对应的熔断时长:30min → 2h → 6h(封顶)。"""
    return min(SORT_UNHEALTHY_MAX,
               SORT_UNHEALTHY_COOLDOWN * (SORT_UNHEALTHY_STEP ** max(0, fails - 1)))


def sort_healthy(sort: str) -> bool:
    """该排序当前是否可用(熔断期内返回 False)。"""
    _ensure_state_loaded()
    return time.monotonic() >= _SORT_UNHEALTHY_UNTIL.get(sort, 0.0)


def mark_sort_unhealthy(sort: str, reason: str = "") -> None:
    """标记某个排序不可用;连续失败次数越多,熔断越久(30min→2h→6h)。"""
    if not sort_healthy(sort):
        return                                  # 已在熔断期内,不重复计时
    fails = _SORT_FAILS.get(sort, 0) + 1
    _SORT_FAILS[sort] = fails
    cd = sort_cooldown(fails)
    _SORT_UNHEALTHY_UNTIL[sort] = time.monotonic() + cd
    logger.warning("[ANOMALY] 排序熔断 %d 分钟(连续第 %d 次): %s %s",
                   int(cd // 60), fails, sort, reason)
    _save_state()


def note_sort_ok(sort: str) -> None:
    """某个排序成功拉到数据 → 清掉它的熔断与失败计数(立即恢复正常轮转)。"""
    had = bool(_SORT_FAILS.pop(sort, None)) or sort in _SORT_UNHEALTHY_UNTIL
    _SORT_UNHEALTHY_UNTIL.pop(sort, None)
    if had:
        logger.info("[ANOMALY] 排序恢复: %s", sort)
        _save_state()


def healthy_sorts(sorts: List[str]) -> List[str]:
    """过滤掉熔断期内的排序。"""
    return [s for s in sorts if sort_healthy(s)]


def next_healthy_sort(idx: int, sorts: List[str]) -> Tuple[Optional[str], int]:
    """从 idx 起找一个未熔断的排序;返回 (sort 或 None, 新的 idx)。全部熔断返回 (None, idx)。"""
    for _ in range(len(sorts)):
        cand = sorts[idx % len(sorts)]
        idx += 1
        if sort_healthy(cand):
            return cand, idx
    return None, idx


def _row_scorable(it: Any, scan_threshold: int, price_floor: float) -> bool:
    """扫描阶段能看到的"可评分"判据:盘口齐全 + 在售达阈 + 主参考价达阈。

    价格闸(min_price)本身在评分阶段,扫描阶段看不见;这里提前算出来只用于判断
    "这一页还值不值得翻下一页",不参与任何拦截判定。
    """
    sell_num = _buff_yyyp_sell_num(it)
    if sell_num is None:
        return False
    if scan_threshold > 0 and sell_num < scan_threshold:
        return False
    return float(getattr(it, "main_price", 0) or 0) >= price_floor


def _should_early_stop(page: int, total_scorable: int, confirm_pages: int) -> bool:
    """本 sort 是否该提前止损(V0.3.5)。

    判据用**整轮累计**而不是"连续 N 页":只要这一个 sort 曾经翻出过哪怕 1 个可评分品,
    就一路翻到底,绝不因为"中间夹了几页空的"而误杀长尾(实测价格榜每页 3~8 个有货,
    百分比榜每页 0~1 个,两类标签清晰)。confirm_pages ≤ 0 表示关闭。
    """
    return confirm_pages > 0 and page >= confirm_pages and total_scorable == 0


_LEVEL_ICON = {"L1": "👀", "L2": "⚠️", "L3": "🔥", "L4": "🔥"}
_TYPE_LABEL = {
    "surge": "突然拉升",
    "dump": "突然跳水",
    "demand": "需求增强",
    "supply": "供应减少",
    "divergence": "上涨供需背离",
    "stabilize": "下跌后企稳",
    "accum": "建仓监测",       # V0.2.0 资金周期四阶段
    "launch": "启动确认",
    "distribute": "派发预警",
    "normal": "异动",
}
_COOLDOWN_BY_LEVEL = {"L2": "cooldown_l2_min", "L3": "cooldown_l3_min", "L4": "cooldown_l4_min"}


def _level_of(cfg, score: float) -> str:
    """按评分阈值归级(L1/L2/L3/L4),用于判断两次异动是否同一级别。"""
    if score >= cfg.level_extreme:
        return "L4"
    if score >= cfg.level_strong:
        return "L3"
    if score >= cfg.level_warning:
        return "L2"
    return "L1"

# 榜单源缺求购字段的补查缓存:good_id -> (BUFF+悠悠求购合计, 过期时间戳)。
# 两平台求购字段缺一时不把单平台数量冒充合计；补查成功后缓存完整合计。
_buy_fix_cache: Dict[int, Tuple[int, float]] = {}

# BUFF+悠悠在售同步的轮转游标(模块级,跨任务轮次记忆进度)
_platform_cursor: int = 0


class _TokenBucket:
    """全局令牌桶限流(PRD §29:异动模块内部控制目标 ≤ max_req_per_min)。"""

    def __init__(self, rate_per_min: int):
        self._rate = max(1, int(rate_per_min))
        self._tokens = float(self._rate)
        self._updated = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            while True:
                now = time.monotonic()
                self._tokens = min(
                    float(self._rate),
                    self._tokens + (now - self._updated) * self._rate / 60.0,
                )
                self._updated = now
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return
                await asyncio.sleep(60.0 / self._rate)


def _fmt(v: Optional[float]) -> str:
    return "N/A" if v is None else f"{v:+.1f}%"


def _rank_buy_counts(it: Any) -> Tuple[int, int, int, bool]:
    """读取榜单的 BUFF+悠悠求购，并标记两个原始字段是否同时存在。"""
    raw = getattr(it, "raw", None) or {}
    buff_raw = raw.get("buff_buy_num") if "buff_buy_num" in raw else raw.get("buffBuyNum")
    yyyp_raw = raw.get("yyyp_buy_num") if "yyyp_buy_num" in raw else raw.get("yyypBuyNum")
    if not raw:  # 测试/内部构造 RankItem 时没有 raw，按显式属性处理。
        buff_raw = getattr(it, "buff_buy_num", 0)
        yyyp_raw = getattr(it, "yyyp_buy_num", 0)
    buff = int(buff_raw or 0) if buff_raw is not None else 0
    yyyp = int(yyyp_raw or 0) if yyyp_raw is not None else 0
    return buff + yyyp, buff, yyyp, buff_raw is not None and yyyp_raw is not None


def _snapshot_from_rank(it: Any) -> MetricSnapshot:
    """榜单条目 -> 评分快照(全部取真实 API 字段,不做任何估算)。"""
    counts = aggregate_buff_yyyp_sell(getattr(it, "raw", None) or {})
    buy_total, buy_buff, buy_yyyp, buy_complete = _rank_buy_counts(it)
    return MetricSnapshot(
        good_id=int(getattr(it, "good_id", 0) or 0),
        name=getattr(it, "name", "") or "",
        price=float(getattr(it, "main_price", 0) or 0),
        sell_price=float(getattr(it, "buff_sell_price", 0) or 0),
        buy_price=float(getattr(it, "buff_buy_price", 0) or 0),
        sell_num=int(getattr(it, "buff_sell_num", 0) or 0)
                  + int(getattr(it, "yyyp_sell_num", 0) or 0),
        buy_num=buy_total,
        buy_num_observed=buy_complete,
        yyyp_sell_num=int(getattr(it, "yyyp_sell_num", 0) or 0),
        buff_yyyp_complete=bool(counts.get("sell_complete")),
        # V0.3.12:分平台原值(仅展示层拆解用) —— 完整才传,否则传 None 让文案回退合计
        buff_sell_num=(int(counts["buff_sell_num"])
                       if counts.get("sell_complete") else None),
        buff_buy_num=buy_buff if buy_complete else None,
        yyyp_buy_num=buy_yyyp if buy_complete else None,
        rate_7=float(getattr(it, "sell_price_rate_7", 0) or 0),
        rate_30=float(getattr(it, "sell_price_rate_30", 0) or 0),
        # V0.2.29:近1日涨跌仅在榜单回包真的提供时才有值(None 表示不可得,不用 7 日冒充)
        rate_1=(float(getattr(it, "sell_price_rate_1"))
                if getattr(it, "sell_price_rate_1", None) is not None else None),
        price_7=float(getattr(it, "sell_price_7", 0) or 0),
        price_30=float(getattr(it, "sell_price_30", 0) or 0),
        spread_chg=float(getattr(it, "sell_buy_diff_chg_buff", 0) or 0) or None,
    )


def _batch_value(row: Dict[str, Any], *keys: str, default: Any = None) -> Any:
    for key in keys:
        if key in row and row.get(key) is not None:
            return row.get(key)
    return default


def _batch_has(row: Dict[str, Any], *keys: str) -> bool:
    return any(key in row and row.get(key) is not None for key in keys)


def _batch_buy_counts(row: Dict[str, Any]) -> Tuple[Optional[int], Optional[int], Optional[int], bool]:
    """读取批量行情的 BUFF+悠悠求购，并明确两字段是否同时存在。"""
    buff_raw = _batch_value(row, "buffBuyNum", "buff_buy_num")
    yyyp_raw = _batch_value(row, "yyypBuyNum", "yyyp_buy_num")
    buff = int(buff_raw or 0) if buff_raw is not None else None
    yyyp = int(yyyp_raw or 0) if yyyp_raw is not None else None
    complete = buff is not None and yyyp is not None
    return ((buff + yyyp) if complete else None), buff, yyyp, complete


def _api_ip_cooling(api: Any) -> bool:
    """统一判断 CSQAQ 出口 IP 漂移冷却，避免任务先抢令牌再做必然失败请求。"""
    fn = getattr(api, "ip_drift_cooling", None)
    try:
        return bool(fn()) if callable(fn) else False
    except Exception:
        return False


def _buff_yyyp_sell_num(data: Any) -> Optional[int]:
    """仅在接口明确返回 BUFF+悠悠在售字段时返回两平台合计。

    缺少 BUFF 或悠悠字段不能按 0 计入，否则会把“数据不完整”误判成低流动性。
    """
    raw = getattr(data, "raw", None) if not isinstance(data, dict) else data
    if not isinstance(raw, dict):
        return None
    scope = raw.get("sell_scope")
    if scope is not None and scope not in ("buff_yyyp", "all"):
        return None
    counts = aggregate_buff_yyyp_sell(raw)
    if not counts.get("sell_complete"):
        return None
    try:
        return int(counts.get("sell_num") or 0)
    except (TypeError, ValueError):
        return None


async def _remove_scan_filtered(store: SnapshotStore,
                                keys: List[Tuple[int, str]],
                                tag: str, threshold: int,
                                pool: Optional[str] = None) -> None:
    """移除已确认 BUFF+悠悠在售低于扫描门槛的旧监控成员，并记录数量。"""
    if not keys:
        return
    try:
        removed = await store.async_remove_watch_pool_batch(keys, pool=pool)
        if removed:
            logger.info("[ANOMALY] %s 过滤低在售品 %d 件(BUFF+悠悠<%d),移除旧监控 %d 件",
                        tag, len(keys), threshold, removed)
    except Exception as exc:
        logger.warning("[ANOMALY] %s 清理低在售监控失败: %s", tag, exc)


def _latest_known_buy(rows: List[Dict[str, Any]]) -> Optional[Tuple[int, int, int]]:
    """返回最近一个可用的**求购合计(BUFF+悠悠)**，形如 (合计, BUFF, 悠悠)。

    V0.2.24 求购口径统一：只认 `total_buy_scope='buff_yyyp'` 的点（榜单源提供
    BUFF 求购 + 悠悠求购）。旧点(BUFF 单平台)不再作为兜底基线——否则新口径一到位
    就会出现 BUFF→BUFF+悠悠 的跨口径跳变，把量变误判成求购异动。没有新口径点时
    返回 None，调用方跳过本轮评分（与在售侧"缺字段只暂停本轮"一致）。
    """
    for h in reversed(rows or []):
        if h.get("total_buy_scope") != "buff_yyyp" or h.get("total_buy_num") is None:
            continue
        try:
            total = int(h.get("total_buy_num"))
            buff = int(h.get("buff_buy_num") or 0)
            yyyp = int(h.get("yyyp_buy_num") or 0)
        except (TypeError, ValueError):
            continue
        if total > 0:
            return total, buff, yyyp
    return None


def _snapshot_from_batch(good_id: int, name: str,
                         row: Dict[str, Any],
                         buy_carry: Optional[Tuple[int, int, int]] = None) -> MetricSnapshot:
    """批量查价行 -> 评分快照(兼容 camelCase 与 snake_case 字段)。"""
    counts = aggregate_buff_yyyp_sell(row)
    buy_total, buy_buff, buy_yyyp, buy_complete = _batch_buy_counts(row)
    if buy_total is None and buy_carry is not None:
        buy_total = buy_carry[0]
    return MetricSnapshot(
        good_id=int(good_id or 0),
        name=str(name or _batch_value(row, "name", default="")),
        price=float(_batch_value(row, "buffSellPrice", "buff_sell_price", default=0) or 0),
        sell_price=float(_batch_value(row, "buffSellPrice", "buff_sell_price", default=0) or 0),
        buy_price=float(_batch_value(row, "buffBuyPrice", "buff_buy_price", default=0) or 0),
        sell_num=int(counts.get("sell_num") or 0),
        buy_num=int(buy_total or 0),
        buy_num_observed=buy_complete,
        yyyp_sell_num=int(_batch_value(row, "yyypSellNum", "yyyp_sell_num", default=0) or 0),
        buff_yyyp_complete=bool(counts.get("sell_complete")),
        # V0.3.12:分平台原值(仅展示层拆解用)
        buff_sell_num=(int(counts["buff_sell_num"])
                       if counts.get("sell_complete") else None),
        buff_buy_num=int(buy_buff) if (buy_complete and buy_buff is not None) else None,
        yyyp_buy_num=int(buy_yyyp) if (buy_complete and buy_yyyp is not None) else None,
        rate_7=float(_batch_value(row, "sell_price_rate_7", "sellPriceRate7", default=0) or 0),
        rate_30=float(_batch_value(row, "sell_price_rate_30", "sellPriceRate30", default=0) or 0),
        price_7=float(_batch_value(row, "sell_price_7", "sellPrice7", default=0) or 0),
        price_30=float(_batch_value(row, "sell_price_30", "sellPrice30", default=0) or 0),
        spread_chg=None,
    )


def _listing_row_from_batch(good_id: int, name: str,
                            row: Dict[str, Any], ts: str,
                            buy_carry: Optional[Tuple[int, int, int]] = None) -> Dict[str, Any]:
    """把批量查价行转换成 item_listing_history 快照行。

    :param buy_carry: 批量查价回包没有任何 buy 字段时，沿用最近一次
        (合计, BUFF, 悠悠) 双平台求购值（V0.2.24），保证新口径序列连续。
    """
    sell_counts = aggregate_buff_yyyp_sell(row)
    buff_sell = int(_batch_value(row, "buffSellNum", "buff_sell_num", default=0) or 0)
    total_raw, buff_raw, yyyp_raw, buy_complete = _batch_buy_counts(row)
    if buy_complete:
        buff_buy = int(buff_raw or 0)
        yyyp_buy = int(yyyp_raw or 0)
        total_buy: Optional[int] = total_raw
    elif buy_carry is not None:
        total_buy, buff_buy, yyyp_buy = buy_carry
    else:
        total_buy, buff_buy, yyyp_buy = None, None, 0
    return {
        "good_id": int(good_id or 0),
        "name": str(name or ""),
        "ts": ts,
        "buff_sell_num": buff_sell,
        "steam_sell_num": int(_batch_value(row, "steamSellNum", "steam_sell_num", default=0) or 0),
        "buff_buy_num": buff_buy,
        # Steam 无求购机制,保留字段但不把 BUFF 单平台冒充 total_buy_num。
        "steam_buy_num": int(_batch_value(row, "steamBuyNum", "steam_buy_num", default=0) or 0),
        "yyyp_buy_num": int(yyyp_buy or 0),
        "buff_sell_price": float(_batch_value(row, "buffSellPrice", "buff_sell_price", default=0) or 0),
        "buff_buy_price": float(_batch_value(row, "buffBuyPrice", "buff_buy_price", default=0) or 0),
        # V0.3.4: 双平台价格(BUFF+悠悠)。批量查价回包字段名可能是 camelCase,
        # `dual_price_columns` 两种都认;若该接口没有悠悠价则 scope='buff_only',行为不变。
        **dual_price_columns(row),
        # 异动雷达统一使用 BUFF+悠悠口径，只有两字段齐全才写入总量。
        "total_sell_num": sell_counts.get("sell_num"),
        "total_buy_num": total_buy,
        "total_sell_scope": "buff_yyyp" if sell_counts.get("sell_complete") else None,
        "total_buy_scope": "buff_yyyp" if total_buy is not None else None,
    }


def _history_points(rows: List[Dict[str, Any]]) -> List[HistoryPoint]:
    # 新口径数据存在时，丢弃旧 BUFF 单平台点，避免跨口径制造虚假增量；
    # 全部为旧测试/历史点时保留兼容回退。
    scoped = any(h.get("total_buy_scope") == "buff_yyyp"
                 and h.get("total_buy_num") is not None for h in rows)
    source = [h for h in rows if (not scoped or h.get("total_buy_scope") == "buff_yyyp")]
    return [HistoryPoint(
        ts=h.get("ts", ""),
        price=float(h.get("buff_sell_price") or 0),
        sell_num=(int(h.get("total_sell_num") or 0)
                  if h.get("total_sell_scope") == "buff_yyyp"
                  and h.get("total_sell_num") is not None
                  else int(h.get("buff_sell_num") or 0)),
        # V0.2.24: 求购侧同样统一 BUFF+悠悠口径(有 total_buy_num 用合计,否则回退 BUFF)
        buy_num=(int(h.get("total_buy_num") or 0)
                 if h.get("total_buy_scope") == "buff_yyyp"
                 and h.get("total_buy_num") is not None
                 else int(h.get("buff_buy_num") or 0)),
        buy_price=float(h.get("buff_buy_price") or 0),
        # V0.3.12:分平台历史值(展示层拆解用)。悠悠在售列不存在,按口径标签用
        # 「合计 − BUFF」反推;scope 不是 buff_yyyp 的点一律 None(不猜)。
        buff_sell_num=(int(h.get("buff_sell_num"))
                       if h.get("buff_sell_num") is not None else None),
        yyyp_sell_num=(int(h.get("total_sell_num")) - int(h.get("buff_sell_num") or 0)
                       if (h.get("total_sell_scope") == "buff_yyyp"
                           and h.get("total_sell_num") is not None
                           and h.get("buff_sell_num") is not None
                           and int(h.get("total_sell_num")) >= int(h.get("buff_sell_num") or 0))
                       else None),
        buff_buy_num=(int(h.get("buff_buy_num"))
                      if h.get("buff_buy_num") is not None else None),
        yyyp_buy_num=(int(h.get("yyyp_buy_num"))
                      if h.get("yyyp_buy_num") is not None else None),
    ) for h in source
            if h.get("total_sell_scope") == "buff_yyyp"]


async def _hydrate_pool_names(api: Any, store: SnapshotStore,
                              rows: List[Dict[str, Any]], pool: str,
                              limit: int, bucket: _TokenBucket) -> int:
    """补全榜单池成员的 market_hash_name,为批量跟踪准备英文名缓存。"""
    done = 0
    missing = [r for r in rows if not (r.get("market_hash_name") or "").strip()]
    for r in missing[:max(0, int(limit))]:
        gid = int(r.get("good_id") or 0)
        if not gid:
            continue
        if _api_ip_cooling(api):
            # V0.3.15:漂移冷却期**只停榜单**(见 _scan_pool 里的 `榜单扫描跳过`),不再停这里。
            # 盯盘相关(英文名补全/求购补查/A·S 池跟踪/盘口同步)照常试:通得过就拿数据,
            # 通不过是 401 → 走 client 层"超时重绑绕过冷却 + 401 自愈",下一轮马上恢复。
            logger.debug("[ANOMALY] 英文名补全:漂移冷却中仍尝试(V0.3.15)")
        await bucket.acquire()
        try:
            gi = await api.get_good_info(gid)
        except Exception as exc:
            logger.warning("[ANOMALY] 补全英文名失败 pool=%s gid=%s: %s", pool, gid, exc)
            continue
        mhn = (gi or {}).get("market_hash_name") or ""
        if not mhn:
            continue
        await store.async_upsert_watch_pool([{
            "good_id": gid,
            "name": r.get("name") or "",
            "market_hash_name": mhn,
            "pool": pool,
            "score": float(r.get("score") or 0),
            "last_update": r.get("last_update") or datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "last_push_at": r.get("last_push_at"),
            "cooldown_until": r.get("cooldown_until"),
            "hit_count": int(r.get("hit_count") or 0),
            "quiet_count": int(r.get("quiet_count") or 0),
            "last_score": float(r.get("last_score") or 0),
            "anomaly_buy_low_since": r.get("anomaly_buy_low_since"),
        }])
        done += 1
    return done


def format_anomaly_alert(r: Any) -> str:
    """生成 QQ 异动告警文案(PRD §34)。

    注意:QQ markdown 模板不支持 ** 加粗,一律纯文本(此前踩过拒收的坑)。
    措辞严格遵守 PRD §20 禁用词表:不得出现 主力/放量/量价背离/资金流 等表述。
    """
    d = r.details or {}
    icon = _LEVEL_ICON.get(r.level, "⚠️")
    label = _TYPE_LABEL.get(r.anomaly_type, "异动")
    pos = d.get("position") or "中位"
    lines = [
        f"{icon} 饰品{label}异动 [{r.level}]",
        "",
        r.name,
        f"当前：¥{d.get('price', 0):,.0f}",
        f"5m：{_fmt(d.get('price_change_5m'))}  30m：{_fmt(d.get('price_change_30m'))}  "
        f"1h：{_fmt(d.get('price_change_1h'))}",
    ]
    bc, sc = d.get("buy_change"), d.get("sell_change")
    if bc is not None or sc is not None:
        lines.append(f"求购：{_fmt(bc)}  在售：{_fmt(sc)}")
    lines.append(f"7d：{_fmt(d.get('rate_7'))}  30d：{_fmt(d.get('rate_30'))}")
    lines.append("")
    lines.append(f"异动评分：{r.score:.0f}/100   位置：{pos}   方向：{r.direction}")
    if r.reasons:
        lines.append("")
        lines.append("原因：")
        lines.append(" + ".join(r.reasons))
    lines.append("")
    lines.append("⚠️ 仅代表行情异动，不代表一定上涨。")
    return "\n".join(lines)


_RANGE_TTL_SEC = 10800.0     # 单品区间缓存时长(3 小时:区间由**日收盘**定义,一天最多变一次;
                             # B 池一圈 ≈180 分钟,故每圈最多重算一次)
_RANGE_MISS_TTL_SEC = 900.0  # 查不到区间的品(样本<5 个交易日)15 分钟后再试
_RANGE_BATCH = 1500          # 单次批量拉取上限(防一轮几万件时把库拖住)


async def _ensure_price_ranges(engine: AnomalyEngine, store: SnapshotStore,
                               cfg: AnomalyConfig, good_ids: Optional[Iterable[Any]] = None
                               ) -> Dict[int, tuple]:
    """按品 TTL 刷新 `engine.range_map`(近 N 日价格区间),供派发「高位」判定使用。

    V0.3.0:派发的「近 30 日高位」此前用 `price/sell_price_30-1`(30 日**涨幅**),
    与真实区间位置一致率仅 81.3%。改用 `item_listing_history` 的**日收盘**区间。

    性能注意:整表聚合在 1.3GB 库上要 20+ 秒,所以只对**本轮要评估的品**按 good_id
    批量查。V0.3.1 实测:1092 品聚合 12 秒(ROW_NUMBER 改 GROUP BY+MAX(ts) 提速
    2.2 倍、结果完全一致),且改走**只读连接**(WAL 下一 read 一致快照、不占 store 写锁),
    故不再阻塞落库;再叠加 3 小时逐品缓存,稳态下每轮只补新品(小批量)。
    """
    if not good_ids:
        return engine.range_map
    now = time.time()
    want: List[int] = []
    for g in good_ids:
        try:
            gid = int(g or 0)
        except (TypeError, ValueError):
            continue
        if not gid:
            continue
        if now - engine.range_map_ts.get(gid, 0.0) >= _RANGE_TTL_SEC:
            want.append(gid)
    if not want:
        return engine.range_map
    days = max(1, int(getattr(cfg, "distrib_range_days", 30)))
    t0 = time.time()
    try:
        m = await store.async_get_price_range_map(days, 5, want[:_RANGE_BATCH])
    except Exception as exc:
        logger.warning("[ANOMALY] 价格区间刷新失败(沿用旧缓存): %s", exc)
        return engine.range_map
    got = set(m.keys())
    for gid in want[:_RANGE_BATCH]:
        # 查不到的品(样本不足)用更短的 TTL,等它攒够日收盘再补,不被 3 小时缓存锁住
        engine.range_map_ts[gid] = now if gid in got else now - (_RANGE_TTL_SEC - _RANGE_MISS_TTL_SEC)
    if m:
        engine.range_map.update(m)
        logger.info("[ANOMALY] 价格区间已更新 %d 件(近 %d 日日收盘,查询 %d 件耗时 %.1fs,缓存 %d 件)",
                    len(m), days, min(len(want), _RANGE_BATCH), time.time() - t0,
                    len(engine.range_map))
    return engine.range_map


async def _scan_pool(cfg: AnomalyConfig, api: Any, store: SnapshotStore,
                     engine: AnomalyEngine, pool_mgr: WatchPoolManager,
                     bucket: _TokenBucket, sorts: List[str], pages: int,
                     tag: str, allow_downgrade: bool = True,
                     early_stop_pages: int = 0) -> List[Any]:
    """拉榜单 -> 写时序 -> 批量查历史 -> 评分 -> 更新三层池 -> 落事件。

    :param allow_downgrade: B 池传 False,避免稀疏数据把 A/S 池成员打回 B 池。
    :param early_stop_pages: 连续多少页"一个可评分品都没有"就停止翻本 sort 的后续页
        (0=关闭;仅 B 池启用)。判据见 `_row_scorable`。
    """
    items: Dict[int, Any] = {}
    scan_threshold = max(0, int(getattr(cfg, "min_scan_sell_num", 0)))
    price_floor = float(getattr(cfg, "min_price", 0) or 0)
    filtered_keys: List[Tuple[int, str]] = []
    incomplete_keys: List[Tuple[int, str]] = []
    filtered_count = 0
    incomplete_count = 0
    for sort in sorts:
        scorable_total = 0
        for page in range(1, int(pages) + 1):
            if _api_ip_cooling(api):
                logger.info("[ANOMALY] %s 榜单扫描跳过: CSQAQ IP 漂移冷却中", tag)
                return []
            await bucket.acquire()
            try:
                rows = await api.get_rank_list(page_index=page, page_size=100, sort=sort)
            except Exception as exc:
                logger.warning("[ANOMALY] 榜单失败 sort=%s page=%s: %s", sort, page, exc)
                # V0.3.21:401(漂移)/429(全局限频)/白名单 都是**全局**原因,不该熔断某个排序;
                # 只有"这个排序自己挂死"(超时/连接错)才熔断。
                if not any(t in str(exc) for t in ("401", "429", "白名单")):
                    mark_sort_unhealthy(sort, f"(page {page} 失败)")
                break
            note_sort_ok(sort)              # V0.3.22:拉到数据就清熔断与失败计数
            if not rows:
                break
            if early_stop_pages > 0:
                scorable_total += sum(1 for it in rows
                                      if _row_scorable(it, scan_threshold, price_floor))
                if _should_early_stop(page, scorable_total, early_stop_pages):
                    logger.info("[ANOMALY] %s 提前止损 sort=%s: 已翻 %d 页仍无可评分品"
                                "(在售≥%d 且价≥¥%g),本 sort 共 %d 页,省 %d 页",
                                tag, sort, page, scan_threshold, price_floor,
                                int(pages), max(0, int(pages) - page))
                    break
            for it in rows:
                gid = int(getattr(it, "good_id", 0) or 0)
                sell_num = _buff_yyyp_sell_num(it)
                if gid and sell_num is None:
                    incomplete_count += 1
                    incomplete_keys.append((gid, getattr(it, "name", "") or ""))
                    continue
                if (gid and scan_threshold > 0 and sell_num < scan_threshold):
                    filtered_count += 1
                    filtered_keys.append((gid, getattr(it, "name", "") or ""))
                    continue
                if gid and gid not in items:
                    items[gid] = it
            if len(rows) < 100:
                break

    complete_gids = set(items)
    # 同一 good_id 可能出现在多个排序榜；只要任一回包给出完整 BUFF+悠悠，
    # 就以完整记录为准，不因另一个排序的缺字段/低值副本误删。
    filtered_keys = [key for key in filtered_keys if key[0] not in complete_gids]
    incomplete_keys = [key for key in incomplete_keys if key[0] not in complete_gids]
    filtered_count = len(filtered_keys)
    incomplete_count = len(incomplete_keys)
    if filtered_keys:
        await _remove_scan_filtered(
            store, filtered_keys, tag, scan_threshold,
            pool=(POOL_B if tag == POOL_B else None))
    if incomplete_keys:
        # B 池硬规则：任一扫描发现缺 BUFF/悠悠字段，都不能留下已有 B 记录；
        # pool 条件确保不会误删已有 A/S 成员。
        removed = await store.async_remove_watch_pool_batch(
            incomplete_keys, pool=POOL_B)
        if removed:
            logger.info("[ANOMALY] %s 缺 BUFF/悠悠在售字段 %d 件,移除 B 池 %d 件",
                        tag, len(incomplete_keys), removed)
    if not items:
        logger.info("[ANOMALY] %s 池本轮无数据(过滤低在售%d件,缺完整盘口%d件,BUFF+悠悠<%d)",
                    tag, filtered_count, incomplete_count, scan_threshold)
        return []
    if filtered_count or incomplete_count:
        logger.info(
            "[ANOMALY] %s 预过滤低在售%d件、缺完整盘口%d件(BUFF+悠悠<%d/缺字段),剩余%d件进入评分",
            tag, filtered_count, incomplete_count, scan_threshold, len(items))

    # 1) 写时序快照(先落盘,再评分:当前点 ts=now 不会被历史窗口选中)
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    snap_rows = [{
        "good_id": gid,
        "name": getattr(it, "name", "") or "",
        "ts": now_str,
        "buff_sell_num": int(getattr(it, "buff_sell_num", 0) or 0),
        "steam_sell_num": int(getattr(it, "steam_sell_num", 0) or 0),
        "yyyp_sell_num": int(getattr(it, "yyyp_sell_num", 0) or 0),
        "buff_buy_num": int(getattr(it, "buff_buy_num", 0) or 0),
        "steam_buy_num": int(getattr(it, "steam_buy_num", 0) or 0),
        # V0.2.24: 榜单源自带悠悠求购 → 求购侧统一 BUFF+悠悠 合计
        "yyyp_buy_num": int(getattr(it, "yyyp_buy_num", 0) or 0),
        "buff_sell_price": float(getattr(it, "buff_sell_price", 0) or 0),
        "buff_buy_price": float(getattr(it, "buff_buy_price", 0) or 0),
        # V0.3.4: 双平台价格(BUFF+悠悠)—— 数量口径早就统一了,价格此前只有 BUFF
        **rank_dual_price_columns(it),
        "total_sell_num": int(getattr(it, "buff_sell_num", 0) or 0)
                          + int(getattr(it, "yyyp_sell_num", 0) or 0),
        "total_buy_num": (_rank_buy_counts(it)[0]
                          if _rank_buy_counts(it)[3] else None),
        "total_sell_scope": "buff_yyyp",
        "total_buy_scope": "buff_yyyp" if _rank_buy_counts(it)[3] else None,
    } for gid, it in items.items()]
    try:
        await store.async_save_listing_history_batch(snap_rows)
    except Exception as exc:
        logger.exception("[ANOMALY] 写时序失败: %s", exc)

    # 2) 批量查历史(一次 SQL,避免逐件往返)
    try:
        phase_hours = max(1.0, float(getattr(cfg, "phase_window_minutes", 60)) / 60.0)
        # 至少覆盖一个完整阶段窗口并留出边界余量，避免总结快照恰好落在 since 边界被漏掉。
        series = await store.async_get_listing_series_batch(
            list(items.keys()), hours=max(2.0, phase_hours * 3.0))
    except Exception as exc:
        logger.exception("[ANOMALY] 查历史失败: %s", exc)
        series = {}
    # 2.5) 榜单源缺求购字段补查(漏报修复):get_rank_list 对极少数品不返回 buff_buy_num(恒 0),
    #      会被 min_buy_num 闸门永久拦截(如手套武器箱真实求购 1099)。对 buy_num==0 的品补查
    #      get_good_info(一次一个),结果缓存 300s;顺手把全平台在售写入 multi_snapshot。
    fix_candidates = [gid for gid, it in items.items()
                      if gid and not _rank_buy_counts(it)[3]]
    buy_fix: Dict[int, int] = {}
    if fix_candidates:
        now_ts = time.time()
        for gid in fix_candidates:
            cached = _buy_fix_cache.get(gid)
            if cached and cached[1] > now_ts:
                buy_fix[gid] = cached[0]
                continue
            if _api_ip_cooling(api):
                # V0.3.15:同英文名补全 —— 求购价是推送卡片要用的,漂移期不再整段跳过
                logger.debug("[ANOMALY] 求购补查:漂移冷却中仍尝试(V0.3.15)")
            await bucket.acquire()
            try:
                info = await api.get_good_info(gid)
                d = info or {}   # get_good_info 已解包返回 goods_info dict(勿再取 .data)
                buff_bn = int(d.get("buff_buy_num") or 0)
                yyyp_bn = int(d.get("yyyp_buy_num") or 0)
                bn = buff_bn + yyyp_bn
                buy_fix[gid] = bn
                _buy_fix_cache[gid] = (bn, now_ts + 300)
                if d.get("name"):
                    if d.get("turnover_number") is not None:
                        await store.async_upsert_turnover_snapshot([{
                            "good_id": gid, "name": d.get("name", ""),
                            "turnover_number": d.get("turnover_number"),
                            "turnover_avg_price": d.get("turnover_avg_price"),
                            "period_at": d.get("period_at"),
                        }])
                    await store.async_upsert_multi_snapshot([{
                        "good_id": gid, "name": d.get("name", ""),
                        "buff_sell_num": int(d.get("buff_sell_num") or 0),
                        "buff_buy_num": buff_bn,
                        "yyyp_buy_num": yyyp_bn,
                        "steam_sell_num": int(d.get("steam_sell_num") or 0),
                        "yyyp_sell_num": int(d.get("yyyp_sell_num") or 0),
                        "c5_sell_num": int(d.get("c5_sell_num") or 0),
                        "igxe_sell_num": int(d.get("igxe_sell_num") or 0),
                        "eco_sell_num": int(d.get("eco_sell_num") or 0),
                        "sell_scope": "buff_yyyp" if aggregate_buff_yyyp_sell(d).get("sell_complete") else None,
                        "buff_sell_price": float(d.get("buff_sell_price") or 0),
                        "buff_buy_price": float(d.get("buff_buy_price") or 0),
                        # V0.3.4: 双平台价格(BUFF+悠悠)
                        **dual_price_columns(d),
                    }])
            except Exception as exc:
                logger.warning("[ANOMALY] 补查求购失败 gid=%s: %s", gid, exc)
        if buy_fix:
            logger.info("[ANOMALY] 榜单缺求购字段补查 %d 个(漏报修复)", len(buy_fix))

    # 3) 评分
    # V0.3.0:先把本轮要评估的品的近 N 日价格区间补进缓存(派发「高位」真实口径)
    await _ensure_price_ranges(engine, store, cfg, items.keys())
    results: List[Any] = []
    for gid, it in items.items():
        hist_source = series.get(gid, [])
        scoped_hist = any(h.get("total_buy_scope") == "buff_yyyp"
                          and h.get("total_buy_num") is not None for h in hist_source)
        hist = [HistoryPoint(
            ts=h.get("ts", ""),
            price=float(h.get("buff_sell_price") or 0),
            sell_num=(int(h.get("total_sell_num") or 0)
                      if h.get("total_sell_scope") == "buff_yyyp"
                      and h.get("total_sell_num") is not None
                      else int(h.get("buff_sell_num") or 0)),
            buy_num=(int(h.get("total_buy_num") or 0)
                     if h.get("total_buy_scope") == "buff_yyyp"
                     and h.get("total_buy_num") is not None
                     else int(h.get("buff_buy_num") or 0)),
            buy_price=float(h.get("buff_buy_price") or 0),
            # V0.3.12:分平台历史值(展示层拆解;悠悠在售由 scope 保护反推)
            buff_sell_num=(int(h.get("buff_sell_num"))
                           if h.get("buff_sell_num") is not None else None),
            yyyp_sell_num=(int(h.get("total_sell_num")) - int(h.get("buff_sell_num") or 0)
                           if (h.get("total_sell_scope") == "buff_yyyp"
                               and h.get("total_sell_num") is not None
                               and h.get("buff_sell_num") is not None
                               and int(h.get("total_sell_num")) >= int(h.get("buff_sell_num") or 0))
                           else None),
            buff_buy_num=(int(h.get("buff_buy_num"))
                          if h.get("buff_buy_num") is not None else None),
            yyyp_buy_num=(int(h.get("yyyp_buy_num"))
                          if h.get("yyyp_buy_num") is not None else None),
        ) for h in hist_source
                if h.get("total_sell_scope") == "buff_yyyp"
                and (not scoped_hist or h.get("total_buy_scope") == "buff_yyyp")]
        try:
            snap = _snapshot_from_rank(it)
            if snap.buy_num == 0 and buy_fix.get(gid):
                snap.buy_num = buy_fix[gid]   # 补查真实求购,避免被 min_buy_num 误拦
                snap.buy_num_observed = True
            result = engine.evaluate(snap, hist)
            # 补查请求失败时,0 只是“求购字段未知”,不能当成真实死盘移除。
            if snap.buy_num == 0 and gid in fix_candidates and gid not in buy_fix:
                result.details["data_incomplete"] = "buy_num_missing"
            results.append(result)
        except Exception as exc:
            logger.warning("[ANOMALY] 评分失败 item=%s: %s", getattr(it, "name", ""), exc)

    # 4) 更新三层池 + 5) 落事件(含冷却判定)
    try:
        mhn_map = {
            int(gid): str(getattr(it, "market_hash_name", "") or "")
            for gid, it in items.items()
            if getattr(it, "market_hash_name", "")
        }
        changes = await pool_mgr.sync(results, mhn_map=mhn_map,
                                      allow_downgrade=allow_downgrade)
        logger.info("[ANOMALY] %s scan done items=%d 升级%s 降级%s",
                    tag, len(items), len(changes.get("upgrade", [])),
                    len(changes.get("downgrade", [])))
        # 结构化统计:闸门拦截分布 + 放行等级分布 —— 直接回答「为什么没推/被哪道闸拦」
        gated: Dict[str, int] = {}
        lvl_cnt: Dict[str, int] = {}
        for r in results:
            rej = (r.details or {}).get("rejected")
            if rej:
                gated[rej] = gated.get(rej, 0) + 1
            else:
                lvl_cnt[r.level] = lvl_cnt.get(r.level, 0) + 1
        stat: List[str] = []
        if gated:
            stat.append("拦截:" + " ".join(f"{k}={v}" for k, v in sorted(gated.items())))
        if lvl_cnt:
            stat.append("放行:" + " ".join(f"{k}={v}" for k, v in sorted(lvl_cnt.items())))
        if stat:
            logger.info("[ANOMALY] %s 评分分布 %s", tag, " | ".join(stat))
        for c in changes.get("upgrade", []):
            logger.info("[WATCH] 升级 %s", c)
        for c in changes.get("downgrade", []):
            logger.info("[WATCH] 降级 %s", c)
    except Exception as exc:
        logger.exception("[ANOMALY] 更新监控池失败: %s", exc)

    await _persist_events(cfg, store, results, tag)
    return results


async def _persist_events(cfg: AnomalyConfig, store: SnapshotStore,
                          results: List[Any], tag: str) -> None:
    """把达到预警等级(L2+)的异动写入 anomaly_events,并按分级冷却去重。"""
    if not results:
        return
    now = datetime.now()
    rows = [r for r in results if r.level in ("L2", "L3", "L4")]
    if not rows:
        return

    # 冷却从常驻表 anomaly_push_cooldown 读取(独立于三层池:出池 DELETE 不会清掉冷却,
    # 否则同一品反复进出池会被当成"新异动"重复推送,如咆哮刷屏)。
    cooldowns = await store.async_list_anomaly_cooldown()
    # V0.3.16 每品每日推送上限(最后一道兜底)。放在所有冷却判定**之后**执行:
    # 冷却管"同一波别重复报",上限管"这一品今天已经报够了"——6822/7474 那种
    # 分数在 73↔96 振荡的品,三道放行口每轮都能命中一条,只有硬上限拦得住。
    # 超限的**仍然落库**(push_status='capped'),/分析 与 signal_outcomes 照常可用。
    cap = int(getattr(cfg, "max_push_per_item_per_day", 0) or 0)
    today_counts: Dict[int, int] = {}
    if cap > 0:
        try:
            today_counts = await store.async_count_anomaly_pushes_today(
                datetime.now().strftime("%Y-%m-%d 00:00:00"))
        except Exception as exc:
            logger.warning("[ANOMALY] 读取当日推送计数失败,本轮不设上限: %s", exc)
            cap = 0
    out: List[Dict[str, Any]] = []
    cooldown_skipped = 0
    capped_skipped = 0
    for r in rows:
        cd = cooldowns.get((int(r.good_id or 0), r.name)) or {}
        until = _parse_dt(cd.get("cooldown_until"))
        last_score = float(cd.get("last_score") or 0)
        last_type = str(cd.get("last_type") or "")
        upgraded = (r.score - last_score) >= cfg.cooldown_upgrade_delta
        if until and now < until and not upgraded:
            cooldown_skipped += 1  # 冷却期内且评分未明显升级 -> 不重复报
            continue
        minutes = int(getattr(cfg, _COOLDOWN_BY_LEVEL.get(r.level, "cooldown_l2_min"), 10))
        # V0.0.13: 冷却已过期但评分与上次几乎相同且同级 = 同一波异动延续,
        # 顺延冷却不重推(X 射线 12:25/12:48 同分 71.3 重复推送事故)。
        # V0.2.0: 同波判定加类型一致 —— 建仓→启动→派发是形态升级(递进信息),
        # 即使评分相同也必须推,不能被同波规则吞掉。last_type 为空(老记录)视为同波,
        # 保证升级部署后老冷却记录的防重复语义不变。
        # 只有评分明显变化(±≥5)/跨级/周期形态升级,才视为新异动再次推送。
        if (until and now >= until
                and abs(r.score - last_score) < 5.0
                and _level_of(cfg, r.score) == _level_of(cfg, last_score)
                and last_type in ("", r.anomaly_type)):
            extended = (now + timedelta(minutes=minutes)).strftime("%Y-%m-%d %H:%M:%S")
            await store.async_set_anomaly_cooldown(
                int(r.good_id or 0), r.name, extended, last_score, last_type=r.anomaly_type)
            cooldown_skipped += 1
            continue
        # V0.3.16:这一品当天的推送额度是否已用尽(含本轮前面已计入的)
        over_cap = cap > 0 and today_counts.get(int(r.good_id or 0), 0) >= cap
        d = r.details or {}
        # 周期阶段事件:reason 用「盘口 1 小时」对比块(起点→终点),推送文案直接展示
        phase_info = d.get("phase") or {}
        phase_lines = phase_info.get("lines") or []
        reason_text = ("\n".join(phase_lines) if phase_lines
                       else (" + ".join(r.reasons) if r.reasons else ""))
        out.append({
            "good_id": r.good_id,
            "name": r.name,
            "anomaly_type": r.anomaly_type,
            "drop_pct": 0,
            "abs_drop": 0,
            "window_min": 0,
            "sell_prev": 0,
            "sell_cur": int(d.get("sell_num") or 0),
            "level": r.level,
            "detail": " / ".join(r.reasons) if r.reasons else "",
            "score": r.score,
            "pool": tag,
            "price": float(d.get("price") or 0),
            "price_change_5m": d.get("price_change_5m") or 0,
            "price_change_30m": d.get("price_change_30m") or 0,
            # V0.2.29 口径修正:price_change_1d 只写**真实近1日**涨跌(榜单无该字段时写 NULL,
            # 让下游判定"缺失"而不是"持平");7 日涨跌改由新增的 rate_7 列承载 ——
            # 此前把 rate_7 写进 1d 列,下游 alert/commands 又当"近1日"喂给量价解算,
            # 造成 7日 vs 30分钟 的尺度错配。
            "price_change_1d": d.get("rate_1"),
            "rate_7": d.get("rate_7") or 0,
            "buy_change": d.get("buy_change") or 0,
            "sell_change": d.get("sell_change") or 0,
            "position": d.get("position") or "",
            "reason": reason_text,
            # V0.3.16:额度用尽 → 落库但不推(推送任务只取 pending)
            "push_status": "capped" if over_cap else "pending",
            "push_detail": (f"每品每日上限 {cap} 条已用尽(V0.3.16)" if over_cap else None),
            "_cooldown_until": (now + timedelta(minutes=minutes)).strftime("%Y-%m-%d %H:%M:%S"),
        })
        today_counts[int(r.good_id or 0)] = today_counts.get(int(r.good_id or 0), 0) + 1
        if over_cap:
            capped_skipped += 1
            logger.info("[ANOMALY] 每品每日上限 %d 条已用尽:%s(%s 级,池=%s)—— 本次落库不推送",
                        cap, r.name, r.level, tag)
    if not out:
        if cooldown_skipped:
            logger.info("[ANOMALY] 全部被冷却拦截(%d 件,池=%s):评分未升级,防重复推送",
                        cooldown_skipped, tag)
        return
    if cooldown_skipped:
        logger.info("[ANOMALY] 落库 %d 件,冷却跳过 %d 件(池=%s)",
                    len(out), cooldown_skipped, tag)
    if capped_skipped:
        logger.info("[ANOMALY] 其中 %d 件因每品每日上限只落库不推送(池=%s)", capped_skipped, tag)
    cooldowns = {(r["good_id"], r["name"]): (r.pop("_cooldown_until"), float(r.get("score") or 0),
                                             r.get("anomaly_type") or "normal")
                 for r in out}
    # V0.3.27 成本门槛观察:落库前先把「折后边际」算出来(只记录,不改推送)。
    # 与事件共用同一个秒级 ts,便于 cost_gate_observe 与 signal_outcomes 精确 join。
    ts_now = now.strftime("%Y-%m-%d %H:%M:%S")
    await _observe_cost(cfg, store, out, ts_now, tag)
    try:
        await store.async_insert_anomaly_events(out, ts=ts_now)
        await _mark_cooldown(store, cooldowns)
        logger.info("[ANOMALY] 落库异动 %d 件(池=%s)", len(out), tag)
    except Exception as exc:
        logger.exception("[ANOMALY] 写异动事件失败: %s", exc)


async def _observe_cost(cfg: AnomalyConfig, store: SnapshotStore,
                        rows: List[Dict[str, Any]], ts: str, tag: str) -> None:
    """V0.3.27 成本门槛【观察模式】:算每条候选的折后边际,记日志 + 落库。

    为什么要算:一笔倒货的往返成本 = 卖出费(2.5%/2.0%)+ 提现(1%,最低 ¥2),
    实测池内买卖价差中位 4.2%、¥50 以下 49% —— 而引擎对 0.26% 的波动也计分。
    本函数**只观察不拦截**:mode=observe 时推送行为与本版本之前完全一致。

    口径(见 bot/anomaly/cost.py 顶部注释):
      · 买入价取**两平台里更便宜的有效报价**(只看 BUFF 会系统性高估成本)
      · 基准价用该平台**近 N 日日收盘中位价**(30 日窗口在池内商品上点数不足,已弃用)
      · 两平台价差 >15% 判离群(实测 55% 原始样本是单边无接盘的假价)
      · 折扣 = (基准 - 买入价)/基准;边际 = 折扣 - 成本;边际高 = 排序靠前
    """
    gate = getattr(cfg, "cost_gate", None)
    if not rows or not gate or not getattr(gate, "enable", False):
        return
    now = datetime.now()
    obs_list: List[Any] = []
    for r in rows:
        gid = int(r.get("good_id") or 0)
        if gid <= 0:
            continue
        try:
            raw = await store.async_get_price_quotes(gid, days=int(gate.baseline_days))
        except Exception as exc:      # 取数失败不该影响落库与推送
            logger.warning("[COST] 取报价失败 %s: %s", r.get("name"), exc)
            continue
        quotes: Dict[str, Any] = {}
        for platform, q in (raw or {}).items():
            age = 1e9
            if q.get("ask_ts"):
                qts = _parse_dt(q["ask_ts"])
                if qts:
                    age = max(0.0, (now - qts).total_seconds() / 60.0)
            quotes[platform] = PlatformQuote(
                platform=platform, ask=float(q.get("ask") or 0),
                bid=float(q.get("bid") or 0), sell_num=int(q.get("sell_num") or 0),
                ask_age_min=age, closes=list(q.get("closes") or []))
        obs_list.append(build_observation(
            gid, ts, quotes, gate, name=str(r.get("name") or ""), pool=tag,
            level=str(r.get("level") or ""), score=float(r.get("score") or 0),
            push_status=str(r.get("push_status") or "")))
    if not obs_list:
        return
    rank_observations(obs_list)
    for o in obs_list:
        logger.info("%s", o.log_line())
    logger.info("[COST] 汇总 %s(池=%s)", summarize(obs_list), tag)
    try:
        await store.async_insert_cost_observations([o.as_row() for o in obs_list])
    except Exception as exc:
        logger.exception("[COST] 落库失败: %s", exc)


async def pool_mgr_rows(store: SnapshotStore) -> Dict[Any, Dict[str, Any]]:
    try:
        rows = await store.async_list_watch_pool(limit=10000)
    except Exception:
        return {}
    return {(int(r.get("good_id") or 0), r.get("name") or ""): r for r in rows}


async def _mark_cooldown(store: SnapshotStore,
                         cooldowns: Dict[tuple, tuple]) -> None:
    """按真实 good_id 回填推送冷却到常驻表 anomaly_push_cooldown(出池不丢)。"""
    for (gid, name), (until, last_score, last_type) in cooldowns.items():
        try:
            await store.async_set_anomaly_cooldown(int(gid or 0), name, until,
                                                   last_score=float(last_score or 0),
                                                   last_type=str(last_type or ""))
        except Exception as exc:
            logger.warning("[ANOMALY] 回填推送冷却失败 %s: %s", name, exc)


def _parse_dt(raw: Any) -> Optional[datetime]:
    if not raw:
        return None
    try:
        return datetime.strptime(str(raw), "%Y-%m-%d %H:%M:%S")
    except (ValueError, TypeError):
        return None


# ---------------- 独立任务 ----------------

async def anomaly_a_scan_loop(cfg: AnomalyConfig, api: Any, store: SnapshotStore,
                              engine: AnomalyEngine, pool_mgr: WatchPoolManager,
                              bucket: _TokenBucket) -> None:
    """A 活跃池榜单发现:多维度榜单前 N 页,跟踪由独立批量任务负责。"""
    interval_min = max(1, int(getattr(cfg, "a_discover_interval_min",
                                     cfg.a_interval_min)))
    interval = interval_min * 60
    logger.info("[ANOMALY] A 池发现已开启,每 %d 分钟一轮(10 sort × %d 页)",
                interval_min, cfg.a_pages)
    while True:
        try:
            # V0.3.20:跳过熔断期内的排序(实测「近30天」两个排序会在服务端挂死 80 秒)
            rounds = healthy_sorts(RANK_SORTS)
            if not rounds:
                logger.warning("[ANOMALY] A 池发现:所有排序都在熔断期,本轮跳过")
            else:
                await _scan_pool(cfg, api, store, engine, pool_mgr, bucket,
                                 rounds, cfg.a_pages, POOL_A)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception("[ANOMALY] A 池扫描异常: %s", exc)
        await asyncio.sleep(interval)


_a_track_cursor: int = 0


async def anomaly_a_track_loop(cfg: AnomalyConfig, api: Any, store: SnapshotStore,
                               engine: AnomalyEngine, pool_mgr: WatchPoolManager,
                               bucket: _TokenBucket) -> None:
    """A 池成员批量跟踪:独立于榜单发现,持续积累 30m/1h 时序。"""
    if not getattr(cfg, "a_track_enable", True):
        logger.info("[ANOMALY] A 池批量跟踪已关闭")
        return
    interval = max(30, int(getattr(cfg, "a_track_interval_sec", 180)))
    batch_limit = max(50, int(getattr(cfg, "a_track_batch_size", cfg.a_size)))
    hydrate_limit = max(0, int(getattr(cfg, "a_track_hydrate_per_round", 5)))
    scan_threshold = max(0, int(getattr(cfg, "min_scan_sell_num", 0)))
    logger.info("[ANOMALY] A 池批量跟踪已开启,每 %d 秒最多跟踪 %d 件",
                interval, batch_limit)
    global _a_track_cursor
    while True:
        try:
            rows_a = await store.async_list_watch_pool(pool=POOL_A,
                                                       limit=max(cfg.a_size, batch_limit))
            # V0.3.28 回扫看门狗:顺带把「最久没被扫描」的池成员拉进来一起查价。
            # 实测 B 池 1116 件里 952 件超 3 天没被碰过 —— 只靠 10 个 sort 轮转永远轮不到。
            stale_rows, stale_ids = await _fetch_stale_pool_rows(cfg, store)
            await _ensure_price_ranges(engine, store, cfg,
                                       [r.get("good_id") for r in (rows_a or []) + stale_rows])
            rows = list(rows_a or [])
            if rows:
                hydrated = await _hydrate_pool_names(
                    api, store, rows, POOL_A, hydrate_limit, bucket)
                if hydrated:
                    rows = await store.async_list_watch_pool(
                        pool=POOL_A, limit=max(cfg.a_size, batch_limit))
            if stale_rows:
                seen = {int(r.get("good_id") or 0) for r in rows}
                for r in stale_rows:
                    gid = int(r.get("good_id") or 0)
                    if gid and gid not in seen:
                        rows.append(r)
                        seen.add(gid)
            if rows:
                named = [r for r in rows if (r.get("market_hash_name") or "").strip()]
                if named:
                    # A 池通常不超过 800 件;若超出上限,轮转而不是每轮偏置前缀。
                    count = min(len(named), batch_limit)
                    start = _a_track_cursor % len(named)
                    selected = [named[(start + i) % len(named)] for i in range(count)]
                    _a_track_cursor = (start + count) % len(named)
                    snap_by_gid: Dict[int, MetricSnapshot] = {}
                    pending: List[Tuple[int, str, Dict[str, Any], str]] = []
                    mhn_map: Dict[int, str] = {}
                    filtered_keys: List[Tuple[int, str]] = []
                    incomplete_count = 0
                    polled_gids: List[int] = []
                    now_ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    for i in range(0, len(selected), 50):
                        chunk = selected[i:i + 50]
                        if _api_ip_cooling(api):
                            # V0.3.15:A 池跟踪是盯盘雷达本体,漂移期不再整段跳过
                            logger.debug("[ANOMALY] A 池批量跟踪:漂移冷却中仍尝试(V0.3.15)")
                        await bucket.acquire()
                        try:
                            prices = await api.get_price_by_market_hash_name(
                                [r["market_hash_name"] for r in chunk])
                        except Exception as exc:
                            logger.warning("[ANOMALY] A 池批量跟踪查价失败: %s", exc)
                            continue
                        for r in chunk:
                            mhn = r.get("market_hash_name") or ""
                            row = prices.get(mhn) or {}
                            gid = int(r.get("good_id") or 0)
                            if not row or not gid:
                                continue
                            sell_num = _buff_yyyp_sell_num(row)
                            if sell_num is None:
                                incomplete_count += 1
                                continue
                            if (scan_threshold > 0 and sell_num < scan_threshold):
                                filtered_keys.append((gid, r.get("name") or ""))
                                continue
                            name = r.get("name") or ""
                            pending.append((gid, name, row, mhn))
                            polled_gids.append(gid)
                            mhn_map[gid] = mhn
                    await _remove_scan_filtered(
                        store, filtered_keys, "A跟踪", scan_threshold)
                    if incomplete_count:
                        logger.info("[ANOMALY] A跟踪跳过缺 BUFF+悠悠 在售字段 %d 件",
                                    incomplete_count)
                    if pending:
                        # V0.2.24: 先读历史算求购 carry-forward，再落快照(口径连续)。
                        series = await store.async_get_listing_series_batch(
                            [gid for gid, _n, _r, _m in pending], hours=2.0)
                        snap_rows: List[Dict[str, Any]] = []
                        carry_by_gid: Dict[int, Optional[Tuple[int, int, int]]] = {}
                        no_baseline: set = set()
                        for gid, name, row, _mhn in pending:
                            has_buy = _batch_buy_counts(row)[3]
                            carry = None if has_buy else _latest_known_buy(series.get(gid, []))
                            carry_by_gid[gid] = carry
                            if not has_buy and carry is None:
                                no_baseline.add(gid)
                            snap_by_gid[gid] = _snapshot_from_batch(gid, name, row)
                            snap_rows.append(_listing_row_from_batch(
                                gid, name, row, now_ts, buy_carry=carry))
                        try:
                            await store.async_save_listing_history_batch(snap_rows)
                        except Exception as exc:
                            logger.warning("[ANOMALY] A 池跟踪快照落库失败: %s", exc)
                        # 回扫过的品即便这一轮没进评分(缺求购基线等),也要打上
                        # 「看过了」的时间戳,否则它会一直排在回扫队首被反复查价。
                        if stale_ids:
                            touch = [g for g in polled_gids if g in stale_ids]
                            if touch:
                                try:
                                    await store.async_touch_watch_pool(touch, now_ts)
                                except Exception as exc:
                                    logger.warning("[ANOMALY] 回扫时间戳刷新失败: %s", exc)
                        results = []
                        for gid, snap in snap_by_gid.items():
                            hist = series.get(gid, [])
                            carry = carry_by_gid.get(gid)
                            if carry is not None:
                                snap.buy_num = carry[0]
                                snap.buy_num_observed = False
                            elif gid in no_baseline:
                                logger.debug(
                                    "[ANOMALY] A 池求购基线缺失 gid=%s,等待累积", gid)
                                continue
                            result = engine.evaluate(snap, _history_points(hist))
                            if carry is not None:
                                result.details["data_incomplete"] = "buy_carried_forward"
                            results.append(result)
                        total = len(results)
                        await pool_mgr.sync(results, mhn_map=mhn_map)
                        # 回扫批只刷新池数据;推送仍归 A 成员与 B 发现扫描,
                        # 否则一次性回扫 200 件会把群刷成流水账。
                        if stale_ids and not bool(getattr(cfg, "rescan_push_enable", False)):
                            pushable = [r for r in results if r.good_id not in stale_ids]
                        else:
                            pushable = results
                        await _persist_events(cfg, store, pushable, "A")
                        logger.info("[ANOMALY] A 池跟踪完成 items=%d(回扫 %d,推送候选 %d)",
                                    total, len(stale_ids), len(pushable))
            
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception("[ANOMALY] A 池批量跟踪异常: %s", exc)
        await asyncio.sleep(interval)


async def anomaly_b_scan_loop(cfg: AnomalyConfig, api: Any, store: SnapshotStore,
                              engine: AnomalyEngine, pool_mgr: WatchPoolManager,
                              bucket: _TokenBucket) -> None:
    """B 全量池:每轮深翻 1 个 sort,10 轮 = b_interval_min 分钟完成一圈。"""
    if not cfg.b_enable:
        logger.info("[ANOMALY] B 池扫描已关闭")
        return
    per_round = max(1, cfg.b_interval_min // max(1, len(RANK_SORTS)))
    interval = per_round * 60
    logger.info("[ANOMALY] B 池扫描已开启,每 %d 分钟深翻 1 个 sort"
                "(价格榜 %d 页 / 百分比榜 %d 页;整轮零可评分品时第 %d 页止损),"
                "%d 轮 ≈ %d 分钟一圈",
                per_round, max(1, int(getattr(cfg, "b_pages_per_sort", 25) or 25)),
                max(1, int(getattr(cfg, "b_pages_pct_sort", 5) or 5)),
                max(0, int(getattr(cfg, "b_early_stop_pages", 2) or 0)),
                len(RANK_SORTS), per_round * len(RANK_SORTS))
    idx = 0
    while True:
        try:
            # V0.3.20:轮转到熔断期内的排序就顺延到下一个可用的(全熔断则本轮休眠)
            sort, idx = next_healthy_sort(idx, RANK_SORTS)
            if sort is None:
                logger.warning("[ANOMALY] B 深翻:所有排序都在熔断期,本轮跳过")
                await asyncio.sleep(max(60, interval))
                continue
            await _scan_pool(cfg, api, store, engine, pool_mgr, bucket,
                             [sort], pages_for_sort(sort, cfg), POOL_B,
                             allow_downgrade=False,
                             early_stop_pages=max(0, int(
                                 getattr(cfg, "b_early_stop_pages", 2) or 0)))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception("[ANOMALY] B 池扫描异常: %s", exc)
        await asyncio.sleep(interval)


async def anomaly_s_scan_loop(cfg: AnomalyConfig, api: Any, store: SnapshotStore,
                              engine: AnomalyEngine, pool_mgr: WatchPoolManager,
                              bucket: _TokenBucket) -> None:
    """S 超级池:批量查价(50 件/批),每 s_interval_sec 秒一轮。

    英文名获取:S 池成员来自榜单(无 market_hash_name),首次进入时用 /info/good
    补全并写回 watch_pool,每轮最多补 3 个以免打爆额度。
    """
    scan_threshold = max(0, int(getattr(cfg, "min_scan_sell_num", 0)))
    logger.info("[ANOMALY] S 池扫描已开启,每 %d 秒一轮", cfg.s_interval_sec)
    while True:
        try:
            rows = await store.async_list_watch_pool(pool=POOL_S, limit=cfg.s_size)
            if not rows:
                await asyncio.sleep(max(5, cfg.s_interval_sec))
                continue
            await _ensure_price_ranges(engine, store, cfg,
                                       [r.get("good_id") for r in rows])
            # 补全缺失的英文名(每轮限 3 个)
            await _hydrate_pool_names(api, store, rows, POOL_S, 3, bucket)
            # 批量查价(每 50 件 1 次请求)
            named = [r for r in rows if (r.get("market_hash_name") or "").strip()]
            for i in range(0, len(named), 50):
                chunk = named[i:i + 50]
                if _api_ip_cooling(api):
                    # V0.3.15:S 池(30 秒一轮)是盯盘最核心的一层,漂移期不再整段跳过 ——
                    # 否则一次漂移就等于把整个雷达停掉(实测 2026-10-13 停了 90 分钟)
                    logger.debug("[ANOMALY] S 池批量跟踪:漂移冷却中仍尝试(V0.3.15)")
                await bucket.acquire()
                try:
                    prices = await api.get_price_by_market_hash_name(
                        [r["market_hash_name"] for r in chunk])
                except Exception as exc:
                    logger.warning("[ANOMALY] S 池查价失败: %s", exc)
                    continue
                # V0.2.24: 先读历史(用于求购 carry-forward)，再落本轮快照。
                # 批量查价回包没有任何 buy 字段 → 求购沿用最近一次 BUFF+悠悠 合计，
                # 保证"求购 2h 变化"是同一口径的连续序列（老 BUFF 单平台点不参与）。
                now_ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                filtered_keys: List[Tuple[int, str]] = []
                pending: List[Tuple[int, str, Dict[str, Any]]] = []
                for r in chunk:
                    row = prices.get(r["market_hash_name"]) or {}
                    gid = int(r.get("good_id") or 0)
                    if not row or not gid:
                        continue
                    sell_num = _buff_yyyp_sell_num(row)
                    if sell_num is None:
                        continue
                    if (scan_threshold > 0 and sell_num < scan_threshold):
                        filtered_keys.append((gid, r.get("name") or ""))
                        continue
                    pending.append((gid, r.get("name") or "", row))
                await _remove_scan_filtered(
                    store, filtered_keys, "S扫描", scan_threshold)
                if not pending:
                    continue
                series = await store.async_get_listing_series_batch(
                    [gid for gid, _n, _r in pending], hours=2.0)
                valid_rows = []
                snap_by_gid: Dict[int, MetricSnapshot] = {}
                carry_by_gid: Dict[int, Optional[Tuple[int, int, int]]] = {}
                no_baseline: set = set()
                for gid, name, row in pending:
                    has_buy = _batch_buy_counts(row)[3]
                    carry = None if has_buy else _latest_known_buy(series.get(gid, []))
                    carry_by_gid[gid] = carry
                    if not has_buy and carry is None:
                        # 冷启动没有新口径求购基线:只保留在售时序,本轮不评分(等基线累积)。
                        logger.debug("[ANOMALY] S 池求购基线缺失 gid=%s,等待累积", gid)
                        no_baseline.add(gid)
                    valid_rows.append(_listing_row_from_batch(gid, name, row, now_ts,
                                                              buy_carry=carry))
                    snap_by_gid[gid] = _snapshot_from_batch(gid, name, row)
                try:
                    await store.async_save_listing_history_batch(valid_rows)
                except Exception as exc:
                    logger.warning("[ANOMALY] S 池快照落库失败: %s", exc)
                results = []
                for gid, snap in snap_by_gid.items():
                    hist = series.get(gid, [])
                    carry = carry_by_gid.get(gid)
                    if carry is not None:
                        snap.buy_num = carry[0]
                        snap.buy_num_observed = False
                    elif gid in no_baseline:
                        continue   # 无求购基线:不评分,避免用 0 求购误判
                    result = engine.evaluate(snap, _history_points(hist))
                    if carry is not None:
                        result.details["data_incomplete"] = "buy_carried_forward"
                    results.append(result)
                if results:
                    await pool_mgr.sync(results, mhn_map={
                        int(r.get("good_id") or 0): r.get("market_hash_name") or ""
                        for r in chunk if r.get("market_hash_name")
                    })
                    await _persist_events(cfg, store, results, POOL_S)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception("[ANOMALY] S 池扫描异常: %s", exc)
        await asyncio.sleep(max(5, cfg.s_interval_sec))


async def _fetch_stale_pool_rows(cfg: AnomalyConfig, store: SnapshotStore
                                 ) -> Tuple[List[Dict[str, Any]], set]:
    """取「超时没被扫描」的 B 池成员一起回扫,返回 (行, good_id 集合)。

    为什么由 A 跟踪循环来扛:它是池内品唯一的高频查价通道(3 分钟一轮),
    B 深翻按 sort 轮转(10 个 sort × 18 分钟一圈),长尾成员永远排不上号。
    这里按 last_update 最旧优先取,每轮限量,刷完自然退出队列。
    """
    hours = int(getattr(cfg, "stale_rescan_hours", 0) or 0)
    limit = int(getattr(cfg, "stale_rescan_per_cycle", 0) or 0)
    if hours <= 0 or limit <= 0:
        return [], set()
    try:
        rows = await store.async_list_stale_watch_pool(hours, limit, pool=POOL_B)
    except Exception as exc:
        logger.warning("[ANOMALY] 回扫队列读取失败: %s", exc)
        return [], set()
    rows = [r for r in rows if (r.get("market_hash_name") or "").strip()]
    ids = {int(r.get("good_id") or 0) for r in rows}
    if rows:
        logger.info("[ANOMALY] 回扫看门狗:本轮补扫 %d 件(>%dh 未更新)", len(rows), hours)
    return rows, ids


async def _sink_stale_members(cfg: AnomalyConfig, store: SnapshotStore) -> None:
    """沉底:池子占用超过阈值时,把「进池够久且历史最高分从未达标」的 B 成员清出。

    口径刻意保守 —— 用 best_score(历史最高)而不是当前分,并且只在池子接近
    容量上限时触发。目的不是「池越小越好」,而是别让从没冒过信号的成员长期
    占着发现层的名额,把新候选挤掉。
    """
    days = int(getattr(cfg, "stale_sink_days", 0) or 0)
    b_size = int(getattr(cfg, "b_size", 0) or 0)
    if days <= 0 or b_size <= 0:
        return
    ratio = float(getattr(cfg, "stale_sink_trigger_ratio", 0.8) or 0.8)
    counts = await store.async_count_watch_pool()
    occupied = int(counts.get(POOL_B, 0))
    if occupied < b_size * ratio:
        return
    per_cycle = max(1, int(getattr(cfg, "stale_sink_per_cycle", 50) or 50))
    removed = await store.async_purge_stale_watch_pool(
        days, float(cfg.b_to_a), per_cycle, POOL_B)
    if removed:
        logger.info("[WATCH] B 沉底 %d 件(%d 天从未达 %.0f 分,占用 %d/%d)",
                    len(removed), days, cfg.b_to_a, occupied, b_size)


async def anomaly_cleanup_loop(cfg: AnomalyConfig, store: SnapshotStore) -> None:
    """清理:降级超时的 S 池成员、删除过期异动事件。"""
    logger.info("[ANOMALY] 清理任务已开启")
    while True:
        try:
            rows = await store.async_list_watch_pool(pool=POOL_S, limit=cfg.s_size * 2)
            now = datetime.now()
            rearm_min = max(0, int(getattr(cfg, "s_rearm_minutes", 45)))
            for r in rows:
                entered = _parse_dt(r.get("enter_time"))
                if entered and (now - entered) > timedelta(minutes=cfg.s_max_minutes):
                    await store.async_upsert_watch_pool([{
                        "good_id": int(r.get("good_id") or 0),
                        "name": r.get("name") or "",
                        "market_hash_name": r.get("market_hash_name") or "",
                        "pool": POOL_A,
                        "score": float(r.get("score") or 0),
                        # V0.3.28:降级也要重置 enter_time,否则下次升回 S 时
                        # TTL 立刻又是"已过期",立刻再被踢下来 → 抖动。
                        "enter_time": now.strftime("%Y-%m-%d %H:%M:%S"),
                        "last_update": now.strftime("%Y-%m-%d %H:%M:%S"),
                        "last_push_at": r.get("last_push_at"),
                        "cooldown_until": r.get("cooldown_until"),
                        "hit_count": 0,
                        "quiet_count": 0,
                        "last_score": float(r.get("last_score") or 0),
                        "anomaly_buy_low_since": r.get("anomaly_buy_low_since"),
                        "s_rearm_at": ((now + timedelta(minutes=rearm_min))
                                       .strftime("%Y-%m-%d %H:%M:%S") if rearm_min else None),
                        "best_score": float(r.get("best_score") or 0),
                    }])
                    logger.info("[WATCH] S -> A item=%s reason=ttl", r.get("name"))
            # V0.3.28 沉底:池子快满时,清掉「进池多日从没达标」的占位成员,
            # 把容量让给新候选(不满就不删 —— 空池乱清没有收益)。
            await _sink_stale_members(cfg, store)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception("[ANOMALY] 清理异常: %s", exc)
        await asyncio.sleep(300)


async def anomaly_platform_sync_loop(cfg: AnomalyConfig, api: Any, store: SnapshotStore,
                                     bucket: _TokenBucket) -> None:
    """BUFF+悠悠同步:每 10 分钟分片补查池内品 get_good_info 的两平台在售,写入 multi_snapshot。

    预算:共享限流 13 req/min,本任务默认每周期 ≤20 个品(=2 req/min),按 S>A>B 评分排序
    轮转覆盖,一轮(约 1000+ 品)约 5~6 小时。数据供推送展示「BUFF+悠悠在售」。
    """
    global _platform_cursor
    batch_size = max(1, int(getattr(cfg, "platform_sync_batch_size", 20)))
    logger.info("[ANOMALY] BUFF+悠悠在售同步已开启(每 10 分钟,≤%d 品/周期)", batch_size)
    while True:
        try:
            rows = await store.async_list_watch_pool(limit=3000)
            items = []
            for r in rows:
                gid = int(r.get("good_id") or 0)
                if gid:
                    pool = r.get("pool") or POOL_B
                    prio = 0 if pool == POOL_S else (1 if pool == POOL_A else 2)
                    # 保留原池记录：详情同步成功后顺手回填 market_hash_name，
                    # 不新增请求，解决历史 B 池长期为空英文名的问题。
                    items.append((prio, -float(r.get("score") or 0), gid,
                                  r.get("name") or "", r))
            if not items:
                await asyncio.sleep(600)
                continue
            items.sort(key=lambda x: (x[0], x[1]))
            if _platform_cursor >= len(items):
                _platform_cursor = 0
            chunk = items[_platform_cursor:_platform_cursor + batch_size]
            _platform_cursor += batch_size
            for prio, nscore, gid, name, pool_row in chunk:
                if _api_ip_cooling(api):
                    # V0.3.15:盘口同步(分平台在售/求购)是推送卡片与 /分析 的数据来源,
                    # 实测每天被漂移跳过 24 次(≈17% 周期)导致显示旧值 —— 不再整段跳过
                    logger.debug("[ANOMALY] BUFF+悠悠同步:漂移冷却中仍尝试(V0.3.15)")
                await bucket.acquire()
                try:
                    info = await api.get_good_info(gid)
                    d = info or {}   # get_good_info 已解包返回 goods_info dict
                    if not d.get("name"):
                        continue
                    mhn = str(d.get("market_hash_name") or d.get("marketHashName") or "").strip()
                    if mhn and not str(pool_row.get("market_hash_name") or "").strip():
                        await store.async_upsert_watch_pool([{
                            "good_id": gid,
                            "name": name,
                            "market_hash_name": mhn,
                            "pool": pool_row.get("pool") or POOL_B,
                            "score": float(pool_row.get("score") or 0),
                            "enter_time": pool_row.get("enter_time"),
                            "last_update": pool_row.get("last_update"),
                            "last_push_at": pool_row.get("last_push_at"),
                            "cooldown_until": pool_row.get("cooldown_until"),
                            "hit_count": int(pool_row.get("hit_count") or 0),
                            "quiet_count": int(pool_row.get("quiet_count") or 0),
                            "last_score": float(pool_row.get("last_score") or 0),
                            "quality_reject_count": int(pool_row.get("quality_reject_count") or 0),
                            "anomaly_buy_low_since": pool_row.get("anomaly_buy_low_since"),
                        }])
                        logger.info("[ANOMALY] 回填英文名 pool=%s gid=%s mhn=%s",
                                    pool_row.get("pool") or POOL_B, gid, mhn)
                    scan_threshold = max(0, int(getattr(cfg, "min_scan_sell_num", 0)))
                    counts = aggregate_buff_yyyp_sell(d)
                    total_sell = int(counts.get("sell_num") or 0)
                    if (scan_threshold > 0 and counts.get("sell_complete")
                            and total_sell < scan_threshold):
                        removed = await store.async_remove_watch_pool_batch(
                            [(gid, name)])
                        logger.info(
                            "[ANOMALY] BUFF+悠悠同步过滤低在售 %s: %d<%d,移除监控%d件",
                            d.get("name") or name, total_sell, scan_threshold, removed)
                        continue
                    if d.get("turnover_number") is not None:
                        await store.async_upsert_turnover_snapshot([{
                            "good_id": gid, "name": d.get("name", ""),
                            "turnover_number": d.get("turnover_number"),
                            "turnover_avg_price": d.get("turnover_avg_price"),
                            "period_at": d.get("period_at"),
                        }])
                    await store.async_upsert_multi_snapshot([{
                        "good_id": gid, "name": d.get("name", ""),
                        "buff_sell_num": int(d.get("buff_sell_num") or 0),
                        "buff_buy_num": int(d.get("buff_buy_num") or 0),
                        "steam_sell_num": int(d.get("steam_sell_num") or 0),
                        "yyyp_sell_num": int(d.get("yyyp_sell_num") or 0),
                        "c5_sell_num": int(d.get("c5_sell_num") or 0),
                        "igxe_sell_num": int(d.get("igxe_sell_num") or 0),
                        "eco_sell_num": int(d.get("eco_sell_num") or 0),
                        "sell_scope": "buff_yyyp" if aggregate_buff_yyyp_sell(d).get("sell_complete") else None,
                        "buff_sell_price": float(d.get("buff_sell_price") or 0),
                        "buff_buy_price": float(d.get("buff_buy_price") or 0),
                        # V0.3.4: 双平台价格(BUFF+悠悠)
                        **dual_price_columns(d),
                    }])
                except Exception as exc:
                    logger.warning("[ANOMALY] 全平台同步失败 gid=%s: %s", gid, exc)
            logger.info("[ANOMALY] BUFF+悠悠在售同步 %d 个(游标 %d/%d)", len(chunk), _platform_cursor, len(items))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception("[ANOMALY] 全平台同步异常: %s", exc)
        await asyncio.sleep(600)


async def anomaly_summary_snapshot_loop(cfg: AnomalyConfig, api: Any,
                                        store: SnapshotStore,
                                        bucket: _TokenBucket) -> None:
    """每两小时用榜单批量接口写入全量周期总结快照。

    只使用榜单接口，不逐品调用 /info/good；完整 BUFF+悠悠在售且合计达到
    summary_min_sell_num 的品才落库。约 10 sort × 25 页 = 250 请求/2 小时，
    仍由异动任务共用的令牌桶统一限流。
    """
    if not getattr(cfg, "summary_enable", True):
        logger.info("[ANOMALY] 全量周期总结快照已关闭")
        return
    interval = max(30, int(getattr(cfg, "summary_interval_min", 120))) * 60
    pages = max(1, int(getattr(cfg, "summary_pages_per_sort", 25)))
    threshold = max(0, int(getattr(cfg, "summary_min_sell_num", 200)))
    logger.info("[ANOMALY] 全量周期总结快照已开启(每%d分钟,每sort≤%d页,BUFF+悠悠≥%d)",
                interval // 60, pages, threshold)
    while True:
        started = time.monotonic()
        try:
            items: Dict[int, Any] = {}
            requests = 0
            incomplete = 0
            abort_cycle = False
            # V0.3.20:跳过熔断期内的排序(实测「近30天」两个排序会在服务端挂死 80 秒)
            for sort in healthy_sorts(RANK_SORTS):
                for page in range(1, pages + 1):
                    if _api_ip_cooling(api):
                        abort_cycle = True
                        break
                    await bucket.acquire()
                    requests += 1
                    try:
                        rows = await api.get_rank_list(
                            page_index=page, page_size=100, sort=sort)
                    except Exception as exc:
                        logger.warning("[ANOMALY] 总结快照榜单失败 sort=%s page=%s: %s",
                                       sort, page, exc)
                        # V0.3.21:同上,401/429/白名单 是全局原因,不熔断排序
                        if not any(t in str(exc) for t in ("401", "429", "白名单")):
                            mark_sort_unhealthy(sort, f"(快照 page {page} 失败)")
                        # IP 漂移/白名单冷却期间，停止本轮剩余分页，避免无效请求继续占额度。
                        if any(token in str(exc) for token in ("IP 漂移冷却", "HTTP 401", "白名单")):
                            abort_cycle = True
                        break
                    note_sort_ok(sort)      # V0.3.22:快照拉到了也清熔断
                    if not rows:
                        break
                    for it in rows:
                        gid = int(getattr(it, "good_id", 0) or 0)
                        if not gid or gid in items:
                            continue
                        sell_num = _buff_yyyp_sell_num(it)
                        if sell_num is None:
                            incomplete += 1
                            continue
                        if sell_num >= threshold:
                            items[gid] = it
                    if len(rows) < 100:
                        break
                if abort_cycle:
                    break

            if items:
                ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                snap_rows = [{
                    "good_id": gid,
                    "name": getattr(it, "name", "") or "",
                    "ts": ts,
                    "buff_sell_num": int(getattr(it, "buff_sell_num", 0) or 0),
                    "steam_sell_num": int(getattr(it, "steam_sell_num", 0) or 0),
                    "yyyp_sell_num": int(getattr(it, "yyyp_sell_num", 0) or 0),
                    "buff_buy_num": int(getattr(it, "buff_buy_num", 0) or 0),
                    "steam_buy_num": int(getattr(it, "steam_buy_num", 0) or 0),
                    "yyyp_buy_num": int(getattr(it, "yyyp_buy_num", 0) or 0),
                    "buff_sell_price": float(getattr(it, "buff_sell_price", 0) or 0),
                    "buff_buy_price": float(getattr(it, "buff_buy_price", 0) or 0),
                    # V0.3.4: 双平台价格(BUFF+悠悠)
                    **rank_dual_price_columns(it),
                    "total_sell_num": int(getattr(it, "buff_sell_num", 0) or 0)
                                       + int(getattr(it, "yyyp_sell_num", 0) or 0),
                    # V0.2.24: 求购侧同样 BUFF+悠悠(榜单源自带 yyyp_buy_num)
                    "total_buy_num": (_rank_buy_counts(it)[0]
                                      if _rank_buy_counts(it)[3] else None),
                    "total_sell_scope": "buff_yyyp",
                    "total_buy_scope": "buff_yyyp" if _rank_buy_counts(it)[3] else None,
                } for gid, it in items.items()]
                await store.async_save_listing_history_batch(snap_rows)
            elapsed = time.monotonic() - started
            logger.info("[ANOMALY] 全量周期总结快照完成 items=%d incomplete=%d requests=%d elapsed=%.1fs",
                        len(items), incomplete, requests, elapsed)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception("[ANOMALY] 全量周期总结快照异常: %s", exc)
        await asyncio.sleep(max(30, interval - max(0.0, time.monotonic() - started)))


async def anomaly_push_loop(cfg: AnomalyConfig, store: SnapshotStore, client: Any,
                            api: Any = None, bucket: Optional[_TokenBucket] = None) -> None:
    """推送:定期把未推送的异动事件发到目标群(纯文本,避开 QQ markdown 拒收)。

    增强:每次推送前取市场环境快照(5 分钟缓存),失败静默不阻塞推送。
    """
    from bot.anomaly import alert as _alert
    logger.info("[ANOMALY] 推送任务已开启")
    while True:
        try:
            # 市场环境需要 CSQAQ 配置(cfg 是 AnomalyConfig 无 csqaq 属性,须从 client._cfg 取)
            csqaq_cfg = getattr(getattr(client, "_cfg", None), "csqaq", None)
            mkt = (await _alert.fetch_market_snapshot(csqaq_cfg, store)
                   if csqaq_cfg else None)
            market_line = (mkt or {}).get("line")
            pending = await store.async_list_pending_anomalies(
                top_n=cfg.push_top_n, min_score=cfg.level_warning)
            if pending:
                targets = list(getattr(client, "_anomaly_targets", []) or [])
                if targets:
                    # 群功能开关过滤:只推「异动」功能开启的群(无记录默认开,管理员私聊 /功能 控制)
                    feats = await store.async_get_group_features()
                    targets = [g for g in targets if feats.get((g, "anomaly"), True)]
                if targets:
                    # 推送前全平台流动性复核(实时 get_good_info,pending 通常 ≤5 品,预算可控):
                    # BUFF+悠悠在售按价格带定下限,不足=流动性差,跳过不推;
                    # 复核数据写 multi_snapshot 供文案展示。
                    # 复核失败(IP 401/限频/超时)→ 改用本地 multi_snapshot 缓存兜底复核;
                    # 无可用缓存 → 暂时拦截,IP 恢复后重试。
                    # 已明确低于流动性下限 → 记录为 rejected_liquidity 并结束该事件，
                    # 避免永久待推、每 30 秒重复打接口。一个饰品有多条待推事件时只复核一次。
                    to_push = []
                    grouped: Dict[tuple, List[Dict[str, Any]]] = {}
                    for row in pending:
                        row_gid = int(row.get("good_id") or 0)
                        key = (row_gid, "" if row_gid else str(row.get("name") or ""))
                        grouped.setdefault(key, []).append(row)
                    for item_rows in grouped.values():
                        # DB 已按时间倒序返回，积压期间同一饰品只发送最新状态。
                        r = item_rows[0]
                        gid = int(r.get("good_id") or 0)
                        if gid and api is not None:
                            if bucket is not None:
                                await bucket.acquire()
                            d: Dict[str, Any] = {}
                            cached = None
                            try:
                                info = await api.get_good_info(gid)
                                d = info or {}
                            except Exception as exc:
                                logger.warning("[ANOMALY] 推送复核失败(转缓存兜底) %s: %s",
                                                r.get("name"), exc)
                                cached = (await store.async_get_multi_snapshot([gid])).get(gid)
                                if cached:
                                    d = dict(cached)
                            if not d and not cached:
                                # 实时与缓存都拿不到流动性数据:拦截,等 IP 恢复自动重试
                                logger.warning("[ANOMALY] 推送复核拦截(无流动性数据,待重试) %s",
                                                r.get("name"))
                                continue
                            if d.get("name") and not cached:
                                # 即便随后因流动性不足被拒绝，也保存这次有效实时快照，
                                # 供其他链路使用并避免形成“成功查询却无缓存”的空洞。
                                await store.async_upsert_multi_snapshot([{
                                    "good_id": gid, "name": d.get("name", ""),
                                    "buff_sell_num": int(d.get("buff_sell_num") or 0),
                                    "buff_buy_num": int(d.get("buff_buy_num") or 0),
                                    "steam_sell_num": int(d.get("steam_sell_num") or 0),
                                    "yyyp_sell_num": int(d.get("yyyp_sell_num") or 0),
                                    "c5_sell_num": int(d.get("c5_sell_num") or 0),
                                    "igxe_sell_num": int(d.get("igxe_sell_num") or 0),
                                    "eco_sell_num": int(d.get("eco_sell_num") or 0),
                                    "sell_scope": "buff_yyyp" if aggregate_buff_yyyp_sell(d).get("sell_complete") else None,
                                    "buff_sell_price": float(d.get("buff_sell_price") or 0),
                                    "buff_buy_price": float(d.get("buff_buy_price") or 0),
                                    # V0.3.4: 双平台价格(BUFF+悠悠)
                                    **dual_price_columns(d),
                                }])
                            # BUFF+悠悠在售按价格带定下限:
                            # 低价品靠量(BUFF+悠悠<200 的 ¥300 以下品无倒手空间);
                            # 中高价品挂单天然少,下限放宽。
                            counts = aggregate_buff_yyyp_sell(d)
                            if not counts.get("sell_complete"):
                                # 详情/缓存字段不完整时不能把缺失平台按 0 算入，
                                # 否则会误拒绝；保留事件，等下一次拿到完整口径再复核。
                                logger.warning(
                                    "[ANOMALY] 推送复核拦截(BUFF+悠悠字段不完整,待重试) %s",
                                    r.get("name"))
                                continue
                            total_sell = int(counts.get("sell_num") or 0)
                            price_now = float(d.get("buff_sell_price") or 0) or float(r.get("price") or 0)
                            need = (cfg.platform_sell_low if price_now < cfg.platform_sell_tier
                                    else cfg.platform_sell_high)
                            src = "缓存" if cached else "实时"
                            if total_sell < need:
                                detail = f"{src}BUFF+悠悠在售{total_sell}<下限{need}"
                                await _resolve_push_events(
                                    store, item_rows, "rejected_liquidity", detail)
                                logger.info(
                                    "[ANOMALY] 推送复核拒绝(BUFF+悠悠在售过少[%s]) %s: "
                                    "%d(需≥%d),归档%d条事件",
                                    src, r.get("name"), total_sell, need, len(item_rows))
                                continue
                        to_push.append((r, item_rows))
                    if not to_push:
                        await asyncio.sleep(30)
                        continue
                    # BUFF+悠悠在售展示:从 multi_snapshot 取最新(无记录则省略,不阻塞推送)
                    multi_map = await store.async_get_multi_snapshot(
                        [int(r.get("good_id") or 0) for r, _ in to_push])
                    sent_rows = []
                    superseded_rows = []
                    for r, item_rows in to_push:
                        text = _format_from_row(r, market=market_line, mkt=mkt,
                                                multi=multi_map.get(int(r.get("good_id") or 0)))
                        delivered = False
                        for gid in targets:
                            try:
                                # 兼容旧版/测试 client:返回 None 代表调用完成；
                                # 只有明确返回 False 才视为发送失败。
                                result = await client.push_to_group(gid, text)
                                if result is not False:
                                    delivered = True
                            except Exception as exc:
                                logger.warning("[ANOMALY] 推送失败 %s: %s", gid, exc)
                        if delivered:
                            sent_rows.append(r)
                            superseded_rows.extend(item_rows[1:])
                            logger.info("[PUSH] send anomaly item=%s score=%s",
                                        r.get("name"), r.get("score"))
                        else:
                            # 不回填 last_push_at，下一轮继续尝试，避免 QQ 临时故障造成漏推。
                            logger.warning("[ANOMALY] 所有目标群发送失败,保留待推状态: %s",
                                           r.get("name"))
                    await _mark_pushed(store, sent_rows)
                    await _resolve_push_events(
                        store, superseded_rows, "superseded",
                        "同一饰品已有更新事件成功发送")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception("[ANOMALY] 推送任务异常: %s", exc)
        await asyncio.sleep(30)


async def _mark_pushed(store: SnapshotStore, rows: List[Dict[str, Any]]) -> None:
    """仅将实际至少送达一个目标群的事件按 id 标记为已推送。"""
    ids = [int(r.get("id") or 0) for r in rows if int(r.get("id") or 0) > 0]
    if not ids:
        return
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        await store.async_mark_anomalies_pushed_ids(ids, now)
    except Exception as exc:
        logger.warning("[ANOMALY] 回填推送标记失败 ids=%s: %s", ids, exc)


async def _resolve_push_events(store: SnapshotStore, rows: List[Dict[str, Any]],
                               status: str, detail: str = "") -> None:
    """归档未实际发送但已无需重试的事件。"""
    ids = [int(r.get("id") or 0) for r in rows if int(r.get("id") or 0) > 0]
    if not ids:
        return
    try:
        await store.async_resolve_anomaly_push_ids(ids, status, detail)
    except Exception as exc:
        logger.warning("[ANOMALY] 回填推送处理状态失败 ids=%s status=%s: %s",
                       ids, status, exc)


def _format_from_row(r: Dict[str, Any], market: Optional[str] = None,
                     mkt: Optional[Dict[str, Any]] = None,
                     multi: Optional[Dict[str, Any]] = None) -> str:
    """从 DB 行渲染告警文案(推送任务用,走 alert 模块的增强版:状态+预测+市场+BUFF+悠悠在售)。"""
    from bot.anomaly import alert as _alert
    return _alert.format_row_alert(r, market=market, mkt=mkt, multi=multi)


async def start_anomaly_tasks(cfg: AnomalyConfig, api: Any, store: SnapshotStore,
                              client: Any) -> List[asyncio.Task]:
    """创建异动雷达的全部任务(彼此独立,单任务异常不影响其他)。"""
    engine = AnomalyEngine(cfg)
    pool_mgr = WatchPoolManager(cfg, store)
    bucket = _TokenBucket(cfg.max_req_per_min)

    # 启动时清理历史监控池中的低在售成员，避免旧版本积累的长尾品继续占用
    # 平台同步/批量跟踪额度。后续新扫描仍在入口处实时过滤。
    scan_threshold = max(0, int(getattr(cfg, "min_scan_sell_num", 0)))
    if scan_threshold > 0:
        try:
            removed = await store.async_purge_watch_pool_low_sell(scan_threshold)
            if removed:
                logger.info("[ANOMALY] 启动清理低在售历史监控 %d 件(BUFF+悠悠<%d)",
                            removed, scan_threshold)
        except Exception as exc:
            logger.warning("[ANOMALY] 启动清理低在售历史监控失败: %s", exc)

    # 推送目标群:vip_only 时只推 VIP 群;push_group_openids 非空时进一步限定白名单
    # (V0.2.0:异动功能暂时只对 1105775575 开放)
    targets = list(cfg_vip_groups(client)) if cfg.vip_only else list(all_groups(client))
    allow = list(getattr(cfg, "push_group_openids", []) or [])
    if allow:
        targets = [g for g in targets if g in allow]
    try:
        client._anomaly_targets = targets
    except Exception:
        pass

    names = [
        ("anomaly_a_scan", anomaly_a_scan_loop(cfg, api, store, engine, pool_mgr, bucket)),
        ("anomaly_a_track", anomaly_a_track_loop(cfg, api, store, engine, pool_mgr, bucket)),
        ("anomaly_b_scan", anomaly_b_scan_loop(cfg, api, store, engine, pool_mgr, bucket)),
        ("anomaly_s_scan", anomaly_s_scan_loop(cfg, api, store, engine, pool_mgr, bucket)),
        ("anomaly_cleanup", anomaly_cleanup_loop(cfg, store)),
        ("anomaly_push", anomaly_push_loop(cfg, store, client, api, bucket)),
        ("anomaly_platform_sync", anomaly_platform_sync_loop(cfg, api, store, bucket)),
        ("anomaly_summary_snapshot", anomaly_summary_snapshot_loop(cfg, api, store, bucket)),
    ]
    tasks = []
    for name, coro in names:
        t = asyncio.create_task(coro, name=name)
        tasks.append(t)
    logger.info("[ANOMALY] 已启动 %d 个任务,推送目标群 %d 个", len(tasks), len(targets))
    return tasks


def cfg_vip_groups(client: Any) -> List[str]:
    try:
        return list(getattr(client, "_cfg", None).vip.group_openids or [])
    except Exception:
        return []


def all_groups(client: Any) -> List[str]:
    try:
        cfg = getattr(client, "_cfg", None)
        return list(cfg.vip.group_openids or []) + list(cfg.free.group_openids or [])
    except Exception:
        return []
