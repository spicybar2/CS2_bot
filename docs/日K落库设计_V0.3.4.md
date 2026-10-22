# SteamDT 365 根日K 落库设计（目标版本 V0.3.4）

> 立项依据：2026-09-28 复盘。**先量后改**——本设计里的每条约束都来自实测，不是推测。
> 相关实测（只读脚本 `probe_kline_vs_buff.py`，10 个池内品）：
> - 每品 **365 根**，跨度 2025-09-29 ~ 2026-09-28；单根字段仅 `timestamp/open/close/high/low` → **没有成交量**。
> - **日K收盘 ÷ 我们库里的 BUFF 现价 = 中位 0.938（0.855~1.030）**：既不是 BUFF 口径(1.00)、也不是 Steam 含 15% 费口径(1.15)，是 SteamDT 自己的参考口径，**且逐品偏差不同**。
> - 结论一：**可以**用它做"同一序列内的相对量"（涨跌幅/均线/回撤/区间位置/跨平台价差）。
> - 结论二：**不可以**把它与 BUFF 价格混算绝对阈值（派发高位闸 `现价 ≥ 近30日最高价×90%` 会被 6% 的口径差整类翻面）。

---

## 0. 目标与非目标

**目标（按价值排序）**
1. 让产品里所有 **20/60 日线、30 日跌幅**类判断**第一次可验证**（本地日收盘目前只有 31 天：20 日线仅够 1 个样本点，60 日线永远没有）。
2. **新发现的品当天就有 20/60 日上下文**（现在要等 5 个交易日，期间派发判定被迫回退到已废弃的动量口径）。
3. **长周期结果评估**（T+30/T+90）与**去幸存者偏差**（把已出池/退市的品纳入基准）。
4. **省配额、抗重启**：`/分析` 优先读本地日K（现在每次现取，内存缓存重启即失效）。

**非目标（明确不做）**
- ❌ 不补成交量（数据源没有该字段）——项目"不把盘口数量当成交量"的红线不变。
- ❌ 不补 float / 图案 / 贴纸维度（仍是同名品级别）。
- ❌ **不用它替换或回填 BUFF 口径的绝对阈值**（见上面结论二）。
- ❌ 不全库落 2.5 万品（900 万行、收益不成比例）。

---

## 1. 数据模型（P0）

`bot/storage/db.py` 建表（放在 `_SCHEMA` 里，随现有 `CREATE TABLE IF NOT EXISTS` 一起执行）：

```sql
-- SteamDT 日K 落库(V0.3.4)。口径 source='steamdt',与 BUFF 快照价不可混算绝对阈值。
CREATE TABLE IF NOT EXISTS item_kline_daily (
    good_id          INTEGER NOT NULL,
    d                TEXT    NOT NULL,   -- SteamDT 侧日期 YYYY-MM-DD(由 timestamp/1000 转本地日期)
    open             REAL,
    close            REAL,
    high             REAL,
    low              REAL,
    source           TEXT    NOT NULL DEFAULT 'steamdt',
    market_hash_name TEXT,               -- 取数时用的名字(排查 ★/改名问题用)
    fetched_at       TEXT    NOT NULL,
    PRIMARY KEY (good_id, d)
);
CREATE INDEX IF NOT EXISTS idx_kline_d_good_d ON item_kline_daily (good_id, d);
```

- **主键 `(good_id, d)`** → 天然幂等：同一天重复跑只更新时间戳与价格。
- `source` 列是**防线**：任何读这张表的代码都必须显式声明口径；门禁路径（评分/派发闸）**禁止**读它（用测试钉死，见 §6）。
- 行宽约 60~80 B。**池内 1164 品 × 365 天 ≈ 42.5 万行 ≈ 25~35 MB**，可忽略；此后 +1164 行/天。

**新增 `SnapshotStore` 方法**（遵守 V0.3.1 全局锁铁律）：

| 方法 | 说明 | 连接 |
|---|---|---|
| `upsert_kline_daily(rows)` | `executemany` + `ON CONFLICT(good_id,d) DO UPDATE`，一次短事务 | `with self._lock, self._connect()` |
| `get_kline_daily(good_id, days=365)` | 升序返回 dict 列表 | `_connect_ro()`（WAL 只读，不占写锁） |
| `async_get_kline_daily(...)` | `asyncio.to_thread` 包装 | 同上 |
| `kline_coverage()` | 覆盖品数 / 最新日期（给运维与测试看） | `_connect_ro()` |
| `prune_kline_daily(keep_days=400)` | 删超期行 | 短事务 |

---

## 2. 取数任务（P0）

**落点**：`bot/anomaly/runner.py` 新增第 9 个任务 `_kline_backfill_loop()`（现有 8 个任务不动，日志计数相应变成 9 —— 记得同步 SKILL 与重启核对串）。

**范围**：`watch_pool`（B/A/S 三层池，现 1164 品）∪ `watches`（用户关注品）∪ 当日 `/分析` 访问过的品（顺手记一个内存集合）。`market_hash_name` 直接取 `watch_pool.market_hash_name`（**已存在，无需额外接口**）。

**节奏**：
- 每天在 `kline.daily_hour`（默认 04:00，避开 03:10 的结果结算）之后首次 tick 触发一轮；
- 单轮最多 `per_run_limit`（默认 300）品，请求间隔 `sleep_ms`（默认 400ms）→ 1164 品约 **2 轮 / 8 分钟**，全天内摊平；
- 跳过条件：当天已有行 **且** `fetched_at` 距今 < `min_interval_hours`（默认 20h）；
- 复用 `SteamDTClient` 既有**限流/退避**：命中 4005 时按族退避并**整轮退出**（不要把配额烧光，影响 `/分析` 现取路径）；
- 失败不重试超过 2 次，逐品跳过并计数。

**日志**（一行一轮，便于运维核对）：
```
[KLINE] 日K落库 完成 300 品(新增 1164 行,跳过 41,失败 2,耗时 138.2s,库内最新 2026-09-28)
```

**配置**（`config.yaml` + `config.example.yaml` + `bot/config.py::KlineConfig`）：

```yaml
kline:
  enable: true
  scope: pool            # pool | pool+watch(默认 pool+watch)
  daily_hour: 4
  per_run_limit: 300
  sleep_ms: 400
  min_interval_hours: 20
  keep_days: 400
```

---

## 3. 消费方接线（P0/P1）

### 3.1 `/分析`：日K 优先读本地（P0）
`bot/qqbot/bot.py::_analysis_reply` 中 `get_item_kline` 之前插入：
```python
local = await self._store.async_get_kline_daily(gid, days=365)
if len(local) >= 60 and _kline_is_fresh(local, hours=36):
    steam_points = [{"timestamp": ..., "open":..., "close":..., "high":..., "low":...} for r in local]
    kline_src = "本地"
else:
    kline = await self._steamdt.get_item_kline(steam_name, periods=2)   # 现状不变
    kline_src = "实时"
```
- 数据不足或过期 → **回退现状**（现取），不报错、不降级；
- 正文【中期结构】尾部追加一行来源标注：`日K来源: 本地库(截至 09-28) / 实时(SteamDT)`——**口径透明**是本项目的既有原则（同"回退口径"标注）。

### 3.2 派发 30 日区间：**门禁不动**（P0，重要）
- `_ensure_price_ranges` / `get_price_range_map` **继续只用我们自己的落库日收盘**（绝对阈值必须同源）；
  - V0.3.4 起该序列的**候选口径是 BUFF+悠悠合成价**（`total_sell_price`，见 `bot/csqaq/market_counts.py`），
    但仍**不得**与 SteamDT 日K混算 —— 三者是三套口径：BUFF、BUFF+悠悠合成、SteamDT(实测中位差 -6.2%)。
  - 合成价的启用条件（V0.3.5 起）：**窗口内全部为 `total_sell_price_scope='buff_yyyp'` 的点**，
    且"当前价"必须同时切换到同一口径（否则就是拿 A 口径的现价比 B 口径的区间）。
- 本地日K只用于**展示**（例如【中期结构】里已有"近20日区间位置"，那就是同一序列内的相对量 ✓）与**回测工具**；
- 若要在缺 BUFF 区间时给新品"参考位置"，必须写成独立字段并标注 `SteamDT口径(与BUFF中位差 -6.2%)`，**不得进入 `detect_phase` 的高位判定**。

### 3.3 长周期结果评估（P2，脚本先行）
新增 `scripts/eval_signals_long.py`（只读）：用 `item_kline_daily` 为每个 `anomaly_events` 行计算 **T+30d / T+90d** 收益，并**把已出池/退市的品一起算**（这就是去幸存者偏差的关键）；同时输出**相对基准**列（同期同池中位），修掉现有 `format_summary` 缺基准列的问题。

---

## 4. 验证工具（P1，本项目的核心目的）

`backtest_buy_points_365d.py`（只读，替代现在的 10 日版代理）：
- 数据：`item_kline_daily` 全量（365 天 × 上千品）；
- 复算 `/分析` 真实口径：**20/60 日均线、30 日区间位置、30 日跌幅**，以及左右侧买点规则；
- **按区间分层**：用 CSQAQ 饰品指数或全池中位涨跌把样本切成"上行/震荡/下行"，**每层单独出** T+7 超额与胜率（现在的结论只有单一 27 天下行区间）；
- 输出与 09-24 同格式的表（`n / T+7中位 / 超额中位 / 超额胜率`），可直接对比"10 日代理 vs 真实 20/60 日口径"。

**改规则的判据（先写死，避免事后找理由）**：
- 左侧"低位+深跌"若在**上行与下行两层都是负超额** → 改规则（砍掉或改成"深跌 + 确认项≥2"必备）；
- 右侧"站上均线+新高"若在**上行层显著正超额** → 保留并上调权重；下行层负 → 加"趋势过滤"（仅在指数不下行时给）；
- 任何在分层里**样本 <100** 的结论一律标注"未观察到"，不得写进文案。

---

## 5. 分阶段实施与工作量

| 阶段 | 内容 | 交付 | 预估 |
|---|---|---|---|
| **P0** | 建表 + 4 个 store 方法 + `_kline_backfill_loop` + `KlineConfig` + `/分析` 本地优先 | 可运行、每天自动落库 | 0.5 天 |
| **P1** | `backtest_buy_points_365d.py` + 分层报告 → 决定买点规则去留 | 一份分区间结论 | 0.5 天 |
| **P2** | `eval_signals_long.py`（T+30/T+90 + 基准列 + 去幸存者偏差） | 长周期评估 | 0.5 天 |

**测试（P0 必带）**：新增 `test_kline_store.py`
1. 同日重复 upsert → 仍 1 行（幂等）；
2. `get_kline_daily` 升序 + `days` 边界；
3. **文件库下批量读走 `_connect_ro()`**（沿用 `test_range_cache_v031` 的写法，防回归到全局锁）；
4. `prune_kline_daily` 只删超期行；
5. 配置加载（默认值 + YAML 覆盖 + `enable: false`）；
6. **门禁不读日K表**：静态断言 `engine.py` 的区间查询 SQL 里不出现 `item_kline_daily`（口径防线的机器化）。

**上线核验（沿用项目铁律）**：无控制台重启 → `已启动 N 个任务`（8→9）→ 首轮 `[KLINE] 日K落库 完成 …` → `/分析` 日志出现 `日K来源: 本地库` → **0 Traceback**；`价格区间已更新` 耗时不得恶化（V0.3.1 的回归线）。

---

## 6. 风险与回滚

| 风险 | 影响 | 缓解 |
|---|---|---|
| **口径混算**（把 SteamDT 价格当 BUFF 用） | 派发/买点整类信号翻面 | `source` 列 + §3.2 门禁不读 + 测试第 6 条钉死 |
| **日期错位**（SteamDT 的"当日"与我们快照的"当日"可能差几小时） | 跨源对齐时错配一天 | 存他们原始日期 `d`；跨源比较只用同日或 ±1 天容差；文档写明 |
| 配额被烧（kline 族虽独立但会 4005） | `/分析` 现取失败 | 单轮上限 + 间隔 + 命中 4005 整轮退出 + 复用既有族退避 |
| 库增长 | 磁盘 | 只落池内品（42.5 万行/年 ≈ 30MB）+ `prune_kline_daily(keep_days=400)` |
| 任务变多导致日志噪声 | 排查困难 | 每轮一行汇总日志；不逐品打日志 |
| **回滚** | — | `kline.enable: false` → 任务空转；`DROP TABLE item_kline_daily` 不影响任何既有路径（除新增的显示/回测） |

---

## 7. 验收标准（可量化）

1. **覆盖**：池内品 24h 内落库成功率 **≥95%**；`kline_coverage()` 报告品数与最新日期；
2. **可用**：任取 20 个池内品，`/分析` 的 20/60 日线与"实时取数"结果**一致**（同源校验，允许末根差异）；
3. **不劣化**：`价格区间已更新` 耗时与 V0.3.1 基线同量级；`/分析` 响应不被拖慢（本地读 <50ms）；
4. **产出结论**：`backtest_buy_points_365d.py` 给出**分区间**的买点规则结论，样本阈值按 §4 执行；
5. **闭环**：长周期评估能对 `distribute` 的"0.31→0~6 条/天"给出是否误报的判断。
