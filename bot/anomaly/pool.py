"""WatchPoolManager:三层监控池 B/A/S 的升降级、TTL 与容量淘汰(PRD V1.0 §24)。

升降级规则(全部来自 config,不写死):
- B → A: Score ≥ b_to_a
- A → S: Score ≥ a_to_s
- S → A: 连续 s_to_a_rounds 轮 Score < s_to_a,或超过 TTL(s_max_minutes)
- A → B: 连续 a_to_b_rounds 轮 Score < a_to_b
容量:S 池超过 s_size、A 池超过 a_size 时,按评分倒序淘汰最低分。
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from bot.config import AnomalyConfig
from bot.storage.db import SnapshotStore

logger = logging.getLogger(__name__)

POOL_B = "B"
POOL_A = "A"
POOL_S = "S"

_POOL_ORDER = {POOL_B: 0, POOL_A: 1, POOL_S: 2}


def _key(good_id: int, name: str) -> Tuple[int, str]:
    return (int(good_id or 0), name or "")


class WatchPoolManager:
    """维护 watch_pool 表的池归属与生命周期。"""

    def __init__(self, cfg: AnomalyConfig, store: SnapshotStore):
        self._cfg = cfg
        self._store = store

    async def load(self) -> Dict[Tuple[int, str], Dict[str, Any]]:
        """读取当前三层池,返回 {(good_id, name): row}。"""
        try:
            rows = await self._store.async_list_watch_pool(limit=10000)
        except Exception as exc:
            logger.exception("[anomaly] 读取监控池失败: %s", exc)
            return {}
        return {_key(int(r.get("good_id") or 0), r.get("name") or ""): r for r in rows}

    @staticmethod
    def _expired(row: Dict[str, Any], max_minutes: int) -> bool:
        """S 池 TTL:超时强制降级,防止强异动商品永久占满超级池。"""
        if max_minutes <= 0:
            return False
        raw = row.get("enter_time") or row.get("last_update")
        if not raw:
            return False
        try:
            t = datetime.strptime(raw, "%Y-%m-%d %H:%M:%S")
        except (ValueError, TypeError):
            return False
        return (datetime.now() - t) > timedelta(minutes=max_minutes)

    def _stale(self, row: Dict[str, Any], max_minutes: int, low_score: float) -> bool:
        """A 池「跌出榜单僵尸」判定:超过 max_minutes 未被任何扫描更新(=已不在活跃榜单)
        且上次评分 < low_score → 回落 B 让位(僵尸长期占位不贡献,清掉)。"""
        if max_minutes <= 0:
            return False
        raw = row.get("last_update") or row.get("enter_time")
        if not raw:
            return False
        try:
            t = datetime.strptime(raw, "%Y-%m-%d %H:%M:%S")
        except (ValueError, TypeError):
            return False
        if (datetime.now() - t) <= timedelta(minutes=max_minutes):
            return False
        return float(row.get("last_score") or 0) < low_score

    @staticmethod
    def _rearm_blocked(row: Dict[str, Any]) -> bool:
        """S 池降级冷却期内禁止再升回 S(V0.3.28 反抖)。

        实测 2026-10-27:一枚印花在 1 小时 45 分钟里 A→S 升级 10 次,
        每次升上去几十秒就被 TTL/低分打回来,日志、事件、推送全被同一件货刷屏。
        """
        raw = row.get("s_rearm_at")
        if not raw:
            return False
        try:
            t = datetime.strptime(str(raw), "%Y-%m-%d %H:%M:%S")
        except (ValueError, TypeError):
            return False
        return datetime.now() < t

    def decide(self, cur: Optional[Dict[str, Any]], score: float,
               allow_downgrade: bool = True,
               hit_rounds: int = 0, strong: bool = False) -> Tuple[str, str]:
        """根据当前池状态与最新评分,返回 (目标池, 变动原因)。cur=None 表示新商品。

        :param allow_downgrade: 是否允许降级。B 池(低频大范围)扫描时应传 False,
            否则会用它那套稀疏数据把 A/S 池的高频跟踪结果给覆盖降级 —— 实测 B 池
            扫到同一商品时评分普遍偏低,会把 A 池成员全部打回 B 池。
        :param hit_rounds: 已有连续信号轮数(hit_count;sync 维护)。
        :param strong: 本轮是否盘口强信号(百分比+绝对量+基线三重判定达标)。
            2026-09-04:存量 B→A 升级需「strong 单轮 或 连续 b_confirm_rounds 轮」确认,
            防单轮偶发高分把噪声抬进高频层。
        """
        c = self._cfg
        if cur is None:
            if score >= c.a_to_s:
                return POOL_S, "new_strong"
            if score >= c.b_to_a:
                return POOL_A, "new_active"
            return POOL_B, "discovered"

        confirm_rounds = c.book_confirm_rounds if c.book_confirm_rounds > 0 else c.b_confirm_rounds
        pool = cur.get("pool") or POOL_B
        if not allow_downgrade and pool in (POOL_A, POOL_S):
            # B 池扫描只做"发现与升级",不干预 A/S 池的归属
            if (pool == POOL_A and score >= c.a_to_s
                    and not self._rearm_blocked(cur)):
                return POOL_S, "score_high"
            return pool, ""
        if pool == POOL_S:
            if self._expired(cur, c.s_max_minutes):
                return POOL_A, "ttl"
            if score < c.s_to_a:
                quiet = int(cur.get("quiet_count") or 0) + 1
                if quiet >= c.s_to_a_rounds:
                    return POOL_A, "score_low"
            return POOL_S, ""
        if pool == POOL_A:
            # V0.3.28:A→S 也要连续确认 + 冷却,原先单轮高分即直升,
            # 会让「一分钟前 100 分、这一分钟 14 分」的噪声来回升。
            if (score >= c.a_to_s and hit_rounds >= max(1, confirm_rounds)
                    and not self._rearm_blocked(cur)):
                return POOL_S, "score_high_confirmed"
            # 僵尸回落:长时间未被打分(已跌出活跃榜)且上次分低 → 降 B,别占 A 高频名额
            if self._stale(cur, c.a_stale_minutes, c.b_to_a):
                return POOL_B, "stale_off_list"
            if score < c.a_to_b:
                quiet = int(cur.get("quiet_count") or 0) + 1
                if quiet >= c.a_to_b_rounds:
                    return POOL_B, "quiet"
            return POOL_A, ""
        # B 池存量升级:强信号可单轮直升,普通高分需连续两轮确认(降抖)
        confirmed = strong or hit_rounds >= max(1, confirm_rounds)
        if score >= c.a_to_s and confirmed:
            return POOL_S, "score_high_confirmed"
        if score >= c.b_to_a and confirmed:
            return POOL_A, "score_high_confirmed"
        if score >= c.b_to_a:
            return POOL_B, "watch_confirm"   # 高分但未确认:留在 B 观察,hit 累计
        return POOL_B, ""

    async def sync(self, results, mhn_map: Optional[Dict[int, str]] = None,
                   allow_downgrade: bool = True) -> Dict[str, List[str]]:
        """按最新评分更新三层池。

        :param results: AnomalyResult 列表(来自 AnomalyEngine)
        :param mhn_map: good_id -> market_hash_name(S 池批量查价需要)
        :param allow_downgrade: B 池扫描传 False(只升不降)
        :return: 变动记录 {"upgrade": [...], "downgrade": [...]}
        """
        mhn_map = mhn_map or {}
        c = self._cfg
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        pool_rows = await self.load()
        changes: Dict[str, List[str]] = {"upgrade": [], "downgrade": []}
        upserts: List[Dict[str, Any]] = []
        reject_need = max(1, int(getattr(c, "quality_reject_confirm_rounds", 3)))

        def carry(row: Dict[str, Any], reject_count: int,
                  anomaly_buy_low_since: Optional[str] = None) -> Dict[str, Any]:
            """保留原池归属/评分,仅刷新时刻与质量失败计数。"""
            return {
                "good_id": int(row.get("good_id") or 0),
                "name": row.get("name") or "",
                "market_hash_name": row.get("market_hash_name") or "",
                "pool": row.get("pool") or POOL_B,
                "score": float(row.get("score") or 0),
                "enter_time": row.get("enter_time") or now,
                "last_update": now,
                "last_push_at": row.get("last_push_at"),
                "cooldown_until": row.get("cooldown_until"),
                "hit_count": int(row.get("hit_count") or 0),
                "quiet_count": int(row.get("quiet_count") or 0),
                "last_score": float(row.get("last_score") or 0),
                "quality_reject_count": int(reject_count),
                "anomaly_buy_low_since": anomaly_buy_low_since,
                "s_rearm_at": row.get("s_rearm_at"),
                "best_score": float(row.get("best_score") or 0),
            }

        for r in results:
            k = _key(r.good_id, r.name)
            cur = pool_rows.get(k)

            details = r.details or {}
            rejected = details.get("rejected")
            incomplete = details.get("data_incomplete")

            # 未进入任何池的新商品若已被质量闸门拦截，不应因为一次评分
            # 结果又被写回 B 池；后续 B 扫描仍可在数据恢复后重新发现。
            if cur is None and rejected:
                logger.debug("[WATCH] 跳过新商品 item=%s reason=%s", r.name, rejected)
                continue

            # 异动求购深度只是“本轮不评分/不推送”的业务门槛，不是基础质量故障。
            # 引擎已经先跑完薄盘/价差/离谱波动等真实质量检查，因此走到这里表示
            # 其他质量合格；已有成员保留，新商品仍由上面的 rejected 分支跳过。
            if cur is not None and rejected == "min_anomaly_buy_num":
                low_since = cur.get("anomaly_buy_low_since") or now
                grace_min = max(0, int(getattr(c, "anomaly_buy_grace_minutes", 60)))
                low_at = None
                try:
                    low_at = datetime.strptime(str(low_since), "%Y-%m-%d %H:%M:%S")
                except (ValueError, TypeError):
                    low_at = datetime.now()
                    low_since = now
                elapsed_min = (datetime.now() - low_at).total_seconds() / 60.0
                if grace_min <= 0 or elapsed_min >= grace_min:
                    await self._store.async_remove_from_watch_pool(r.good_id, r.name)
                    changes["downgrade"].append(
                        f"{r.name} {cur.get('pool')}→移除(求购持续低于异动门槛)")
                    logger.info("[WATCH] %s 移除 item=%s reason=anomaly_buy_depth_timeout(%dm)",
                                cur.get("pool"), r.name, grace_min)
                    continue
                upserts.append(carry(cur, 0, str(low_since)))
                logger.info("[WATCH] %s 保留 item=%s reason=anomaly_buy_depth_grace(%d/%dm)",
                            cur.get("pool"), r.name, int(elapsed_min), grace_min)
                continue

            # 批量回包缺字段不等于真实质量失败:保留池成员,等待下一轮完整数据。
            if incomplete and incomplete != "buy_carried_forward":
                if cur is not None:
                    upserts.append(carry(
                        cur, int(cur.get("quality_reject_count") or 0),
                        cur.get("anomaly_buy_low_since")))
                    logger.debug("[WATCH] %s 保留 item=%s reason=data_incomplete:%s",
                                 cur.get("pool"), r.name, incomplete)
                else:
                    logger.debug("[WATCH] 跳过 item=%s reason=data_incomplete:%s",
                                 r.name, incomplete)
                continue

            # 质量不达标需要连续确认,防止 401/429/字段缺失导致一次性误杀。
            if cur is not None and r.score <= 0 and rejected:
                # B 深翻是发现层,allow_downgrade=False 时不能用一次稀疏/陈旧
                # 结果清掉 A/S 高频跟踪成员；只有当前就是 B 才执行质量移除。
                if not allow_downgrade and (cur.get("pool") or POOL_B) in (POOL_A, POOL_S):
                    continue
                reject_count = int(cur.get("quality_reject_count") or 0) + 1
                if reject_count < reject_need:
                    upserts.append(carry(cur, reject_count, cur.get("anomaly_buy_low_since")))
                    logger.info("[WATCH] %s 保留 item=%s reason=quality_hold(%d/%d,%s)",
                                cur.get("pool"), r.name, reject_count, reject_need, rejected)
                    continue
                await self._store.async_remove_from_watch_pool(r.good_id, r.name)
                changes["downgrade"].append(f"{r.name} {cur.get('pool')}→移除(质量不达标)")
                logger.info("[WATCH] %s 移除 item=%s reason=quality", cur.get("pool"), r.name)
                continue

            # 连续异动/平静计数:用于多轮确认后才升降级,避免阈值附近抖动。
            # 2026-09-04:hit_count 语义 = 「强信号 或 达预警分」的连续轮数(供 B→A 两轮确认)。
            strong = ((r.details or {}).get("buy_signal") == "strong"
                      or (r.details or {}).get("sell_signal") == "strong")
            hit = int(cur.get("hit_count") or 0) if cur else 0
            quiet = int(cur.get("quiet_count") or 0) if cur else 0
            if strong or r.score >= c.b_to_a:
                hit += 1
                quiet = 0
            elif r.score < c.a_to_b:
                quiet += 1
                hit = 0
            else:
                hit = 0
                quiet = 0

            # decide 需要"更新前的 hit"(判断此前已有几轮),用 cur 旧值传入
            target, why = self.decide(cur, r.score, allow_downgrade=allow_downgrade,
                                      hit_rounds=hit,
                                      strong=strong)

            prev_pool = (cur.get("pool") or POOL_B) if cur else None
            entered_new_pool = cur is None or prev_pool != target
            # V0.3.28:enter_time 必须是「进入当前池的时刻」——TTL 从这里起算。
            # 之前 UPSERT 漏了 enter_time,它就永远是首次发现时间,S 池 TTL 一旦
            # 过期就永久过期,于是每轮都 S→A(ttl)、下一轮又 A→S,无限抖动。
            enter_time = now if entered_new_pool else ((cur or {}).get("enter_time") or now)
            # 反抖:S 池降级后进冷却,冷却期内不再升回 S;真正回到 S 时清空冷却。
            s_rearm_at = (cur or {}).get("s_rearm_at")
            if cur is not None and prev_pool != target:
                rearm_min = max(0, int(getattr(c, "s_rearm_minutes", 45)))
                if prev_pool == POOL_S and rearm_min > 0:
                    s_rearm_at = (datetime.now() + timedelta(minutes=rearm_min))
                    s_rearm_at = s_rearm_at.strftime("%Y-%m-%d %H:%M:%S")
                elif target == POOL_S:
                    s_rearm_at = None
            best_score = max(float((cur or {}).get("best_score") or 0), float(r.score or 0))
            if cur and prev_pool != target:
                if _POOL_ORDER[target] > _POOL_ORDER[prev_pool]:
                    changes["upgrade"].append(f"{r.name} {prev_pool}→{target} score={r.score}")
                    logger.info("[WATCH] %s -> %s item=%s score=%.1f reason=%s",
                                prev_pool, target, r.name, r.score, why)
                else:
                    changes["downgrade"].append(f"{r.name} {prev_pool}→{target} score={r.score}")
                    logger.info("[WATCH] %s -> %s item=%s score=%.1f reason=%s",
                                prev_pool, target, r.name, r.score, why)

            upserts.append({
                "good_id": r.good_id,
                "name": r.name,
                "market_hash_name": (cur or {}).get("market_hash_name") or mhn_map.get(r.good_id) or "",
                "pool": target,
                "score": r.score,
                "enter_time": enter_time,
                "last_update": now,
                "last_push_at": (cur or {}).get("last_push_at"),
                "cooldown_until": (cur or {}).get("cooldown_until"),
                "hit_count": hit,
                "quiet_count": quiet,
                "last_score": r.score,
                "quality_reject_count": 0,
                # carry-forward 轮次不应清掉此前由真实求购观测开启的低深度计时；
                # 只有新的实时求购结果恢复到门槛以上时才重置。
                "anomaly_buy_low_since": (
                    (cur or {}).get("anomaly_buy_low_since")
                    if incomplete == "buy_carried_forward" else None),
                "s_rearm_at": s_rearm_at,
                "best_score": best_score,
            })

        # 容量淘汰:S 池与 A 池按评分倒序保留前 N,其余降级
        upserts = self._enforce_capacity(upserts, POOL_S, c.s_size, POOL_A, "s_full")
        upserts = self._enforce_capacity(upserts, POOL_A, c.a_size, POOL_B, "a_full")

        if upserts:
            try:
                await self._store.async_upsert_watch_pool(upserts)
            except Exception as exc:
                logger.exception("[anomaly] 写监控池失败: %s", exc)
        # B 是发现层，没有可继续降级的下一级，必须在落库后做全池硬容量淘汰；
        # 仅对本批 upserts 做 _enforce_capacity 无法约束数据库中的历史存量。
        b_limit = max(0, int(getattr(c, "b_size", 0)))
        if b_limit > 0:
            try:
                removed = await self._store.async_trim_watch_pool(POOL_B, b_limit)
                if removed:
                    logger.info("[WATCH] B 池容量淘汰 %d 件(limit=%d)", removed, b_limit)
            except Exception as exc:
                logger.exception("[anomaly] B 池容量淘汰失败: %s", exc)
        return changes

    def _enforce_capacity(self, rows: List[Dict[str, Any]], pool: str, limit: int,
                          demote_to: str, reason: str) -> List[Dict[str, Any]]:
        """某池超过容量时,按评分倒序保留前 limit 个,其余降级。"""
        if limit <= 0:
            return rows
        members = [r for r in rows if r["pool"] == pool]
        if len(members) <= limit:
            return rows
        members.sort(key=lambda r: float(r.get("score") or 0), reverse=True)
        overflow = members[limit:]
        for r in overflow:
            logger.info("[WATCH] %s -> %s item=%s reason=%s", pool, demote_to, r["name"], reason)
            r["pool"] = demote_to
        return rows
