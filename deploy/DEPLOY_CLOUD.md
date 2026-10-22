# CS2 机器人云端部署与双机维护手册（方案 C）

> 目标架构：机器人在**云服务器**上 7x24 运行，公司/家里两台电脑只做维护终端。
> 数据（csqaq.db）、密钥（config.yaml）、CSQAQ 白名单全部只在云端一份，两端之间**无需同步数据**。

---

## 一、一次性准备

### 1. 申请云服务器（二选一）

| 渠道 | 配置 | 说明 |
|---|---|---|
| WorkBuddy × 腾讯云 Lighthouse 活动 | 轻量 2核2G4M，免费 1 个月 | 2026-08-13 ~ 11-13，每日 0 点限量 400 台，在 WorkBuddy 内激活腾讯轻量云专家领取 |
| 腾讯云免费中心（cloud.tencent.com/act/free） | 轻量/CVM 2核2G3M 40G 盘，免费 1 个月 | 个人实名新用户；到期续费最低 3.5 折 |

**创建实例时选镜像：Ubuntu 22.04**（自带 Python 3.10，省内存）。记住公网 IP、root/初始密码（或配置 SSH 密钥）。

### 2. 建 Git 私有仓库并推送代码（两端 + 云端共用）

在任一平台建私有仓库（GitHub / Gitee / 腾讯云 Coding 均可），然后在本机：

```bash
cd D:\cs2-qq-bot
git init
git add .
git commit -m "init: CS2 QQ bot"
git remote add origin <你的仓库URL>
git push -u origin main
```

`.gitignore` 已排除 `config.yaml`、`data/`、`.venv/`、`logs/`、`botpy.log`——**密钥和历史数据不会上库**。

---

## 二、云端部署（约 10 分钟）

SSH 登录服务器后执行：

```bash
bash setup_cloud.sh <你的Git仓库URL>
# 例: bash setup_cloud.sh git@github.com:yourname/cs2-qq-bot.git
```

脚本会自动：装依赖 → clone 代码到 /opt/cs2-qq-bot → 建 venv → 装依赖 → 注册 systemd 服务（开机自启 + 崩溃自动重启）→ 启动。

### 关键手工步骤（脚本无法代做）

**① 上传 config.yaml（含密钥，不走 Git）**

在本机执行（或用任意 SFTP 工具）：

```bash
scp D:\cs2-qq-bot\config.yaml <user>@<服务器IP>:/opt/cs2-qq-bot/config.yaml
```

**② CSQAQ 白名单绑定云端 IP**

服务器上执行一次，若报 401 按提示 IP 到 csqaq.com 绑定：

```bash
cd /opt/cs2-qq-bot && ./.venv/bin/python scripts/fetch_once.py --pages 1 --page-size 20
```

**③ 重建商品索引（首次约 4-5 分钟）**

```bash
cd /opt/cs2-qq-bot && ./.venv/bin/python scripts/build_goods_index.py
```

**④ 验证启动成功**

```bash
journalctl -u cs2qqbot -f        # 看到「机器人「倒货波」启动成功！」即成功
systemctl status cs2qqbot        # active (running)
```

### 停掉本机旧实例（防双实例顶号）

云端确认运行后，在本机执行：

```powershell
Stop-Process -Name python -Force
```

---

## 三、双机日常维护（公司 / 家里，流程相同）

### 改代码

```bash
# 任意一台电脑
git pull && 修改 && git commit -m "..." && git push

# 云端
ssh <user>@<IP>
cd /opt/cs2-qq-bot && git pull && sudo systemctl restart cs2qqbot
```

> 进阶（可选）：云端挂 webhook 或 systemd 定时 `git pull`，push 后免手动重启。

### 排障（顺序不变：日志 → CSQAQ → 白名单/权限）

```bash
ssh <user>@<IP>
journalctl -u cs2qqbot -n 50          # 看日志（替代本机 botpy.log）
cd /opt/cs2-qq-bot
./.venv/bin/python scripts/fetch_once.py --pages 1 --page-size 20   # 测 CSQAQ
```

### 常用管理命令

| 操作 | 命令 |
|---|---|
| 重启 | `sudo systemctl restart cs2qqbot` |
| 开机自启管理 | `sudo systemctl enable/disable cs2qqbot` |
| 盯盘数据 | `./.venv/bin/python -c "import sqlite3;db=sqlite3.connect('data/csqaq.db');print(*db.execute('SELECT id,zh_name,base_price,target_price,threshold FROM watchlist'),sep='\n')"` |

---

## 四、注意事项

1. **绝不在本机再启动机器人**：云端是唯一实例，本机只保留代码用于开发（本机跑会双实例顶号 + 数据分叉）。
2. **免费到期**：1 个月免费期结束前在控制台续费（约 40-60 元/月），避免停机；续费后 IP 不变，白名单无需重绑。
3. **安全**：云服务器安全组只放行 22 端口（SSH）；不要放行 3306/6379 等无关端口；config.yaml 权限 `sudo chmod 600`。
4. **数据备份（可选）**：每周用 `scp` 把 `/opt/cs2-qq-bot/data/csqaq.db` 拉回本地一份，防云端磁盘故障。

---

## 五、本机遗留工具（B 方案产物，转 C 后仅作备份）

- `scripts/export_state.py` / `scripts/import_state.py`：轮换模式的状态同步工具，转 C 后不再需要，保留不删。
- `stop_and_export.bat` / `import_and_start.bat`：同上。
