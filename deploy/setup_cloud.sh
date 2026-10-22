#!/usr/bin/env bash
# ============================================================
# CS2 QQ 机器人 云端一键部署脚本 (Ubuntu 22.04 / Python 3.10)
#
# 用法:
#   bash setup_cloud.sh <Git仓库URL> [部署目录]
# 示例:
#   bash setup_cloud.sh git@github.com:yourname/cs2-qq-bot.git
#   bash setup_cloud.sh https://gitee.com/yourname/cs2-qq-bot.git /opt/cs2-qq-bot
#
# 默认部署目录: /opt/cs2-qq-bot
# 步骤: 装依赖 -> 拉代码 -> venv -> 检查config -> systemd 服务 -> 启动
# ============================================================
set -e

REPO_URL="${1:?用法: bash setup_cloud.sh <Git仓库URL> [部署目录]}"
DEPLOY_DIR="${2:-/opt/cs2-qq-bot}"

echo "==> [1/6] 安装系统依赖 (python3-venv git curl)"
sudo apt-get update -y
sudo apt-get install -y python3-venv git curl

echo "==> [2/6] 拉取代码到 $DEPLOY_DIR"
sudo mkdir -p "$DEPLOY_DIR"
sudo chown "$USER":"$USER" "$DEPLOY_DIR"
if [ -d "$DEPLOY_DIR/.git" ]; then
  cd "$DEPLOY_DIR"
  git pull
else
  git clone "$REPO_URL" "$DEPLOY_DIR"
fi
cd "$DEPLOY_DIR"

echo "==> [3/6] 创建 venv 并安装依赖"
python3 -m venv .venv
./.venv/bin/pip install -r requirements.txt

echo "==> [4/6] 检查 config.yaml (含密钥, 不入 Git)"
if [ ! -f config.yaml ]; then
  echo "!! 未找到 config.yaml !!"
  echo "!! 在本地电脑执行:  scp config.yaml <user>@<服务器IP>:$DEPLOY_DIR/config.yaml"
  echo "!! 或服务器上手动编辑: sudo nano $DEPLOY_DIR/config.yaml"
fi

echo "==> [5/6] 安装 systemd 服务并设置开机自启"
sudo cp deploy/service/cs2qqbot.service /etc/systemd/system/cs2qqbot.service
sudo sed -i "s|/opt/cs2-qq-bot|$DEPLOY_DIR|g" /etc/systemd/system/cs2qqbot.service
sudo systemctl daemon-reload
sudo systemctl enable cs2qqbot

echo "==> [6/6] 启动服务"
sudo systemctl restart cs2qqbot
sleep 3
sudo systemctl status cs2qqbot --no-pager | head -15

echo ""
echo "完成! 后续常用命令:"
echo "  查看日志:   journalctl -u cs2qqbot -f"
echo "  重启服务:   sudo systemctl restart cs2qqbot"
echo "  更新代码:   cd $DEPLOY_DIR && git pull && sudo systemctl restart cs2qqbot"
echo "  验证 CSQAQ: $DEPLOY_DIR/.venv/bin/python scripts/fetch_once.py --pages 1 --page-size 20"
echo "  重建索引:   $DEPLOY_DIR/.venv/bin/python scripts/build_goods_index.py"
