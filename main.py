# -*- coding: utf-8 -*-
"""CS2 饰品行情 QQ 机器人 - 程序入口。

运行:  python main.py
"""

import asyncio
import logging
import os
import sys
import time
from logging.handlers import RotatingFileHandler
from typing import Any, Dict, Optional

# ---- 日志先行配置：必须在 import botpy 之前 ----
# botpy 在 import 时会调用 logging.basicConfig 抢占 root logger，导致我们这里的
# FileHandler 因 "root 已有 handler" 而不生效、bot.log 一直为空(历史坑)。
# 故提前 basicConfig(force=True) 强制接管，并用 filter 只把业务日志(bot.*/main)
# 写入 bot.log；botpy 等框架日志默认丢弃（需要看时用 CS2QQBOT_LOG_CONSOLE=1 跑前台）。
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
os.makedirs(os.path.join(BASE_DIR, "logs"), exist_ok=True)
os.makedirs(os.path.join(BASE_DIR, "data"), exist_ok=True)


class _BizOnlyFilter(logging.Filter):
    """仅放行业务代码日志(bot.* 包与 main)，过滤 botpy 等第三方噪声。"""

    def filter(self, record: logging.LogRecord) -> bool:
        return record.name == "main" or record.name.startswith("bot.")


_file_handler = RotatingFileHandler(
    os.path.join(BASE_DIR, "logs", "bot.log"),
    # V0.3.37:原 FileHandler 只增不减,2026-11-04 已达 115MB(两个月),检索日志要翻十几万行。
    # 改滚动:单文件 20MB、留 5 份,上限 120MB。bot.log 永远是**当前**那份,旧的依次是 .1~.5。
    maxBytes=20 * 1024 * 1024,
    backupCount=5,
    encoding="utf-8",
)
_file_handler.setFormatter(
    logging.Formatter("%(asctime)s [%(name)s] %(levelname)s: %(message)s")
)
_file_handler.addFilter(_BizOnlyFilter())

_handlers = [_file_handler]
# V0.3.6:控制台输出改为**显式开关**(CS2QQBOT_LOG_CONSOLE=1),默认只写 bot.log。
# 历史坑(两次挂死):①2026-09-08:计划任务会话里 stdout 是无人消费的管道,401 风暴的
# WARNING 灌满管道缓冲后 `logging.emit` **永久阻塞且持有全局锁** → 所有线程写日志锁死,
# 进程存活但全部任务冻结 40+ 小时;当时用 `sys.stdout.isatty()` 兜底。
# ②2026-09-28 11:11:py-spy 抓到 MainThread 卡在 `StreamHandler.emit` → `stream.write`
# (locals 显示 stream 是 `_io.TextIOWrapper`),**说明计划任务给进程的隐藏控制台/管道
# isatty() 竟然是 True**,isatty 兜底因此失效,进程从 11:11:49 起彻底静默(雷达 8 个任务
# 都没起来)。→ 结论:生产路径不能靠猜,必须显式开关;并顺手把 stdout/stderr 指向 devnull,
# 让任何意外的 print 也不可能冻住进程。前台调试:set CS2QQBOT_LOG_CONSOLE=1 再跑。
def _console_logging_enabled(env: Optional[Dict[str, str]] = None) -> bool:
    """是否把日志同时输出到控制台(V0.3.6)。

    默认**关闭**:生产由计划任务启动,隐藏控制台/管道一旦无人消费,写 stdout 会永久
    阻塞并因 logging 全局锁冻死整个进程(2026-09-08、2026-09-28 两次实锤)。
    前台调试时设 CS2QQBOT_LOG_CONSOLE=1。
    """
    src = os.environ if env is None else env
    return str(src.get("CS2QQBOT_LOG_CONSOLE", "")).strip().lower() in (
        "1", "true", "yes", "on")


_console_on = _console_logging_enabled()
if _console_on:
    _handlers.append(logging.StreamHandler(sys.stdout))
else:
    try:
        sys.stdout = open(os.devnull, "w", encoding="utf-8")
        sys.stderr = open(os.devnull, "w", encoding="utf-8")
    except Exception:
        pass

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    force=True,
    handlers=_handlers,
)
logging.getLogger("main").info(
    "[main] 日志模式: %s", "文件 + 控制台(调试)" if _console_on else "仅文件 logs/bot.log")

# ---- 单例保护：保证全局仅一个 main.py 实例运行，杜绝双实例重复回复 ----
# 机制：Windows 用 msvcrt 文件排他锁；其他平台降级为绑定本地端口。
# 放在 import botpy 之前，使后起的重复实例在连 QQ 前即退出，干净无副作用。
import socket as _socket
_SINGLETON_PORT = 18765
try:
    if os.name == "nt":
        import msvcrt
        _singleton_lock_path = os.path.join(BASE_DIR, ".bot_singleton.lock")
        _singleton_lock_fd = open(_singleton_lock_path, "w")
        msvcrt.locking(_singleton_lock_fd.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        _singleton_sock = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
        _singleton_sock.bind(("127.0.0.1", _SINGLETON_PORT))
        _singleton_sock.listen(1)
except OSError:
    sys.stderr.write(
        f"[singleton] 检测到已有实例在运行，本进程退出（端口/锁={_SINGLETON_PORT}）。\n"
    )
    sys.exit(0)

import botpy

from bot.analyzer.recommender import Recommender
from bot.config import load_config, validate_config
from bot.csqaq.client import (
    CSQAQClient,
    RANK_SORT_SELL_PRICE_DESC,
    RANK_SORT_BUY_PRICE_DESC,
    RANK_SORT_DROP_7D,
    RANK_SORT_RISE_7D,
    close_shared_session,
    try_rebind_local_ip,
)
from bot.qqbot.bot import CS2BotClient
from bot.qqbot.daily_word import format_daily_word
from bot.steamdt.client import SteamDTClient, SteamDTRateLimit
from bot.storage.db import SnapshotStore
from bot.anomaly.runner import start_anomaly_tasks
from bot.csqaq.market_counts import (aggregate_buff_yyyp_sell, aggregate_market_counts,
                                     dual_price_columns, watch_price_pair, watch_primary_price)

logger = logging.getLogger("main")

# get_price_by_market_hash_name 主要返回 BUFF/悠悠/Steam 售价和在售，
# 全平台求购及 C5/IGXE/ECO 数量需由 /info/good 补齐。关注循环按 good_id
# 缓存详情，避免同一轮多人关注同一饰品重复请求。
_WATCH_DETAIL_CACHE: Dict[int, tuple] = {}
_WATCH_DETAIL_TTL = 900.0


async def fetch_loop(cfg, store: SnapshotStore):
    """定时抓取 CSQAQ 排行数据并入库。"""
    interval = max(5, cfg.scheduler.fetch_interval_minutes) * 60
    while True:
        try:
            async with CSQAQClient(cfg.csqaq) as api:
                items = await api.get_rank_items_all(
                    page_size=cfg.scheduler.rank_page_size,
                    max_pages=cfg.scheduler.rank_max_pages,
                    sorts=[
                        RANK_SORT_SELL_PRICE_DESC,   # 售价降序
                        RANK_SORT_BUY_PRICE_DESC,    # 求购价降序
                        RANK_SORT_DROP_7D,           # 7天跌幅（覆盖超跌榜样本）
                        RANK_SORT_RISE_7D,           # 7天涨幅
                    ],
                )
                if items:
                    await store.async_insert_snapshot_batch(items)
                    logger.info("[scheduler] 本次抓取 %s 条", len(items))
                else:
                    logger.warning("[scheduler] 抓取结果为空")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception("[scheduler] 抓取失败: %s", exc)
        await asyncio.sleep(interval)


async def push_loop(cfg, client: CS2BotClient, store: SnapshotStore, recommender: Recommender):
    """每天固定时间向记录的群推送「市场播报 + 选品参考」。

    调度精确：计算到下一次目标时刻的秒数再睡眠，准点触发。
    推送目标：config 里配置的群 + 所有在群里@过机器人的群（自动记录）。
    """
    import datetime
    from bot.qqbot import commands as cmds

    _times = list(cfg.push.times)
    logger.info("[push] 定时播报已开启，每天 %s 推送", "、".join(_times))
    while True:
        # 计算距下一个定时点(支持多个)的秒数，准点触发
        now = datetime.datetime.now()
        cands = []
        for t in _times:
            hh, mm = (int(x) for x in t.split(":"))
            tg = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
            if tg <= now:
                tg += datetime.timedelta(days=1)
            cands.append(tg)
        target = min(cands)
        await asyncio.sleep(max(1, (target - now).total_seconds()))
        logger.info("[push] 开始推送每日播报")

        # 群目标：配置群 + 自动记录群
        targets = list(cfg.push.group_openids)
        auto_groups = await store.async_list_groups()
        for gid in auto_groups:
            if gid not in targets:
                targets.append(gid)
        # 群「播报」功能开关(V0.0.12):未开放的群不再收定时播报
        try:
            feats = await store.async_get_group_features()
            if feats:
                skipped = [g for g in targets if not feats.get((g, "report"), True)]
                targets = [g for g in targets if feats.get((g, "report"), True)]
                if skipped:
                    logger.info("[push] %d 个群因「播报」功能关闭被跳过", len(skipped))
        except Exception as exc:
            logger.warning("[push] 群功能开关读取失败(按全部开放处理): %s", exc)
        if not targets:
            logger.warning("[push] 没有可推送的群，请先在群里@机器人一次")
            continue

        # 生成播报:统一为 SteamDT 大盘卡 + CSQAQ 市场全景(与 /大盘 /播报 一致)
        text, chart = await client._broad_report()

        # 附选品参考(单品,走 CSQAQ 本地快照,非大盘指数,无双源冲突)
        rows = await store.async_query_latest(limit=2000)
        if rows:
            result = recommender.recommend(rows, "low_7d")
            if result.items:
                text += "\n\n📋 今日选品参考：\n"
                for i, it in enumerate(result.items[:3], 1):
                    text += it.format_line(i) + "\n"

        # 每日一言（按日期稳定；当前默认每日 15:00 播报）
        text += "\n\n" + format_daily_word()

        for gid in targets:
            await client.push_to_group(gid, text)
            if chart and os.path.exists(chart):
                try:
                    await client._send_group_image(gid, chart)
                except Exception as exc:
                    logger.exception("[push] 走势图发送失败 %s: %s", gid, exc)
        if chart and os.path.exists(chart):
            try:
                os.remove(chart)
            except Exception:
                pass


async def _watch_fetch_rate7(api, pd: Dict[str, Any], good_id) -> Optional[float]:
    """取 7日涨跌幅百分比:优先批量结果,缺失再实时补 get_good_info。"""
    r7 = pd.get("sellPriceRate7")
    if r7 is None:
        r7 = pd.get("sell_price_rate_7")
    if r7 is None and good_id:
        try:
            info = await api.get_good_info(int(good_id))
            r7 = info.get("sell_price_rate_7") or info.get("sellPriceRate7")
        except Exception:
            return None
    return float(r7) if r7 is not None else None


async def watch_loop(cfg, client: CS2BotClient, store: SnapshotStore,
                   sdt: Optional[SteamDTClient] = None):
    """自选盯盘 + 大盘指数盯盘:定时检查,按 alert_type 分派触发,24h 冷却。"""
    import datetime
    from bot.qqbot import commands as cmds

    interval = max(5, cfg.scheduler.fetch_interval_minutes) * 60
    logger.info("[watch] 盯盘循环已开启,每 %d 分钟检查一次", interval // 60)
    while True:
        try:
            now = datetime.datetime.now()
            rows = await store.async_list_all_watches()
            if rows:
                names = list({r["market_hash_name"] for r in rows})
                async with CSQAQClient(cfg.csqaq) as api:
                    prices = await api.get_price_by_market_hash_name(names)
                    # 批量查价接口通常没有全平台求购/C5/IGXE/ECO 字段。
                    # 对缺少完整平台盘口的关注品补查 /info/good，并合并到本轮数据；
                    # 详情按 good_id 缓存 15 分钟，控制额度且多人关注同品只请求一次。
                    now_mono = time.monotonic()
                    detail_by_gid: Dict[int, Dict[str, Any]] = {}
                    gid_names: Dict[int, str] = {}
                    for _r in rows:
                        _gid = int(_r.get("good_id") or 0)
                        if _gid:
                            gid_names.setdefault(_gid, _r["market_hash_name"])
                    detail_skip_logged = False
                    for _gid, _mhn in gid_names.items():
                        _pd0 = prices.get(_mhn) or {}
                        _cnt0 = aggregate_market_counts(_pd0)
                        cached_detail = _WATCH_DETAIL_CACHE.get(_gid)
                        if cached_detail and now_mono - cached_detail[0] < _WATCH_DETAIL_TTL:
                            detail_by_gid[_gid] = dict(cached_detail[1])
                            continue
                        if _cnt0["buy_complete"] and _cnt0["sell_complete"]:
                            continue
                        if getattr(api, "ip_drift_cooling", lambda: False)():
                            if not detail_skip_logged:
                                logger.info("[watch] 全平台盘口补查跳过: CSQAQ IP 漂移冷却中")
                                detail_skip_logged = True
                            break
                        try:
                            _detail = await api.get_good_info(_gid)
                            if _detail:
                                _WATCH_DETAIL_CACHE[_gid] = (now_mono, dict(_detail))
                                detail_by_gid[_gid] = dict(_detail)
                        except Exception as exc:
                            logger.warning("[watch] 全平台盘口补查失败 gid=%s: %s", _gid, exc)
                    if detail_by_gid:
                        _merged_prices: Dict[str, Dict[str, Any]] = {}
                        for _mhn, _pd in prices.items():
                            _merged = dict(_pd or {})
                            # 通过 watchlist gid 找对应详情，避免中文名/英文名差异。
                            _gid = next((g for g, m in gid_names.items() if m == _mhn), 0)
                            if _gid and _gid in detail_by_gid:
                                _merged.update(detail_by_gid[_gid])
                            _merged_prices[_mhn] = _merged
                        prices = _merged_prices
                now = datetime.datetime.now()
                # V0.2.3 周期监测数据面:关注品查价回包顺路写 item_listing_history 快照
                # (15 分钟/点,1 小时后即可支撑四阶段判定;失败不影响盯盘主流程)
                try:
                    _snap_rows = []
                    for r in rows:
                        _pd = prices.get(r["market_hash_name"])
                        _gid = int(r.get("good_id") or 0)
                        if not _pd or not _gid:
                            continue
                        _counts = aggregate_market_counts(_pd)
                        _liquidity = aggregate_buff_yyyp_sell(_pd)
                        # V0.2.24: 求购侧统一 BUFF+悠悠(详情/合并回包提供两平台求购)
                        _buff_buy_raw = (_pd.get("buffBuyNum") if "buffBuyNum" in _pd
                                         else _pd.get("buff_buy_num"))
                        _yyyp_buy_raw = (_pd.get("yyypBuyNum") if "yyypBuyNum" in _pd
                                         else _pd.get("yyyp_buy_num"))
                        _buy_complete = _buff_buy_raw is not None and _yyyp_buy_raw is not None
                        _buy_total = (int(_buff_buy_raw or 0) + int(_yyyp_buy_raw or 0)
                                      if _buy_complete else None)
                        _snap_rows.append({
                            "good_id": _gid, "name": r.get("zh_name") or "",
                            "ts": now.strftime("%Y-%m-%d %H:%M:%S"),
                            "buff_sell_num": int(_pd.get("buffSellNum") or 0),
                            "steam_sell_num": int(_pd.get("steamSellNum") or 0),
                            "buff_buy_num": int(_pd.get("buffBuyNum") or 0),
                            "steam_buy_num": int(_pd.get("steamBuyNum") or 0),
                            "yyyp_buy_num": int(_pd.get("yyypBuyNum") or _pd.get("yyyp_buy_num") or 0),
                            "buff_sell_price": float(_pd.get("buffSellPrice") or 0),
                            "buff_buy_price": float(_pd.get("buffBuyPrice") or 0),
                            # V0.3.4: 双平台价格(BUFF+悠悠)
                            **dual_price_columns(_pd),
                            "total_sell_num": (_liquidity["sell_num"]
                                                if _liquidity["sell_complete"] else None),
                            "total_buy_num": _buy_total,
                            "total_sell_scope": "buff_yyyp" if _liquidity["sell_complete"] else None,
                            "total_buy_scope": "buff_yyyp" if _buy_complete else None,
                        })
                    if _snap_rows:
                        await store.async_save_listing_history_batch(_snap_rows)
                except Exception as exc:
                    logger.warning("[watch] 关注品快照落库失败(不影响盯盘): %s", exc)
                # V0.2.3 周期监测:对 alert_type=cycle 的关注品跑四阶段指纹,命中 @ 关注者
                cycle_rows = [r for r in rows if (r.get("alert_type") or "price") == "cycle"]
                if cycle_rows:
                    try:
                        from bot.anomaly.cycle import run_cycle_watch
                        await run_cycle_watch(cfg.anomaly, store, client, cycle_rows, prices, now)
                    except Exception as exc:
                        logger.warning("[watch] 周期监测失败(不影响盯盘): %s", exc)
                for r in rows:
                    pd = prices.get(r["market_hash_name"])
                    if not pd:
                        continue
                    at = r.get("alert_type") or "price"
                    base = float(r.get("base_price") or 0)
                    param = float(r.get("param") or 0)
                    # V0.3.23:现价与基准同口径 —— 统一"悠悠有品优先",缺失回退 BUFF/Steam
                    cur, cur_scope = watch_primary_price(pd)
                    if not cur_scope:
                        continue
                    # 老记录基准是 BUFF 口径,和新口径不一致时首轮只重设基准、不比较,避免误报
                    if str(r.get("base_price_scope") or "buff") != cur_scope:
                        await store.async_update_watch_base(r["id"], cur, cur_scope)
                        logger.info("[watch] 基准口径迁移 id=%s item=%s %s→%s ¥%.2f",
                                    r["id"], r["zh_name"],
                                    r.get("base_price_scope") or "buff", cur_scope, cur)
                        continue
                    buff_now, yyyp_now = watch_price_pair(pd)
                    if at in ("pct7d_up", "pct7d_down"):
                        # 7日涨/跌:取 7日涨跌幅,超 param% 触发
                        rate_7 = await _watch_fetch_rate7(api, pd, r.get("good_id"))
                        if rate_7 is None:
                            continue
                        trigger, reason = cmds.watch_should_alert(
                            base, cur, r.get("threshold"), r.get("target_price"),
                            at, param, rate_7=rate_7)
                    elif at == "supply":
                        # 在售骤降:对比六个平台合计在售件数,降幅超 param% 触发
                        _watch_counts = aggregate_market_counts(pd)
                        if not _watch_counts["sell_complete"]:
                            # 详情补查失败/回包缺平台字段时，不把部分平台合计当成全平台。
                            continue
                        sell_num = _watch_counts["sell_num"]
                        # 旧版关注记录的 last_sell_num 是 BUFF 口径，切换全平台后
                        # 首轮只迁移基准，不拿不同口径做降幅比较，避免误提醒。
                        if str(r.get("last_sell_num_scope") or "buff") != "all":
                            await store.async_update_watch_sell_num(r["id"], sell_num, "all")
                            continue
                        trigger, reason = cmds.watch_should_alert(
                            base, cur, r.get("threshold"), r.get("target_price"),
                            at, param, sell_num=sell_num,
                            last_sell_num=int(r.get("last_sell_num") or 0))
                        # 每次都更新最近在售件数,供下一轮对比
                        await store.async_update_watch_sell_num(r["id"], sell_num)
                    else:  # price:价格阈值 / 目标价
                        if cur <= 0 or base <= 0:
                            continue
                        trigger, reason = cmds.watch_should_alert(
                            base, cur, r.get("threshold"), r.get("target_price"))
                    if not trigger:
                        continue
                    last = r.get("last_alert_at")
                    if last:
                        try:
                            last_dt = datetime.datetime.strptime(last, "%Y-%m-%d %H:%M:%S")
                            if (now - last_dt).total_seconds() < 86400:
                                continue
                        except ValueError:
                            pass
                    # 提醒:优先群内推送(群主开启"机器人主动发言"后可用,不限条数)
                    # 私聊主动消息每月仅 4 条/用户,仅作兜底
                    # 注意:群内 @ 标签(<qqbot-at-user>)仅在 markdown 消息中渲染,
                    # 纯文本不渲染,所以群内带 @ 时用 push_to_group_markdown
                    alert = cmds.format_watch_alert(r["zh_name"], base, cur, reason,
                                                    buff_price=buff_now, yyyp_price=yyyp_now)
                    if r.get("group_openid"):
                        # 群内提醒:@设置盯盘的人(QQ 官方 @ 标记,id 用群 member_openid)
                        uid = r.get("user_openid") or ""
                        if uid:
                            alert = f'<qqbot-at-user id="{uid}" />\n' + alert
                        delivered = await client.push_to_group_markdown(r["group_openid"], alert)
                    else:
                        delivered = await client.push_to_user(r["user_openid"], alert)
                    if delivered is False:
                        _invalid_group = getattr(client, "is_invalid_group_target", None)
                        _invalid_user = getattr(client, "is_invalid_user_target", None)
                        invalid_target = (
                            bool(r.get("group_openid"))
                            and callable(_invalid_group)
                            and bool(_invalid_group(r["group_openid"]))
                        ) or (
                            not r.get("group_openid")
                            and callable(_invalid_user)
                            and bool(_invalid_user(r.get("user_openid") or ""))
                        )
                        if invalid_target:
                            await store.async_remove_watch_by_id(r["id"])
                            logger.warning(
                                "[watch] 推送目标已注销，自动删除失效关注 id=%s item=%s",
                                r.get("id"), r.get("zh_name"),
                            )
                            continue
                        # 发送失败不写冷却/不更新基准，下一轮继续尝试，避免盯盘提醒丢失。
                        logger.warning("[watch] 提醒发送失败,保留待提醒状态: %s", r.get("zh_name"))
                        continue
                    # 价格类提醒后重置基准;其余类型仅记冷却时间,不动基准
                    if at == "price" and (r.get("target_price") or r.get("threshold")):
                        await store.async_update_watch_after_alert(r["id"], cur)
                    else:
                        await store.async_update_watch_last_alert(r["id"])
                    logger.info("[watch] 提醒 %s: %s %s", r["user_openid"], r["zh_name"], reason)

            # ---- 大盘指数盯盘(触发源 = SteamDT 大盘指数) ----
            if sdt is not None:
                idx_rows = await store.async_list_all_index_watches()
                if idx_rows:
                    try:
                        bi = await sdt.get_broad_index()
                    except Exception as exc:
                        logger.warning("[watch] 大盘指数获取失败,本轮跳过: %s", exc)
                        bi = None
                    if bi:
                        idx_val = bi["index"]
                        ratio = bi["diff_ratio_pct"]
                        for r in idx_rows:
                            at = r.get("alert_type") or ""
                            param = float(r.get("param") or 0)
                            trigger, reason = cmds.index_should_alert(at, param, idx_val, ratio)
                            if not trigger:
                                continue
                            last = r.get("last_alert_at")
                            if last:
                                try:
                                    last_dt = datetime.datetime.strptime(last, "%Y-%m-%d %H:%M:%S")
                                    if (now - last_dt).total_seconds() < 86400:
                                        continue
                                except ValueError:
                                    pass
                            alert = cmds.format_index_watch_alert(at, param, bi)
                            uid = r.get("user_openid") or ""
                            if r.get("group_openid"):
                                if uid:
                                    alert = f'<qqbot-at-user id="{uid}" />\n' + alert
                                delivered = await client.push_to_group_markdown(r["group_openid"], alert)
                            else:
                                delivered = await client.push_to_user(r["user_openid"], alert)
                            if delivered is False:
                                _invalid_group = getattr(client, "is_invalid_group_target", None)
                                _invalid_user = getattr(client, "is_invalid_user_target", None)
                                invalid_target = (
                                    bool(r.get("group_openid"))
                                    and callable(_invalid_group)
                                    and bool(_invalid_group(r["group_openid"]))
                                ) or (
                                    not r.get("group_openid")
                                    and callable(_invalid_user)
                                    and bool(_invalid_user(r.get("user_openid") or ""))
                                )
                                if invalid_target:
                                    await store.async_remove_index_watch_by_id(r["id"])
                                    logger.warning(
                                        "[watch] 推送目标已注销，自动删除失效大盘关注 id=%s",
                                        r.get("id"),
                                    )
                                    continue
                                logger.warning("[watch] 大盘提醒发送失败,保留待提醒状态: %s",
                                               r.get("user_openid"))
                                continue
                            await store.async_update_index_watch_last_alert(r["id"])
                            logger.info("[watch] 大盘提醒 %s: %s", r["user_openid"], reason)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # 预期内降级(IP 漂移冷却/限频/白名单不符):单行 WARNING,不打全栈刷日志;
            # 只有真正的意外错误才 logger.exception。
            _msg = str(exc)
            if any(k in _msg for k in ("漂移冷却中", "HTTP 429", "HTTP 401", "HTTP 500", "限频")):
                logger.warning("[watch] 盯盘检查本轮跳过(数据源降级): %s", _msg[:120])
            else:
                logger.exception("[watch] 盯盘检查失败: %s", exc)
        await asyncio.sleep(interval)


async def steamdt_health_loop(cfg, sdt: SteamDTClient):
    """SteamDT 大盘源健康检测(与 CSQAQ 的 ip_check_loop 同构的自动恢复)。

    SteamDT 仅 Bearer Key 认证、无 IP 白名单,故不存在"IP 漂移→绑定失败"模式;
    这里做等效健壮性:周期性探测,真实源故障(鉴权失败/网络/5xx)时置 healthy=False 告警,
    恢复时静默切回。各调用方(大盘盯盘 / /大盘 / 播报)在 SteamDT 不可用时自动回退 CSQAQ 兜底,
    保证大盘数据不断、不误触——即 CSQAQ "IP 漂移自动恢复" 的等价效果。

    关于限频(HTTP 429 / 业务码 4005):属良性限流,**不**当源故障处理,不翻转 healthy,
    仅静默跳过(期间 _get 返回缓存,无缓存时调用方走 CSQAQ 兜底)。

    V0.0.8:探测循环 120s→600s——真实网络请求由缓存 TTL(3600s)决定,循环只影响故障发现粒度;
    降频省 CPU 与日志噪音,对小时级大盘数据无感。
    """
    interval = 600
    logger.info("[steamdt] 大盘源健康检测已开启(每 %d 秒)", interval)
    while True:
        try:
            # V0.2.4: broad 族退避期内跳过探测(锁到期后下一轮自动恢复),
            # 否则每轮都清锁重探,退避形同虚设+警告刷屏(实测 116 次/天)。
            if not sdt.broad_backoff_active():
                _, network_ok = await sdt.get_broad_index_ex()   # 非 force:命中缓存则不打网络,省额度
                if network_ok and not sdt.healthy:
                    sdt.healthy = True
                    logger.info('source=steamdt event=recovered msg="SteamDT 恢复成功,自动切回"')
        except SteamDTRateLimit as exc:
            # 限频:良性,不当 outage,不翻转 healthy,静默(调用方走 CSQAQ 兜底)
            logger.debug("source=steamdt event=rate_limit_skip msg=\"限频退避中,本轮健康检测跳过\" err=%s", exc)
        except Exception as exc:
            if sdt.healthy:
                sdt.healthy = False
                logger.warning('source=steamdt event=down msg="SteamDT 不可用,回退 CSQAQ" err=%s', exc)
        await asyncio.sleep(interval)


async def ip_check_loop(cfg, client: CS2BotClient, store: SnapshotStore):
    """IP 漂移检测:每 2 分钟探测 CSQAQ 白名单状态。

    漂移到新 IP -> 向所有已知群推送提醒(每 IP 一次);
    恢复 -> 推送恢复通知。
    """
    interval = 120
    last_bad_ip: Optional[str] = None
    was_bad = False
    logger.info("[ipcheck] IP 漂移检测已开启(每 %d 秒)", interval)
    while True:
        try:
            async with CSQAQClient(cfg.csqaq) as api:
                # allow_drift_skip=False:探测必须真打网络——漂移冷却期普通请求会跳过,
                # 但探测链路要能穿过去发现漂移,冷却满后由本循环自动重绑自愈。
                ok, ip = await api.probe_ip_status(allow_drift_skip=False)
            # 推送目标:优先通知群(私密),否则所有已知群
            targets = [cfg.notify_group_openid] if cfg.notify_group_openid else await store.async_list_groups()
            if ok:
                if was_bad:
                    logger.info("[ipcheck] IP 已恢复白名单匹配,静默处理(不发群消息)")
                    was_bad = False
                    last_bad_ip = None
            elif ip and ip != last_bad_ip:
                # 1) 先尝试自动绑定新 IP（官方「绑定本机白名单IP」接口,30秒/次;
                #    ip_check_loop 探测间隔 120 秒,天然满足频率限制）
                # 自动绑定成功/重绑冷却中 -> 静默(只记日志);
                # 仅真正绑定失败/复测仍失败/异常 -> 发群消息提示手动修正
                text = None
                try:
                    bound, info = await try_rebind_local_ip(
                        cfg.csqaq, ip_hint=ip, source="ipcheck",
                        cooldown_seconds=int(getattr(cfg.csqaq, "rebind_cooldown_seconds", 240)))
                    if bound:
                        # 2) 绑定后复测确认
                        async with CSQAQClient(cfg.csqaq) as api:
                            ok2, _ = await api.probe_ip_status(allow_drift_skip=False)
                        if ok2:
                            logger.info("[ipcheck] 自动绑定成功: %s (%s),静默处理", ip, info)
                            # 服务已恢复,重置状态避免重复推送
                            last_bad_ip = None
                            was_bad = False
                        else:
                            text = (f"⚠️ CSQAQ IP 漂移!当前 IP: {ip}\n"
                                    "已尝试自动绑定但复测仍失败,请到 csqaq.com 手动检查白名单")
                            logger.warning("[ipcheck] 自动绑定后复测仍失败: %s", ip)
                    elif info in ("cooldown", "inflight"):
                        # 重绑冷却/上一轮重绑未结束:预期等待,静默;
                        # 不设 last_bad_ip —— 下轮继续尝试,由 rebind 240s 冷却自然限频,
                        # 冷却一满立即重绑自愈(设了会挡住同 IP 后续所有尝试,造成死等)。
                        logger.info("[ipcheck] 重绑冷却中(ip=%s state=%s),下轮再试", ip, info)
                    else:
                        text = (f"⚠️ CSQAQ IP 漂移!当前 IP: {ip}\n"
                                f"自动绑定失败({info[:80]}),请到 csqaq.com 手动绑定")
                        logger.warning("[ipcheck] 自动绑定失败: %s", info)
                except Exception as exc:
                    text = (f"⚠️ CSQAQ IP 漂移!当前 IP: {ip}\n"
                            f"自动绑定异常({str(exc)[:80]}),请到 csqaq.com 手动绑定")
                    logger.exception("[ipcheck] 自动绑定异常: %s", exc)
                if text:
                    for gid in targets:
                        await client.push_to_group(gid, text)
                    last_bad_ip = ip
                    was_bad = True
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception("[ipcheck] 检测异常: %s", exc)
        await asyncio.sleep(interval)


async def amain() -> int:
    os.makedirs(os.path.join(BASE_DIR, "logs"), exist_ok=True)
    os.chdir(BASE_DIR)

    cfg = load_config("config.yaml")
    errors = validate_config(cfg)
    if errors:
        for e in errors:
            logger.error("配置缺失: %s", e)
        logger.error("请参考 config.example.yaml 填写 config.yaml 后重试")
        return 1

    store = SnapshotStore(os.path.join(BASE_DIR, "data", "csqaq.db"),
                        history_retention_days=cfg.history_retention_days)
    recommender = Recommender(cfg.recommend)

    # SteamDT 大盘源客户端:长生命周期,共享给播报/大盘指令/大盘盯盘/健康循环
    sdt = SteamDTClient(cfg.steamdt)
    await sdt.ensure_session()

    intents = botpy.Intents(public_messages=True)
    client = CS2BotClient(cfg, store, recommender, intents, steamdt=sdt)

    tasks = [
        asyncio.create_task(fetch_loop(cfg, store), name="fetch-loop"),
        asyncio.create_task(watch_loop(cfg, client, store, sdt), name="watch-loop"),
        asyncio.create_task(ip_check_loop(cfg, client, store), name="ip-check-loop"),
        asyncio.create_task(steamdt_health_loop(cfg, sdt), name="steamdt-health-loop"),
    ]
    if cfg.push.enable:
        tasks.append(asyncio.create_task(
            push_loop(cfg, client, store, recommender), name="push-loop"))

    # 异动雷达(PRD V1.0):三层池 B/A/S 五个独立任务,共享一个长生命周期 API 客户端
    anomaly_api = None
    if cfg.anomaly.enable:
        anomaly_api = CSQAQClient(cfg.csqaq)
        await anomaly_api.__aenter__()
        tasks.extend(await start_anomaly_tasks(cfg.anomaly, anomaly_api, store, client))

    try:
        await client.start(appid=cfg.qq.appid, secret=cfg.qq.secret)
    finally:
        for t in tasks:
            t.cancel()
        try:
            await sdt.close()
        except Exception:
            pass
        if anomaly_api is not None:
            try:
                await anomaly_api.close()
            except Exception:
                pass
        # V0.3.8:共享单连接会话由进程收尾统一关闭(实例 close() 只释放引用)
        try:
            await close_shared_session()
        except Exception:
            pass
    return 0


def main():
    try:
        sys.exit(asyncio.run(amain()))
    except KeyboardInterrupt:
        print("\n已停止。")


if __name__ == "__main__":
    main()
