---
name: claw-do-pg-architecture
审核状态: 待审核
审核人: 
审核时间: 
---

# Claw DO - PG 数据库架构设计方案

## 1. 网络架构

```
Dosh Server (香港, 118.122.92.55)              Server-86 (广州, 125.67.215.86)
┌──────────────────────────┐                 ┌──────────────────────────┐
│  pool-bind   :8765       │   TCP 5432      │  PostgreSQL 16           │
│  pool-admin  :8766 (127) │◄──────────────► │  ├─ 数据库: jreye        │
│  pool-proxy  :8767       │   ~5ms 延迟     │  ├─ 数据库: sim_trade    │
│                         │                 │  └─ 数据库: claw_do (新建)│
│  hermes-weixin-001       │                 │                          │
│  hermes-weixin-002       │                 │  用户: claw_do_user      │
│  ...                     │                 │  密码: 【自动生成】       │
└──────────────────────────┘                 │  仅允许访问 claw_do 库    │
                                             └──────────────────────────┘
```

**连通性已验证：** dosh → 125.67.215.86:5432 ✅ OPEN（ping 延迟 ~5ms）

## 2. PostgreSQL 账号配置

在 Server-86 上创建专用于 claw-do 的数据库和用户：

```sql
-- 创建数据库
CREATE DATABASE claw_do WITH ENCODING 'UTF8' LC_COLLATE='en_US.UTF-8' LC_CTYPE='en_US.UTF-8';

-- 创建专用用户（密码自动生成，Docker 启动时传入）
CREATE USER claw_do_user WITH PASSWORD '【自动生成】';

-- 授予仅 claw_do 数据库的权限
GRANT ALL PRIVILEGES ON DATABASE claw_do TO claw_do_user;

-- 限制该用户只能从 dosh 服务器 IP 连接
-- pg_hba.conf 中加一行：
-- host    claw_do    claw_do_user    118.122.92.55/32    md5
```

**安全要点：**
- 密码不硬编码，首次部署时自动生成 32 位随机字符串
- 数据库用户只对 `claw_do` 库有权限，无法访问其他数据库
- 防火墙只允许 dosh IP 的 5432 入站（可选）
- 连接使用 TLS/SSL（如果 Server-86 PG 支持）

## 3. 数据表设计

### 3.1 bindings — 微信绑定记录（核心表）

| 字段 | 类型 | 说明 | 来源 |
|------|------|------|------|
| id | SERIAL PK | 自增主键 | |
| profile_name | VARCHAR(32) UNIQUE NOT NULL | 配置名 (weixin-001) | 系统 |
| user_id | VARCHAR(128) | 微信用户 ID (ilink_user_id) | WeChat 扫码确认 |
| account_id | VARCHAR(128) | 微信机器人账号 ID (ilink_bot_id) | WeChat 扫码确认 |
| nickname | VARCHAR(128) | 微信昵称 | WeChat 扫码确认（有则存） |
| avatar_url | TEXT | 微信头像 URL | WeChat 扫码确认（有则存） |
| status | VARCHAR(20) NOT NULL DEFAULT 'active' | active / inactive / expired | 系统 |
| bound_at | TIMESTAMPTZ NOT NULL DEFAULT NOW() | 绑定时间 | 系统 |
| last_active_at | TIMESTAMPTZ | 最后活跃时间 | 活跃事件更新 |
| last_heartbeat_at | TIMESTAMPTZ | 最后心跳时间 | 健康检查更新 |
| bot_token | VARCHAR(256) | 机器人 Token（加密存储/不存） | WeChat 扫码确认 |
| bot_base_url | VARCHAR(256) | 机器人 API Base URL | WeChat |
| machine_ip | VARCHAR(45) | 宿主机 IP | 容器运行机 IP |
| machine_hostname | VARCHAR(64) | 宿主机名 | dosh / s87 等 |
| created_at | TIMESTAMPTZ DEFAULT NOW() | 记录创建时间 | |
| updated_at | TIMESTAMPTZ DEFAULT NOW() | 记录更新时间 | |

> ⚠️ **关于 bot_token：** 是否明文存储需讨论。如果存储，数据库泄露将导致 WeChat 凭证暴露。可以选择不存此字段，而信任现有的 `.env` 文件机制。

### 3.2 docker_containers — Docker 容器信息

| 字段 | 类型 | 说明 |
|------|------|------|
| id | SERIAL PK | |
| binding_id | INTEGER NOT NULL REFERENCES bindings(id) | 外键→bindings |
| container_name | VARCHAR(64) UNIQUE NOT NULL | hermes-weixin-001 |
| container_id | VARCHAR(64) | Docker 短 ID |
| image | VARCHAR(128) | hermes-bot:latest |
| status | VARCHAR(20) NOT NULL | running / stopped / exited |
| memory_limit | VARCHAR(16) | 2g |
| restart_count | INTEGER DEFAULT 0 | 重启次数 |
| ports | JSONB | 端口映射 {"8765":"...", "9900":"..."} |
| created_at | TIMESTAMPTZ DEFAULT NOW() | 容器创建时间 |
| updated_at | TIMESTAMPTZ DEFAULT NOW() | 最后更新 |

### 3.3 qr_history — 二维码扫码历史

| 字段 | 类型 | 说明 |
|------|------|------|
| id | SERIAL PK | |
| profile_name | VARCHAR(32) NOT NULL | 对应哪个槽位 |
| user_id | VARCHAR(128) | 扫码时获取的用户 ID（有则存） |
| status | VARCHAR(16) NOT NULL | created / scanned / confirmed / expired / failed |
| refresh_count | INTEGER DEFAULT 0 | 此 session 刷了几次 |
| created_at | TIMESTAMPTZ DEFAULT NOW() | |
| resolved_at | TIMESTAMPTZ | 最终结果时间 |

### 3.4 gateway_health_log — 网关健康检查日志

| 字段 | 类型 | 说明 |
|------|------|------|
| id | SERIAL PK | |
| profile_name | VARCHAR(32) NOT NULL | 哪个 profile |
| status | VARCHAR(20) NOT NULL | healthy / unhealthy |
| restart_count | INTEGER | 此时的重启次数 |
| memory_usage | VARCHAR(32) | 容器内存用量（可选） |
| error_message | TEXT | 异常信息 |
| checked_at | TIMESTAMPTZ DEFAULT NOW() | |

### 3.5 索引设计

```sql
-- bindings
CREATE INDEX idx_bindings_user_id ON bindings(user_id);
CREATE INDEX idx_bindings_status ON bindings(status);
CREATE INDEX idx_bindings_profile ON bindings(profile_name);

-- docker_containers
CREATE INDEX idx_containers_binding ON docker_containers(binding_id);
CREATE INDEX idx_containers_status ON docker_containers(status);

-- qr_history
CREATE INDEX idx_qr_profile ON qr_history(profile_name);
CREATE INDEX idx_qr_status ON qr_history(status);

-- gateway_health_log
CREATE INDEX idx_health_profile ON gateway_health_log(profile_name);
CREATE INDEX idx_health_time ON gateway_health_log(checked_at DESC);
```

## 4. 集成方案

### 4.1 新增模块: `pool_manager/pg_store.py`

新建 Python 模块，封装所有 PG 操作：

```python
class PgStore:
    """PG 持久化层——处理所有数据库操作。"""
    
    def __init__(self, dsn: str):
        self.dsn = dsn
        self.pool: Optional[asyncpg.Pool] = None
    
    async def connect(self):
        """创建连接池。"""
        self.pool = await asyncpg.create_pool(self.dsn, min_size=2, max_size=10)
        await self._ensure_schema()
    
    async def _ensure_schema(self):
        """创建表（幂等）。"""
        ...  # CREATE TABLE IF NOT EXISTS
    
    # bindings CRUD
    async def upsert_binding(self, data: dict) -> int: ...
    async def get_binding(self, profile: str) -> Optional[dict]: ...
    async def list_bindings(self) -> list[dict]: ...
    
    # docker_containers CRUD
    async def upsert_container(self, binding_id: int, data: dict) -> int: ...
    async def get_container(self, profile: str) -> Optional[dict]: ...
    async def update_container_status(self, profile: str, status: str): ...
    
    # qr_history
    async def log_qr_event(self, profile: str, status: str, user_id: str = ""): ...
    
    # gateway_health
    async def log_health_check(self, profile: str, status: str, 
                                restart_count: int, error: str = ""): ...
```

### 4.2 启动流程变更（service.py）

3 个容器的启动时序：

```
pool-proxy (启动)
  → 无变更，不涉及 PG

pool-bind (启动)
  → state.load()
  → PgStore.connect()
  → 扫描 Docker 容器，更新 PG 中的 binding/container 记录
  → 启动热池

pool-admin (启动)
  → state.load()
  → PgStore.connect()
  → 初始化管理员 token
```

### 4.3 绑定流程变更（hot_pool.py → 回调）

扫码确认后的回调 `_on_confirmed()`：

```
当前流程:
  on_confirmed
    → 去重检查（内存 state）
    → 写入 .env 和 config.yaml
    → 创建 Docker 容器
    → 记录到内存 state

新流程:
  on_confirmed
    → 去重检查（PG bindings）
    → 写入 .env 和 config.yaml  
    → 创建 Docker 容器
    → PG: 写入 binding 记录
    → PG: 写入 docker_container 记录
    → PG: 写入 qr_history (confirmed)
    → 更新内存 state + 保存 JSON
```

### 4.4 数据流示意图

```
扫码确认（hot_pool._on_confirmed）
    │
    ├──→ 写入 .env / config.yaml（文件系统）
    ├──→ Docker.create_container（Docker API）
    ├──→ PgStore.upsert_binding()  
    ├──→ PgStore.upsert_container()
    ├──→ PgStore.log_qr_event()
    └──→ PoolState.mark_bound() + state.save()
                 │
          JSON 文件（/home/data/pool-manager/pool_state.json）
          ← 快速读取、启动时恢复
          ← PG 为权威数据源、JSON 为缓存
```

## 5. DSN 配置

连接字符串通过环境变量注入 Docker 容器：

```
# .env 文件中
CLAW_DO_DSN=postgresql://claw_do_user:【密码】@125.67.215.86:5432/claw_do
```

`docker-compose.yml` 中每个服务继承此环境变量：

```yaml
services:
  pool-bind:
    environment:
      - CLAW_DO_DSN=${CLAW_DO_DSN}
  pool-admin:
    environment:
      - CLAW_DO_DSN=${CLAW_DO_DSN}
```

## 6. 迁移策略

### 6.1 现有数据

目前已有 6 个绑定、6 个 Docker 容器。首次部署 PG 时需要：

1. 创建数据库 + 表
2. 扫描已有 Docker 容器 → 写入 bindings 表 + docker_containers 表
3. 后续所有操作以 PG 为准

### 6.2 JSON 状态文件的角色变化

| 阶段 | JSON 文件 | PG |
|------|-----------|-----|
| 当前 | 唯一状态存储 | 无 |
| 过渡期 | 读取 + 写入 | 写入 + 更新 |
| 最终 | 只读缓存（快速恢复） | 权威数据源 |

JSON 文件保留但降级为只读缓存——用于热池启动时快速恢复状态（比 PG 快）。所有写入操作双写 PG + JSON。

## 7. 依赖变化

新增依赖（添加到 `requirements.txt`）：

```
asyncpg>=0.29.0    # PostgreSQL 异步驱动
sqlalchemy[asyncio]>=2.0  # 可选：ORM 层（爱用不用）
```

也可以用纯 `asyncpg` 不用 ORM，减少依赖和心智负担。

## 8. 待讨论问题

| # | 问题 | 选项 |
|---|------|------|
| 1 | bot_token 是否存 PG？ | 存 / 不存（信任 .env） |
| 2 | 用 ORM 还是 raw SQL？ | SQLAlchemy / asyncpg 纯 SQL / 混合 |
| 3 | 密码怎么管理？ | setup.sh 自动生成 / docker-compose .env 预置 |
| 4 | PG 连接失败是否阻止启动？ | 容错启动（PG 不可用仍可运行，日志警告） |
| 5 | 历史 QR 记录要不要保留？ | 保留完整历史 / 只保留最近 N 条 |
| 6 | 存储微信昵称/头像？ | 存（WeChat API 能否拿到？） |
| 7 | 第一次部署 6 个已有容器的数据迁移时机？ | setup.sh 做 / 容器启动时自动迁移 |