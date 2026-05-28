#!/bin/bash
#
# setup.sh — WeChat Gateway Pool Manager 一键部署脚本（Docker 版 v3）
#
# 架构 v3（Docker 容器化）：
# - 每个微信用户一个 Docker 容器
# - Pool Manager 也在 Docker 中运行
# - LLM Proxy 内嵌在 Pool Manager 中，负责负载均衡
# - 数据目录: /home/data/{尾数}/{profile}/.hermes/
#
# 用法:
#   bash setup.sh                                 # 默认 100 个槽位
#   bash setup.sh --total 30 --hot-pool 3
#   bash setup.sh --help
#

set -euo pipefail

# ── 默认值 ──────────────────────────────────────────────────────────────
TOTAL=100
HOT_POOL=5
MAX_BOUND=80
PORT=8765
PREFIX="weixin-"
HOST="0.0.0.0"

# ── 参数解析 ──────────────────────────────────────────────────────────────
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
      echo "  --total NUM    总槽位数（默认 100）"
      echo "  --hot-pool NUM 热池常驻槽位数（默认 5）"
      echo "  --max-bound NUM 最大同时运行容器数（默认 80）"
      echo "  --port NUM     API 端口（默认 8765）"
      echo "  --prefix STR   命名前缀（默认 weixin-）"
      echo "  --host STR     监听地址（默认 0.0.0.0）"
      echo ""
      echo "示例:"
      echo "  bash setup.sh --total 30 --hot-pool 3"
      exit 0
      ;;
    *) echo "未知参数: $1"; exit 1 ;;
  esac
done

echo "══════════════════════════════════════════════════"
echo "   WeChat Gateway Pool Manager v3 (Docker)"
echo "══════════════════════════════════════════════════"
echo ""
echo "配置:"
echo "  total_profiles: $TOTAL"
echo "  hot_pool_size:  $HOT_POOL"
echo "  max_bound:      $MAX_BOUND"
echo "  port:           $PORT"
echo "  prefix:         $PREFIX"
echo ""

# ── 检查 Docker ──────────────────────────────────────────────────────────
echo "[1/5] 🐳 检查 Docker..."
if ! command -v docker &>/dev/null; then
  echo "  Docker 未安装！请先安装 Docker:"
  echo "  curl -fsSL https://get.docker.com | bash"
  exit 1
fi
echo "  Docker $(docker --version)"

# ── 复制配置文件 ──────────────────────────────────────────────────────────
echo "[2/5] 📁 准备配置文件..."
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# 创建数据目录基础结构
DATA_ROOT="$HOME/data"
if [ ! -d "$DATA_ROOT" ]; then
  echo "  创建 $DATA_ROOT 目录..."
  mkdir -p "$DATA_ROOT"
fi

# 生成 config.yaml（Docker 版）
cat > "$PROJECT_DIR/config.yaml" << CONFIGEOF
# WeChat Gateway Pool Manager v3 — Docker 模式
pool:
  total_profiles: $TOTAL
  hot_pool_size: $HOT_POOL
  max_bound_gateways: $MAX_BOUND
  profile_prefix: "$PREFIX"
  max_qr_refresh: 3

docker:
  image: "hermes-bot:latest"
  network: "hermes-pool-net"
  data_root: "/home/data/"
  max_containers: $MAX_BOUND
  container_defaults:
    memory_limit: "256m"
    memory_reservation: "128m"
    cpu_shares: 256
    cpu_quota: 50000

proxy:
  default_provider: "deepseek"
  fallback_providers: ["alibaba"]
  circuit_breaker:
    max_errors: 5
    recovery_window: 60

frontend:
  api_port: $PORT
  qr_refresh_seconds: 45
  host: "$HOST"
  public_url: ""

logging:
  level: INFO
  dir: "/app/logs/"
  retain_days: 7
  stats_window_hours: 168

ilink:
  base_url: "https://ilinkai.weixin.qq.com"
  qr_timeout_seconds: 480
  qr_poll_interval: 1
CONFIGEOF
echo "  config.yaml 已生成"

# ── 构建 Docker 镜像 ──────────────────────────────────────────────────────
echo "[3/5] 🔨 构建 Docker 镜像..."

echo "  构建 hermes-bot（微信用户容器镜像）..."
docker build -f "$PROJECT_DIR/Dockerfile.bot" -t hermes-bot:latest "$PROJECT_DIR" 2>&1 | tail -3

echo "  构建 pool-manager（管理服务镜像）..."
docker build -f "$PROJECT_DIR/Dockerfile.pool" -t pool-manager:latest "$PROJECT_DIR" 2>&1 | tail -3

# ── 创建 Docker 网络 ──────────────────────────────────────────────────────
echo "[4/5] 🌐 创建 Docker 网络..."
docker network create hermes-pool-net 2>/dev/null || echo "  网络已存在"

# ── 启动 Pool Manager ──────────────────────────────────────────────────────
echo "[5/5] 🚀 启动 Pool Manager (Docker)..."

# 停止旧容器（如果存在）
docker stop pool-manager 2>/dev/null || true
docker rm pool-manager 2>/dev/null || true

# 启动新的 pool-manager
docker run -d \
  --name pool-manager \
  --network hermes-pool-net \
  -p "$HOST:$PORT:8765" \
  -v /var/run/docker.sock:/var/run/docker.sock:ro \
  -v "$HOME/data:/home/data" \
  -v "$PROJECT_DIR/config.yaml:/app/config.yaml:ro" \
  --restart unless-stopped \
  pool-manager:latest

echo ""
echo "══════════════════════════════════════════════════"
echo "   ✅ 部署完成！"
echo ""
echo "   Pool Manager:  http://$HOST:$PORT"
echo "   LLM Proxy:     http://$HOST:$PORT/v1"
echo "   前端页面:      http://$HOST:$PORT"
echo "   健康检查:      http://$HOST:$PORT/health"
echo ""
echo "   管理命令:"
echo "     docker logs -f pool-manager          # 查看管理日志"
echo "     docker ps --filter label=managed_by=pool-manager  # 查看用户容器"
echo "     curl http://$HOST:$PORT/health       # 健康检查"
echo ""
echo "   ⚠️ 首次使用前需配置 LLM API Key:"
echo "     curl -X POST http://$HOST:$PORT/api/v1/proxy/keys \\"
echo "       -H 'Content-Type: application/json' \\"
echo "       -d '{\"provider\":\"deepseek\",\"key\":\"sk-xxx-1\",\"label\":\"key-1\"}'"
echo ""
echo "   更多管理:"
echo "     curl http://$HOST:$PORT/api/v1/proxy/status    # 查看 proxy 状态"
echo "     curl http://$HOST:$PORT/api/v1/pool/stats      # 池统计"
echo "══════════════════════════════════════════════════"
