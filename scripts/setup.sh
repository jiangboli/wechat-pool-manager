#!/bin/bash
#
# setup.sh — WeChat Gateway Pool Manager 一键部署脚本（Docker 版 v4）
#
# 架构 v4（3 容器安全隔离）：
# - pool-bind:   绑定页，对外公开（8765）
# - pool-admin:  管理接口，仅 127.0.0.1 + Token 认证（8766）
# - pool-proxy:  LLM Proxy，仅 /v1/chat/completions，bot 容器用（8767）
# - hermes-bot:  每个微信用户一个 Docker 容器
#
# 用法:
#   bash setup.sh                                    # 默认 100 槽位
#   bash setup.sh --total 30 --hot-pool 3
#   bash setup.sh --admin-token my-secret-token
#   bash setup.sh --help
#

set -euo pipefail

# ── 默认值 ──────────────────────────────────────────────────────────────
TOTAL=500
HOT_POOL=5
MAX_BOUND=300
PORT=8765
PREFIX="weixin-"
HOST="0.0.0.0"
ADMIN_TOKEN=""
PG_DSN=""
MACHINE_IP=""

# ── 参数解析 ──────────────────────────────────────────────────────────────
while [[ $# -gt 0 ]]; do
  case "$1" in
    --total)       TOTAL="$2";      shift 2 ;;
    --hot-pool)    HOT_POOL="$2";   shift 2 ;;
    --max-bound)   MAX_BOUND="$2";  shift 2 ;;
    --port)        PORT="$2";       shift 2 ;;
    --prefix)      PREFIX="$2";     shift 2 ;;
    --host)        HOST="$2";       shift 2 ;;
    --admin-token) ADMIN_TOKEN="$2"; shift 2 ;;
    --pg-dsn)     PG_DSN="$2";       shift 2 ;;
    --machine-ip) MACHINE_IP="$2";   shift 2 ;;
    --help|-h)
      echo "用法: bash setup.sh [选项]"
      echo ""
      echo "选项:"
      echo "  --total NUM      总槽位数（默认 100）"
      echo "  --hot-pool NUM   热池常驻槽位数（默认 5）"
      echo "  --max-bound NUM  最大同时运行容器数（默认 80）"
      echo "  --port NUM       API 端口（默认 8765）"
      echo "  --prefix STR     命名前缀（默认 weixin-）"
      echo "  --host STR       监听地址（默认 0.0.0.0）"
      echo "  --admin-token STR 管理接口 Token（不传则自动生成随机 Token）"
      echo "  --pg-dsn STR    PG 连接 DSN (postgresql+asyncpg://user:pass@host:port/db)"
      echo "  --machine-ip STR 本机外网 IP（默认自动检测）"
      echo ""
      echo "示例:"
      echo "  bash setup.sh --total 30 --hot-pool 3"
      echo "  bash setup.sh --admin-token my-secret-token --pg-dsn \"postgresql+asyncpg://...\""
      exit 0
      ;;
    *) echo "未知参数: $1"; exit 1 ;;
  esac
done

echo "══════════════════════════════════════════════════"
echo "   WeChat Gateway Pool Manager v4 (3 容器)"
echo "══════════════════════════════════════════════════"
echo ""
echo "配置:"
echo "  total_profiles: $TOTAL"
echo "  hot_pool_size:  $HOT_POOL"
echo "  max_bound:      $MAX_BOUND"
echo "  port:           $PORT"
echo "  prefix:         $PREFIX"
echo "  host:           $HOST"
echo ""

# ── 自动检测 MACHINE_IP ─────────────────────────────────────────────
if [ -z "$MACHINE_IP" ]; then
  MACHINE_IP=$(hostname -I | awk '{print $1}' 2>/dev/null || echo "")
  if [ -z "$MACHINE_IP" ]; then
    MACHINE_IP="auto"
  fi
fi
echo "  machine_ip:     $MACHINE_IP"

# ── 检查 Docker ──────────────────────────────────────────────────────────
echo "[1/6] 🐳 检查 Docker..."
if ! command -v docker &>/dev/null; then
  echo "  Docker 未安装！请先安装 Docker:"
  echo "  curl -fsSL https://get.docker.com | bash"
  exit 1
fi
echo "  Docker $(docker --version)"

# ── Docker 存储迁移（containerd 到大分区） ─────────────────────────────────
echo "[1a/6] 💾 检查 Docker 存储分区..."
DOCKER_ROOT=$(docker info 2>/dev/null | grep "Docker Root Dir" | awk '{print $NF}')
echo "  当前 Docker Root Dir: $DOCKER_ROOT"

# 检测 /home 分区（可能是大容量独立分区）
HOME_DEV=$(df --output=source /home 2>/dev/null | tail -1)
ROOT_DEV=$(df --output=source / 2>/dev/null | tail -1)

if [ "$HOME_DEV" != "$ROOT_DEV" ] && [ -d "/home" ]; then
  echo "  ✅ 检测到 /home 是独立分区（$HOME_DEV），空间充裕"

  # 1. 配置 Docker data-root 到 /home
  if echo "$DOCKER_ROOT" | grep -q "^/home"; then
    echo "  ✅ Docker Root Dir 已在 /home 分区"
  else
    echo "  ⚠️  Docker Root Dir 在根分区，迁到 /home..."
    DOCKER_CFG="/etc/docker/daemon.json"
    sudo mkdir -p /etc/docker
    echo "{\"data-root\": \"/home/dosh/docker-data\"}" | sudo tee "$DOCKER_CFG" > /dev/null
    echo "  ✅ Docker data-root 配置完成"
  fi

  # 2. 配置 containerd root 到 /home
  CONTAINERD_TARGET="/home/dosh/containerd-data"
  CTRD_ROOT=""
  if [ -f /etc/containerd/config.toml ]; then
    CTRD_ROOT=$(grep "^root " /etc/containerd/config.toml | tr -d ' "' | cut -d= -f2)
  fi

  if [ "$CTRD_ROOT" = "$CONTAINERD_TARGET" ]; then
    echo "  ✅ containerd root 已经在 $CONTAINERD_TARGET"
  else
    echo "  ⚠️  配置 containerd root → $CONTAINERD_TARGET..."
    sudo mkdir -p /etc/containerd "$CONTAINERD_TARGET"

    if command -v containerd &>/dev/null; then
      containerd config default | sudo tee /etc/containerd/config.toml > /dev/null
    fi

    if [ -f /etc/containerd/config.toml ]; then
      sudo sed -i "s|root = .*|root = \"$CONTAINERD_TARGET\"|" /etc/containerd/config.toml
    else
      echo "version = 3
root = \"$CONTAINERD_TARGET\"
state = '/run/containerd'" | sudo tee /etc/containerd/config.toml > /dev/null
    fi
    echo "  ✅ containerd 配置完成"
  fi

  # 3. 迁移现有数据（第一次部署时旧路径可能已有数据）
  if [ -d "/var/lib/containerd" ] && [ ! -L "/var/lib/containerd" ]; then
    CTRD_SIZE=$(sudo du -sm /var/lib/containerd/ 2>/dev/null | cut -f1)
    if [ -n "$CTRD_SIZE" ] && [ "$CTRD_SIZE" -gt 0 ] 2>/dev/null; then
      echo "  ⚠️  发现旧 containerd 数据（${CTRD_SIZE}MB），迁移中..."
      sudo rsync -aHAXS /var/lib/containerd/ "$CONTAINERD_TARGET/"
      echo "  ✅ 数据已迁移到 $CONTAINERD_TARGET"

      echo "  重启 Docker 服务以加载新配置..."
      sudo systemctl stop docker
      sudo systemctl stop containerd
      sleep 2
      sudo systemctl start containerd
      sudo systemctl start docker
      sleep 3
      echo "  ✅ Docker 重启完成"

      sudo mv /var/lib/containerd /var/lib/containerd.bak
      echo "  ✅ 旧数据已备份为 /var/lib/containerd.bak（确认正常后可删除）"
    fi
  fi
else
  echo "  ℹ️  /home 与 / 在同一个分区（或 /home 不存在），跳过存储迁移"
  echo "     如需迁移，请手动配大容量外挂盘到 /home 后再运行 setup.sh"
fi
echo ""
if ! command -v docker &>/dev/null; then
  echo "  Docker 未安装！请先安装 Docker:"
  echo "  curl -fsSL https://get.docker.com | bash"
  exit 1
fi
echo "  Docker $(docker --version)"

# ── 检查 Docker Compose ──────────────────────────────────────────────────
if ! docker compose version &>/dev/null; then
  echo "  Docker Compose 不可用，请安装 Docker Compose v2+"
  exit 1
fi
echo "  Docker Compose $(docker compose version)"

# ── 准备配置和目录 ──────────────────────────────────────────────────────────
echo "[2/7] 📁 准备配置文件和目录..."
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# 创建数据目录基础结构
DATA_ROOT="$HOME/data"
if [ ! -d "$DATA_ROOT" ]; then
  echo "  创建 $DATA_ROOT 目录..."
  mkdir -p "$DATA_ROOT"
fi

# 生成 config.yaml（Docker 版）
cat > "$PROJECT_DIR/config.yaml" << CONFIGEOF
# WeChat Gateway Pool Manager v4 — 3 容器模式
pool:
  total_profiles: $TOTAL
  hot_pool_size: $HOT_POOL
  max_bound_gateways: $MAX_BOUND
  profile_prefix: "$PREFIX"
  max_qr_refresh: 3

docker:
  image: "hermes-bot:latest"
  network: "hermes-pool-net"
  data_root: "$DATA_ROOT/"
  data_root_host: "$DATA_ROOT/"
  max_containers: $MAX_BOUND
  container_defaults:
    memory_limit: "2g"
    memory_reservation: "128m"
    cpu_shares: 256
    cpu_quota: 50000
    pids_limit: 1024
    ulimit_nofile: 2048

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

# ── 生成 ADMIN_TOKEN ─────────────────────────────────────────────────────
if [ -z "$ADMIN_TOKEN" ]; then
  ADMIN_TOKEN=$(openssl rand -hex 16 2>/dev/null || python3 -c "import secrets; print(secrets.token_hex(16))")
fi
echo "  Admin Token: $ADMIN_TOKEN"

# 创建 .env 文件供 docker-compose 使用
cat > "$PROJECT_DIR/.env" << ENVEOF
ADMIN_TOKEN=$ADMIN_TOKEN
HOME_DATA=$DATA_ROOT
CLAW_DO_DSN=$PG_DSN
MACHINE_IP=$MACHINE_IP
ENVEOF
echo "  .env 已生成 (包含 ADMIN_TOKEN + HOME_DATA + CLAW_DO_DSN + MACHINE_IP)"

# ── 构建 Docker 镜像 ──────────────────────────────────────────────────────
echo "[3/7] 🔨 构建 Docker 镜像..."

echo "  构建 hermes-bot（微信用户容器镜像）..."
docker build -f "$PROJECT_DIR/Dockerfile.bot" -t hermes-bot:latest "$PROJECT_DIR" 2>&1 | tail -3

echo "  构建 pool-manager（管理服务镜像）..."
docker build -f "$PROJECT_DIR/Dockerfile.pool" -t pool-manager:latest "$PROJECT_DIR" 2>&1 | tail -3

# ── 创建 Docker 网络 ──────────────────────────────────────────────────────
echo "[4/7] 🌐 创建 Docker 网络..."
docker network create hermes-pool-net 2>/dev/null || echo "  网络已存在"

# ── 启动 3 个管理容器 ──────────────────────────────────────────────────────
echo "[5/7] 🚀 启动 3 个管理容器..."

# 停止旧容器（兼容旧版单容器 + 新版 3 容器）
for c in pool-manager pool-bind pool-admin pool-proxy; do
  docker stop "$c" 2>/dev/null || true
  docker rm "$c" 2>/dev/null || true
done

# 通过 docker compose 启动 3 个服务
cd "$PROJECT_DIR"
docker compose up -d 2>&1
echo "  3 个容器已启动"

# ── 修复数据目录权限 ──────────────────────────────────────────────────────
echo "[6/7] 🔧 修复数据目录权限..."
sleep 2
sudo chown -R "$(whoami):$(whoami)" "$DATA_ROOT" 2>/dev/null || echo "  提示：数据目录文件归 root 所有，可手动运行: sudo chown -R $(whoami):$(whoami) $DATA_ROOT"
echo "  数据目录权限已修复"

# ── 保存 Token 到文件（方便查看）──
echo "$ADMIN_TOKEN" > "$DATA_ROOT/pool-manager/admin_token" 2>/dev/null || true

echo ""
echo "══════════════════════════════════════════════════"
echo "   ✅ 部署完成！3 容器已运行"
echo ""
echo "   服务端口:"
echo "     pool-bind:   http://$HOST:$PORT              # 绑定页（公开）"
echo "     pool-admin:  http://127.0.0.1:8766            # 管理接口（需 Token）"
echo "     pool-proxy:  http://$HOST:8767                # LLM 代理（Docker 内网）"
echo ""
echo "   管理 Token:  $ADMIN_TOKEN"
echo "   Token 文件:  $DATA_ROOT/pool-manager/admin_token"
echo ""
echo "   管理命令:"
echo "     docker logs -f pool-bind                     # 查看绑定页日志"
echo "     docker logs -f pool-proxy                     # 查看 proxy 日志"
echo "     docker ps --filter label=managed_by=pool-manager  # 查看用户容器"
echo ""
if [ -n "$PG_DSN" ]; then
  echo "   ✅ PG 中心存储已启用"
  echo "     DSN: ${PG_DSN%%@*}@${PG_DSN##*@}  # 隐藏密码"
else
  echo "   ⚠️  PG 未配置（不传 --pg-dsn 则使用 JSON 文件存储）"
  echo "     需要中心存储时加参重新部署:"
  echo "     bash setup.sh --pg-dsn \"postgresql+asyncpg://user:pass@host:5432/claw_do\""
fi
echo ""
echo "   ⚠️  管理接口需 Token（请求头 X-Admin-Token）:"
echo "     curl -H 'X-Admin-Token: $ADMIN_TOKEN' http://127.0.0.1:8766/api/v1/pool/stats"
echo "     curl -H 'X-Admin-Token: $ADMIN_TOKEN' http://127.0.0.1:8766/api/v1/proxy/status"
echo ""
echo "   ⚠️  首次使用前需配置 LLM API Key:"
echo "     curl -X POST http://$HOST:$PORT/api/v1/proxy/keys \\"
echo "       -H 'Content-Type: application/json' \\"
echo "       -d '{\"provider\":\"deepseek\",\"key\":\"sk-xxx-1\",\"label\":\"key-1\"}'"
echo ""
echo "   ⚠️  如更换服务器，通过 .env 自定义数据目录:"
echo "     echo 'HOME_DATA=/path/to/data' >> $PROJECT_DIR/.env"
echo "     docker compose up -d"
echo ""
echo "══════════════════════════════════════════════════"