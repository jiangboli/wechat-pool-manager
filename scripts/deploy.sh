#!/bin/bash
#
# deploy.sh — 一键部署 WeChat Gateway Pool Manager（从 GitHub）
#
# 用法:
#   bash <(curl -fsSL https://raw.githubusercontent.com/jiangboli/wechat-pool-manager/main/scripts/deploy.sh)
#   或:
#   curl -fsSL https://raw.githubusercontent.com/jiangboli/wechat-pool-manager/main/scripts/deploy.sh | bash
#
# 支持参数:
#   bash deploy.sh --total 50 --hot-pool 3
#

set -euo pipefail

REPO_URL="https://github.com/jiangboli/wechat-pool-manager.git"
TEMP_DIR=$(mktemp -d)

echo "=== WeChat Gateway Pool Manager 一键部署 ==="
echo ""

# 检查 Git
if ! command -v git &>/dev/null; then
  echo "? 安装 git..."
  sudo apt-get update -qq && sudo apt-get install -y -qq git
fi

# 克隆仓库
echo "? 下载项目..."
git clone --depth 1 "$REPO_URL" "$TEMP_DIR" 2>/dev/null

# 执行 setup.sh
cd "$TEMP_DIR"
bash scripts/setup.sh "$@"

# 清理
rm -rf "$TEMP_DIR"