# WeChat Gateway Pool Manager

为多个微信用户提供独立的 Hermes Gateway 实例的池管理器。

每个用户通过扫码绑定自己的微信 bot，获得一个专属的 Hermes 助手。

## 架构

```
Pool Manager (FastAPI, :8765)
├── 热池: 保持 3-5 个 QR 扫码槽位
├── 健康检查: 每 60s 检查所有 gateway
├── Gateway 管理: 通过 systemd 控制每个 instance
├── 前端页面: 扫码绑定
└── 告警: 通过飞书通知管理员

每个 bound profile
  → systemd service (hermes-gateway@weixin-001)
  → 独立 HERMES_HOME + WeChat iLink bot
  → 独立会话/记忆/技能
```

## 快速开始

```bash
# 1. 配置（按机器内存调整）
bash scripts/setup.sh --total 50 --hot-pool 3 --max-bound 20

# 2. 打开前端页面
open http://<your-ip>:8765

# 3. 用户扫码绑定 → 自动完成
```

> 服务会自动设置为**随系统开机启动**。需要 `sudo loginctl enable-linger $USER` 权限（首次部署时脚本会自动尝试）。

## 参数参考

| 参数 | 说明 | 默认 | 4GB 建议 | 8GB 建议 | 16GB 建议 |
|------|------|------|----------|----------|-----------|
| `--total` | profile 总数 | 100 | 30 | 60 | 100 |
| `--hot-pool` | 热池大小 | 5 | 3 | 4 | 5 |
| `--max-bound` | 最大运行实例 | 80 | 15 | 40 | 80 |

## 目录结构

```
~/.hermes/wechat-pool/
├── config.yaml              # 配置
├── pool_manager/            # Python 源码（部署时复制）
├── static/                  # 前端页面
├── logs/                    # 日志
└── profiles/                # profile 引用（可选）

~/.hermes/profiles/          # Hermes profiles
├── weixin-001/              # 每个用户一个
├── weixin-002/
└── ...
```

## 管理命令

```bash
# 查看所有 gateway 状态
curl http://localhost:8765/api/v1/gateways | jq

# 池统计
curl http://localhost:8765/api/v1/pool/stats | jq

# 查看某个 gateway 日志
journalctl --user -u hermes-gateway@weixin-001 -f

# 重启 Pool Manager
systemctl --user restart hermes-pool
```

## 移植到新机器

```bash
# 1. 新机器装 Hermes
curl -fsSL https://raw.githubusercontent.com/NousResearch/hermes-agent/main/scripts/install.sh | bash

# 2. 克隆项目
git clone https://github.com/jiangboli/wechat-pool-manager.git
cd wechat-pool-manager

# 3. 按机器配置部署
bash scripts/setup.sh --total 30 --hot-pool 3 --max-bound 15
```