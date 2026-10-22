# 项目管理说明（倒货波 · CS2 饰品行情机器人）

> 版本 V0.3.24 · 整理日期 2026-10-22 · 完整改动史见 `更新日志.md`

## 一、目录职责（谁该放在哪）

```
D:\cs2-qq-bot\
├─ main.py                 机器人唯一入口(调度器 + 异动雷达 + 盯盘循环)
├─ config.yaml             运行配置(不入库)；config.example.yaml 为模板
├─ bot/                    业务代码包
│  ├─ csqaq/               CSQAQ 接口(客户端/限流/价格/图谱/成交量)
│  ├─ anomaly/             异动雷达(引擎/三层池/调度器/预警/情绪)
│  ├─ qqbot/               QQ 侧(bot 主体/指令渲染/每日一言)
│  ├─ storage/             SQLite 存取(唯一数据出口)
│  ├─ analyzer/            白盒分析(买点/推荐/结果追踪)
│  ├─ steamdt/             SteamDT K 线
│  └─ llm/                 智谱 GLM 智能回复(可选)
├─ tests/                  离线自测(27 个文件,见第四节)
├─ tools/                  常驻排查工具(见第三节)
├─ scripts/                运维脚本(重启/看门狗/状态导入导出/整理脚本)
├─ docs/                   设计文档(市场规律、日K落库、项目提示词)
├─ deploy/                 云端部署模板(Dockerfile/systemd)
├─ data/                   运行时数据(SQLite 库、钉 IP 缓存、熔断状态)
├─ logs/                   运行日志(bot.log)
└─ archive/<日期>/         已退役的历史脚本与残留(可整体删除)
```

**放文件的规矩**

| 新东西 | 放哪 | 说明 |
|---|---|---|
| 业务代码 | `bot/` 下对应子包 | 不要塞进 `main.py` |
| 离线自测 | `tests/test_xxx_vNNNN.py` | 版本号后缀，必须可独立运行 |
| 一次性验证/取证脚本 | `tools/`（还要长期用）或 `archive/日期/`（用完即弃） | 见下文 |
| 运维入口 | `scripts/` + 根目录 `.bat` | `.bat` 只保留人工双击入口 |
| 设计/评估文档 | `docs/` | README 只留用法 |

## 二、日常入口

| 场景 | 命令 |
|---|---|
| 后台重启（唯一推荐） | `wscript.exe //B //Nologo D:\cs2-qq-bot\scripts\restart_bot_hidden.vbs` |
| 前台看日志调试 | 双击 `start_bot.bat`（控制台日志开启） |
| 只看日志 | 双击 `live_log.bat`（只读跟随 `logs\bot.log`） |
| 本机切换前导出/导入状态 | `stop_and_export.bat` / `import_and_start.bat` |
| 全套自测 | `.venv\Scripts\python.exe -X utf8 tests\run_all.py` |
| 单个自测 | `.venv\Scripts\python.exe -X utf8 tests\test_xxx.py` |

> ⚠️ 重启脚本会杀掉**所有**命令行里带 `cs2-qq-bot` 的 python 进程——跑测试期间别重启。

## 三、tools/ 常驻排查工具

| 工具 | 用途 |
|---|---|
| `probe_status.py` | 一键巡检：进程存活、日志计数、DB 规模、三层池件数 |
| `probe_sort_health.py` | 榜单排序可用性（那两个「近30天」排序挂死时用） |
| `probe_live_pin.py` | 钉住服务器 IP 的真机连通验证 |
| `probe_egress_ip_drift.py` | 出口 IP 漂移诊断（401 根因） |
| `probe_watch_price_fields.py` | `/关注` 价格字段核实（BUFF / 悠悠） |
| `probe_watch_render.py` | `/关注`、`/我的关注`、盯盘提醒 的文案渲染预览 |

**一次性脚本不进仓库根目录**：用到时现写，跑完要么进 `archive/日期/`，要么合并进 `tools/`。
判断标准：这周还会再跑吗？不会 → `archive/`。

## 四、测试

- 位置：`tests/`，全部独立进程运行，互不影响；每个文件开头有两行 bootstrap 把仓库根加进 `sys.path`。
- 入口：`tests/run_all.py`（支持关键字过滤：`run_all.py breaker`）。
- 约定：新功能必须带 `tests/test_xxx_vNNNN.py`，并在 `更新日志.md` 里写明项数。
- 隔离要求：**凡是"模块级全局 + 落盘"的特性，测试第一步就把路径指向临时目录**（V0.3.24 的教训：熔断状态曾把测试数据写进线上 `data/sort_breaker.json`）。

当前规模：27 个文件 / 约 700 项断言，全部通过。

## 五、清理与归档策略

- `scripts/cleanup_project.py`：**只移动不删除**，默认 dry-run。
  - `--apply` 会把历史脚本挪进 `archive/<日期>/`，保留 `tools/` 与文档。
  - 删除受限（本机有 safe-delete 批量保护），所以清理一律用"移动归档"。
- `archive/` 随时可整体删除，不影响运行；里面是历史探针、一次性校验脚本、旧日志、旧配置备份。
- 运行时垃圾（`__pycache__`、`*.pyc`、`botpy.log.*`）同样归入 `archive/<日期>/junk/`，需要时手动清。

## 六、已知待办（未获批准或未排期）

| 事项 | 现状 | 影响 |
|---|---|---|
| DB 保留策略 | `price_snapshots` 469 万行，DB 1837 MB，每天 +7~8 万行 | 磁盘持续增长 |
| 日志轮转 | 单个 `logs\bot.log` 已 110 MB，无 rotation | 归档/排查不便 |
| `/info/get_rank_list` 重试 6→3 | 老大要求先不改 | 挂死排序时多耗时间 |
| 5 条僵尸 `push_status='pending'` | 历史遗留 | 无实际推送影响 |

## 七、变更纪律

1. 改代码 → 升版本号 → `更新日志.md` 置顶新条目 → 同步 `SKILL.md` → 写当日记忆。
2. 重启只走 `scripts\restart_bot_hidden.vbs`，重启后核对：401=0、假漂移冷却=0、换钉=0、ERROR=0。
3. 每次改动都必须有自测；测试全绿再上线。

## 八、版本管理与在家部署

完整操作步骤见 **`docs/部署与同步.md`**，这里是速查。

| 场景 | 命令 |
|---|---|
| 本机改完上传 | `scripts\push.bat "说明"`（等于 add -A + commit + push） |
| 家里首次部署 | `git clone <仓库>` → `scripts\setup_home.bat` → 填 `config.yaml` → `scripts\register_tasks.bat` |
| 家里更新并重启 | `scripts\update_and_restart.bat`（pull --ff-only + 重启） |
| 迁移关注/推送群 | 旧机 `stop_and_export.bat` → `data\state_export.json` 拷到新机 → `import_and_start.bat` |

**收藏三条铁律**

1. `config.yaml`（token/密钥）**永不入库**；`data/`、`logs/`、`archive/`、`.venv/` 同样不入库。
2. **同一时间只让一台机器在线**：同 AppID 互相顶号、同 token 分限流、CSQAQ 还有 IP 白名单（换网络就 401）。
3. 运维脚本一律 **ASCII-only**（PowerShell 5.1 会把无 BOM 的 UTF-8 当 GBK 解析），且**只用相对路径**
   （`$PSScriptRoot` / `%~dp0` / `WScript.ScriptFullName`），否则克隆到别的目录就起不来。

当前状态：`main` 分支，首个提交 tag `v0.3.25`，102 个文件 / 1.27 MB；`.gitignore` 已挡住全部敏感与运行时产物。
