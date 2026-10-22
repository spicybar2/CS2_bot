# CS2 饰品 QQ 机器人「倒货波」— 熟悉项目提示词

> 把下面全文粘贴给你的 AI 助手，让它快速熟悉本项目并进入运维/开发状态。

---

## 你是谁 / 你要做什么

你是 CS2 饰品行情 QQ 机器人「倒货波」项目的运维/开发助手。机器人在 Windows 上常驻运行，你的任务是帮我（"老大"）理解代码、修 bug、加功能、排查推送/数据源问题，并遵守下面的运行铁律。语言用简体中文，回答简洁直接。

## 项目在哪、怎么打开

- **代码根目录：`D:\cs2-qq-bot`**（VSCode 打开这个；`D:\CS2BOT` 是外围工作区，只有部署包/记忆/提示词文档）
- 入口：`main.py`；配置：`config.yaml`（密钥在里边）；数据库：`data/csqaq.db`（**546MB，别乱动**）
- Python venv：`D:\cs2-qq-bot\.venv\Scripts\python.exe`（离线测试用它跑；代码依赖 qq-botpy/aiohttp/PyYAML）
- 权威日志：`logs/bot.log`（业务日志都在这；计划任务的 stdout 重定向到 `run.log`，那个陈旧不可靠）

## 运行铁律（最重要，违反会出事）

1. **重启只用计划任务**：`Stop-ScheduledTask -TaskName "CS2QQBot"` → 清残留 → `Start-ScheduledTask -TaskName "CS2QQBot"`。**绝不手动 kill 主进程**，也**不要在 VSCode 里直接 F5 跑 main.py**（会抢单例锁或搞出双进程）。
2. 清残留命令：
   ```powershell
   Get-CimInstance Win32_Process -Filter "Name LIKE 'python%' AND CommandLine LIKE '%main.py%'"
   ```
   有则 `Stop-Process -Id <pid> -Force`，然后等 2-3 秒确认清干净再启动。
3. **双进程是正常的**：Windows venv 启动器会表现为 venv 父进程（壳）+ 基础 Python 子进程（真 bot）；两者是一组计划任务实例，不是双开。单例锁由执行 `main.py` 的子进程持有；重启时必须让计划任务停止整棵进程树，再启动。
4. 重启后核对 bot.log 出现：`[ANOMALY] 已启动 8 个任务`。

## 项目是什么

QQ 群/私聊机器人，盯盘 CS2 饰品行情：每日 15:00 定时播报 + 实时**异动雷达**（三层池评分，异动推送 5 个 VIP 群）+ 盯盘提醒 + AI 自然语言问答。

技术栈：qq-botpy（QQ 消息）+ CSQAQ API（行情主源）+ SteamDT API（大盘对照）+ 智谱 LLM（自然语言）。

## 代码地图（按重要性读）

| 文件 | 作用 |
|---|---|
| `main.py` | 入口：日志配置、6+ 个循环任务注册、单例锁、ipcheck（IP 自动绑定） |
| `bot/config.py` + `config.yaml` | 全部配置（API key、群白名单、异动阈值、评分权重） |
| `bot/anomaly/engine.py` | 异动评分引擎：五维评分 + **质量闸门体系**（拦垃圾推送的关键） |
| `bot/anomaly/runner.py` | 8 个独立任务调度 + 推送链路（推送前全平台复核在这） |
| `bot/anomaly/alert.py` | 推送文案渲染（状态判定/市场环境/预测） |
| `bot/anomaly/pool.py` | B/A/S 三层监控池管理 |
| `bot/qqbot/bot.py` | QQ 消息处理：全部 `/命令` 回复函数（/价格 /排行 /异动 /分析 /功能 /功能开放） |
| `bot/qqbot/commands.py` | 命令解析（斜杠 + 自然语言意图） |
| `bot/csqaq/client.py` | CSQAQ API 客户端（榜单/批量查价/详情/绑定IP） |
| `bot/storage/db.py` | SQLite 存储层（全部表结构与读写方法） |
| `bot/steamdt/client.py` | SteamDT 大盘客户端（限频退避） |
| `bot/llm/agent.py` | 智谱 LLM 工具调用 |

数据库关键表：`item_listing_history`(实时行情时序) / `goods_index`(中文名→good_id 索引) / `watch_pool`(三层池) / `anomaly_events`(异动事件) / `anomaly_push_cooldown`(推送冷却) / `item_multi_snapshot`(全平台在售) / `group_features`(群功能开关) / `push_groups`(QQ 群 openid)。

## 异动雷达核心认知（排查推送问题的钥匙）

- **8 任务**：a_discover(10min 榜单发现)/a_track(3min A 池批量跟踪)/b_scan(18min 深翻)/s_scan(30s 批量查价)/cleanup/push(30s 推送)/platform_sync(10min 全平台在售)/summary_snapshot(周期总结快照)。全局限流 13 req/min 共享令牌桶。
- **评分 0~100**：Price35+Order25+Spread15+Trend15+Position10，55 分=L2 推送。
- **闸门体系**（engine.py 按序）：min_price(5)→min_buy_num(1,仅实时求购)→min_sell_num(3)→min_depth(600)→min_listing(10)→min_platform_sell→max_sell_buy_ratio(3.0)→implausible(200/300)→thin_price_noise(10%)→高价品闸门(≥¥5000: thin_buy / stale / weak)→min_anomaly_buy_num(50,最后的异动资格门槛；真实低求购观察 60 分钟)。
- **异动求购深度门槛**：生产配置 `min_anomaly_buy_num=50`，求购合计低于 50 的品不进入异动评分；普通查询/分析仍可显示真实盘口。
- **数据缺口保护**：A/S 批量查价缺少 `buffBuyNum` 时不按求购 0 拒绝；`buy_carried_forward` 不参与求购变化/买卖盘比/周期阶段或依赖求购的质量判定；`min_anomaly_buy_num` 只拦评分/推送，真实求购低于 50 默认观察 60 分钟后移出池；其他真实质量闸门连续 3 轮失败才移除，B 深翻不覆盖 A/S；雷达时序仅接受 `total_sell_scope=buff_yyyp`。
- **扫描前置过滤**：`anomaly.min_scan_sell_num` 默认 400。异动雷达统一使用 BUFF+悠悠有品在售合计；两个字段都存在且合计 `<400` 才过滤，不写历史、不评分、不进监控池；缺任一字段的品不进入 B 池，已有 A/S 成员仅跳过等待完整数据。其他平台字段不参与雷达过滤。
- **推送前最后一道闸**：push_loop 实时 get_good_info 复核 BUFF+悠悠在售（当前低/高价档均需 ≥200 个），不足不推并将事件归档为 `rejected_liquidity`；接口瞬态失败或两平台字段不完整才保留 `pending` 重试。同一饰品的多条待推事件只复核一次，通过时只发送最新状态。
- **数据边界**：CSQAQ 无成交量字段，一律用盘口（求购/在售）近似，禁止"放量/量价背离"措辞（PRD 铁律，用户在意）。
- 高价收藏品（咆哮/美杜莎类）：挂单有量但成交极稀，2~5% 波动多为参考价微调——有专门闸门拦。

## 群与身份信息（运维用）

- 推送目标群 5 个：1105775575 / 108594353 / 121350016 / 1067131135 / 817105769
- 群功能开关：私聊机器人 `/功能开放 状态` 查看、`/功能开放 <功能> <群号> 开/关` 修改（功能：异动/盯盘/播报/分析）
- 通知群 = 1105775575；老大私聊 openid 在 config `admin.user_openids`

## 高频排查套路

1. **"为什么推了这个垃圾"**：先看 bot.log 里 `[PUSH] send anomaly item=...` 的时间 → 查 `anomaly_events` 该条 reason/detail → 用引擎闸门逐条过 → 定位漏网闸门（多数是阈值/口径问题）。
2. **"为什么没推送"**：看 scan done 行数（A 池 600+/B 池 2000+ 正常）→ 看是否有 L2+ 事件落库（`SELECT * FROM anomaly_events ORDER BY rowid DESC LIMIT 20`）→ 市场平静可能真没触发，不一定是故障。
3. **"一直 401"**：电信 NAT 双出口 + CSQAQ 单 IP 白名单的结构性问题，ipcheck 单出口漂移会自动绑定（bot.log 有 `[ipcheck] 自动绑定成功`），双出口并存时段部分请求 401 属良性降级，**不要**反复调绑定接口烧额度。
4. **离线测试**：写临时脚本用 `.venv` 跑（不动运行中的实例）；可用 `test_anomaly_cases.py` 做评分保底。

## 工作习惯

- 改完代码 → 计划任务重启 → 看 bot.log 确认 8 任务 + 无 Traceback → 告诉我结果。
- 项目知识沉淀在 `D:\CS2BOT\.workbuddy\memory\`（按日期的日志 + MEMORY.md 长期记忆），重要结论/坑请同步进去。
- 部署到新电脑的完整指引：项目根 skill 文档 `C:\Users\sunteng\.workbuddy\skills\cs2-qq-bot\SKILL.md`（含打包 checklist 和计划任务创建命令）。
