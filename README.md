# WeChat Gateway Pool Manager v4 (Docker 3-容器隔离)

为多个微信用户提供独立的 Hermes Gateway 实例的池管理器。

每个用户通过扫码绑定自己的微信 bot，获得一个专属的 Docker 容器。

## 一键安装（推荐）

```bash
# 最简安装（自动装 Docker + 下载代码 + 启动服务）
curl -fsSL https://raw.githubusercontent.com/jiangboli/wechat-pool-manager/main/scripts/install.sh | bash

# 指定参数 + 传入 API Key
DEEPSEEK_KEY=sk-xxx curl -fsSL .../install.sh | bash -s -- --total 500 --hot-pool 5

# 带 PG 持久化
curl -fsSL .../install.sh | bash -s -- --pg-dsn "postgresql+asyncpg://user:pass@host:5432/db"
```

## 架构（3 容器安全隔离）

```
WeChat iLink / 用户浏览器
        │
        ▼
┌──────────────────────────────────────────────────┐
│ pool-bind  (公开 8765)                           │
│ 绑定页 + 热池 + Docker 调度 + 前端                │
│ PG 持久化 + 心跳                                 │
└─────────────┬─────────────────────┬──────────────┘
              │ (扫码确认后)         │ (LLM 请求路由)
              ▼                     ▼
┌──────────────────────┐  ┌──────────────────────┐
│ pool-admin (127.0.0.1│  │ pool-proxy (公开 8767)│
│  :8766 + Token 认证)  │  │ OpenAI 兼容代理       │
│                       │  │ 负载均衡/熔断/多Provider│
│ 管理接口:             │  │ Bot 容器 -> 走此代理   │
│  - 池状态             │  └──────────────────────┘
│  - Proxy Key 管理     │
│  - 容器管理            │
└──────────────────────┘

每个 bound profile
  → Docker 容器 (hermes-weixin-001 / 002 / ...)
  → 独立 ~/.hermes/ (数据分散到 10 个目录)
  → LLM 请求经过 pool-proxy (:8767)
  → API key 只在 pool manager 内存中
```

## 手动部署

```bash
# 前提：服务器已安装 Docker
git clone https://github.com/jiangboli/wechat-pool-manager.git
cd wechat-pool-manager

# 基本部署
bash scripts/setup.sh --total 100 --hot-pool 5

# 完整参数
bash scripts/setup.sh \
  --total 100 \
  --hot-pool 5 \
  --max-bound 80 \
  --admin-token my-secret-token \
  --pg-dsn "postgresql+asyncpg://user:pass@host:5432/db" \
  --machine-ip 118.122.92.55

# 参数说明:
#   --total       总槽位数（默认 100）
#   --hot-pool    热池常驻槽位数（默认 5）
#   --max-bound   最大同时运行容器数（默认 80）
#   --admin-token 管理接口 Token（不传则自动生成随机 Token）
#   --pg-dsn      PostgreSQL 连接 DSN
#   --machine-ip  本机外网 IP（不传则自动检测）
```

## 绑定流程（两步）

1. **填写信息页** — 用户打开绑定页面，填写手机号、龙虾名、用户名
2. **扫码页** — 提交信息后显示 iLink 二维码，微信扫码完成绑定

## 服务端口

| 服务 | 端口 | 说明 |
|------|------|------|
| **pool-bind** | `8765` | 绑定页、热池轮询、公开访问 |
| **pool-admin** | `127.0.0.1:8766` | 管理接口、需 Token 认证 |
| **pool-proxy** | `8767` | LLM Proxy，仅 `/v1/chat/completions` |

## 添加 API Key

```bash
# 绑定页端口（公开）
curl -X POST http://localhost:8765/api/v1/proxy/keys \
  -d '{"provider":"deepseek","key":"sk-xxx","label":"主key"}'

# 查看状态
curl http://localhost:8765/api/v1/proxy/status

# 删除 Key
curl -X DELETE http://localhost:8765/api/v1/proxy/keys/{key_id}
```

## 管理命令

```bash
# 查看容器日志
docker logs -f pool-bind          # 绑定页
docker logs -f pool-admin         # 管理端
docker logs -f pool-proxy         # LLM Proxy

# 查看所有微信容器
docker ps --filter label=managed_by=pool-manager

# 查看某个用户容器日志
docker logs hermes-weixin-001

# 健康检查
curl http://localhost:8765/health

# 管理 API（需 Token）
curl -H 'X-Admin-Token: <token>' http://127.0.0.1:8766/api/v1/pool/stats
curl -H 'X-Admin-Token: <token>' http://127.0.0.1:8766/api/v1/proxy/status
```

## PostgreSQL 持久化（可选）

```bash
# 部署时传入 --pg-dsn
bash setup.sh --pg-dsn "postgresql+asyncpg://user:password@host:5432/claw_do"

# 表结构自动创建（幂等）
# - machines:     机器注册
# - bindings:     绑定信息（含 phone, lobster_name, user_name）
# - docker_containers: 容器状态
# - qr_history:   二维码历史
```

PG 不可用时自动降级为 JSON 文件存储，服务不中断。

## 数据目录

```
$HOME/data/                          # 可通过 .env HOME_DATA 自定义
├── pool-manager/
│   ├── admin_token                  # 管理 Token
│   └── pool_state.json              # 池状态持久化
├── 0/wx010/.hermes/                 # 按末位分散
├── 1/wx001/.hermes/
├── ...
└── 9/wx009/.hermes/
```

## 环境变量

```bash
# .env 文件（setup.sh 自动生成，也可手动编辑）
ADMIN_TOKEN=my-secret-token
HOME_DATA=/home/dosh/data            # 数据目录
CLAW_DO_DSN=postgresql+asyncpg://... # PG 连接
MACHINE_IP=118.122.92.55             # 本机外网 IP
```
