#!/bin/bash
#
# setup.sh — WeChat Gateway Pool Manager 一键部署脚本
#
# 架构 v2：
# - 每个微信用户对应一个独立 Linux 用户（文件系统隔离）
# - API key 通过 LLM Proxy（内嵌在 pool manager 中）管理
# - 所有 LLM 请求转发到 proxy → 注入 key → 发到真实 API
# - wx 用户 config.yaml 的 base_url 指向 localhost:8765/v1
# - 不设工具集限制（全权限放开）
#
# 用法:
#   bash setup.sh                          # 默认 100 个槽位
#   bash setup.sh --total 30 --hot-pool 3  # 自定义
#   bash setup.sh --help                   # 看全部参数
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
      echo "  --max-bound NUM 最大同时运行 gateway 数（默认 80）"
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
echo "   WeChat Gateway Pool Manager v2 — 部署"
echo "══════════════════════════════════════════════════"
echo ""
echo "配置:"
echo "  total_profiles: $TOTAL"
echo "  hot_pool_size:  $HOT_POOL"
echo "  max_bound:      $MAX_BOUND"
echo "  port:           $PORT"
echo "  prefix:         $PREFIX"
echo ""

# ── 检查 Hermes ──────────────────────────────────────────────────────────
if ! command -v hermes &>/dev/null; then
  echo "[1/6] 📦 Hermes 未找到，开始安装..."
  curl -fsSL https://raw.githubusercontent.com/NousResearch/hermes-agent/main/scripts/install.sh | bash
else
  echo "[1/6] 📦 Hermes 已安装"
fi

# ── 安装 Python 依赖 ─────────────────────────────────────────────────────
echo "[2/6] 📦 安装 Python 依赖..."
HERMES_VENV="$HOME/.hermes/hermes-agent/venv"
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

install_deps() {
  if [ -f "$HERMES_VENV/bin/pip" ]; then
    echo "  使用 Hermes venv pip..."
    "$HERMES_VENV/bin/pip" install -r "$PROJECT_DIR/requirements.txt" -q && return 0
  fi
  if command -v uv &>/dev/null; then
    if [ -d "$HERMES_VENV" ]; then
      echo "  使用 uv (venv)..."
      VIRTUAL_ENV="$HERMES_VENV" uv pip install -r "$PROJECT_DIR/requirements.txt" -q && return 0
    fi
    echo "  使用 uv (--system)..."
    uv pip install --system -r "$PROJECT_DIR/requirements.txt" -q && return 0
  fi
  if command -v pip3 &>/dev/null; then
    echo "  使用 pip3..."
    pip3 install -r "$PROJECT_DIR/requirements.txt" -q && return 0
  fi
  echo "  ⚠️ 未找到 pip/uv，请手动安装: pip install -r $PROJECT_DIR/requirements.txt"
  return 1
}

if [ -f "$PROJECT_DIR/requirements.txt" ]; then
  install_deps
fi

# ── 创建目录结构 + 配置 ──────────────────────────────────────────────────
echo "[3/6] 📁 创建目录结构..."
mkdir -p "$HOME/.hermes/wechat-pool/logs"
mkdir -p "$HOME/.hermes/wechat-pool/pool_manager"
mkdir -p "$HOME/.hermes/wechat-pool/static"

# 复制代码
cp -r "$PROJECT_DIR/pool_manager/"*".py" "$HOME/.hermes/wechat-pool/pool_manager/"
cp -r "$PROJECT_DIR/static/"* "$HOME/.hermes/wechat-pool/static/" 2>/dev/null || true

# 生成配置文件
cat > "$HOME/.hermes/wechat-pool/config.yaml" << CONFIGEOF
# WeChat Gateway Pool Manager v2 Configuration
# API key 通过 proxy 管理，不写入用户文件

pool:
  total_profiles: $TOTAL
  hot_pool_size: $HOT_POOL
  max_bound_gateways: $MAX_BOUND
  profile_prefix: "$PREFIX"
  max_qr_refresh: 3

gateway:
  idle_timeout_minutes: 1440
  restart_on_crash: true
  max_restart_attempts: 3
  restart_delay_seconds: 10
  health_check_interval: 60

frontend:
  api_port: $PORT
  qr_refresh_seconds: 45
  host: "$HOST"
  public_url: ""

logging:
  level: INFO
  dir: "$HOME/.hermes/wechat-pool/logs/"
  retain_days: 7
  stats_window_hours: 168

ilink:
  base_url: "https://ilinkai.weixin.qq.com"
  qr_timeout_seconds: 480
  qr_poll_interval: 1
CONFIGEOF
echo "  目录结构 + 配置文件创建完成"

# ── 复制并安装 systemd 服务 ─────────────────────────────────────────────
echo "[4/6] ⚙️ 部署 systemd 服务..."
SYSTEMD_DIR="$HOME/.config/systemd/user"
mkdir -p "$SYSTEMD_DIR"

# Pool Manager 以 user service 运行（需要读 dosh 的 auth.json）
cat > "$SYSTEMD_DIR/hermes-pool.service" << SERVICEEOF
[Unit]
Description=Hermes WeChat Gateway Pool Manager v2
After=network-online.target
Wants=network-online.target
StartLimitIntervalSec=0

[Service]
Type=simple
ExecStart=%h/.hermes/hermes-agent/venv/bin/python \
  -c "import sys; sys.path.insert(0, '%h/.hermes/wechat-pool/'); from pool_manager.service import main; main()" \
  --config %h/.hermes/wechat-pool/config.yaml
WorkingDirectory=%h/.hermes/wechat-pool/
Environment="PATH=%h/.hermes/hermes-agent/venv/bin:%h/.hermes/node/bin:%h/.local/bin:%h/.cargo/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
Environment="VIRTUAL_ENV=%h/.hermes/hermes-agent/venv"
Restart=on-failure
RestartSec=10
KillMode=mixed
KillSignal=SIGTERM
TimeoutStopSec=90
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=default.target
SERVICEEOF

# Gateway 服务模板（每个 Linux 用户一个系统级服务）
# 需要 root 权限写入 /etc/systemd/system/
if [ -f "$PROJECT_DIR/systemd/hermes-gateway@.service" ]; then
  sudo cp "$PROJECT_DIR/systemd/hermes-gateway@.service" /etc/systemd/system/
  sudo systemctl daemon-reload
fi
echo "  systemd 配置已加载"

# ── 创建 Linux 用户 ──────────────────────────────────────────────────────
echo "[5/6] 👤 创建 Linux 用户..."
python3 "$PROJECT_DIR/scripts/create_profiles.py" \
  --count "$TOTAL" --prefix "wx"

# ── 添加 sudoers 规则 ────────────────────────────────────────────────────
echo "  配置 sudoers..."
SUDOERS_FILE="/etc/sudoers.d/hermes-pool"
sudo tee "$SUDOERS_FILE" > /dev/null << SUDOERSEOF
# Hermes Pool Manager — passwordless sudo for gateway management
dosh ALL=(ALL) NOPASSWD: /usr/bin/systemctl
dosh ALL=(ALL) NOPASSWD: /usr/sbin/useradd
dosh ALL=(ALL) NOPASSWD: /usr/sbin/userdel
dosh ALL=(ALL) NOPASSWD: /usr/bin/mkdir
dosh ALL=(ALL) NOPASSWD: /usr/bin/chown
dosh ALL=(ALL) NOPASSWD: /usr/bin/chmod
dosh ALL=(ALL) NOPASSWD: /usr/bin/cp
dosh ALL=(ALL) NOPASSWD: /usr/bin/rm
dosh ALL=(ALL) NOPASSWD: /usr/bin/cat
dosh ALL=(ALL) NOPASSWD: /usr/bin/ln
dosh ALL=(ALL) NOPASSWD: /usr/bin/id
SUDOERSEOF
sudo chmod 440 "$SUDOERS_FILE"
echo "  sudoers 已配置"

# ── 启动 Pool Manager ────────────────────────────────────────────────────
echo "[6/6] 🚀 启动 Pool Manager..."

systemctl --user enable hermes-pool 2>&1 || echo "  ⚠️ enable 失败"

if command -v loginctl &>/dev/null; then
  if loginctl show-user "$USER" 2>/dev/null | grep -q "Linger=no"; then
    echo "  🔄 启用 linger..."
    sudo loginctl enable-linger "$USER" 2>/dev/null || echo "  ⚠️ 需要手动: sudo loginctl enable-linger $USER"
  fi
fi

systemctl --user restart hermes-pool

echo ""
echo "══════════════════════════════════════════════════"
echo "   ✅ 部署完成！"
echo ""
echo "   Pool Manager:  http://$HOST:$PORT"
echo "   LLM Proxy:     http://$HOST:$PORT/v1 (自动注入 API key)"
echo "   前端页面:      http://$HOST:$PORT"
echo "   健康检查:      http://$HOST:$PORT/health"
echo "   自启动:        ✅ 已设置"
echo ""
echo "   管理命令:"
echo "     systemctl --user status hermes-pool     # 查看状态"
echo "     journalctl --user -u hermes-pool -f     # 查看日志"
echo "     curl -X POST http://$HOST:$PORT/api/v1/pool/sync-models  # 同步模型"
echo ""
echo "   ⚠️ 首次使用前需配置凭证池:"
echo "     hermes auth add deepseek --api-key \"sk-xxx-1\""
echo "     hermes auth add deepseek --api-key \"sk-xxx-2\""
echo "     # ... 添加所有 25 个 key"
echo ""
echo "   ⚠️ Gateway 模板未配置时，需手动安装:"
echo "     sudo cp systemd/hermes-gateway@.service /etc/systemd/system/"
echo "     sudo systemctl daemon-reload"
echo "══════════════════════════════════════════════════"
