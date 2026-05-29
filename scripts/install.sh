#!/bin/bash
#
# install.sh — WeChat Gateway Pool Manager 一键安装脚本
#
# 用法:
#   curl -fsSL https://raw.githubusercontent.com/jiangboli/wechat-pool-manager/main/scripts/install.sh | bash
#   curl -fsSL .../install.sh | bash -s -- --total 500 --hot-pool 5
#   DEEPSEEK_KEY=sk-xxx curl -fsSL .../install.sh | bash
#
# 先决条件:
#   - Linux 系统 (Ubuntu/Debian 推荐)
#   - sudo 权限（用于安装 Docker）
#
# 效果:
#   1. 自动安装 Docker + Docker Compose
#   2. 下载项目代码到 ~/wechat-pool-manager/
#   3. 运行 setup.sh 构建镜像并启动 3 个管理容器
#   4. 如未检测到 auth.json 且传入 DEEPSEEK_KEY 环境变量，自动初始化 API Key
#

set -euo pipefail

GITHUB_REPO="jiangboli/wechat-pool-manager"
PROJECT_DIR="$HOME/wechat-pool-manager"

echo "══════════════════════════════════════════════════"
echo "   WeChat Gateway Pool Manager — 一键安装"
echo "   仓库: $GITHUB_REPO"
echo "══════════════════════════════════════════════════"
echo ""

# ── Step 1: 检查 sudo ────────────────────────────────────────────────────
if ! command -v sudo &>/dev/null; then
  echo "❌ 需要 sudo 权限来安装 Docker。"
  echo "   请使用有 sudo 权限的用户运行。"
  echo "   或手动安装 Docker 后重新运行。"
  exit 1
fi

# ── Step 2: 安装 Docker（如未安装）────────────────────────────────────────
echo "[1/3] 🐳 检查 Docker..."
if ! command -v docker &>/dev/null; then
  echo "  Docker 未安装，正在自动安装..."
  curl -fsSL https://get.docker.com | sudo bash 2>&1 | tail -2
  sudo usermod -aG docker "$(whoami)" 2>/dev/null || true
  echo "  ✅ Docker 已安装"
else
  echo "  ✅ Docker $(docker --version 2>/dev/null || echo '已安装')"
fi

if ! docker compose version &>/dev/null; then
  echo "  安装 Docker Compose 插件..."
  sudo apt-get update -qq && sudo apt-get install -y -qq docker-compose-plugin 2>&1 | tail -1
fi

# ── Step 3: 下载项目代码 ──────────────────────────────────────────────────
echo "[2/3] 📦 下载项目代码..."
if [ -d "$PROJECT_DIR" ]; then
  echo "  项目目录已存在 ($PROJECT_DIR)，跳过下载。"
  echo "  如需重新下载，请先删除该目录。"
else
  echo "  从 GitHub 下载..."
  mkdir -p "$PROJECT_DIR"
  cd /tmp
  curl -sL "https://github.com/$GITHUB_REPO/archive/refs/heads/main.tar.gz" | tar xz -C "$PROJECT_DIR" --strip-components=1
  echo "  ✅ 代码已下载到 $PROJECT_DIR"
fi

# ── Step 4: 运行 setup.sh ────────────────────────────────────────────────
echo "[3/3] 🚀 配置并启动..."
cd "$PROJECT_DIR"
exec bash scripts/setup.sh --prompt-keys "$@"
