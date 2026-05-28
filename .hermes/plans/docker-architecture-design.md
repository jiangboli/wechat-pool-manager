# WeChat Pool Manager Docker 化架构设计

> 状态：已审核通过 ✅
> 
> 审核人: jreye
> 审核时间: 2026-05-29
> 审核意见: 同意，按计划执行
>
> 版本: v1.0
> 日期: 2026-05-29

---

## 一、目标

将 WeChat Pool Manager 从**基于 Linux 用户 + systemd 的隔离架构**重构为**基于 Docker 容器的隔离架构**，同时整合三大核心能力：

1. **LLM 代理 + 负载均衡** — 所有 Docker 容器的 LLM 请求集中到此项目，支持多 Provider 多 Key 的智能转发
2. **扫码绑定** — 扫码绑定微信到 Docker 容器
3. **Docker 调度** — 容器的创建、调度、启停、健康检查、资源管理

## 二、架构总览

```
                             ┌─────────────────────────┐
                             │    WeChat iLink API     │
                             └──────────┬──────────────┘
                                        │
┌──────────────────────────────────────────────────────────────────┐
│ docker-compose up — 所有服务都在 Docker 内                      │
│                                                                  │
│  ┌─────────────────────────────────────────────────────────┐    │
│  │  pool-manager (:8765)                                   │    │
│  │  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐  │    │
│  │  │  扫码绑定引擎  │  │ Docker调度器  │  │ LLM Proxy   │  │    │
│  │  │  (HotPool)    │  │ (DockerSvc)  │  │ (负载均衡)    │  │    │
│  │  └──────┬───────┘  └──────┬───────┘  └──────┬───────┘  │    │
│  └─────────┼─────────────────┼─────────────────┼──────────┘    │
│            │                 │                 │                │
│            ▼                 ▼                 ▼                │
│  ┌──────────┐  ┌──────────┐  ┌──────────┐         ┌──────────┐ │
│  │ hermes-  │  │ hermes-  │  │ hermes-  │  ...    │ hermes-  │ │
│  │ wx001    │  │ wx002    │  │ wx003    │         │ wxNNN    │ │
│  │ (容器)    │  │ (容器)    │  │ (容器)    │         │ (容器)    │ │
│  │ LLM→pm:  │  │ LLM→pm:  │  │ LLM→pm:  │         │ LLM→pm:  │ │
│  └──────────┘  └──────────┘  └──────────┘         └──────────┘ │
│                                                                  │
└──────────────────────────────────────────────────────────────────┘
         │
         ▼
┌──────────────────────────────────────────────────────────────────┐
│   宿主机数据卷: /home/data/{尾数}/{profile}/.hermes/            │
│   /home/data/1/wx001/.hermes/  /home/data/2/wx002/.hermes/ ... │
│   尾数 = profile 编号的末位 (0-9)，分散到 10 个目录             │
└──────────────────────────────────────────────────────────────────┘
         │
         ▼
┌──────────────────────────────────────────────────────────────────┐
│                      外部 LLM API                                 │
│  DeepSeek / OpenAI / Alibaba / 其他                               │
└──────────────────────────────────────────────────────────────────┘
```

### 核心变化

| 原有架构 | 新架构 |
|---------|--------|
| Pool Manager 在 systemd --user 服务 | **Pool Manager 也在 Docker 内 (docker-compose)** |
| 每个用户一个 Linux 用户 (wx001) | 每个用户一个 Docker 容器 (hermes-wx001) |
| systemd system 服务 (hermes-gateway@wx001) | Docker 容器 (hermes-wx001) |
| 文件系统隔离 (Unix 权限) | 容器级隔离 (cgroups + namespaces) |
| Hermes 共享 venv 在 /opt/hermes/ | Hermes 打包在 Docker 镜像内 |
| LLM Proxy 读 auth.json | LLM Proxy 通过 API 动态管理凭证池 |
| systemd 管理启停 | Docker API / docker-compose 管理启停 |
| 数据在 /home/xxx/.hermes/ | 数据在 **/home/data/{机器号}/wxNNN/.hermes/** |

### 数据目录规范

```
/home/data/{尾数}/{profile}/.hermes/
  ├── .env              # 微信凭证
  ├── config.yaml       # Hermes 配置
  ├── sessions.db       # 会话历史
  └── logs/

示例:
  /home/data/1/wx001/.hermes/       ← wx001 尾数 1
  /home/data/2/wx002/.hermes/       ← wx002 尾数 2
  /home/data/0/wx010/.hermes/       ← wx010 尾数 0
  /home/data/9/wx019/.hermes/       ← wx019 尾数 9
  /home/data/5/wx025/.hermes/       ← wx025 尾数 5
```

{尾数} = profile 编号数字部分的末位（0-9），自动计算，无需传入参数。
- 100 个 profile 分散到 10 个目录，每个目录约 10 个
- `ls /home/data/` 看到的是 0-9 十个文件夹，不会炸列表

---

## 三、模块设计

### 3.1 LLM 代理模块 (已有 + 增强)

**当前状态：** `pool_manager/proxy.py` 已实现基本转发能力，但凭证从 auth.json 静态加载。

**增强目标：**

| 功能 | 实现方式 |
|------|---------|
| 多 Provider 支持 | 每个 provider 独立凭证池（deepseek → [key1, key2, ...]） |
| Round-robin 负载均衡 | 每个 provider 维护索引，按序分配 |
| 动态凭证管理 | REST API 增删凭证（热更新，不重启） |
| 健康检查 | 定期测试各 provider 可用性，标记死 key |
| Fallback 策略 | provider 不可用时自动切换到备用 provider |
| 流式/非流式 | 完全透明转发 SSE 流 |
| **请求量统计** | 记录每个 provider/key 的调用量、延迟、错误率 |
| **熔断保护** | 连续错误 N 次后暂停使用某个 key（时间窗口恢复） |

**凭证管理 API (新增):**

```yaml
POST /api/v1/proxy/keys          # 添加 API key
  {"provider": "deepseek", "key": "sk-xxx", "label": "主key-1"}

DELETE /api/v1/proxy/keys/{id}   # 删除 API key

GET  /api/v1/proxy/status        # 查看所有 provider/key 状态
  {"deepseek": {"keys": [{"label": "主key-1", "healthy": true, "used_today": 123}], "total_used": 123}}

POST /api/v1/proxy/fallback      # 配置 fallback provider 链
  {"primary": "deepseek", "fallback": "alibaba"}
```

**转发流程：**

```
Docker 容器内 Hermes
    ↓ POST /v1/chat/completions (model=deepseek-v4-flash)
Pool Manager Proxy
    ↓ 1. 解析 model → 匹配 provider (deepseek)
    ↓ 2. round_robin 从 deepseek 池选一个 key
    ↓ 3. 替换 Authorization header
    ↓ 4. 记录调用（增加计数器）
    ↓ 5. 转发到 https://api.deepseek.com/v1/chat/completions
    ↓ 6. 返回响应/流
```

**Docker 容器配置示例：**

```yaml
# 容器内 ~/.hermes/config.yaml 的 model 配置
model:
  provider: custom:pool-proxy
  base_url: http://pool-manager:8765/v1   # Docker 网络内访问
custom_providers:
  pool-proxy:
    base_url: http://pool-manager:8765/v1
    api_type: openai
    # 不需要 api_key——proxy 负责注入
```

### 3.2 Docker 调度模块 (新增)

**新文件:** `pool_manager/docker_scheduler.py`

**核心职责：**

1. **容器生命周期管理** — create, start, stop, restart, remove
2. **镜像管理** — 构建/拉取/hermes-bot 镜像
3. **资源限制** — CPU/Memory 限制（防止一个容器吃掉所有资源）
4. **健康检查** — 定期检查容器状态，自动重启异常容器
5. **日志管理** — 日志轮转、集中查看
6. **调度策略** — 基于资源使用率的智能调度

**调度器接口：**

```python
class DockerScheduler:
    async def create_container(self, profile: str, credentials: dict) -> bool
    async def start_container(self, profile: str) -> bool
    async def stop_container(self, profile: str) -> bool
    async def restart_container(self, profile: str) -> bool
    async def remove_container(self, profile: str) -> bool
    async def get_container_status(self, profile: str) -> dict
    async def list_containers(self) -> list[dict]
    async def get_container_logs(self, profile: str, tail: int) -> str
```

**容器命名规范：**

```
hermes-{profile_name}  →  hermes-wx001, hermes-wx002, ...
```

**资源限制：**

```yaml
# config.yaml 新增 section
docker:
  # 容器资源限制
  container_defaults:
    memory_limit: "256m"        # 每个容器最多 256MB
    memory_reservation: "128m"  # 预留 128MB
    cpu_shares: 256             # CPU 权重 (默认 1024)
    cpu_quota: 50000            # 最多使用 0.5 核 (100000=1核)

  # 最大同时运行容器数
  max_containers: 80

  # Docker 网络
  network: "hermes-pool-net"

  # 数据卷根目录
  data_root: "/data/hermes/"
```

**容器创建流程：**

```
1. scheduler.create_container(profile, credentials)
    ↓
2. 校验: 资源是否够 (running_count < max_containers)
    ↓
3. 创建数据目录: mkdir -p /data/hermes/wx001/.hermes/
    ↓
4. 写入容器内 ~/.hermes/.env（微信凭证，不含 API key）
    ↓
5. 写入容器内 ~/.hermes/config.yaml（platform 配置 + LLM proxy 地址）
    ↓
6. docker run -d \
     --name hermes-wx001 \
     --network hermes-pool-net \
     --memory 256m --memory-reservation 128m \
     -v /data/hermes/wx001/.hermes:/home/hermes/.hermes \
     hermes-bot:latest
    ↓
7. 等待容器就绪 (health check)
    ↓
8. 更新 state: profile→bound_healthy
```

**容器镜像 (`hermes-bot`):**

```dockerfile
FROM python:3.11-slim

# 安装 Hermes Agent
RUN pip install hermes-agent

# 运行用户
RUN useradd -m hermes
USER hermes
WORKDIR /home/hermes

# 健康检查
HEALTHCHECK --interval=30s --timeout=10s --retries=3 \
  CMD pgrep -f "hermes gateway" || exit 1

# 启动 gateway
CMD ["hermes", "gateway", "run"]
```

### 3.3 扫码绑定模块 (已有 + 适配)

**当前状态：** `pool_manager/hot_pool.py` 已实现完整的 QR 扫码+确认+绑定流程。

**适配改动：**

| 原有行为 | Docker 化行为 |
|---------|-------------|
| `pm.setup_linux_profile()` 创建 Linux 用户 | `scheduler.create_container()` 创建 Docker 容器 |
| `gm.start(profile)` 启动 systemd service | `scheduler.start_container()` 启动容器 |
| `list_linux_users()` 获取已创建的用户列表 | `scheduler.list_containers()` 获取容器列表 |
| `update_credentials()` 改 Linux 用户文件 | `docker cp` 或容器内 `~/.hermes/` volume mount 直接操作 |

**去重机制不变**：同一微信用户二次扫码 → 找到已有容器 → 更新凭证 → 重启容器。

---

## 四、Docker 网络设计

```
hermes-pool-net (bridge network)
    │
    ├── pool-manager (:8765) —— LLM Proxy 端点
    │
    ├── hermes-wx001 (:动态端口或无需暴露)
    ├── hermes-wx002
    ├── hermes-wx003
    └── ...
```

**策略：**

- 所有容器在同一 bridge 网络 (`hermes-pool-net`)，通过容器名 DNS 互通
- **Hermes Gateway 不需要对外暴露端口** — 它连接 iLink 时是作为客户端出站
- Pool Manager 暴露端口 8765 供外部访问（Web 前端 + LLM Proxy 端点）
- 如果需要外部 API 访问（如飞书 Webhook），通过 Pool Manager 反向代理

---

## 五、数据持久化

```
/home/data/{尾数}/{profile}/.hermes/
  ├── .env              # 微信凭证 (account_id, token)
  ├── config.yaml       # Hermes 配置 (platform, model→proxy)
  ├── sessions.db       # 会话历史
  └── logs/

示例:
  /home/data/1/wx001/.hermes/       ← wx001 尾数 1
  /home/data/9/wx019/.hermes/       ← wx019 尾数 9
```

{尾数} 自动从 profile 编号末位计算（0-9），100 个 profile 分散到 10 个目录。

每个容器的 `~/.hermes/` 通过 volume mount 挂载到宿主机 `/home/data/{尾数}/{profile}/.hermes/`。这样：

- 容器重启/重建后数据不丢失
- 宿主机可直接查看/修改配置
- Pool Manager 可以直接写 `.env` 和 `config.yaml` 到宿主机路径（通过 volume mount）
- 多机部署时路径天然隔离（机器号不同）

---

## 六、配置变化

### config.yaml 新增 section

```yaml
# ── Docker 配置 ──
docker:
  image: "hermes-bot:latest"          # 容器镜像
  network: "hermes-pool-net"           # Docker 网络
  data_root: "/home/data/"             # 数据卷根目录（实际路径 = data_root + {尾数}/ + {profile}/）
  max_containers: 80                   # 最大同时运行容器数
  container_defaults:
    memory_limit: "256m"
    memory_reservation: "128m"
    cpu_shares: 256
    cpu_quota: 50000
  restart_policy: "unless-stopped"

# ── LLM Proxy 配置 ──
proxy:
  default_provider: "deepseek"
  fallback_providers: ["alibaba"]
  health_check_interval: 300           # 每 5 分钟检查一次 key 健康
  circuit_breaker:
    max_errors: 5                       # 连续 5 次错误触发熔断
    recovery_window: 60                 # 60 秒后尝试恢复
```

### config.yaml 移除/弃用 section

```yaml
# ❌ 移除: gateway 相关配置（不再使用 systemd）
gateway:
  idle_timeout_minutes: ...
  restart_on_crash: ...
  max_restart_attempts: ...
  restart_delay_seconds: ...
  health_check_interval: ...

# ❌ 移除: ilink 配置保留但 qr_timeout 等参数移到 pool 段
```

---

## 七、实施步骤

### Phase 1: 基础设施

| 步骤 | 内容 | 涉及文件 |
|------|------|---------|
| 1.1 | 创建 Dockerfile 和 docker-compose.yml | 新增 |
| 1.2 | 构建 hermes-bot 基础镜像 | 新增 |
| 1.3 | 创建 Docker bridge 网络 | 新增 |
| 1.4 | 实现 DockerScheduler 类 (create/start/stop/restart) | `pool_manager/docker_scheduler.py` |
| 1.5 | 实现容器健康检查循环 | `pool_manager/docker_scheduler.py` |

### Phase 2: 适配现有模块

| 步骤 | 内容 | 涉及文件 |
|------|------|---------|
| 2.1 | 修改 HotPool._on_confirmed 适配 Docker 调度 | `pool_manager/hot_pool.py` |
| 2.2 | 修改 Service 启动逻辑（Docker 替代 systemd 初始化） | `pool_manager/service.py` |
| 2.3 | 更新 state.py 移除 Linux 用户相关逻辑 | `pool_manager/state.py` |
| 2.4 | 更新 gateway_manager.py 为 Docker 调度器包装 | `pool_manager/gateway_manager.py` |
| 2.5 | 适配 API 端点（list_gateways 改为查 Docker） | `pool_manager/service.py` |

### Phase 3: LLM Proxy 增强

| 步骤 | 内容 | 涉及文件 |
|------|------|---------|
| 3.1 | 实现动态凭证管理 API（增删查） | `pool_manager/proxy.py` |
| 3.2 | 实现熔断保护 (circuit breaker) | `pool_manager/proxy.py` |
| 3.3 | 实现调用量统计 | `pool_manager/proxy.py` |
| 3.4 | 实现 Fallback provider 链 | `pool_manager/proxy.py` |
| 3.5 | 扩展凭证源：env 变量 / API 参数传递 | `pool_manager/proxy.py` |

### Phase 4: 配置与测试

| 步骤 | 内容 | 涉及文件 |
|------|------|---------|
| 4.1 | 更新 config.yaml（新增 docker/proxy 段） | `config.yaml` |
| 4.2 | 更新 setup.sh 支持 Docker 模式参数 | `scripts/setup.sh` |
| 4.3 | 更新创建 profile 脚本（不再需要 Linux 用户） | `scripts/create_profiles.py` |
| 4.4 | 端到端测试：扫码绑定 → 创建容器 → LLM 请求 → 健康检查 | 测试 |
| 4.5 | 编写部署文档 | `README.md` |

---

## 八、删除/废弃的组件

| 组件 | 原因 | 替代方案 |
|------|------|---------|
| `systemd/hermes-gateway@.service` | 不再需要 systemd 模板 | Docker 容器生命周期管理 |
| `systemd/hermes-pool.service` | Pool Manager 自身可 Docker 化 | docker-compose up pool-manager |
| `scripts/setup-isolation.sh` | 不再需要 systemd + sudoers 设置 | Docker 原生隔离 |
| `scripts/create_profiles.py` (Linux 用户版) | 不再需要创建 Linux 用户 | Create Docker volumes |
| 所有 sudoers 配置 | 不再需要 sudo 权限 | Docker API 无需 sudo（docker group） |

---

## 九、风险与注意事项

| 风险 | 影响 | 缓解措施 |
|------|------|---------|
| Docker daemon 资源消耗 | 80 个容器 × 256MB = 20GB+ RAM | 资源限制 + 动态调度（非活跃容器可停止） |
| 容器启动速度慢 | 首次扫码后需等待容器就绪 | 热池预启动容器（类似现成的 hot pool 机制） |
| LLM Proxy 成为单点故障 | Proxy 挂了所有容器都无法使用 LLM | Proxy 也 Docker 化 + 健康检查 + 自动重启 |
| Docker 镜像版本兼容性 | Hermes 更新后需要重建镜像 | CI 自动构建 + 多阶段 Dockerfile |
| 数据卷权限 | 容器进程可能写入权限问题 | 容器用户 UID 固定 + 宿主机 chown 脚本 |

---

## 十、验证方式

1. **扫码绑定** — 扫码 → 确认 → State 显示 bound_healthy → `docker ps` 显示容器运行
2. **LLM 代理** — 容器内 `curl -X POST http://pool-manager:8765/v1/chat/completions` 返回正常
3. **负载均衡** — 多次请求后各 key 使用量均衡
4. **资源隔离** — 一个容器崩溃不影响其他容器
5. **数据持久** — 容器重启后会话历史不丢失

---

## 十一、文件变更清单

### 新增文件

| 路径 | 说明 |
|------|------|
| `Dockerfile` | hermes-bot 容器镜像 |
| `docker-compose.yml` | Pool Manager + 服务编排 |
| `pool_manager/docker_scheduler.py` | Docker 容器调度器 |
| `pool_manager/proxy_config.py` | Proxy 配置管理 |

### 修改文件

| 路径 | 变更内容 |
|------|---------|
| `pool_manager/proxy.py` | 动态凭证管理 + 熔断 + 统计 + Fallback |
| `pool_manager/hot_pool.py` | _on_confirmed 改为 Docker 调度 |
| `pool_manager/service.py` | 启动逻辑适配 Docker，端点适配 |
| `pool_manager/state.py` | 移除 Linux 用户相关逻辑 |
| `pool_manager/gateway_manager.py` | Docker 调度器包装 |
| `pool_manager/config.py` | 加载 Docker 配置段 |
| `config.yaml` | 新增 docker/proxy 段，移除 gateway 段 |
| `scripts/setup.sh` | Docker 模式参数 |
| `README.md` | Docker 部署文档 |

### 删除文件

| 路径 | 原因 |
|------|------|
| `systemd/hermes-gateway@.service` | 不再需要 |
| `systemd/hermes-pool.service` | Pool Manager Docker 化 |
| `scripts/setup-isolation.sh` | 不再需要 |
| `scripts/create_profiles.py` (Linux 用户版) | 不再需要 |

---

> **审核说明：** 以上是完整的 Docker 化架构设计方案。核心变化是：Linux 用户 + systemd → Docker 容器。Pool Manager 自身也 Docker 化或保留为 systemd 服务（待定）。请审核确认后开始实施。
