# -*- coding: utf-8 -*-
"""SteamDT 客户端:大盘指数 + 单品 K 线。"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

import aiohttp

from bot.config import SteamDTConfig
from bot.steamdt.kline import normalize_item_kline_rows

logger = logging.getLogger("bot.steamdt")

_BROAD_INDEX_PATH = "/open/cs2/broad/v1/index"
_ITEM_KLINE_PATH = "/open/cs2/item/v1/kline"


class SteamDTRateLimit(RuntimeError):
    """SteamDT 限频。"""


def _cache_key(path: str, payload: Optional[Dict[str, Any]] = None) -> str:
    if not payload:
        return path
    return f"{path}?{json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(',', ':'))}"


@dataclass
class _CachedResp:
    expire_at: float
    data: Dict[str, Any]


class SteamDTClient:
    def __init__(self, cfg: SteamDTConfig):
        self._base = (getattr(cfg, "base_url", "") or "https://open.steamdt.com").rstrip("/")
        self._key = getattr(cfg, "api_key", "") or ""
        self._retry = max(1, int(getattr(cfg, "retry_times", 3) or 3))
        self._interval = float(getattr(cfg, "request_interval", 1.0) or 1.0)
        self._cache_ttl = max(0, int(getattr(cfg, "cache_ttl_seconds", 300) or 300))
        self._session: Optional[aiohttp.ClientSession] = None
        self._cache: Dict[str, _CachedResp] = {}
        self.healthy = True
        # V0.2.1: 限频退避**按接口族**隔离——SteamDT 免费配额是按接口独立计算的
        # (实测 2026-09-06: 大盘 index 4005 持续时,单品 kline 同 Key 同出口正常拿 365 根)。
        # 旧版全局退避锁导致大盘 4005 误杀 K 线:/分析 走势图消失,仅缓存内单品幸免。
        # 族: broad=大盘指数 / kline=单品K线。
        self._rate_limited: Dict[str, float] = {"broad": 0.0, "kline": 0.0}
        self._rl_fail_streak: Dict[str, int] = {"broad": 0, "kline": 0}
        self._last_network_ok = 0.0
        self._last_request_at = 0.0
        # V0.3.14:broad 接口实测是「约 30 秒一次」的频率限,不是配额总量耗尽(2026-10-12 实测)。
        # 旧实现在失败后 1.5 秒连打 3 次 → 3 次必 4005 → 再锁 1800 秒,自己制造限频并自锁,
        # 自 2026-09-04 起天天如此(日志 6435 条 code4005)。改为:单次尝试 + 最小间隔 + 后台补抓。
        # 注意:取值必须用 None 判断,**不能用 `or 默认值`** —— `0.0 or 30.0` 会把
        # "允许 0 秒间隔"这种合法配置吃掉(本改动第一版就踩了,被回归用例抓到)。
        def _num(name: str, default: float) -> float:
            raw = getattr(cfg, name, None)
            if raw is None:
                return default
            try:
                return float(raw)
            except (TypeError, ValueError):
                return default

        self._broad_min_gap = max(0.0, _num("broad_min_gap_seconds", 30.0))
        self._broad_retry_delay = max(0.0, _num("broad_retry_delay_seconds", 35.0))
        self._broad_cooldown = max(self._broad_min_gap, _num("broad_cooldown_seconds", 120.0))
        self._broad_last_attempt = 0.0
        self._broad_refresh_task: Optional[asyncio.Task] = None

    async def ensure_session(self) -> None:
        if self._session is None or self._session.closed:
            # UA 关键坑(2026-09-05 实锤):SteamDT 限频按 User-Agent 计数——
            # aiohttp 默认 UA(Python/3.x aiohttp/3.x)的配额池已被打爆(4005 恒定),
            # 同一出口同一秒换 curl/浏览器 UA 即正常。必须伪装浏览器 UA。
            self._session = aiohttp.ClientSession(headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                              "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
                "Accept": "application/json",
            })

    async def close(self) -> None:
        task = self._broad_refresh_task
        if task is not None and not task.done():
            task.cancel()
        self._broad_refresh_task = None
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None

    def _headers(self) -> Dict[str, str]:
        return {"Authorization": f"Bearer {self._key}", "Accept": "application/json"}

    @staticmethod
    def _family(path: str) -> str:
        """按路径划分接口族(配额按接口独立,退避必须隔离)。"""
        return "kline" if "item/v1/kline" in path else "broad"

    def _enter_rate_limit(self, family: Optional[str] = None) -> None:
        # V0.0.8: 600→1800s——免费配额耗尽后(4005/空数据)短时间内怎么打都失败,
        # 退避期内走缓存/CSQAQ 兜底。
        # V0.2.1: 退避按接口族隔离(大盘 4005 不再误杀 K 线,反之亦然)。
        # V0.2.4: ①连续失败(streak≥8,≈2 轮探测)退避自动加长到 3600s(配额长期耗尽时探测减半);
        #         ②退避期内重复上锁**静默**(只记 DEBUG)——否则健康循环每 10 分钟
        #           清锁重探一遍,警告刷屏 116 次/天(2026-09-07 实测)。
        # V0.3.14:基础退避按族分开 —— broad 实测 30 秒就能恢复,1800s 的锁会把"其实能好"
        # 的窗口整个盖住(自 2026-09-04 每天 8 次触发,用户端天天看到 CSQAQ 兜底);
        # 改为基础 120s,连续失败再逐级加长(300/900/1800)。kline 保持 1800s 不变。
        family = family or "broad"
        base = self._broad_cooldown if family == "broad" else 1800.0
        streak = self._rl_fail_streak.get(family, 0) + 1
        self._rl_fail_streak[family] = streak
        if streak >= 8:
            backoff = max(base, 3600.0)
        elif streak >= 4:
            backoff = max(base, 900.0)
        elif streak >= 2:
            backoff = max(base, 300.0)
        else:
            backoff = base
        new_until = time.time() + backoff
        old = self._rate_limited.get(family, 0.0)
        self._rate_limited[family] = max(old, new_until)
        if old <= time.time() + 60:   # 首次上锁/锁临期才记 WARNING
            logger.warning(
                "[steamdt] 接口族[%s] 触发限频退避(连续失败%d次),%.0f 秒内不联网",
                family, streak, backoff,
            )
        else:
            logger.debug(
                "[steamdt] 接口族[%s] 退避期内续锁(连续失败%d次),静默至 %.0f 秒后",
                family, streak, self._rate_limited[family] - time.time(),
            )

    def broad_backoff_active(self) -> bool:
        """broad 族限频退避是否活跃(健康循环据此跳过探测,零网络零刷屏)。"""
        return time.time() < self._rate_limited.get("broad", 0.0)

    def _schedule_broad_refresh(self) -> None:
        """失败后在**后台**补抓一次(不阻塞调用方)。

        V0.3.14:原实现是失败后 1.5 秒连打 3 次"换出口抽签"。实测(2026-10-12):
        broad 的限频是**请求间隔**约 30 秒 —— 1.5s/15s 必回 code=4005,30s/60s 必成功;
        也就是说那 3 次重试是**自己制造**的 4005,还把接口族锁 1800 秒,自 2026-09-04 起
        天天如此。改为:调用方立刻拿 CSQAQ 兜底(不卡推送/播报),35 秒后后台再补一次。
        """
        task = self._broad_refresh_task
        if task is not None and not task.done():
            return

        async def _runner() -> None:
            await asyncio.sleep(self._broad_retry_delay)
            try:
                # 这次补抓是"被批准的联网":清掉本轮退避,打完再按结果决定是否重新上锁
                self._rate_limited["broad"] = 0.0
                d, _ = await self._request_json(
                    "GET", _BROAD_INDEX_PATH, force=True)
                idx = self._parse_broad(d)["index"]      # V0.3.14:必须拆信封再读
                if idx > 0:
                    logger.info(
                        "source=steamdt event=broad_refresh_ok index=%.2f "
                        "(后台补抓成功,%.0fs 后下一次调用即可用真数据)",
                        idx, self._broad_retry_delay)
                    return
                self._cache.pop(_BROAD_INDEX_PATH, None)
                self._enter_rate_limit("broad")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning(
                    "source=steamdt event=broad_refresh_fail msg=%s",
                    str(exc)[:90])

        try:
            self._broad_refresh_task = asyncio.create_task(_runner())
        except RuntimeError:      # 事件循环已关闭(退出流程)
            self._broad_refresh_task = None

    async def _throttle(self) -> None:
        now = time.monotonic()
        wait = self._interval - (now - self._last_request_at)
        if wait > 0:
            await asyncio.sleep(wait)
        self._last_request_at = time.monotonic()

    async def _curl_request(self, method: str, path: str,
                            payload: Optional[Dict[str, Any]]) -> Tuple[int, str]:
        """经 curl 子进程发请求,返回 (http_status, body)。

        关键原因(2026-09-05 实锤):SteamDT 网关按 **TLS 指纹** 限流——python(ssl 栈)
        的 JA3 特征被拉黑,同一秒同一 Key 同一出口,curl 正常 / aiohttp 恒 4005"已达上限"。
        Windows 10 1803+ 自带 curl.exe,子进程直调绕开指纹识别。头/缓存/退避逻辑不变。

        V0.0.11 关键加固:curl 调用放到 **线程池** 里跑同步 subprocess.run——
        事件循环里直接 await 子进程 communicate 在计划任务/Proactor 环境下可能死锁
        (2026-09-06 实锤:开机自启后事件循环挂死 13 小时无日志)。to_thread + timeout
        保证任何情况下事件循环都不被阻塞。
        """
        url = self._base + path
        cmd = ["curl", "-s", "-m", "20", "-w", "\n%{http_code}",
               "-H", f"Authorization: Bearer {self._key}",
               "-H", "Accept: application/json"]
        if method.upper() == "POST":
            cmd += ["-X", "POST",
                    "-H", "Content-Type: application/json",
                    # V0.3.2:必须 ensure_ascii=True —— 含 `★` 的 marketHashName(刀/手套)以原始
                    # UTF-8 放进 body 时 SteamDT 网关返 HTTP 400(实测 2026-09-13),转义成
                    # \u2605 后 HTTP 200 + 完整 365 根日K。此前用 False 导致 goods_index 中
                    # 3471/25399(14%,全部刀/手套)在 /分析 里静默降级:无中期结构/买点/走势图。
                    "-d", json.dumps(payload or {}, ensure_ascii=True)]
        cmd.append(url)

        def _run_sync() -> str:
            import subprocess
            proc = subprocess.run(cmd, capture_output=True, timeout=25)
            return proc.stdout.decode("utf-8", "ignore")

        text = await asyncio.wait_for(asyncio.to_thread(_run_sync), timeout=30)
        body, _, code_str = text.rpartition("\n")
        try:
            status = int(code_str.strip())
        except ValueError:
            status, body = 0, text
        return status, body

    async def _aiohttp_request(self, method: str, path: str,
                               payload: Optional[Dict[str, Any]]) -> Tuple[int, str]:
        """aiohttp 请求(备用:系统无 curl 时回退;会被 TLS 指纹限流,仅兜底)。"""
        await self.ensure_session()
        req_kwargs: Dict[str, Any] = {
            "headers": self._headers(),
            "timeout": aiohttp.ClientTimeout(total=20),
        }
        url = self._base + path
        if method.upper() == "GET":
            req_kwargs["params"] = payload or {}
            req = self._session.get(url, **req_kwargs)
        else:
            req_kwargs["json"] = payload or {}
            req = self._session.post(url, **req_kwargs)
        async with req as resp:
            return resp.status, await resp.text()

    async def _request_json(
        self,
        method: str,
        path: str,
        payload: Optional[Dict[str, Any]] = None,
        *,
        force: bool = False,
    ) -> Tuple[Dict[str, Any], bool]:
        key = _cache_key(path, payload)
        family = self._family(path)

        if time.time() < self._rate_limited.get(family, 0.0):
            cached = self._cache.get(key)
            if cached is not None:
                logger.warning("[steamdt] 接口族[%s] 限频退避期内,返回缓存数据(不联网,避免烧额度)", family)
                return cached.data, False
            raise SteamDTRateLimit(f"SteamDT 接口族[{family}] 限频退避中且无缓存")

        if not force:
            cached = self._cache.get(key)
            if cached and time.time() < cached.expire_at:
                return cached.data, False

        last_exc: Optional[Exception] = None
        for attempt in range(1, self._retry + 1):
            await self._throttle()
            try:
                try:
                    status, body = await self._curl_request(method, path, payload)
                except FileNotFoundError:
                    # 系统无 curl(极老系统):回退 aiohttp(可能被 TLS 指纹限流)
                    status, body = await self._aiohttp_request(method, path, payload)
                if status == 429:
                    logger.warning(
                        "source=steamdt event=rate_limit type=http429 family=%s "
                        "msg=\"SteamDT HTTP 429 限频,非源故障;退避期内走缓存/CSQAQ 兜底\"",
                        family,
                    )
                    self._enter_rate_limit(family)
                    raise SteamDTRateLimit(f"SteamDT HTTP 429: {body[:120]}")
                if status != 200:
                    if status in (401, 403):
                        raise RuntimeError(f"SteamDT 鉴权失败 HTTP {status}: {body[:160]}")
                    raise RuntimeError(f"SteamDT HTTP {status}: {body[:160]}")
                try:
                    data = json.loads(body)
                except Exception as exc:
                    raise RuntimeError(f"SteamDT 返回非 JSON: {body[:160]}") from exc

                if not isinstance(data, dict):
                    raise RuntimeError("SteamDT 返回结构异常")

                if data.get("success") is False:
                    code = data.get("errorCode")
                    msg = str(data.get("errorMsg") or "")
                    if code == 4005 or "达到上限" in msg:
                        logger.warning(
                            "source=steamdt event=rate_limit type=code4005 family=%s code=%s msg=%r "
                            "限频,非源故障;退避期内走缓存/CSQAQ 兜底",
                            family, code, msg[:80],
                        )
                        self._enter_rate_limit(family)
                        raise SteamDTRateLimit(f"SteamDT 业务限频 code={code}: {msg}")
                    raise RuntimeError(f"SteamDT 业务错误: {msg} code={code}")

                self._rate_limited[family] = 0.0
                self._rl_fail_streak[family] = 0
                self._last_network_ok = time.time()
                self._cache[key] = _CachedResp(time.time() + self._cache_ttl, data)
                return data, True
            except (asyncio.TimeoutError, OSError) as exc:
                last_exc = exc
                await asyncio.sleep(min(2.0 ** attempt, 8.0))
            except RuntimeError:
                raise
        raise RuntimeError(f"SteamDT 请求失败({path}),重试{self._retry}次") from last_exc

    @staticmethod
    def _broad_payload(d: Any) -> Dict[str, Any]:
        """取出大盘字段所在的 data 层。

        V0.3.14 **真正的根因**:SteamDT 回包是信封 `{"success":true,"data":{...大盘字段...}}`,
        拆包由 `normalize_item_kline_rows` 负责(K 线一直正常),但大盘这条路径漏了 ——
        `d.get("broadMarketIndex")` 恒为 None → **每次成功都被判成"空数据"** →
        触发换出口重试 → 撞 4005 → 锁 1800 秒。日志里 90 条
        `broad_index_ok index=0.00 diff_ratio_pct=0.00 update_time=0`
        (2026-09-05 11:54 是最后一条)就是它:SteamDT 大盘**从来没真正接上过**,
        用户看到的指数一直来自 CSQAQ 兜底。
        """
        if isinstance(d, dict):
            inner = d.get("data")
            if isinstance(inner, dict):
                return inner
            return d
        return {}

    @classmethod
    def _parse_broad(cls, d: Any) -> Dict[str, Any]:
        """信封 → 对外的大盘 dict(所有出口都必须走这里,含缓存命中)。"""
        p = cls._broad_payload(d)
        hist = p.get("historyMarketIndexList") or []
        return {
            "index": float(p.get("broadMarketIndex") or 0),
            "diff_yesterday": float(p.get("diffYesterday") or 0),
            "diff_ratio_pct": float(p.get("diffYesterdayRatio") or 0),
            "update_time": int(p.get("updateTime") or 0),
            "history": [(float(x[0]), float(x[1])) for x in hist
                        if isinstance(x, (list, tuple)) and len(x) >= 2],
            "raw": d,
        }

    async def get_broad_index_ex(self, force: bool = False) -> Tuple[Dict[str, Any], bool]:
        """大盘指数(V0.3.14 重写:拆信封 + 单次尝试 + 最小间隔 + 后台补抓)。

        旧实现在失败后 **1.5 秒连打 3 次**"换出口抽签"(当年实测新 curl=新 TCP=新出口,
        ~87% 能命中干净口)。2026-10-12 实测推翻了这个前提:
          · 原始单发 GET /open/cs2/broad/v1/index → HTTP 200 + success:true + 完整数据
          · 间隔 1.5s / 15s 再打 → code=4005「当前接口请求已达到上限」
          · 间隔 30s / 60s 再打 → 成功
        更关键的是:回包是信封,大盘字段在 `data` 里,旧代码直接读顶层 → 恒为 0 →
        立即被当成"空数据" → 才引出那 3 次重试。**两个 bug 叠在一起**:
        (a) 漏拆信封(大盘永远是 0);(b) 1.5 秒连试 3 次(自己制造 4005 并锁 30 分钟)。
        现在:①所有出口统一拆信封;②两次联网之间强制 ≥ broad_min_gap_seconds;
        ③只打一次,失败立刻让调用方拿 CSQAQ 兜底;④35 秒后后台补抓,补上就写缓存。
        """
        now = time.time()
        cached = self._cache.get(_BROAD_INDEX_PATH)
        # ① 退避期内:有缓存给缓存,没有就明说(不联网,避免继续撞限频)
        if now < self._rate_limited.get("broad", 0.0):
            if cached is not None:
                logger.warning("[steamdt] 接口族[broad] 限频退避期内,返回缓存数据(不联网,避免烧额度)")
                return self._parse_broad(cached.data), False
            raise SteamDTRateLimit("SteamDT 接口族[broad] 限频退避中且无缓存")
        # ② 距上次联网不足最小间隔:打了也是 4005,直接走缓存/明说
        gap = now - self._broad_last_attempt
        if self._broad_last_attempt and gap < self._broad_min_gap:
            if cached is not None:
                return self._parse_broad(cached.data), False
            raise SteamDTRateLimit(
                f"SteamDT broad 距上次请求仅 {gap:.0f}s(<{self._broad_min_gap:g}s),跳过联网")
        # ③ 缓存新鲜且未 force → 直接用,零网络
        if not force and cached is not None and now < cached.expire_at:
            return self._parse_broad(cached.data), False
        # ④ 单次联网
        self._broad_last_attempt = time.time()
        try:
            d, network_ok = await self._request_json(
                "GET", _BROAD_INDEX_PATH, force=True)
        except SteamDTRateLimit as exc:
            if "退避中" in str(exc):
                raise
            self._cache.pop(_BROAD_INDEX_PATH, None)
            logger.warning(
                "source=steamdt event=broad_index_rate_limited attempt=1/1 "
                "msg=%s (改为 %.0fs 后后台补抓)", str(exc)[:80], self._broad_retry_delay)
            # 注意:4005 路径里 `_request_json` 已经上过锁(streak 已 +1),这里**不再**
            # 重复 _enter_rate_limit —— 否则一次失败算两次,首轮退避就从 120s 变 300s。
            self._schedule_broad_refresh()
            raise
        parsed = self._parse_broad(d)
        if parsed["index"] <= 0 and parsed["update_time"] <= 0:
            # 真·空数据(success=true 但指数/时间全 0,该出口不可用)—— 绝不能渲染成真实指数
            self._cache.pop(_BROAD_INDEX_PATH, None)
            logger.warning(
                "source=steamdt event=broad_index_invalid attempt=1/1 "
                "msg=空数据(该出口不可用),改为 %.0fs 后后台补抓", self._broad_retry_delay)
            self._enter_rate_limit("broad")
            self._schedule_broad_refresh()
            raise SteamDTRateLimit("SteamDT broad index 空数据(该出口不可用)")
        if network_ok:
            logger.info(
                "source=steamdt event=broad_index_ok index=%.2f diff_ratio_pct=%.2f update_time=%d",
                parsed["index"], parsed["diff_ratio_pct"], parsed["update_time"])
        return parsed, network_ok

    async def get_broad_index(self, force: bool = False) -> Dict[str, Any]:
        d, _ = await self.get_broad_index_ex(force=force)
        return d

    async def get_item_kline(
        self,
        market_hash_name: str,
        *,
        periods: int = 2,
        max_time: Optional[int] = None,
        force: bool = False,
    ) -> Dict[str, Any]:
        """获取单品 K 线。"""
        if not market_hash_name:
            raise ValueError("market_hash_name is required")
        payloads: List[Dict[str, Any]] = [
            {"marketHashName": market_hash_name, "type": int(periods)},
            {"market_hash_name": market_hash_name, "type": int(periods)},
        ]
        if max_time is not None:
            payloads.extend([
                {"marketHashName": market_hash_name, "type": int(periods), "max_time": int(max_time)},
                {"market_hash_name": market_hash_name, "type": int(periods), "max_time": int(max_time)},
            ])

        last_exc: Optional[Exception] = None
        for payload in payloads:
            try:
                raw, network_ok = await self._request_json("POST", _ITEM_KLINE_PATH, payload, force=force)
                points = normalize_item_kline_rows(raw)
                if points:
                    logger.info(
                        "source=steamdt event=item_kline_ok marketHashName=%s periods=%s points=%s",
                        market_hash_name, periods, len(points),
                    )
                return {
                    "market_hash_name": market_hash_name,
                    "periods": int(periods),
                    "points": points,
                    "raw": raw,
                    "network_ok": network_ok,
                    "payload": payload,
                }
            except SteamDTRateLimit:
                # V0.2.1: 限频是确定性配额状态,换 payload 变体重试无意义,直接透传,
                # 让调用方(如 /分析)能按限频语义静默降级,而不是吞成通用 RuntimeError。
                raise
            except Exception as exc:
                last_exc = exc
        raise RuntimeError(f"SteamDT 单品K线请求失败 marketHashName={market_hash_name}") from last_exc

    @property
    def has_key(self) -> bool:
        return bool(self._key)
