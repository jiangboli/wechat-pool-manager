#!/bin/bash
#
# setup.sh — WeChat Gateway Pool Manager 一键部署脚本
#
# 用法:
#   bash setup.sh                          # 默认配置（100 profiles）
#   bash setup.sh --total 50 --hot-pool 3  # 按机器配置自定义
#   bash setup.sh --help                   # 看全部参数
#

set -euo pipefail

# ── 默认配置 ──────────────────────────────────────────────────────────
TOTAL=100
HOT_POOL=5
MAX_BOUND=80
PORT=8765
PREFIX="weixin-"
HOST="0.0.0.0"

# ── 参数解析 ──────────────────────────────────────────────────────────
while [[ $# -gt 0 ]]; do
  case "$1" in
    --total)       TOTAL="$2";    shift 2 ;;
    --hot-pool)    HOT_POOL="$2"; shift 2 ;;
    --max-bound)   MAX_BOUND="$2"; shift 2 ;;
    --port)        PORT="$2";     shift 2 ;;
    --prefix)      PREFIX="$2";   shift 2 ;;
    --host)        HOST="$2";     shift 2 ;;
    --help|-h)
      echo "用法: bash setup.sh [选项]"
      echo ""
      echo "选项:"
      echo "  --total NUM    预创建 profile 总数（默认 100）"
      echo "  --hot-pool NUM 热池常驻槽位数（默认 5）"
      echo "  --max-bound NUM 最大同时运行 gateway 数（默认 80）"
      echo "  --port NUM     API 端口（默认 8765）"
      echo "  --prefix STR   profile 名前缀（默认 weixin-）"
      echo "  --host STR     监听地址（默认 0.0.0.0）"
      echo ""
      echo "示例:"
      echo "  bash setup.sh --total 30 --hot-pool 3 --max-bound 15"
      echo "  bash setup.sh --total 100 --hot-pool 5 --max-bound 80"
      exit 0
      ;;
    *)
      echo "未知参数: $1"
      echo "使用 --help 查看帮助"
      exit 1
      ;;
  esac
done

echo "══════════════════════════════════════════════════"
echo "   WeChat Gateway Pool Manager — 部署"
echo "══════════════════════════════════════════════════"
echo ""
echo "配置:"
echo "  total_profiles: $TOTAL"
echo "  hot_pool_size:  $HOT_POOL"
echo "  max_bound:      $MAX_BOUND"
echo "  port:           $PORT"
echo "  prefix:         $PREFIX"
echo ""

# ── 检查 Hermes ───────────────────────────────────────────────────────
if ! command -v hermes &>/dev/null; then
  echo "[1/6] ? Hermes 未找到，开始安装..."
  curl -fsSL https://raw.githubusercontent.com/NousResearch/hermes-agent/main/scripts/install.sh | bash
  echo "  安装完成"
else
  echo "[1/6] ? Hermes 已安装 ( $(hermes --version 2>/dev/null || echo "版本未知") )"
fi

# ── 安装 Python 依赖 ──────────────────────────────────────────────────
echo "[2/6] ? 安装 Python 依赖..."
HERMES_VENV="$HOME/.hermes/hermes-agent/venv"
if [ -f "$HERMES_VENV/bin/pip" ]; then
  PIP="$HERMES_VENV/bin/pip"
else
  PIP="pip3"
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

if [ -f "$PROJECT_DIR/requirements.txt" ]; then
  "$PIP" install -r "$PROJECT_DIR/requirements.txt" -q 2>&1 | tail -1
fi
echo "  依赖安装完成"

# ── 创建目录结构 ──────────────────────────────────────────────────────
echo "[3/6] ? 创建目录结构..."
mkdir -p "$HOME/.hermes/wechat-pool/logs"
mkdir -p "$HOME/.hermes/wechat-pool/profiles"

# 复制配置文件
if [ -f "$PROJECT_DIR/config.yaml" ]; then
  cp "$PROJECT_DIR/config.yaml" "$HOME/.hermes/wechat-pool/config.yaml"
  # 如果提供了 CLI 参数，更新配置文件
  if [ "$TOTAL" -ne 100 ] || [ "$HOT_POOL" -ne 5 ] || [ "$MAX_BOUND" -ne 80 ]; then
    echo "pool.total_profiles: $TOTAL" >> "$HOME/.hermes/wechat-pool/config.override"
    sed -i "s/total_profiles:.*/total_profiles: $TOTAL/" "$HOME/.hermes/wechat-pool/config.yaml" 2>/dev/null || true
  fi
fi
echo "  目录结构创建完成"

# ── 复制 systemd 配置 ─────────────────────────────────────────────────
echo "[4/6] ? 部署 systemd 服务..."
SYSTEMD_DIR="$HOME/.config/systemd/user"
mkdir -p "$SYSTEMD_DIR"

if [ -f "$PROJECT_DIR/systemd/hermes-gateway@.service" ]; then
  cp "$PROJECT_DIR/systemd/hermes-gateway@.service" "$SYSTEMD_DIR/"
  echo "  ? hermes-gateway@.service"
fi
if [ -f "$PROJECT_DIR/systemd/hermes-pool.service" ]; then
  cp "$PROJECT_DIR/systemd/hermes-pool.service" "$SYSTEMD_DIR/"
  echo "  ? hermes-pool.service"
fi

systemctl --user daemon-reload
echo "  systemd 配置已加载"

# ── 批量创建 profile ──────────────────────────────────────────────────
echo "[5/6] ? 创建 profile..."
python3 "$PROJECT_DIR/scripts/create_profiles.py" \
  --count "$TOTAL" --prefix "$PREFIX" 2>&1

# ── 启动 Pool Manager ─────────────────────────────────────────────────
echo "[6/6] ? 启动 Pool Manager..."
# 复制 pool_manager 代码到目标位置
PM_DIR="$HOME/.hermes/wechat-pool/pool_manager"
mkdir -p "$PM_DIR"
cp -r "$PROJECT_DIR/pool_manager/"* "$PM_DIR/"

# 复制前端页面
STATIC_DIR="$HOME/.hermes/wechat-pool/static"
mkdir -p "$STATIC_DIR"
cp -r "$PROJECT_DIR/static/"* "$STATIC_DIR/"

# 通过 systemd 启动
systemctl --user enable hermes-pool 2>/dev/null || true
systemctl --user start hermes-pool

echo ""
echo "══════════════════════════════════════════════════"
echo "   ✅ 部署完成！"
echo ""
echo "   Pool Manager 运行在: http://$HOST:$PORT"
echo "   前端页面:          http://$HOST:$PORT"
echo "   健康检查:          http://$HOST:$PORT/health"
echo ""
echo "   查看日志:"
echo "     journalctl --user -u hermes-pool -f"
echo "     tail -f $HOME/.hermes/wechat-pool/logs/pool_manager.log"
echo "══════════════════════════════════════════════════"