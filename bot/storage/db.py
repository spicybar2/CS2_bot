# -*- coding: utf-8 -*-
"""SQLite 存储层：保存每次抓取的价格快照，支持历史趋势查询。"""

import asyncio
import logging
import sqlite3
import threading
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from bot.csqaq.client import RankItem

logger = logging.getLogger("storage")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS price_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    good_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    exterior TEXT,
    rarity TEXT,
    img TEXT,
    main_price REAL DEFAULT 0,
    buff_sell_price REAL DEFAULT 0,
    buff_buy_price REAL DEFAULT 0,
    steam_sell_price REAL DEFAULT 0,
    steam_buy_price REAL DEFAULT 0,
    yyyp_sell_price REAL DEFAULT 0,
    buff_price_chg REAL DEFAULT 0,
    sell_price_rate_7 REAL DEFAULT 0,
    sell_price_rate_30 REAL DEFAULT 0,
    sell_price_rate_90 REAL DEFAULT 0,
    sell_price_7 REAL DEFAULT 0,
    sell_price_30 REAL DEFAULT 0,
    buff_sell_num INTEGER DEFAULT 0,
    buff_buy_num INTEGER DEFAULT 0,
    steam_sell_num INTEGER DEFAULT 0,
    steam_buy_num INTEGER DEFAULT 0,
    sell_buy_diff_chg_buff REAL DEFAULT 0,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_snap_good_time ON price_snapshots (good_id, created_at);
CREATE INDEX IF NOT EXISTS idx_snap_time ON price_snapshots (created_at);

CREATE TABLE IF NOT EXISTS push_groups (
    group_openid TEXT PRIMARY KEY,
    created_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

-- 全量饰品索引(中文名 -> good_id),用于词典未收录时按中文名匹配
CREATE TABLE IF NOT EXISTS goods_index (
    good_id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    img TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_goods_index_name ON goods_index (name);

CREATE TABLE IF NOT EXISTS watchlist (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    group_openid TEXT NOT NULL,
    user_openid TEXT NOT NULL,
    good_id INTEGER DEFAULT 0,
    market_hash_name TEXT NOT NULL,
    zh_name TEXT NOT NULL,
    base_price REAL NOT NULL,
    threshold REAL DEFAULT 10.0,
    target_price REAL,
    created_at TEXT NOT NULL,
    last_alert_at TEXT,
    last_sell_num_scope TEXT NOT NULL DEFAULT 'buff',
    base_price_scope TEXT NOT NULL DEFAULT 'buff',
    UNIQUE(group_openid, user_openid, market_hash_name)
);
CREATE INDEX IF NOT EXISTS idx_watch_group_user ON watchlist (group_openid, user_openid);

-- 异动监控:在售时序(监控池 = Top200 热门 + 全量 watchlist),按 good_id+ts 查询
CREATE TABLE IF NOT EXISTS item_listing_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    good_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    ts TEXT NOT NULL,
    buff_sell_num INTEGER DEFAULT 0,
    steam_sell_num INTEGER DEFAULT 0,
    buff_buy_num INTEGER DEFAULT 0,
    steam_buy_num INTEGER DEFAULT 0,
    yyyp_buy_num INTEGER DEFAULT 0,
    buff_sell_price REAL DEFAULT 0,
    buff_buy_price REAL DEFAULT 0,
    total_sell_num INTEGER,
    total_buy_num INTEGER,
    total_sell_scope TEXT,
    total_buy_scope TEXT
);
CREATE INDEX IF NOT EXISTS idx_lh_good ON item_listing_history (good_id, ts);

-- 异动事件:检测层产出的扫货/下架/假动作,供 /异动 命令与实时推送读取
CREATE TABLE IF NOT EXISTS anomaly_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    good_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    anomaly_type TEXT NOT NULL,
    drop_pct REAL DEFAULT 0,
    abs_drop INTEGER DEFAULT 0,
    window_min INTEGER DEFAULT 0,
    sell_prev INTEGER DEFAULT 0,
    sell_cur INTEGER DEFAULT 0,
    level TEXT NOT NULL DEFAULT 'mid',
    detail TEXT,
    ts TEXT NOT NULL,
    last_push_at TEXT,
    push_status TEXT NOT NULL DEFAULT 'pending',
    push_detail TEXT,
    score REAL DEFAULT 0,
    pool TEXT,
    price REAL DEFAULT 0,
    price_change_5m REAL DEFAULT 0,
    price_change_30m REAL DEFAULT 0,
    price_change_1d REAL DEFAULT 0,
    rate_7 REAL DEFAULT 0,
    buy_change REAL DEFAULT 0,
    sell_change REAL DEFAULT 0,
    position TEXT,
    reason TEXT,
    transaction_volume INTEGER,
    transaction_avg_price REAL,
    transaction_period_at TEXT,
    transaction_volume_change REAL,
    transaction_volume_fresh INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_anom_ts ON anomaly_events (ts);

-- 异动雷达三层监控池(B 全量发现 / A 活跃 / S 超级异动),由 WatchPoolManager 维护
CREATE TABLE IF NOT EXISTS watch_pool (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    good_id INTEGER NOT NULL DEFAULT 0,
    name TEXT NOT NULL,
    market_hash_name TEXT,
    pool TEXT NOT NULL DEFAULT 'B',
    score REAL DEFAULT 0,
    enter_time TEXT NOT NULL,
    last_update TEXT,
    last_push_at TEXT,
    cooldown_until TEXT,
    hit_count INTEGER DEFAULT 0,
    quiet_count INTEGER DEFAULT 0,
    last_score REAL DEFAULT 0,
    quality_reject_count INTEGER DEFAULT 0,
    anomaly_buy_low_since TEXT,
    UNIQUE(good_id, name)
);
CREATE INDEX IF NOT EXISTS idx_wp_pool ON watch_pool (pool, score);
CREATE INDEX IF NOT EXISTS idx_wp_update ON watch_pool (last_update);

-- 异动推送冷却:独立于三层池生命周期,按 (good_id, name) 记录推送冷却截止时间。
-- 关键坑:旧实现把冷却存在 watch_pool.cooldown_until,而物品出池(DELETE 整行)会清掉冷却,
-- 导致重新进池时被当成"新异动"再次推送(咆哮反复刷屏即此因)。故冷却必须落到这张常驻表。
CREATE TABLE IF NOT EXISTS anomaly_push_cooldown (
    good_id INTEGER NOT NULL DEFAULT 0,
    name TEXT NOT NULL,
    cooldown_until TEXT,
    last_score REAL DEFAULT 0,
    last_type TEXT DEFAULT '',
    PRIMARY KEY (good_id, name)
);

-- V0.3.27 成本门槛观察:每条异动候选的「折后边际」快照。
-- 动机:推送前从没算过「这笔能不能覆盖 2.5% 手续费 + 1% 提现 + 买卖价差」。
-- 实测池内买卖价差中位 4.2%、¥50 以下 49%,人民币品往返成本 3.5%~12.5%,
-- 而引擎对 ¥58 品 0.26% 的波动也计分 —— 扣费后是负收益。
-- 本表先只观察不改推送;与 signal_outcomes 按 (good_id, ts) join 即可回答
-- 「边际高的候选是否真的更赚钱」——这是唯一能验证门槛对错的证据。
-- 关键:**没推送的候选也要记录**(push_status='capped'),否则被拦的那批永远没有结果。
CREATE TABLE IF NOT EXISTS cost_gate_observe (
    good_id INTEGER NOT NULL,
    ts TEXT NOT NULL,
    name TEXT,
    pool TEXT,
    level TEXT,
    score REAL DEFAULT 0,
    verdict TEXT NOT NULL DEFAULT 'skip',   -- pass 达标 / block 未达标 / skip 数据不足
    reason TEXT,
    buy_platform TEXT,                      -- buff / yyyp(取更便宜的那个)
    buy_ask REAL DEFAULT 0,
    buff_ask REAL DEFAULT 0,
    yyyp_ask REAL DEFAULT 0,
    gap_pct REAL DEFAULT 0,                 -- 两平台价差%(>15% 判离群)
    baseline REAL DEFAULT 0,                -- 近 N 日日收盘中位价
    baseline_days INTEGER DEFAULT 0,
    sell_fee_pct REAL DEFAULT 0,
    cost_pct REAL DEFAULT 0,                -- 往返成本 = 卖出费 + 提现
    discount_pct REAL DEFAULT 0,            -- (基准 - 买入价) / 基准
    edge_pct REAL DEFAULT 0,                -- 折扣 - 成本 = 排序键
    spread_pct REAL DEFAULT 0,              -- 买入平台的要价-求购价差
    rank INTEGER DEFAULT 0,                 -- 本轮候选内的边际排名(1 = 最优)
    push_status TEXT,
    created_at TEXT,
    PRIMARY KEY (good_id, ts)
);
CREATE INDEX IF NOT EXISTS idx_cost_verdict ON cost_gate_observe (verdict, edge_pct);

-- V0.3.17 市场情绪按日归档(老大 2026-10-14 选做 D)。
-- 目的:大盘卡只显示一个静态「市场热度」词,而 /current_data 还带 greedy(热度历史)与
-- rate_data(同一批约 2.6 万件商品在 1/7/15/30/90/180 天窗口下的涨跌家数,三态之和恒等)。
-- 存下来才能和 signal_outcomes(fwd_24h/72h/168h)按日期 join,回答
-- 「热度 ≥70 那几天推的建仓信号胜率是不是更低」。每天一行,同日重复采样覆盖更新。
CREATE TABLE IF NOT EXISTS market_sentiment (
    day TEXT PRIMARY KEY,              -- YYYY-MM-DD
    updated_at TEXT NOT NULL,          -- 当天最后一次采样时间
    heat_label TEXT,                   -- 中等 / 高 / 低
    heat_level TEXT,                   -- medium / ...
    heat_value REAL,                   -- greedy 最新点(同日重复点取最后一个)
    index_chg REAL,                    -- 饰品指数当日涨跌%
    up_1 INTEGER, down_1 INTEGER, flat_1 INTEGER,
    up_7 INTEGER, down_7 INTEGER, flat_7 INTEGER,
    up_30 INTEGER, down_30 INTEGER, flat_30 INTEGER,
    index_json TEXT                    -- 23 个分类指数快照(JSON)
);

-- 多平台在售快照:由详情接口采集，sell_scope=buff_yyyp 表示 BUFF+悠悠字段完整。
-- 用途:异常雷达按 BUFF+悠悠过滤；其余平台字段仅保留供展示兼容。
CREATE TABLE IF NOT EXISTS item_multi_snapshot (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    good_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    ts TEXT NOT NULL,
    buff_sell_num INTEGER DEFAULT 0,
    buff_buy_num INTEGER DEFAULT 0,
    steam_sell_num INTEGER DEFAULT 0,
    yyyp_sell_num INTEGER DEFAULT 0,
    c5_sell_num INTEGER DEFAULT 0,
    igxe_sell_num INTEGER DEFAULT 0,
    eco_sell_num INTEGER DEFAULT 0,
    sell_scope TEXT,
    buff_sell_price REAL DEFAULT 0,
    buff_buy_price REAL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_ms_good ON item_multi_snapshot (good_id, ts);

-- Steam 成交量历史:来自 CSQAQ 单件详情 turnover_number(按 period_at 去重)
CREATE TABLE IF NOT EXISTS item_turnover_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    good_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    turnover_number INTEGER,
    turnover_avg_price REAL,
    period_at TEXT,
    UNIQUE(good_id, period_at)
);
CREATE INDEX IF NOT EXISTS idx_th_good_period ON item_turnover_history (good_id, period_at);

-- 群功能开关:管理员私聊控制各群功能(异动/盯盘/播报)启停,异动推送按此过滤。
CREATE TABLE IF NOT EXISTS group_features (
    group_openid TEXT NOT NULL,
    feature TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    updated_at TEXT,
    PRIMARY KEY (group_openid, feature)
);

-- 信号结果闭环(V0.2.29):把每条异动事件之后的真实价格算成 T+1d/3d/7d 收益。
-- 目的:让「这个信号到底有没有前瞻性」可被数据回答,而不是靠感觉。
-- 由 scripts/track_signal_outcomes.py 结算;base_price 用事件落库价(A1 口径=要价)。
CREATE TABLE IF NOT EXISTS signal_outcomes (
    event_id INTEGER PRIMARY KEY,      -- = anomaly_events.id
    good_id INTEGER NOT NULL DEFAULT 0,
    name TEXT NOT NULL DEFAULT '',
    anomaly_type TEXT NOT NULL DEFAULT '',
    level TEXT NOT NULL DEFAULT '',
    score REAL DEFAULT 0,
    event_ts TEXT NOT NULL,
    base_price REAL DEFAULT 0,
    fwd_24h REAL,                      -- 相对 base_price 的百分比收益;NULL=暂无前瞻价
    fwd_72h REAL,
    fwd_168h REAL,
    settled_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_outcome_ts ON signal_outcomes (event_ts);
"""


class SnapshotStore:
    """价格快照存储（线程安全，异步调用方用 to_thread 包装）。"""

    def __init__(self, db_path: str = "data/csqaq.db", history_retention_days: int = 45):
        self._db_path = db_path
        # V0.3.37:保留天数原硬编码 30 天。T+7 前瞻统计需要 37 天以上的原始序列,
        # 30 天会把窗口最前面一周的样本吃掉,也让区间分位长期被截断(样本每周漂 10%)。
        self._retention_days = max(7, int(history_retention_days or 45))
        self._lock = threading.Lock()
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path, timeout=30)
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def _connect_ro(self) -> Optional[sqlite3.Connection]:
        """只读连接(WAL 下不阻塞写、不占 `self._lock`),用于重读取聚合。

        V0.3.1:价格区间聚合在 1.3GB 库上要十几秒,若走 `self._lock` 会**全程阻塞落库**
        (上线实测 46.4 秒)。WAL 模式下另开只读连接可看到一致快照且不干扰写事务。
        内存库 / 文件不存在时返回 None,调用方回退到共享锁路径。
        """
        import os
        if self._db_path == ":memory:" or not os.path.exists(self._db_path):
            return None
        try:
            conn = sqlite3.connect(f"file:{self._db_path}?mode=ro", uri=True, timeout=10)
            conn.execute("PRAGMA query_only=ON")
            return conn
        except sqlite3.Error:
            return None

    def _init_db(self):
        import os
        os.makedirs(os.path.dirname(self._db_path), exist_ok=True)
        with self._lock, self._connect() as conn:
            conn.executescript(_SCHEMA)
            # 迁移:老库补充 target_price 列
            cols = [r[1] for r in conn.execute("PRAGMA table_info(watchlist)")]
            if "target_price" not in cols:
                conn.execute("ALTER TABLE watchlist ADD COLUMN target_price REAL")
            # 迁移:盯盘类型扩展(7日涨跌/在售骤降)
            for col, ddl in (
                ("alert_type", "TEXT NOT NULL DEFAULT 'price'"),
                ("param", "REAL DEFAULT 0"),
                ("last_sell_num", "INTEGER DEFAULT 0"),
                ("last_sell_num_scope", "TEXT NOT NULL DEFAULT 'buff'"),
                # V0.3.23:盯盘基准价口径(buff=老记录/yyyp=悠悠优先);口径不一致时首轮重基准不提醒
                ("base_price_scope", "TEXT NOT NULL DEFAULT 'buff'"),
            ):
                if col not in cols:
                    conn.execute(f"ALTER TABLE watchlist ADD COLUMN {col} {ddl}")
                    cols.append(col)
            # 迁移:anomaly_events 增加 abs_drop 列(老库兼容,抓大商扫货绝对件数)
            a_cols = [r[1] for r in conn.execute("PRAGMA table_info(anomaly_events)")]
            if "abs_drop" not in a_cols:
                conn.execute("ALTER TABLE anomaly_events ADD COLUMN abs_drop INTEGER DEFAULT 0")
            # 迁移:异动雷达评分字段(老库兼容)
            for col, ddl in (
                ("score", "REAL DEFAULT 0"),
                ("pool", "TEXT"),
                ("price", "REAL DEFAULT 0"),
                ("price_change_5m", "REAL DEFAULT 0"),
                ("price_change_30m", "REAL DEFAULT 0"),
                ("price_change_1d", "REAL DEFAULT 0"),
                ("rate_7", "REAL DEFAULT 0"),   # V0.2.29:7日涨跌独立成列(此前混在 1d 列)
                ("buy_change", "REAL DEFAULT 0"),
                ("sell_change", "REAL DEFAULT 0"),
                ("position", "TEXT"),
                ("reason", "TEXT"),
                ("transaction_volume", "INTEGER"),
                ("transaction_avg_price", "REAL"),
                ("transaction_period_at", "TEXT"),
                ("transaction_volume_change", "REAL"),
                ("transaction_volume_fresh", "INTEGER DEFAULT 0"),
                ("push_status", "TEXT NOT NULL DEFAULT 'pending'"),
                ("push_detail", "TEXT"),
            ):
                if col not in a_cols:
                    conn.execute(f"ALTER TABLE anomaly_events ADD COLUMN {col} {ddl}")
            # score 列由迁移补建,索引必须在补列之后创建(否则老库报 no such column: score)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_anom_score ON anomaly_events(score)")
            # 老版本仅用 last_push_at 表示已发送。迁移后补齐明确状态，避免把
            # “复核拒绝”伪装成已发送，也避免永久留在待推队列。
            conn.execute(
                "UPDATE anomaly_events SET push_status='sent' "
                "WHERE last_push_at IS NOT NULL AND "
                "(push_status IS NULL OR push_status='pending')"
            )
            # 迁移:质量闸门连续失败计数。单次 API 缺字段/瞬态脏数据不得立刻移除池成员。
            # V0.3.17 市场情绪表补 flat_7/flat_30 —— 只存 up/down 的话,7/30 日的分母
            # 在库里推不出来(得靠"各窗口和恒等 26,090"这个外部假设),补上才自洽。
            ms_cols = [r[1] for r in conn.execute("PRAGMA table_info(market_sentiment)")]
            for col, ddl in (("flat_7", "INTEGER"), ("flat_30", "INTEGER")):
                if col not in ms_cols:
                    conn.execute(f"ALTER TABLE market_sentiment ADD COLUMN {col} {ddl}")
            wp_cols = [r[1] for r in conn.execute("PRAGMA table_info(watch_pool)")]
            if "quality_reject_count" not in wp_cols:
                conn.execute(
                    "ALTER TABLE watch_pool ADD COLUMN quality_reject_count INTEGER DEFAULT 0"
                )
                wp_cols.append("quality_reject_count")
            if "anomaly_buy_low_since" not in wp_cols:
                conn.execute(
                    "ALTER TABLE watch_pool ADD COLUMN anomaly_buy_low_since TEXT"
                )
                wp_cols.append("anomaly_buy_low_since")
            # V0.3.28 池卫生:
            #  s_rearm_at  —— S 池降级后的「冷却到此刻」,冷却期内禁止再升回 S(防 A↔S 抖动);
            #  best_score  —— 进池以来的历史最高分,沉底判定用(「从没冒过信号」而不是「这一刻分低」)。
            for col, ddl in (("s_rearm_at", "TEXT"), ("best_score", "REAL DEFAULT 0")):
                if col not in wp_cols:
                    conn.execute(f"ALTER TABLE watch_pool ADD COLUMN {col} {ddl}")
                    if col == "best_score":
                        # 回填:老成员的当前分就是唯一已知观测,先当作历史最高分,
                        # 免得升级后第一次清理就把整池当「从未达标」清掉。
                        conn.execute(
                            "UPDATE watch_pool SET best_score="
                            "MAX(COALESCE(score,0), COALESCE(last_score,0))")
                    wp_cols.append(col)
            # 迁移:item_listing_history 增加 buff_buy_price(算买卖价差,异动雷达 SpreadScore 依赖)
            lh_cols = [r[1] for r in conn.execute("PRAGMA table_info(item_listing_history)")]
            if "buff_buy_price" not in lh_cols:
                conn.execute(
                    "ALTER TABLE item_listing_history ADD COLUMN buff_buy_price REAL DEFAULT 0"
                )
                lh_cols.append("buff_buy_price")
            for col, ddl in (("total_sell_num", "INTEGER"),
                             ("total_buy_num", "INTEGER"),
                             ("total_sell_scope", "TEXT"),
                             # V0.2.24: 求购侧统一 BUFF+悠悠 —— 悠悠求购原值 + 合计口径标记
                             ("yyyp_buy_num", "INTEGER DEFAULT 0"),
                             ("total_buy_scope", "TEXT"),
                             # V0.3.4: 价格的双平台口径(数量早就统一了,价格此前只有 BUFF)。
                             # yyyp_* 存原值;total_*_price 是合成价(≥¥5 且两平台偏差在分档闸内才给值);
                             # total_*_price_scope 说明该点口径('buff_yyyp' 已合成 / 'buff_conflict'
                             # 偏差超闸退回 BUFF / 'buff_only' 悠悠缺字段 / 'yyyp_only' / NULL 老数据)。
                             # 合成价绝不能写 0(下游会把 0 当免费)。
                             ("yyyp_sell_price", "REAL DEFAULT 0"),
                             ("yyyp_buy_price", "REAL DEFAULT 0"),
                             ("total_sell_price", "REAL"),
                             ("total_buy_price", "REAL"),
                             ("total_sell_price_scope", "TEXT"),
                             ("total_buy_price_scope", "TEXT")):
                if col not in lh_cols:
                    conn.execute(f"ALTER TABLE item_listing_history ADD COLUMN {col} {ddl}")
                    lh_cols.append(col)
            # 迁移:结果表标注该行结算时用的价格口径(V0.3.4,双平台口径上线后新旧行不可混读)
            so_cols = [r[1] for r in conn.execute("PRAGMA table_info(signal_outcomes)")]
            if "price_scope" not in so_cols:
                conn.execute("ALTER TABLE signal_outcomes ADD COLUMN price_scope TEXT")
            # 迁移:推送冷却表增加 last_type(V0.2.0 周期四阶段:同波判定需类型一致,
            # 建仓→启动→派发是形态升级,同分也要放行递进推送)
            cd_cols = [r[1] for r in conn.execute("PRAGMA table_info(anomaly_push_cooldown)")]
            if "last_type" not in cd_cols:
                conn.execute("ALTER TABLE anomaly_push_cooldown ADD COLUMN last_type TEXT DEFAULT ''")
            # 迁移:多平台快照记录字段完整性，防止把缺失平台按 0 当成全平台合计。
            ms_cols = [r[1] for r in conn.execute("PRAGMA table_info(item_multi_snapshot)")]
            if "sell_scope" not in ms_cols:
                conn.execute("ALTER TABLE item_multi_snapshot ADD COLUMN sell_scope TEXT")
            # 走势图历史表:每次查价写入当前价,供 sparkline 展示
            conn.execute(
                """CREATE TABLE IF NOT EXISTS price_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    good_id INTEGER NOT NULL,
                    name TEXT,
                    price REAL NOT NULL,
                    ts TEXT NOT NULL
                )"""
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_ph_good ON price_history(good_id, ts)"
            )
            # 大盘指数盯盘表(独立,不与单品盯盘混表):以 SteamDT 大盘指数为触发源
            conn.execute(
                """CREATE TABLE IF NOT EXISTS index_watch (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    group_openid TEXT NOT NULL,
                    user_openid TEXT NOT NULL,
                    alert_type TEXT NOT NULL,
                    param REAL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    last_alert_at TEXT,
                    UNIQUE(group_openid, user_openid, alert_type, param)
                )"""
            )
            conn.commit()

    # ---- 写 ----

    def insert_snapshot_batch(self, items: List[RankItem]):
        """批量写入一次抓取快照。"""
        if not items:
            return
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")  # 微秒精度避免同秒批次碰撞
        rows = []
        for it in items:
            rows.append((
                it.good_id, it.name, it.exterior, it.rarity, it.img,
                it.main_price, it.buff_sell_price, it.buff_buy_price,
                it.steam_sell_price, it.steam_buy_price, it.yyyp_sell_price,
                it.buff_price_chg, it.sell_price_rate_7, it.sell_price_rate_30,
                it.sell_price_rate_90, it.sell_price_7, it.sell_price_30,
                it.buff_sell_num, it.buff_buy_num, it.steam_sell_num,
                it.sell_buy_diff_chg_buff, now,
            ))
        with self._lock, self._connect() as conn:
            conn.executemany(
                """INSERT INTO price_snapshots (
                    good_id, name, exterior, rarity, img,
                    main_price, buff_sell_price, buff_buy_price,
                    steam_sell_price, steam_buy_price, yyyp_sell_price,
                    buff_price_chg, sell_price_rate_7, sell_price_rate_30,
                    sell_price_rate_90, sell_price_7, sell_price_30,
                    buff_sell_num, buff_buy_num, steam_sell_num,
                    sell_buy_diff_chg_buff, created_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                rows,
            )
            conn.commit()
        logger.info("[storage] 已写入 %s 条快照 @ %s", len(rows), now)

    async def async_insert_snapshot_batch(self, items: List[RankItem]):
        await asyncio.to_thread(self.insert_snapshot_batch, items)

    # ---- 读 ----

    def query_latest(self, limit: int = 500) -> List[Dict[str, Any]]:
        """查询最新一次抓取快照（全字段）。"""
        with self._lock, self._connect() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """SELECT * FROM price_snapshots
                   WHERE created_at = (SELECT MAX(created_at) FROM price_snapshots)
                   ORDER BY main_price DESC LIMIT ?""",
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]

    def query_by_name(self, keyword: str, limit: int = 10) -> List[Dict[str, Any]]:
        """按饰品名模糊搜索最新快照（用于 /价格 指令）。"""
        like = f"%{keyword}%"
        with self._lock, self._connect() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """SELECT * FROM price_snapshots
                   WHERE name LIKE ?
                     AND created_at = (SELECT MAX(created_at) FROM price_snapshots)
                   ORDER BY main_price DESC LIMIT ?""",
                (like, limit),
            ).fetchall()
        return [dict(r) for r in rows]

    def query_history(self, good_id: int, days: int = 7) -> List[Dict[str, Any]]:
        """查询某饰品的价格历史（用于趋势展示）。"""
        since = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
        with self._lock, self._connect() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """SELECT created_at, main_price, buff_sell_price, sell_price_rate_7
                   FROM price_snapshots
                   WHERE good_id = ? AND created_at >= ?
                   ORDER BY created_at""",
                (good_id, since),
            ).fetchall()
        return [dict(r) for r in rows]

    # ---- 走势图历史(自建累积,每次查价写入) ----

    def add_price_history(self, good_id: int, name: str, price: float):
        """记录一次查价价格(用于 sparkline 走势)。"""
        if price <= 0:
            return
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO price_history (good_id, name, price, ts) VALUES (?,?,?,?)",
                (good_id, name, price, now),
            )
            conn.commit()

    def query_price_history(self, good_id: int, days: int = 30) -> List[float]:
        """返回某饰品最近 days 天的价格序列(升序),用于 sparkline。"""
        since = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT price FROM price_history WHERE good_id=? AND ts>=? ORDER BY ts",
                (good_id, since),
            ).fetchall()
        return [float(r[0]) for r in rows]

    async def async_add_price_history(self, good_id: int, name: str, price: float):
        await asyncio.to_thread(self.add_price_history, good_id, name, price)

    async def async_query_price_history(self, good_id: int, days: int = 30) -> List[float]:
        return await asyncio.to_thread(self.query_price_history, good_id, days)

    def last_fetch_time(self) -> Optional[str]:
        with self._lock, self._connect() as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute("SELECT MAX(created_at) AS t FROM price_snapshots").fetchone()
        if not row or not row["t"]:
            return None
        return row["t"][:19]  # 截取到秒，便于展示

    # ---- 定时推送群管理 ----

    def record_group(self, group_openid: str):
        """记录需要接收定时播报的群 openid（幂等）。"""
        if not group_openid:
            return
        with self._lock, self._connect() as conn:
            conn.execute("INSERT OR IGNORE INTO push_groups (group_openid) VALUES (?)", (group_openid,))
            conn.commit()

    def list_groups(self) -> List[str]:
        """返回所有记录在案的群 openid。"""
        with self._lock, self._connect() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute("SELECT group_openid FROM push_groups").fetchall()
        return [r["group_openid"] for r in rows]

    async def async_record_group(self, group_openid: str):
        await asyncio.to_thread(self.record_group, group_openid)

    async def async_list_groups(self) -> List[str]:
        return await asyncio.to_thread(self.list_groups)

    async def async_query_latest(self, limit: int = 500) -> List[Dict[str, Any]]:
        return await asyncio.to_thread(self.query_latest, limit)

    async def async_query_by_name(self, keyword: str, limit: int = 10) -> List[Dict[str, Any]]:
        return await asyncio.to_thread(self.query_by_name, keyword, limit)

    async def async_query_history(self, good_id: int, days: int = 7) -> List[Dict[str, Any]]:
        return await asyncio.to_thread(self.query_history, good_id, days)

    async def async_last_fetch_time(self) -> Optional[str]:
        return await asyncio.to_thread(self.last_fetch_time)

    # ------------------------------------------------------------------
    # 自选盯盘 (watchlist)
    # ------------------------------------------------------------------

    def add_watch(self, group_openid: str, user_openid: str, market_hash_name: str,
                  zh_name: str, base_price: float, threshold: float = 10.0,
                  good_id: int = 0, target_price: Optional[float] = None,
                  alert_type: str = "price", param: float = 0.0,
                  last_sell_num: int = 0, last_sell_num_scope: str = "all",
                  base_price_scope: str = "buff") -> bool:
        """添加一条盯盘记录(同群同用户同饰品幂等,重复则更新基准价/阈值/目标价/类型)。

        alert_type:
          - 'price'          价格阈值(threshold%)或目标价(target_price)触发
          - 'pct7d_up'       7日涨幅超 param%
          - 'pct7d_down'     7日跌幅超 param%
          - 'supply'         全平台在售件数降幅超 param%(last_sell_num 记录上次合计在售数)
        """
        from datetime import datetime
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with self._lock, self._connect() as conn:
            conn.execute(
                """INSERT INTO watchlist (group_openid, user_openid, good_id, market_hash_name,
                                          zh_name, base_price, threshold, target_price,
                                          alert_type, param, last_sell_num, last_sell_num_scope,
                                          base_price_scope, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(group_openid, user_openid, market_hash_name)
                   DO UPDATE SET base_price=excluded.base_price, threshold=excluded.threshold,
                                 target_price=excluded.target_price,
                                 good_id=excluded.good_id, created_at=excluded.created_at,
                                  alert_type=excluded.alert_type, param=excluded.param,
                                  last_sell_num_scope=excluded.last_sell_num_scope,
                                  base_price_scope=excluded.base_price_scope,
                                  last_alert_at=NULL""",
                (group_openid, user_openid, good_id, market_hash_name, zh_name,
                 base_price, threshold, target_price, alert_type, param,
                 last_sell_num, str(last_sell_num_scope or "all"),
                 str(base_price_scope or "buff"), now),
            )
            conn.commit()
        return True

    def remove_watch(self, group_openid: str, user_openid: str, market_hash_name: str) -> bool:
        with self._lock, self._connect() as conn:
            cur = conn.execute(
                "DELETE FROM watchlist WHERE group_openid=? AND user_openid=? AND market_hash_name=?",
                (group_openid, user_openid, market_hash_name),
            )
            conn.commit()
            return cur.rowcount > 0

    def list_watches_by_user(self, group_openid: str, user_openid: str) -> List[Dict[str, Any]]:
        with self._lock, self._connect() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """SELECT * FROM watchlist WHERE group_openid=? AND user_openid=?
                   ORDER BY created_at DESC""",
                (group_openid, user_openid),
            ).fetchall()
        return [dict(r) for r in rows]

    def list_all_watches(self) -> List[Dict[str, Any]]:
        """全部盯盘记录(供定时查价循环)。"""
        with self._lock, self._connect() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute("SELECT * FROM watchlist ORDER BY created_at").fetchall()
        return [dict(r) for r in rows]

    def update_watch_after_alert(self, watch_id: int, new_base_price: float) -> bool:
        """提醒后:更新基准价为当前价,并记录提醒时间(24h 冷却)。仅价格类盯盘用。"""
        from datetime import datetime
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with self._lock, self._connect() as conn:
            cur = conn.execute(
                "UPDATE watchlist SET base_price=?, last_alert_at=? WHERE id=?",
                (new_base_price, now, watch_id),
            )
            conn.commit()
            return cur.rowcount > 0

    def update_watch_base(self, watch_id: int, base_price: float,
                          base_price_scope: str = "buff") -> bool:
        """重设基准价与口径(不改提醒时间)。V0.3.23:老记录 BUFF 口径 → 悠悠口径的一次性迁移。"""
        with self._lock, self._connect() as conn:
            cur = conn.execute(
                "UPDATE watchlist SET base_price=?, base_price_scope=? WHERE id=?",
                (float(base_price), str(base_price_scope or "buff"), watch_id),
            )
            conn.commit()
            return cur.rowcount > 0

    def update_watch_last_alert(self, watch_id: int) -> bool:
        """仅记录提醒时间(24h 冷却),不改动基准价。用于非价格类盯盘。"""
        from datetime import datetime
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with self._lock, self._connect() as conn:
            cur = conn.execute(
                "UPDATE watchlist SET last_alert_at=? WHERE id=?",
                (now, watch_id),
            )
            conn.commit()
            return cur.rowcount > 0

    def update_watch_sell_num(self, watch_id: int, sell_num: int,
                              scope: str = "all") -> bool:
        """更新最近一次观察到的全平台在售件数(用于在售骤降对比)。"""
        with self._lock, self._connect() as conn:
            cur = conn.execute(
                "UPDATE watchlist SET last_sell_num=?, last_sell_num_scope=? WHERE id=?",
                (sell_num, str(scope or "all"), watch_id),
            )
            conn.commit()
            return cur.rowcount > 0

    def remove_watch_by_id(self, watch_id: int) -> bool:
        with self._lock, self._connect() as conn:
            cur = conn.execute("DELETE FROM watchlist WHERE id=?", (watch_id,))
            conn.commit()
            return cur.rowcount > 0

    # ------------------------------------------------------------------
    # 大盘指数盯盘 (index_watch)
    # ------------------------------------------------------------------

    def add_index_watch(self, group_openid: str, user_openid: str,
                        alert_type: str, param: float) -> bool:
        """添加一条大盘盯盘(同群同用户同类型同阈值幂等,重复则更新)。"""
        from datetime import datetime
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with self._lock, self._connect() as conn:
            conn.execute(
                """INSERT INTO index_watch (group_openid, user_openid, alert_type, param, created_at)
                   VALUES (?,?,?,?,?)
                   ON CONFLICT(group_openid, user_openid, alert_type, param)
                   DO UPDATE SET created_at=excluded.created_at, last_alert_at=NULL""",
                (group_openid, user_openid, alert_type, param, now),
            )
            conn.commit()
        return True

    def list_all_index_watches(self) -> List[Dict[str, Any]]:
        with self._lock, self._connect() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute("SELECT * FROM index_watch ORDER BY created_at").fetchall()
        return [dict(r) for r in rows]

    def list_index_watches_by_user(self, group_openid: str, user_openid: str) -> List[Dict[str, Any]]:
        with self._lock, self._connect() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM index_watch WHERE group_openid=? AND user_openid=? ORDER BY created_at DESC",
                (group_openid, user_openid),
            ).fetchall()
        return [dict(r) for r in rows]

    def remove_index_watch_by_id(self, watch_id: int) -> bool:
        with self._lock, self._connect() as conn:
            cur = conn.execute("DELETE FROM index_watch WHERE id=?", (watch_id,))
            conn.commit()
            return cur.rowcount > 0

    def remove_all_index_watches_by_user(self, group_openid: str, user_openid: str) -> int:
        with self._lock, self._connect() as conn:
            cur = conn.execute(
                "DELETE FROM index_watch WHERE group_openid=? AND user_openid=?",
                (group_openid, user_openid),
            )
            conn.commit()
            return cur.rowcount

    def update_index_watch_last_alert(self, watch_id: int) -> bool:
        """仅记录提醒时间(24h 冷却),不改任何参数。"""
        from datetime import datetime
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with self._lock, self._connect() as conn:
            cur = conn.execute(
                "UPDATE index_watch SET last_alert_at=? WHERE id=?", (now, watch_id))
            conn.commit()
            return cur.rowcount > 0

    async def async_add_index_watch(self, group_openid: str, user_openid: str,
                                    alert_type: str, param: float) -> bool:
        return await asyncio.to_thread(self.add_index_watch, group_openid, user_openid, alert_type, param)

    async def async_list_all_index_watches(self) -> List[Dict[str, Any]]:
        return await asyncio.to_thread(self.list_all_index_watches)

    async def async_list_index_watches_by_user(self, group_openid: str, user_openid: str) -> List[Dict[str, Any]]:
        return await asyncio.to_thread(self.list_index_watches_by_user, group_openid, user_openid)

    async def async_remove_index_watch_by_id(self, watch_id: int) -> bool:
        return await asyncio.to_thread(self.remove_index_watch_by_id, watch_id)

    async def async_remove_all_index_watches_by_user(self, group_openid: str, user_openid: str) -> int:
        return await asyncio.to_thread(self.remove_all_index_watches_by_user, group_openid, user_openid)

    async def async_update_index_watch_last_alert(self, watch_id: int) -> bool:
        return await asyncio.to_thread(self.update_index_watch_last_alert, watch_id)

    async def async_add_watch(self, group_openid: str, user_openid: str, market_hash_name: str,
                              zh_name: str, base_price: float, threshold: float = 10.0,
                              good_id: int = 0, target_price: Optional[float] = None,
                              alert_type: str = "price", param: float = 0.0,
                              last_sell_num: int = 0, last_sell_num_scope: str = "all",
                              base_price_scope: str = "buff") -> bool:
        return await asyncio.to_thread(self.add_watch, group_openid, user_openid,
                                       market_hash_name, zh_name, base_price, threshold,
                                       good_id, target_price, alert_type, param, last_sell_num,
                                       last_sell_num_scope, base_price_scope)

    async def async_remove_watch(self, group_openid: str, user_openid: str,
                                 market_hash_name: str) -> bool:
        return await asyncio.to_thread(self.remove_watch, group_openid, user_openid, market_hash_name)

    async def async_list_watches_by_user(self, group_openid: str, user_openid: str) -> List[Dict[str, Any]]:
        return await asyncio.to_thread(self.list_watches_by_user, group_openid, user_openid)

    async def async_list_all_watches(self) -> List[Dict[str, Any]]:
        return await asyncio.to_thread(self.list_all_watches)

    async def async_update_watch_after_alert(self, watch_id: int, new_base_price: float) -> bool:
        return await asyncio.to_thread(self.update_watch_after_alert, watch_id, new_base_price)

    async def async_update_watch_base(self, watch_id: int, base_price: float,
                                      base_price_scope: str = "buff") -> bool:
        return await asyncio.to_thread(self.update_watch_base, watch_id, base_price,
                                       base_price_scope)

    async def async_update_watch_last_alert(self, watch_id: int) -> bool:
        return await asyncio.to_thread(self.update_watch_last_alert, watch_id)

    async def async_update_watch_sell_num(self, watch_id: int, sell_num: int,
                                          scope: str = "all") -> bool:
        return await asyncio.to_thread(self.update_watch_sell_num, watch_id, sell_num, scope)

    async def async_remove_watch_by_id(self, watch_id: int) -> bool:
        return await asyncio.to_thread(self.remove_watch_by_id, watch_id)

    # ------------------------------------------------------------------
    # 全量饰品索引 (goods_index)
    # ------------------------------------------------------------------

    def save_goods_index(self, rows: List[Dict[str, Any]]):
        """批量保存/更新饰品索引(按 good_id 去重)。"""
        from datetime import datetime
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with self._lock, self._connect() as conn:
            conn.executemany(
                """INSERT OR REPLACE INTO goods_index (good_id, name, img, created_at)
                   VALUES (?,?,?,?)""",
                [(int(r["good_id"]), r["name"], r.get("img") or "", now) for r in rows],
            )
            conn.commit()

    def query_goods_by_keyword(self, keyword: str, limit: int = 10) -> List[Dict[str, Any]]:
        """按中文名模糊匹配饰品索引。"""
        kw = f"%{keyword}%"
        with self._lock, self._connect() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT good_id, name FROM goods_index WHERE name LIKE ? ORDER BY length(name) LIMIT ?",
                (kw, limit),
            ).fetchall()
        return [dict(r) for r in rows]

    def count_goods_index(self) -> int:
        with self._lock, self._connect() as conn:
            return conn.execute("SELECT COUNT(*) FROM goods_index").fetchone()[0]

    async def async_save_goods_index(self, rows: List[Dict[str, Any]]):
        await asyncio.to_thread(self.save_goods_index, rows)

    async def async_query_goods_by_keyword(self, keyword: str, limit: int = 10) -> List[Dict[str, Any]]:
        return await asyncio.to_thread(self.query_goods_by_keyword, keyword, limit)

    async def async_count_goods_index(self) -> int:
        return await asyncio.to_thread(self.count_goods_index)

    # ------------------------------------------------------------------
    # 异动监控时序 (item_listing_history + anomaly_events)
    # ------------------------------------------------------------------

    def save_listing_history_batch(self, rows: List[Dict[str, Any]]):
        """批量写入在售时序快照(监控池 = Top200 热门 + 全量 watchlist)。

        每条 row 需含 good_id/name/ts/buff_sell_num/steam_sell_num/
        buff_buy_num/steam_buy_num/buff_sell_price 键；全平台采样可额外传
        total_sell_num/total_buy_num，缺省表示该点不是完整 BUFF+悠悠口径；
        total_sell_scope='buff_yyyp' 表示异动雷达所需两平台在售完整。
        buff_buy_price 可选(异动雷达算买卖价差用,缺省 0)。
        """
        if not rows:
            return
        # sqlite3 命名参数绑定要求每个 dict 都含该键,缺键会抛 ProgrammingError,
        # 故此处统一补全缺省值(兜底: 任何未传 buff_buy_price 的写入路径都不报错)
        rows = [dict(r, buff_buy_price=r.get("buff_buy_price") or 0,
                     yyyp_buy_num=int(r.get("yyyp_buy_num") or 0),
                     total_sell_num=r.get("total_sell_num"),
                     total_buy_num=r.get("total_buy_num"),
                     total_sell_scope=r.get("total_sell_scope"),
                     total_buy_scope=r.get("total_buy_scope"),
                     # V0.3.4: 双平台价格列同样补全缺省(旧调用路径/老数据都不报错)
                     yyyp_sell_price=r.get("yyyp_sell_price") or 0,
                     yyyp_buy_price=r.get("yyyp_buy_price") or 0,
                     total_sell_price=r.get("total_sell_price"),
                     total_buy_price=r.get("total_buy_price"),
                     total_sell_price_scope=r.get("total_sell_price_scope"),
                     total_buy_price_scope=r.get("total_buy_price_scope")) for r in rows]
        with self._lock, self._connect() as conn:
            conn.executemany(
                """INSERT INTO item_listing_history
                   (good_id, name, ts, buff_sell_num, steam_sell_num,
                   buff_buy_num, steam_buy_num, yyyp_buy_num, buff_sell_price,
                   buff_buy_price, total_sell_num, total_buy_num,
                   total_sell_scope, total_buy_scope,
                   yyyp_sell_price, yyyp_buy_price, total_sell_price, total_buy_price,
                   total_sell_price_scope, total_buy_price_scope)
                   VALUES (:good_id, :name, :ts, :buff_sell_num, :steam_sell_num,
                           :buff_buy_num, :steam_buy_num, :yyyp_buy_num,
                           :buff_sell_price, :buff_buy_price, :total_sell_num,
                           :total_buy_num, :total_sell_scope, :total_buy_scope,
                           :yyyp_sell_price, :yyyp_buy_price, :total_sell_price,
                           :total_buy_price, :total_sell_price_scope,
                           :total_buy_price_scope)""",
                rows,
            )
            # 滚动清理: 保留近 N 天(V0.3.37:30→可配置,默认 45),控制表体积
            conn.execute(
                "DELETE FROM item_listing_history WHERE ts < "
                "datetime('now','localtime',?)",
                (f"-{self._retention_days} days",),
            )
            conn.commit()

    async def async_save_listing_history_batch(self, rows: List[Dict[str, Any]]):
        await asyncio.to_thread(self.save_listing_history_batch, rows)

    # 异动雷达扩展字段的缺省值:任何缺失这些键的写入路径(如 DB 迁移后老数据)都不会报错,
    # sqlite3 命名参数绑定缺键会抛 ProgrammingError,故在此统一补全
    _ANOMALY_DEFAULTS: Dict[str, Any] = {
        "score": 0.0, "pool": "", "price": 0.0,
        "price_change_5m": 0.0, "price_change_30m": 0.0, "price_change_1d": 0.0,
        "rate_7": 0.0,
        "buy_change": 0.0, "sell_change": 0.0, "position": "", "reason": "",
        "transaction_volume": None, "transaction_avg_price": None,
        "transaction_period_at": None, "transaction_volume_change": None,
        "transaction_volume_fresh": 0,
        # V0.3.16:push_status 可由调用方显式指定(每品每日上限命中的行写 'capped',
        # 落库但推送任务不取)。缺省仍是 pending,其他调用方行为不变。
        "push_status": "pending", "push_detail": None,
    }

    def insert_anomaly_events(self, rows: List[Dict[str, Any]], ts: Optional[str] = None):
        """写入本轮检测到的异动事件(含去噪后的扫货/下架/假动作)。

        :param ts: 统一时间戳(秒级)。V0.3.27 起由调用方传入,好让成本观察表
                   `cost_gate_observe` 用 (good_id, ts) 与本表精确 join;不传则现取。
        """
        if not rows:
            return
        now = ts or datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with self._lock, self._connect() as conn:
            conn.executemany(
                """INSERT INTO anomaly_events
                   (good_id, name, anomaly_type, drop_pct, abs_drop, window_min,
                    sell_prev, sell_cur, level, detail, ts,
                    score, pool, price, price_change_5m, price_change_30m, price_change_1d,
                    rate_7,
                    buy_change, sell_change, position, reason,
                    transaction_volume, transaction_avg_price, transaction_period_at,
                    transaction_volume_change, transaction_volume_fresh,
                    push_status, push_detail)
                   VALUES (:good_id, :name, :anomaly_type, :drop_pct, :abs_drop, :window_min,
                           :sell_prev, :sell_cur, :level, :detail, :ts,
                           :score, :pool, :price, :price_change_5m, :price_change_30m,
                           :price_change_1d, :rate_7,
                           :buy_change, :sell_change, :position, :reason,
                           :transaction_volume, :transaction_avg_price, :transaction_period_at,
                           :transaction_volume_change, :transaction_volume_fresh,
                           :push_status, :push_detail)""",
                [dict(self._ANOMALY_DEFAULTS, **dict(r, ts=now)) for r in rows],
            )
            conn.commit()

    async def async_insert_anomaly_events(self, rows: List[Dict[str, Any]],
                                          ts: Optional[str] = None):
        await asyncio.to_thread(self.insert_anomaly_events, rows, ts)

    def count_anomaly_pushes_today(self, day_start: str) -> Dict[int, int]:
        """统计当天每个品已占用推送额度的条数(V0.3.16 每品每日上限用)。

        只算 sent/pending —— 也就是"会真正推出去的"。
        被上限拦下的 capped 与发送失败的 failed 不计,否则一次失败就白吃一个额度。
        """
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT good_id, COUNT(*) FROM anomaly_events "
                "WHERE ts >= ? AND COALESCE(push_status, 'pending') IN ('sent', 'pending') "
                "GROUP BY good_id", (day_start,)).fetchall()
        return {int(g or 0): int(n) for g, n in rows}

    async def async_count_anomaly_pushes_today(self, day_start: str) -> Dict[int, int]:
        return await asyncio.to_thread(self.count_anomaly_pushes_today, day_start)

    # ---- V0.3.17 市场情绪按日归档 ----

    _SENTIMENT_COLS = ("day", "updated_at", "heat_label", "heat_level", "heat_value",
                       "index_chg", "up_1", "down_1", "flat_1", "up_7", "down_7", "flat_7",
                       "up_30", "down_30", "flat_30", "index_json")

    def upsert_market_sentiment(self, row: Dict[str, Any]) -> None:
        """按自然日归档市场情绪(day 为主键,同日重复采样覆盖更新)。"""
        if not row or not row.get("day"):
            return
        cols = self._SENTIMENT_COLS
        sql = ("INSERT OR REPLACE INTO market_sentiment (" + ", ".join(cols) + ") "
               "VALUES (" + ", ".join("?" * len(cols)) + ")")
        with self._lock, self._connect() as conn:
            conn.execute(sql, [row.get(c) for c in cols])
            conn.commit()

    async def async_upsert_market_sentiment(self, row: Dict[str, Any]) -> None:
        await asyncio.to_thread(self.upsert_market_sentiment, row)

    def list_market_sentiment(self, limit: int = 60) -> List[Dict[str, Any]]:
        """最近的按日情绪归档(新→旧);供回看与「按热度分组算信号胜率」。"""
        with self._lock, self._connect() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM market_sentiment ORDER BY day DESC LIMIT ?",
                (int(limit),)).fetchall()
        return [dict(r) for r in rows]

    async def async_list_market_sentiment(self, limit: int = 60) -> List[Dict[str, Any]]:
        return await asyncio.to_thread(self.list_market_sentiment, limit)

    def get_market_sentiment(self, day: str) -> Optional[Dict[str, Any]]:
        with self._lock, self._connect() as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute("SELECT * FROM market_sentiment WHERE day=?", (day,)).fetchone()
        return dict(row) if row else None

    async def async_get_market_sentiment(self, day: str) -> Optional[Dict[str, Any]]:
        return await asyncio.to_thread(self.get_market_sentiment, day)

    def get_last_anomaly_sell_cur(self, good_id: int, name: str,
                                  anomaly_type: str, within_hours: int = 24) -> Optional[int]:
        """数值去重: 返回该 (品+异动类型) 最近 within_hours 内已报的 sell_cur。

        若当前在售数未比此值进一步下降,则无需重复报告(取代原 60 分钟时间冷却)。
        无记录返回 None(视为首次,需报告)。
        """
        since = (datetime.now() - timedelta(hours=within_hours)).strftime("%Y-%m-%d %H:%M:%S")
        with self._lock, self._connect() as conn:
            if good_id:
                row = conn.execute(
                    "SELECT sell_cur FROM anomaly_events WHERE good_id=? AND anomaly_type=? "
                    "AND ts>=? ORDER BY ts DESC LIMIT 1",
                    (good_id, anomaly_type, since),
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT sell_cur FROM anomaly_events WHERE name=? AND anomaly_type=? "
                    "AND ts>=? ORDER BY ts DESC LIMIT 1",
                    (name, anomaly_type, since),
                ).fetchone()
        if not row or row[0] is None:
            return None
        return int(row[0])

    async def async_get_last_anomaly_sell_cur(self, good_id: int, name: str,
                                             anomaly_type: str, within_hours: int = 24) -> Optional[int]:
        return await asyncio.to_thread(
            self.get_last_anomaly_sell_cur, good_id, name, anomaly_type, within_hours
        )

    # ---------- 信号结果闭环(V0.2.29):事件 → T+1d/3d/7d 真实收益 ----------
    # 动机:评分/门槛/推送全都在"事前"做判断,却从未记录"事后"结果 —— 于是
    # 「建仓/启动信号到底有没有前瞻性」只能靠感觉争论。此三方法 + scripts/track_signal_outcomes.py
    # 构成最小闭环:事件落库 → 用 item_listing_history 真实售价结算前瞻收益 → 汇总胜率/超额。

    def list_events_for_settlement(self, limit: int = 1000,
                                   min_age_hours: int = 168) -> List[Dict[str, Any]]:
        """取待结算事件:评分>0、已过最短观察期(默认满 7 天)、且尚无结果行。"""
        cutoff = (datetime.now() - timedelta(hours=min_age_hours)).strftime("%Y-%m-%d %H:%M:%S")
        with self._lock, self._connect() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT e.id, e.good_id, e.name, e.anomaly_type, e.level, e.score, e.ts, e.price "
                "FROM anomaly_events e LEFT JOIN signal_outcomes o ON o.event_id = e.id "
                "WHERE o.event_id IS NULL AND e.score > 0 AND e.good_id > 0 AND e.price > 0 "
                "AND e.ts <= ? ORDER BY e.ts ASC LIMIT ?",
                (cutoff, int(limit)),
            ).fetchall()
        return [dict(r) for r in rows]

    def get_listing_price_series(self, good_id: int, ts_from: str,
                                 ts_to: str) -> List[Tuple[str, float]]:
        """取指定时间窗内该品的 (ts, BUFF 售价) 序列,供结算前瞻收益。

        只用真实落库快照价,不做任何插值/推算 —— 缺口就是缺口,宁缺毋假。
        """
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT ts, buff_sell_price FROM item_listing_history "
                "WHERE good_id=? AND ts BETWEEN ? AND ? AND buff_sell_price>0 ORDER BY ts ASC",
                (int(good_id), ts_from, ts_to),
            ).fetchall()
        return [(r[0], float(r[1])) for r in rows]

    def get_price_range_map(self, days: int = 30, min_days: int = 5,
                            good_id: Optional[int] = None,
                            good_ids: Optional[List[int]] = None
                            ) -> Dict[int, Tuple[float, float]]:
        """取指定品近 N 日的价格区间 `{good_id: (最低, 最高)}`(V0.3.0 派发「高位」口径)。

        口径:先取**每日收盘**(当天最后一个真实落库快照的 BUFF 售价),再对这些日收盘
        取最低/最高 —— 避免单笔异常报价(¥0.02 品一个报价台阶 = +50%)定义区间。
        至少要有 `min_days` 个交易日才返回,样本太薄的品宁缺毋假。

        :param good_id: 只查单个品(关注品周期监测路径用)。
        :param good_ids: 只查这批品(扫描路径用)。**强烈建议传**:整表聚合在 1.3GB 库上
                         要 20+ 秒且会长时间占用写锁,按品查(走 good_id 索引)是毫秒级。
        """
        cutoff = (datetime.now() - timedelta(days=max(1, int(days)))).strftime("%Y-%m-%d %H:%M:%S")
        where = "WHERE ts >= ? AND buff_sell_price > 0"
        args: List[Any] = [cutoff]
        if good_id is not None:
            where += " AND good_id = ?"
            args.append(int(good_id))
        elif good_ids is not None:
            ids = sorted({int(g) for g in good_ids})
            if not ids:
                return {}
            where += f" AND good_id IN ({','.join('?' * len(ids))})"
            args.extend(ids)
        # 日收盘取法:`GROUP BY good_id, 日期` + `MAX(ts)` 裸列 —— SQLite 3.7.11+ 保证
        # 聚合查询里的裸列取自 min/max 所在行,故 p 就是当天最后一笔价。
        # V0.3.1 实测比 ROW_NUMBER 窗口函数**快 2.2 倍且结果 1066/1066 完全一致**
        # (1092 品 27.1s → 12.2s);不取日收盘(全部快照 min/max)虽再快一点,但单笔
        # 异常报价会把最高价抬高 5% 以上的品有 317/1066(max 8.34 倍),故不采用。
        sql = f"""SELECT good_id, MIN(p) lo, MAX(p) hi, COUNT(*) n FROM (
                       SELECT good_id, buff_sell_price p, MAX(ts) mts
                       FROM item_listing_history
                       {where}
                       GROUP BY good_id, substr(ts,1,10)
                   ) GROUP BY good_id HAVING n >= ?"""
        params = (*args, int(min_days))
        conn = self._connect_ro()
        if conn is not None:                       # 只读连接:十几秒的聚合并发落库无影响
            try:
                rows = conn.execute(sql, params).fetchall()
            finally:
                conn.close()
        else:                                      # 内存库/文件缺失:回退共享锁
            with self._lock, self._connect() as c:
                rows = c.execute(sql, params).fetchall()
        return {int(r[0]): (float(r[1]), float(r[2])) for r in rows}

    async def async_get_price_range_map(self, days: int = 30, min_days: int = 5,
                                        good_ids: Optional[List[int]] = None
                                        ) -> Dict[int, Tuple[float, float]]:
        return await asyncio.to_thread(self.get_price_range_map, days, min_days, None, good_ids)

    async def async_get_price_range(self, good_id: int, days: int = 30,
                                    min_days: int = 5) -> Optional[Tuple[float, float]]:
        m = await asyncio.to_thread(self.get_price_range_map, days, min_days, int(good_id))
        return m.get(int(good_id))

    def upsert_signal_outcomes(self, rows: List[Dict[str, Any]]) -> None:
        """写入/更新前瞻收益(按 event_id 幂等,可反复重算)。"""
        if not rows:
            return
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with self._lock, self._connect() as conn:
            conn.executemany(
                """INSERT INTO signal_outcomes
                   (event_id, good_id, name, anomaly_type, level, score, event_ts,
                    base_price, fwd_24h, fwd_72h, fwd_168h, settled_at)
                   VALUES (:event_id, :good_id, :name, :anomaly_type, :level, :score,
                           :event_ts, :base_price, :fwd_24h, :fwd_72h, :fwd_168h, :settled_at)
                   ON CONFLICT(event_id) DO UPDATE SET
                     fwd_24h=excluded.fwd_24h, fwd_72h=excluded.fwd_72h,
                     fwd_168h=excluded.fwd_168h, settled_at=excluded.settled_at""",
                [dict(r, settled_at=now) for r in rows],
            )
            conn.commit()

    def list_signal_outcomes(self, limit: int = 20000) -> List[Dict[str, Any]]:
        """取已结算结果行(汇总胜率/中位收益在脚本侧算,SQL 无 median)。"""
        with self._lock, self._connect() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM signal_outcomes ORDER BY event_ts DESC LIMIT ?", (int(limit),)
            ).fetchall()
        return [dict(r) for r in rows]

    # ---------- 成本门槛观察(V0.3.27) ----------
    # 只在推送落库时按品取一次,全部走 good_id 索引,毫秒级,不占总预算。

    def get_price_quotes(self, good_id: int, days: int = 7) -> Dict[str, Dict[str, Any]]:
        """取单品两平台的「最新报价 + 近 N 日日收盘」。

        日收盘口径与 get_price_range_map 一致:当天最后一笔真实快照(`MAX(ts)` 裸列,
        SQLite 3.7.11+ 保证其余列取自该行),避免单笔异常报价定义基准。
        样本太薄就返回空 closes,由上层判 `baseline_thin` —— 宁缺毋假。
        """
        out: Dict[str, Dict[str, Any]] = {
            "buff": {"ask": 0.0, "bid": 0.0, "sell_num": 0, "ask_ts": "", "closes": []},
            "yyyp": {"ask": 0.0, "bid": 0.0, "sell_num": 0, "ask_ts": "", "closes": []},
        }
        if not good_id:
            return out
        # 多取一天,保证凑满 N 个完整自然日
        cutoff = (datetime.now() - timedelta(days=max(1, int(days)) + 1)
                  ).strftime("%Y-%m-%d %H:%M:%S")
        # 基准只吃**已完结交易日**:当天还没走完,把现价算进基准会自我参照
        # (价格当天跌了 → 基准也跟着跌 → 折扣被自己抹平),故显式排除今天。
        today = datetime.now().strftime("%Y-%m-%d")
        with self._lock, self._connect() as conn:
            conn.row_factory = sqlite3.Row
            latest = conn.execute(
                "SELECT ts, buff_sell_price, buff_buy_price, buff_sell_num, "
                "       yyyp_sell_price, yyyp_buy_price "
                "FROM item_listing_history WHERE good_id=? ORDER BY ts DESC LIMIT 1",
                (int(good_id),)).fetchone()
            if not latest:
                return out
            out["buff"].update(
                ask=float(latest["buff_sell_price"] or 0),
                bid=float(latest["buff_buy_price"] or 0),
                sell_num=int(latest["buff_sell_num"] or 0),
                ask_ts=str(latest["ts"] or ""))
            out["yyyp"].update(
                ask=float(latest["yyyp_sell_price"] or 0),
                bid=float(latest["yyyp_buy_price"] or 0),
                ask_ts=str(latest["ts"] or ""))
            closes = conn.execute(
                """SELECT MAX(ts) mts, buff_sell_price bp, yyyp_sell_price yp
                     FROM item_listing_history
                    WHERE good_id=? AND ts >= ? AND substr(ts,1,10) < ?
                    GROUP BY substr(ts,1,10) ORDER BY mts""",
                (int(good_id), cutoff, today)).fetchall()
        for r in closes:
            bp = float(r["bp"] or 0)
            yp = float(r["yp"] or 0)
            if bp > 0:
                out["buff"]["closes"].append(bp)
            if yp > 0:
                out["yyyp"]["closes"].append(yp)
        # 只保留最近 N 个日收盘
        keep = max(1, int(days))
        out["buff"]["closes"] = out["buff"]["closes"][-keep:]
        out["yyyp"]["closes"] = out["yyyp"]["closes"][-keep:]
        return out

    async def async_get_price_quotes(self, good_id: int,
                                     days: int = 7) -> Dict[str, Dict[str, Any]]:
        return await asyncio.to_thread(self.get_price_quotes, good_id, days)

    def insert_cost_observations(self, rows: List[Dict[str, Any]]) -> None:
        """写入成本门槛观察行(按 (good_id, ts) 幂等)。"""
        if not rows:
            return
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        cols = ("good_id", "ts", "name", "pool", "level", "score", "verdict", "reason",
                "buy_platform", "buy_ask", "buff_ask", "yyyp_ask", "gap_pct", "baseline",
                "baseline_days", "sell_fee_pct", "cost_pct", "discount_pct", "edge_pct",
                "spread_pct", "rank", "push_status", "created_at")
        sql = ("INSERT OR REPLACE INTO cost_gate_observe (" + ", ".join(cols) + ") VALUES ("
               + ", ".join("?" * len(cols)) + ")")
        with self._lock, self._connect() as conn:
            conn.executemany(
                sql, [[r.get(c) if c != "created_at" else now for c in cols] for r in rows])
            conn.commit()

    async def async_insert_cost_observations(self, rows: List[Dict[str, Any]]) -> None:
        await asyncio.to_thread(self.insert_cost_observations, rows)

    def list_cost_observations(self, limit: int = 5000) -> List[Dict[str, Any]]:
        """取观察行(新→旧),供 scripts 汇总「边际分档 vs 实际收益」。"""
        with self._lock, self._connect() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM cost_gate_observe ORDER BY ts DESC LIMIT ?", (int(limit),)
            ).fetchall()
        return [dict(r) for r in rows]

    # ---------- 异动雷达: 三层监控池 watch_pool ----------

    def list_goods_index_names(self, limit: int = 50, offset: int = 0) -> List[Dict[str, Any]]:
        """分页取 goods_index 的 (good_id, name)。

        注意:name 为中文名,而批量查价接口只认 market_hash_name(英文名),
        故 B 池实际走「榜单深翻」,本方法仅作兜底/索引用途。
        """
        with self._lock, self._connect() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT good_id, name FROM goods_index ORDER BY good_id LIMIT ? OFFSET ?",
                (int(limit), int(offset)),
            ).fetchall()
        return [dict(r) for r in rows]

    async def async_list_goods_index_names(self, limit: int = 50,
                                           offset: int = 0) -> List[Dict[str, Any]]:
        return await asyncio.to_thread(self.list_goods_index_names, limit, offset)

    def get_listing_series(self, good_id: int, name: str, hours: float = 24.0,
                           limit: int = 500) -> List[Dict[str, Any]]:
        """取该品最近 hours 小时的在售/求购/价格时序(正序),供 AnomalyEngine 算多周期变化。"""
        since = (datetime.now() - timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M:%S")
        with self._lock, self._connect() as conn:
            conn.row_factory = sqlite3.Row
            if good_id:
                rows = conn.execute(
                    "SELECT ts, buff_sell_num, buff_buy_num, yyyp_buy_num, buff_sell_price, buff_buy_price, "
                    "total_sell_num, total_buy_num, total_sell_scope, total_buy_scope "
                    "FROM item_listing_history WHERE good_id=? AND ts>=? "
                    "ORDER BY ts ASC LIMIT ?",
                    (good_id, since, int(limit)),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT ts, buff_sell_num, buff_buy_num, yyyp_buy_num, buff_sell_price, buff_buy_price, "
                    "total_sell_num, total_buy_num, total_sell_scope, total_buy_scope "
                    "FROM item_listing_history WHERE name=? AND ts>=? "
                    "ORDER BY ts ASC LIMIT ?",
                    (name, since, int(limit)),
                ).fetchall()
        return [dict(r) for r in rows]

    async def async_get_listing_series(self, good_id: int, name: str, hours: float = 24.0,
                                       limit: int = 500) -> List[Dict[str, Any]]:
        return await asyncio.to_thread(self.get_listing_series, good_id, name, hours, limit)

    def get_listing_series_batch(self, good_ids: List[int], hours: float = 2.0,
                                 limit_per_item: int = 120) -> Dict[int, List[Dict[str, Any]]]:
        """批量取多个品的时序(一次 SQL,避免 1000 件逐个往返拖慢扫描)。

        :return: {good_id: [时序点(正序)]}
        """
        ids = [int(g) for g in good_ids if g]
        if not ids:
            return {}
        since = (datetime.now() - timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M:%S")
        out: Dict[int, List[Dict[str, Any]]] = {}
        with self._lock, self._connect() as conn:
            conn.row_factory = sqlite3.Row
            for i in range(0, len(ids), 400):
                chunk = ids[i:i + 400]
                ph = ",".join("?" * len(chunk))
                rows = conn.execute(
                    f"SELECT good_id, ts, buff_sell_num, buff_buy_num, yyyp_buy_num, buff_sell_price, "
                    f"buff_buy_price, total_sell_num, total_buy_num, total_sell_scope, total_buy_scope "
                    f"FROM item_listing_history "
                    f"WHERE good_id IN ({ph}) AND ts>=? ORDER BY ts ASC",
                    (*chunk, since),
                ).fetchall()
                for r in rows:
                    gid = int(r["good_id"])
                    bucket = out.setdefault(gid, [])
                    if len(bucket) < limit_per_item:
                        bucket.append(dict(r))
        return out

    async def async_get_listing_series_batch(self, good_ids: List[int], hours: float = 2.0,
                                             limit_per_item: int = 120
                                             ) -> Dict[int, List[Dict[str, Any]]]:
        return await asyncio.to_thread(
            self.get_listing_series_batch, good_ids, hours, limit_per_item
        )

    def upsert_watch_pool(self, rows: List[Dict[str, Any]]) -> None:
        """批量 UPSERT 三层监控池(按 good_id+name 唯一)。"""
        if not rows:
            return
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with self._lock, self._connect() as conn:
            conn.executemany(
                """INSERT INTO watch_pool
                   (good_id, name, market_hash_name, pool, score, enter_time, last_update,
                   last_push_at, cooldown_until, hit_count, quiet_count, last_score,
                   quality_reject_count, anomaly_buy_low_since, s_rearm_at, best_score)
                   VALUES (:good_id, :name, :market_hash_name, :pool, :score, :enter_time,
                           :last_update, :last_push_at, :cooldown_until, :hit_count,
                           :quiet_count, :last_score, :quality_reject_count,
                           :anomaly_buy_low_since, :s_rearm_at, :best_score)
                   ON CONFLICT(good_id, name) DO UPDATE SET
                     score=excluded.score,
                     pool=excluded.pool,
                     market_hash_name=excluded.market_hash_name,
                     -- V0.3.28:enter_time 必须是「进入当前池的时刻」,之前漏更导致
                     -- S 池 TTL 一旦过期就永久过期 → A↔S 无限抖动。
                     enter_time=excluded.enter_time,
                     last_update=excluded.last_update,
                     last_push_at=excluded.last_push_at,
                     cooldown_until=excluded.cooldown_until,
                     hit_count=excluded.hit_count,
                     quiet_count=excluded.quiet_count,
                     last_score=excluded.last_score,
                     quality_reject_count=excluded.quality_reject_count,
                     anomaly_buy_low_since=excluded.anomaly_buy_low_since,
                     s_rearm_at=excluded.s_rearm_at,
                     best_score=excluded.best_score""",
                [dict(r,
                      enter_time=r.get("enter_time") or now,
                      last_update=r.get("last_update") or now,
                      market_hash_name=r.get("market_hash_name") or "",
                      last_push_at=r.get("last_push_at"),
                      cooldown_until=r.get("cooldown_until"),
                      hit_count=int(r.get("hit_count") or 0),
                      quiet_count=int(r.get("quiet_count") or 0),
                      last_score=float(r.get("last_score") or 0),
                      quality_reject_count=int(r.get("quality_reject_count") or 0),
                      anomaly_buy_low_since=r.get("anomaly_buy_low_since"),
                      s_rearm_at=r.get("s_rearm_at"),
                      best_score=float(r.get("best_score") or 0),
                      score=float(r.get("score") or 0))
                 for r in rows],
            )
            conn.commit()

    async def async_upsert_watch_pool(self, rows: List[Dict[str, Any]]) -> None:
        await asyncio.to_thread(self.upsert_watch_pool, rows)

    def upsert_multi_snapshot(self, rows: List[Dict[str, Any]]) -> None:
        """写多平台在售快照(保留时间序列,按 (good_id) 取最新)。"""
        if not rows:
            return
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with self._lock, self._connect() as conn:
            for r in rows:
                conn.execute(
                    """INSERT INTO item_multi_snapshot
                       (good_id, name, ts, buff_sell_num, buff_buy_num, steam_sell_num,
                        yyyp_sell_num, c5_sell_num, igxe_sell_num, eco_sell_num,
                        sell_scope, buff_sell_price, buff_buy_price)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (int(r.get("good_id") or 0), str(r.get("name") or ""), ts,
                     int(r.get("buff_sell_num") or 0), int(r.get("buff_buy_num") or 0),
                     int(r.get("steam_sell_num") or 0), int(r.get("yyyp_sell_num") or 0),
                     int(r.get("c5_sell_num") or 0), int(r.get("igxe_sell_num") or 0),
                      int(r.get("eco_sell_num") or 0),
                      (str(r.get("sell_scope") or "") or None),
                      float(r.get("buff_sell_price") or 0), float(r.get("buff_buy_price") or 0)))

    async def async_upsert_multi_snapshot(self, rows: List[Dict[str, Any]]) -> None:
        await asyncio.to_thread(self.upsert_multi_snapshot, rows)

    def get_multi_snapshot(self, good_ids: List[int]) -> Dict[int, Dict[str, Any]]:
        """取多个品的最新多平台快照,返回 {good_id: row};无记录返回空。"""
        ids = [int(g) for g in (good_ids or []) if g]
        if not ids:
            return {}
        q = ",".join("?" * len(ids))
        out: Dict[int, Dict[str, Any]] = {}
        with self._lock, self._connect() as conn:
            conn.row_factory = sqlite3.Row
            for row in conn.execute(
                    f"SELECT * FROM item_multi_snapshot WHERE good_id IN ({q}) "
                    "ORDER BY rowid DESC", ids).fetchall():
                d = dict(row)
                gid = int(d.get("good_id") or 0)
                if gid and gid not in out:
                    out[gid] = d
        return out

    async def async_get_multi_snapshot(self, good_ids: List[int]) -> Dict[int, Dict[str, Any]]:
        return await asyncio.to_thread(self.get_multi_snapshot, good_ids)

    # ---------- Steam 成交量历史 ----------

    def upsert_turnover_snapshot(self, rows: List[Dict[str, Any]]) -> None:
        if not rows:
            return
        observed = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with self._lock, self._connect() as conn:
            for r in rows:
                if not r.get("period_at"):
                    # 没有所属周期无法判断时效，也无法用 UNIQUE 去重；不落库，
                    # 避免平台同步每 10 分钟制造一条永远不可用的重复记录。
                    continue
                number = r.get("turnover_number")
                try:
                    number = int(number) if number is not None else None
                except (TypeError, ValueError):
                    number = None
                avg = r.get("turnover_avg_price")
                try:
                    avg = float(avg) if avg is not None else None
                except (TypeError, ValueError):
                    avg = None
                conn.execute(
                    """INSERT INTO item_turnover_history
                       (good_id, name, observed_at, turnover_number, turnover_avg_price, period_at)
                       VALUES (?,?,?,?,?,?)
                       ON CONFLICT(good_id, period_at) DO UPDATE SET
                         name=excluded.name, observed_at=excluded.observed_at,
                         turnover_number=excluded.turnover_number,
                         turnover_avg_price=excluded.turnover_avg_price""",
                    (int(r.get("good_id") or 0), str(r.get("name") or ""), observed,
                     number, avg, r.get("period_at")),
                )
            conn.commit()

    async def async_upsert_turnover_snapshot(self, rows: List[Dict[str, Any]]) -> None:
        await asyncio.to_thread(self.upsert_turnover_snapshot, rows)

    def get_turnover_history(self, good_id: int, limit: int = 8) -> List[Dict[str, Any]]:
        with self._lock, self._connect() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM item_turnover_history WHERE good_id=? "
                "ORDER BY COALESCE(period_at, observed_at) DESC, id DESC LIMIT ?",
                (int(good_id), int(limit)),
            ).fetchall()
        return [dict(r) for r in rows]

    async def async_get_turnover_history(self, good_id: int, limit: int = 8) -> List[Dict[str, Any]]:
        return await asyncio.to_thread(self.get_turnover_history, good_id, limit)

    def get_turnover_history_batch(self, good_ids: List[int], limit: int = 8) -> Dict[int, List[Dict[str, Any]]]:
        ids = list(dict.fromkeys(int(x) for x in (good_ids or []) if int(x) > 0))
        out: Dict[int, List[Dict[str, Any]]] = {}
        if not ids:
            return out
        # 一次连接分片读取，避免 A 池约 800 件时逐件创建 SQLite 连接。
        with self._lock, self._connect() as conn:
            conn.row_factory = sqlite3.Row
            for i in range(0, len(ids), 400):
                chunk = ids[i:i + 400]
                placeholders = ",".join("?" for _ in chunk)
                rows = conn.execute(
                    f"SELECT * FROM item_turnover_history WHERE good_id IN ({placeholders}) "
                    "ORDER BY good_id, COALESCE(period_at, observed_at) DESC, id DESC",
                    chunk,
                ).fetchall()
                for row in rows:
                    d = dict(row)
                    bucket = out.setdefault(int(d["good_id"]), [])
                    if len(bucket) < int(limit):
                        bucket.append(d)
        return out

    async def async_get_turnover_history_batch(self, good_ids: List[int], limit: int = 8) -> Dict[int, List[Dict[str, Any]]]:
        return await asyncio.to_thread(self.get_turnover_history_batch, good_ids, limit)

    def set_group_feature(self, group_openid: str, feature: str, enabled: bool) -> None:
        """设置某群某功能开关(异动/盯盘/播报)。"""
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO group_features (group_openid, feature, enabled, updated_at) "
                "VALUES (?,?,?,?)",
                (str(group_openid), str(feature), 1 if enabled else 0,
                 datetime.now().strftime("%Y-%m-%d %H:%M:%S")))

    async def async_set_group_feature(self, group_openid: str, feature: str, enabled: bool) -> None:
        await asyncio.to_thread(self.set_group_feature, group_openid, feature, enabled)

    def get_group_features(self) -> Dict[Tuple[str, str], bool]:
        """读取全部群功能开关,返回 {(group_openid, feature): enabled};无记录默认 True。"""
        out: Dict[Tuple[str, str], bool] = {}
        with self._lock, self._connect() as conn:
            conn.row_factory = sqlite3.Row
            for r in conn.execute("SELECT group_openid, feature, enabled FROM group_features").fetchall():
                out[(str(r["group_openid"]), str(r["feature"]))] = bool(r["enabled"])
        return out

    async def async_get_group_features(self) -> Dict[Tuple[str, str], bool]:
        return await asyncio.to_thread(self.get_group_features)

    def list_watch_pool(self, pool: Optional[str] = None,
                        limit: int = 10000) -> List[Dict[str, Any]]:
        """读取监控池;pool 为空返回全部(按评分倒序)。"""
        with self._lock, self._connect() as conn:
            conn.row_factory = sqlite3.Row
            if pool:
                rows = conn.execute(
                    "SELECT * FROM watch_pool WHERE pool=? ORDER BY score DESC LIMIT ?",
                    (pool, int(limit)),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM watch_pool ORDER BY score DESC LIMIT ?", (int(limit),)
                ).fetchall()
        return [dict(r) for r in rows]

    async def async_list_watch_pool(self, pool: Optional[str] = None,
                                    limit: int = 10000) -> List[Dict[str, Any]]:
        return await asyncio.to_thread(self.list_watch_pool, pool, limit)

    def list_stale_watch_pool(self, hours: int, limit: int,
                              pool: Optional[str] = None) -> List[Dict[str, Any]]:
        """取「最久没被扫描更新」的池成员,供强制回扫(看门狗)。

        排名口径:last_update 最旧优先(为空的排最前),同新旧按分数从低到高 ——
        先救那些既久没看过、又一直没信号的成员,避免它们永远沉在池底占位。
        """
        cutoff = (datetime.now() - timedelta(hours=max(1, int(hours)))
                  ).strftime("%Y-%m-%d %H:%M:%S")
        sql = "SELECT * FROM watch_pool WHERE (last_update IS NULL OR last_update < ?)"
        args: List[Any] = [cutoff]
        if pool:
            sql += " AND pool=?"
            args.append(str(pool))
        sql += " ORDER BY COALESCE(NULLIF(last_update,''),'') ASC, score ASC LIMIT ?"
        args.append(int(max(1, limit)))
        with self._lock, self._connect() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(sql, args).fetchall()
        return [dict(r) for r in rows]

    async def async_list_stale_watch_pool(self, hours: int, limit: int,
                                         pool: Optional[str] = None
                                         ) -> List[Dict[str, Any]]:
        return await asyncio.to_thread(self.list_stale_watch_pool, hours, limit, pool)

    def touch_watch_pool(self, good_ids: List[int], ts: Optional[str] = None) -> int:
        """只刷新 last_update(表示「这一轮真的看过它了」)。

        回扫时有些品能查到价、却因为缺求购基线等原因没进评分,``sync`` 不会碰它们,
        于是 last_update 停在几天前 → 下一轮又排到队首,同一批品被反复回扫、白烧额度。
        """
        ids = [int(g) for g in dict.fromkeys(good_ids or []) if int(g or 0) > 0]
        if not ids:
            return 0
        stamp = ts or datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        touched = 0
        with self._lock, self._connect() as conn:
            for i in range(0, len(ids), 400):
                chunk = ids[i:i + 400]
                ph = ",".join("?" * len(chunk))
                cur = conn.execute(
                    f"UPDATE watch_pool SET last_update=? WHERE good_id IN ({ph})",
                    [stamp, *chunk])
                touched += int(cur.rowcount or 0)
            conn.commit()
        return touched

    async def async_touch_watch_pool(self, good_ids: List[int],
                                     ts: Optional[str] = None) -> int:
        return await asyncio.to_thread(self.touch_watch_pool, good_ids, ts)

    def purge_stale_watch_pool(self, days: int, below_score: float, limit: int,
                               pool: str = "B") -> List[Tuple[int, str]]:
        """沉底:进池满 ``days`` 天、历史最高分从未到过 ``below_score`` 的成员移出池。

        判定用 best_score(历史最高)而不是当前分:当前分只代表最后一刻,而
        「进池一周从没冒过信号」才是真的占位。返回被移除的 (good_id, name)。
        """
        keep = max(1, int(limit))
        cutoff = (datetime.now() - timedelta(days=max(1, int(days)))
                  ).strftime("%Y-%m-%d %H:%M:%S")
        with self._lock, self._connect() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """SELECT good_id, name FROM watch_pool
                    WHERE pool=? AND COALESCE(enter_time,'') <> '' AND enter_time < ?
                      AND COALESCE(best_score,0) < ?
                    ORDER BY enter_time ASC LIMIT ?""",
                (str(pool), cutoff, float(below_score), keep)).fetchall()
            if not rows:
                return []
            conn.executemany(
                "DELETE FROM watch_pool WHERE good_id=? AND name=?",
                [(int(r["good_id"]), r["name"]) for r in rows])
            conn.commit()
        return [(int(r["good_id"]), r["name"]) for r in rows]

    async def async_purge_stale_watch_pool(self, days: int, below_score: float,
                                           limit: int, pool: str = "B"
                                           ) -> List[Tuple[int, str]]:
        return await asyncio.to_thread(
            self.purge_stale_watch_pool, days, below_score, limit, pool)

    def count_watch_pool(self) -> Dict[str, int]:
        """各池数量统计,供 /异动 状态 命令展示。"""
        with self._lock, self._connect() as conn:
            rows = conn.execute("SELECT pool, COUNT(*) FROM watch_pool GROUP BY pool").fetchall()
        return {str(r[0]): int(r[1]) for r in rows}

    async def async_count_watch_pool(self) -> Dict[str, int]:
        return await asyncio.to_thread(self.count_watch_pool)

    def trim_watch_pool(self, pool: str, limit: int) -> int:
        """把指定池硬限制在前 ``limit`` 个高分成员，返回淘汰数量。

        排序与 list_watch_pool 一致，以 score 为主；同分时优先保留最近更新项。
        用于 B 池长尾兜底，避免只进不出的历史成员无限占用存储和同步预算。
        """
        keep = max(0, int(limit))
        if keep <= 0:
            return 0
        with self._lock, self._connect() as conn:
            before = int(conn.execute(
                "SELECT COUNT(*) FROM watch_pool WHERE pool=?", (str(pool),)
            ).fetchone()[0])
            if before <= keep:
                return 0
            conn.execute(
                "DELETE FROM watch_pool WHERE pool=? AND id NOT IN ("
                "SELECT id FROM watch_pool WHERE pool=? "
                "ORDER BY score DESC, last_update DESC, id DESC LIMIT ?)",
                (str(pool), str(pool), keep),
            )
            conn.commit()
            return before - keep

    async def async_trim_watch_pool(self, pool: str, limit: int) -> int:
        return await asyncio.to_thread(self.trim_watch_pool, pool, limit)

    def remove_from_watch_pool(self, good_id: int, name: str) -> None:
        with self._lock, self._connect() as conn:
            conn.execute("DELETE FROM watch_pool WHERE good_id=? AND name=?", (good_id, name))
            conn.commit()

    async def async_remove_from_watch_pool(self, good_id: int, name: str) -> None:
        await asyncio.to_thread(self.remove_from_watch_pool, good_id, name)

    def remove_watch_pool_batch(self, keys: List[Tuple[int, str]],
                                pool: Optional[str] = None) -> int:
        """批量移除已确认不满足扫描在售门槛的监控成员。"""
        clean = list(dict.fromkeys(
            (int(gid), str(name or "")) for gid, name in (keys or []) if int(gid) > 0
        ))
        if not clean:
            return 0
        with self._lock, self._connect() as conn:
            if pool:
                conn.executemany(
                    "DELETE FROM watch_pool WHERE good_id=? AND name=? AND pool=?",
                    [(gid, name, str(pool)) for gid, name in clean],
                )
            else:
                conn.executemany(
                    "DELETE FROM watch_pool WHERE good_id=? AND name=?", clean
                )
            removed = conn.execute("SELECT changes()").fetchone()[0]
            conn.commit()
        return int(removed)

    async def async_remove_watch_pool_batch(self, keys: List[Tuple[int, str]],
                                            pool: Optional[str] = None) -> int:
        return await asyncio.to_thread(self.remove_watch_pool_batch, keys, pool)

    def purge_watch_pool_low_sell(self, min_sell_num: int) -> int:
        """按最新完整 BUFF+悠悠快照清理 B 池中的低流动性/未知成员。"""
        threshold = max(0, int(min_sell_num))
        if threshold <= 0:
            return 0
        with self._lock, self._connect() as conn:
            cur = conn.execute(
                """WITH observations AS (
                       SELECT m.good_id,
                              m.buff_sell_num + m.yyyp_sell_num AS total_sell,
                              m.ts
                       FROM item_multi_snapshot m
                       WHERE m.sell_scope='buff_yyyp'
                       UNION ALL
                       SELECT h.good_id, h.total_sell_num AS total_sell, h.ts
                       FROM item_listing_history h
                       WHERE h.total_sell_scope='buff_yyyp'
                         AND h.total_sell_num IS NOT NULL
                   ), latest AS (
                       SELECT good_id, total_sell
                       FROM (
                           SELECT o.good_id, o.total_sell,
                                  ROW_NUMBER() OVER (
                                      PARTITION BY o.good_id ORDER BY o.ts DESC
                                  ) AS rn
                           FROM observations o
                       )
                       WHERE rn=1
                    )
                   DELETE FROM watch_pool
                   WHERE EXISTS (
                       SELECT 1 FROM latest l
                       WHERE l.good_id=watch_pool.good_id
                         AND COALESCE(l.total_sell, 0) < ?
                    )
                    OR (pool='B' AND NOT EXISTS (
                        SELECT 1 FROM latest l2 WHERE l2.good_id=watch_pool.good_id
                    ))""",
                (threshold,),
            )
            removed = conn.execute("SELECT changes()").fetchone()[0]
            conn.commit()
        return int(removed if removed >= 0 else 0)

    async def async_purge_watch_pool_low_sell(self, min_sell_num: int) -> int:
        return await asyncio.to_thread(self.purge_watch_pool_low_sell, min_sell_num)

    def set_pool_cooldown(self, good_id: int, name: str, cooldown_until: str) -> None:
        """仅回填某商品冷却截止时间,不改动 pool/score 等字段。

        注意:冷却回填必须按真实 good_id 定位,绝不能用 good_id=0 占位——
        watch_pool 的 UNIQUE(good_id, name) 会把 (0, name) 当成另一行,
        既产生重复记录,又让冷却判定匹配不到真实行而不生效。
        """
        with self._lock, self._connect() as conn:
            conn.execute(
                "UPDATE watch_pool SET cooldown_until=?, last_update=? "
                "WHERE good_id=? AND name=?",
                (cooldown_until,
                 datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                 int(good_id), name),
            )
            conn.commit()

    async def async_set_pool_cooldown(self, good_id: int, name: str,
                                      cooldown_until: str) -> None:
        await asyncio.to_thread(self.set_pool_cooldown, good_id, name, cooldown_until)

    def set_anomaly_cooldown(self, good_id: int, name: str,
                             cooldown_until: str, last_score: float = 0.0,
                             last_type: str = "") -> None:
        """写入(覆盖)某品推送冷却截止时间。常驻表,出池不丢(见 anomaly_push_cooldown 注释)。

        last_type(V0.2.0):最近一次推送的异动类型(accum/launch/distribute/surge...)。
        同波判定需类型一致 —— 建仓→启动→派发是形态升级,同分也要放行递进推送。
        """
        with self._lock, self._connect() as conn:
            conn.execute(
                """INSERT INTO anomaly_push_cooldown
                   (good_id, name, cooldown_until, last_score, last_type)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(good_id, name) DO UPDATE SET
                     cooldown_until=excluded.cooldown_until,
                     last_score=excluded.last_score,
                     last_type=excluded.last_type""",
                (int(good_id), name, cooldown_until, float(last_score), str(last_type or "")),
            )
            conn.commit()

    async def async_set_anomaly_cooldown(self, good_id: int, name: str,
                                         cooldown_until: str, last_score: float = 0.0,
                                         last_type: str = "") -> None:
        await asyncio.to_thread(self.set_anomaly_cooldown, good_id, name, cooldown_until,
                                last_score, last_type)

    def list_anomaly_cooldown(self) -> Dict[Tuple, Dict[str, Any]]:
        """读取全部推送冷却记录(表很小,只含被推送过的品),返回 {(good_id, name): {...}}。"""
        out: Dict[Tuple, Dict[str, Any]] = {}
        with self._lock, self._connect() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT good_id, name, cooldown_until, last_score, last_type "
                "FROM anomaly_push_cooldown"
            ).fetchall()
        for r in rows:
            out[(int(r["good_id"]), r["name"])] = {
                "cooldown_until": r["cooldown_until"],
                "last_score": float(r["last_score"] or 0),
                "last_type": str(r["last_type"] or ""),
            }
        return out

    async def async_list_anomaly_cooldown(self) -> Dict[Tuple, Dict[str, Any]]:
        return await asyncio.to_thread(self.list_anomaly_cooldown)

    def mark_anomalies_pushed(self, ts: str, push_at: str):
        """回填推送时间: 把某批次(ts 相同)且未推送过的异动标记为已推,避免重复推。"""
        with self._lock, self._connect() as conn:
            conn.execute(
                "UPDATE anomaly_events SET last_push_at=?, push_status='sent', push_detail=NULL "
                "WHERE ts=? AND last_push_at IS NULL",
                (push_at, ts),
            )
            conn.commit()

    async def async_mark_anomalies_pushed(self, ts: str, push_at: str):
        await asyncio.to_thread(self.mark_anomalies_pushed, ts, push_at)

    def mark_anomalies_pushed_ids(self, ids: List[int], push_at: str) -> None:
        """按事件主键精确回填推送时间，避免同批次事件被一起标记。"""
        clean = [int(i) for i in (ids or []) if int(i) > 0]
        if not clean:
            return
        placeholders = ",".join("?" for _ in clean)
        with self._lock, self._connect() as conn:
            conn.execute(
                f"UPDATE anomaly_events SET last_push_at=?, push_status='sent', push_detail=NULL "
                f"WHERE id IN ({placeholders}) AND last_push_at IS NULL",
                (push_at, *clean),
            )
            conn.commit()

    async def async_mark_anomalies_pushed_ids(self, ids: List[int], push_at: str) -> None:
        await asyncio.to_thread(self.mark_anomalies_pushed_ids, ids, push_at)

    def resolve_anomaly_push_ids(self, ids: List[int], status: str,
                                 detail: str = "") -> None:
        """将无需继续推送的事件移出待推队列，同时保留未实际发送的事实。"""
        clean = [int(i) for i in (ids or []) if int(i) > 0]
        if not clean:
            return
        if status not in ("rejected_liquidity", "superseded"):
            raise ValueError(f"unsupported anomaly push status: {status}")
        placeholders = ",".join("?" for _ in clean)
        with self._lock, self._connect() as conn:
            conn.execute(
                f"UPDATE anomaly_events SET push_status=?, push_detail=? "
                f"WHERE id IN ({placeholders}) AND last_push_at IS NULL "
                "AND COALESCE(push_status, 'pending')='pending'",
                (status, detail, *clean),
            )
            conn.commit()

    async def async_resolve_anomaly_push_ids(self, ids: List[int], status: str,
                                             detail: str = "") -> None:
        await asyncio.to_thread(self.resolve_anomaly_push_ids, ids, status, detail)

    def list_pending_anomalies(self, top_n: int = 10,
                               min_score: float = 0.0) -> List[Dict[str, Any]]:
        """返回最近 24h 真正待推的事件，不让已发送/已拒绝事件占据名额。"""
        since = (datetime.now() - timedelta(hours=24)).strftime("%Y-%m-%d %H:%M:%S")
        with self._lock, self._connect() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """SELECT * FROM anomaly_events
                   WHERE ts >= ? AND last_push_at IS NULL
                     AND COALESCE(push_status, 'pending')='pending'
                     AND score >= ?
                   ORDER BY ts DESC, id DESC LIMIT ?""",
                (since, float(min_score), int(top_n)),
            ).fetchall()
        return [dict(r) for r in rows]

    async def async_list_pending_anomalies(self, top_n: int = 10,
                                           min_score: float = 0.0) -> List[Dict[str, Any]]:
        return await asyncio.to_thread(self.list_pending_anomalies, top_n, min_score)

    def list_active_anomalies(self, top_n: int = 10) -> List[Dict[str, Any]]:
        """返回最近 24h 内的异动事件(按时间倒序),供 /异动 命令与实时推送读取。"""
        since = (datetime.now() - timedelta(hours=24)).strftime("%Y-%m-%d %H:%M:%S")
        with self._lock, self._connect() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """SELECT * FROM anomaly_events WHERE ts >= ?
                   ORDER BY ts DESC LIMIT ?""",
                (since, top_n),
            ).fetchall()
        return [dict(r) for r in rows]

    async def async_list_active_anomalies(self, top_n: int = 10) -> List[Dict[str, Any]]:
        return await asyncio.to_thread(self.list_active_anomalies, top_n)

    def get_listing_prev(self, good_id: int, name: str,
                         window_min: int) -> Optional[Dict[str, Any]]:
        """取某品在 window_min 分钟之前的最近一条在售快照(用于计算 Δsell)。

        good_id>0 优先按 good_id 查;否则按 name 查(兼容 watchlist 未解析 good_id 的品)。
        """
        since = (datetime.now() - timedelta(minutes=window_min)).strftime("%Y-%m-%d %H:%M:%S")
        with self._lock, self._connect() as conn:
            conn.row_factory = sqlite3.Row
            if good_id:
                row = conn.execute(
                    "SELECT * FROM item_listing_history WHERE good_id=? AND ts<=? "
                    "ORDER BY ts DESC LIMIT 1", (good_id, since)
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT * FROM item_listing_history WHERE name=? AND ts<=? "
                    "ORDER BY ts DESC LIMIT 1", (name, since)
                ).fetchone()
        return dict(row) if row else None

    async def async_get_listing_prev(self, good_id: int, name: str,
                                     window_min: int) -> Optional[Dict[str, Any]]:
        return await asyncio.to_thread(self.get_listing_prev, good_id, name, window_min)
