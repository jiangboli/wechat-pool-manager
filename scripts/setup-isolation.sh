#!/bin/bash
# ============================================================
# Per-Linux-User Isolation Setup Script
# 为 WeChat Gateway Pool 设置 Linux 用户隔离基础设施
# 用法: bash setup-isolation.sh
# ============================================================
set -euo pipefail

echo "=== Per-Linux-User 隔离基础设施部署 ==="
echo ""

# ── 1. Systemd 服务模板 ────────────────────────────────────
SERVICE_FILE="/etc/systemd/system/hermes-gateway@.service"
echo "[1/5] 创建 systemd 服务模板: $SERVICE_FILE"

sudo tee "$SERVICE_FILE" > /dev/null << 'UNITEOF'
[Unit]
Description=Hermes Gateway for %i
After=network.target

[Service]
User=%i
WorkingDirectory=/home/%i/.hermes
ExecStart=/opt/hermes/venv/bin/python -m hermes_cli.main gateway run --replace
Environment=HERMES_HOME=/home/%i/.hermes
ReadWritePaths=/home/%i/.hermes
NoNewPrivileges=true
PrivateTmp=true
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
UNITEOF

echo "  ✅ 已创建"

# ── 2. Sudoers 配置 ─────────────────────────────────────────
SUDOERS_FILE="/etc/sudoers.d/hermes-pool"
echo "[2/5] 配置 passwordless sudo: $SUDOERS_FILE"

sudo tee "$SUDOERS_FILE" > /dev/null << 'SUDOEOF'
# Hermes Pool Manager — 允许启动/停止 per-Linux-user gateway 服务
dosh ALL=(ALL) NOPASSWD: /bin/systemctl
dosh ALL=(ALL) NOPASSWD: /usr/sbin/useradd
dosh ALL=(ALL) NOPASSWD: /usr/sbin/userdel
dosh ALL=(ALL) NOPASSWD: /bin/mkdir
dosh ALL=(ALL) NOPASSWD: /bin/chown
dosh ALL=(ALL) NOPASSWD: /bin/chmod
dosh ALL=(ALL) NOPASSWD: /bin/cp
dosh ALL=(ALL) NOPASSWD: /bin/rm
dosh ALL=(ALL) NOPASSWD: /bin/cat
SUDOEOF

sudo chmod 440 "$SUDOERS_FILE"
echo "  ✅ 已创建（权限 440）"

# ── 3. 验证 sudoers 语法 ────────────────────────────────────
echo "[3/5] 验证 sudoers 语法..."
sudo visudo -c -f "$SUDOERS_FILE"
echo "  ✅ 语法正确"

# ── 4. 启用模板服务 + daemon-reload ────────────────────────
echo "[4/5] 启用 systemd 模板 + daemon-reload"
sudo systemctl daemon-reload
sudo systemctl enable hermes-gateway@
echo "  ✅ 模板已启用"

# ── 5. 验证配置 ─────────────────────────────────────────────
echo "[5/5] 验证配置"
echo ""
echo "  ◆ systemd 模板:"
ls -la "$SERVICE_FILE"
echo ""
echo "  ◆ sudoers:"
sudo cat "$SUDOERS_FILE"
echo ""
echo "  ◆ systemctl daemon-reload:"
sudo systemctl is-enabled hermes-gateway@ 2>&1
echo ""
echo "  ◆ /opt/hermes/venv 存在:"
ls -la /opt/hermes/venv/bin/python

echo ""
echo "=== ✅ 基础设施部署完成 ==="
echo ""
echo "后续步骤:"
echo "  1. 将更新后的代码复制到 ~/.hermes/wechat-pool/"
echo "  2. 重启 hermes-pool 服务"
echo "  3. 验证新绑定流程创建 Linux 用户 + 启动系统服务"