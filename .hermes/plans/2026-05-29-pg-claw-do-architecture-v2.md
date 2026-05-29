---
name: claw-do-pg-architecture-v2
审核状态: 通过 ✅
审核人: jreye
审核时间: 2026-05-29 12:00
审核意见: "暂时先这样，先出第一版" 
---

# Claw DO - PG 中心存储架构方案（v2）

## 1. 整体架构

```
                          ┌──────────────────────────────────┐
                          │   PostgreSQL (Server-86 广州)     │
                          │   Database: claw_do              │
                          │   User: claw_do_user             │
                          │   Host: 125.67.215.86:5432       │
                          │                                  │
                          │   bindings ─── docker_containers │
                          │   qr_history ─── gateway_health  │
                          │   machines (可选)                 │
                          └──────────┬───────────────────────┘
                                     │ TCP 5432
                ┌────────────────────┼────────────────────┐
                │                    │                    │
        ┌───────┴────────┐  ┌───────┴────────┐  ┌───────┴────────┐
        │ Dosh 香港机房   │  │ 新服务器 A     │  │ 新服务器 B     │
        │ 118.122.92.55  │  │ x.x.x.x        │  │ y.y.y.y        │
        │                │  │                │  │                │
        │ pool-bind      │  │ pool-bind      │  │ pool-bind      │
        │ pool-admin     │  │ pool-admin     │  │ pool-admin     │
        │ pool-proxy     │  │ pool-proxy     │  │ pool-proxy     │
        │                │  │                │  │                │
        │ bot-001~006    │  │ bot-007~...    │  │ bot-...        │
        └────────────────┘  └────────────────┘  └────────────────┘
```

- PG 是**唯一中心存储节点**，所有服务器的绑定、容器、健康数据统一存入
- 每个服务器运行自己的 pool-bind/admin/proxy 三容器
- 管理员从任意一台服务器的 `pool-admin`（127.0.0.1:8766）可查询全量数据
- 新增服务器只需 `git clone + setup.sh + 配置 DSN`，自动注册到 PG

## 2. PostgreSQL 配置（Server-86 一次性操作）

### 2.1 创建数据库和用户

```sql
-- 创建数据库
CREATE DATABASE claw_do WITH ENCODING 'UTF8';

-- 创建专用用户（密码自动生成，首次部署时执行）
CREATE USER claw_do_user WITH PASSWORD '【自动生成32位随机密码】';

-- 授予 claw_do 库的所有权限
GRANT ALL PRIVILEGES ON DATABASE claw_do TO claw_do_user;

-- 连接到 claw_do 后还需授予 schema 权限
\c claw_do
GRANT ALL ON SCHEMA public TO claw_do_user;
```

### 2.2 连接白名单

在 Server-86 的 `pg_hba.conf` 中添加：

```
host    claw_do    claw_do_user    118.122.92.55/32    md5
host    claw_do    claw_do_user    x.x.x.x/32          md5    # 后续服务器加这里
```

也可以用更开放的策略（后续不再改配置）：

```
host    claw_do    claw_do_user    0.0.0.0/0           md5
```

> ⚠️ 如果所有连接都走 md5 密码认证且密码安全（32 位随机），全 0 开放在安全上可以接受。密码只在 setup.sh 运行机器上的 `.env` 文件中存在一次。

### 2.3 DSN 配置

`.env` 文件：

```
CLAW_DO_DSN=postgresql+asyncpg://claw_do_user:【随机密码】@125.67.215.86:5432/claw_do
```

`docker-compose.yml` 中所有 3 个服务继承此变量：

```yaml
services:
  pool-bind:
    environment:
      - CLAW_DO_DSN=${CLAW_DO_DSN}
  pool-admin:
    environment:
      - CLAW_DO_DSN=${CLAW_DO_DSN}
```

## 3. 数据表设计

### 3.1 machines — 服务器注册表 ⭐（新增，多机房核心）

| 字段 | 类型 | 说明 |
|------|------|------|
| id | SERIAL PK | 自增主键 |
| machine_ip | VARCHAR(45) UNIQUE NOT NULL | 服务器公网 IP |
| hostname | VARCHAR(64) | 服务器主机名 |
| location | VARCHAR(64) | 机房位置（香港/广州/...） |
| total_slots | INTEGER DEFAULT 100 | 总槽位数 |
| hot_pool_size | INTEGER DEFAULT 5 | 热池大小 |
| status | VARCHAR(20) DEFAULT 'online' | online / offline / maintenance |
| version | VARCHAR(32) | 部署的 pool-manager 版本 |
| last_heartbeat_at | TIMESTAMPTZ | 最后心跳时间 |
| created_at | TIMESTAMPTZ DEFAULT NOW() | |
| updated_at | TIMESTAMPTZ DEFAULT NOW() | |

**作用：**
- `pool-bind` 启动时自动注册/更新此机器记录
- `pool-admin` 可查询所有机器 + 每台机器的绑定统计
- 为后续跨服务器管理、机房间负载调度做准备

### 3.2 bindings — 微信绑定记录（核心表）

| 字段 | 类型 | 说明 | 来源 |
|------|------|------|------|
| id | SERIAL PK | 自增主键 | 系统 |
| profile_name | VARCHAR(32) UNIQUE NOT NULL | 配置名 (weixin-001) | 系统 |
| machine_ip | VARCHAR(45) NOT NULL | **运行本容器的服务器 IP** ⭐ | 自检测 |
| machine_hostname | VARCHAR(64) | 服务器主机名 | 自检测 |
| user_id | VARCHAR(128) | 微信用户 ID (ilink_user_id) | WeChat API |
| account_id | VARCHAR(128) | 机器人账号 ID (ilink_bot_id) | WeChat API |
| nickname | VARCHAR(128) | 微信昵称（有则存） | WeChat API |
| avatar_url | TEXT | 微信头像 URL（有则存） | WeChat API |
| bot_token | VARCHAR(256) | 机器人 Token ✅ **明文存储** | WeChat API 决策① |
| bot_base_url | VARCHAR(256) | 机器人 API 地址 | WeChat API |
| status | VARCHAR(20) NOT NULL DEFAULT 'active' | active / inactive / expired | 系统 |
| bound_at | TIMESTAMPTZ NOT NULL DEFAULT NOW() | 首次绑定时间 | 系统 |
| last_active_at | TIMESTAMPTZ | 最后活跃时间 | 事件更新 |
| last_heartbeat_at | TIMESTAMPTZ | 最后健康心跳 | 健康检查 |
| created_at | TIMESTAMPTZ DEFAULT NOW() | 记录创建时间 | |
| updated_at | TIMESTAMPTZ DEFAULT NOW() | 最后更新时间 | |

### 3.3 docker_containers — 容器详情

| 字段 | 类型 | 说明 |
|------|------|------|
| id | SERIAL PK | |
| binding_id | INTEGER NOT NULL REFERENCES bindings(id) | 外键 |
| container_name | VARCHAR(64) NOT NULL | hermes-weixin-001 |
| container_id | VARCHAR(64) | Docker 短 ID |
| image | VARCHAR(128) | hermes-bot:latest |
| status | VARCHAR(20) NOT NULL | running / stopped / exited |
| memory_limit | VARCHAR(16) | 2g |
| restart_count | INTEGER DEFAULT 0 | |
| ports | JSONB | 端口映射 {"8765":"9901"} |
| machine_ip | VARCHAR(45) | 运行此容器的服务器 IP ⭐ |
| created_at | TIMESTAMPTZ DEFAULT NOW() | |
| updated_at | TIMESTAMPTZ DEFAULT NOW() | |

### 3.4 qr_history — 扫码历史

| 字段 | 类型 | 说明 |
|------|------|------|
| id | SERIAL PK | |
| profile_name | VARCHAR(32) NOT NULL | |
| machine_ip | VARCHAR(45) | 扫码发生在哪个服务器 |
| user_id | VARCHAR(128) | 扫码者（有则存） |
| status | VARCHAR(16) NOT NULL | created / scanned / confirmed / expired / failed |
| refresh_count | INTEGER DEFAULT 0 | |
| created_at | TIMESTAMPTZ DEFAULT NOW() | |
| resolved_at | TIMESTAMPTZ | 最终结果时间 |

### 3.5 gateway_health_log — 健康检查日志

| 字段 | 类型 | 说明 |
|------|------|------|
| id | SERIAL PK | |
| profile_name | VARCHAR(32) NOT NULL | |
| machine_ip | VARCHAR(45) | 哪个服务器上的检查 |
| status | VARCHAR(20) NOT NULL | healthy / unhealthy |
| restart_count | INTEGER | |
| memory_usage | VARCHAR(32) | |
| error_message | TEXT | |
| checked_at | TIMESTAMPTZ DEFAULT NOW() | |

## 4. ORM 模型设计（SQLAlchemy 2.0）

```python
# pool_manager/models.py

from datetime import datetime
from sqlalchemy import Column, Integer, String, Text, DateTime, ForeignKey, JSON, Index
from sqlalchemy.orm import DeclarativeBase, relationship

class Base(DeclarativeBase):
    pass

class Machine(Base):
    __tablename__ = "machines"
    
    id = Column(Integer, primary_key=True)
    machine_ip = Column(String(45), unique=True, nullable=False)
    hostname = Column(String(64))
    location = Column(String(64))
    total_slots = Column(Integer, default=100)
    hot_pool_size = Column(Integer, default=5)
    status = Column(String(20), default="online")
    version = Column(String(32))
    last_heartbeat_at = Column(DateTime(timezone=True))
    created_at = Column(DateTime(timezone=True), default=datetime.utcnow)
    updated_at = Column(DateTime(timezone=True), default=datetime.utcnow, onupdate=datetime.utcnow)

class Binding(Base):
    __tablename__ = "bindings"
    
    id = Column(Integer, primary_key=True)
    profile_name = Column(String(32), unique=True, nullable=False)
    machine_ip = Column(String(45), nullable=False)
    machine_hostname = Column(String(64))
    user_id = Column(String(128))
    account_id = Column(String(128))
    nickname = Column(String(128))
    avatar_url = Column(Text)
    bot_token = Column(String(256))  # 明文存储
    bot_base_url = Column(String(256))
    status = Column(String(20), default="active")
    bound_at = Column(DateTime(timezone=True), default=datetime.utcnow)
    last_active_at = Column(DateTime(timezone=True))
    last_heartbeat_at = Column(DateTime(timezone=True))
    created_at = Column(DateTime(timezone=True), default=datetime.utcnow)
    updated_at = Column(DateTime(timezone=True), default=datetime.utcnow, onupdate=datetime.utcnow)
    
    containers = relationship("DockerContainer", back_populates="binding")

class DockerContainer(Base):
    __tablename__ = "docker_containers"
    
    id = Column(Integer, primary_key=True)
    binding_id = Column(Integer, ForeignKey("bindings.id"), nullable=False)
    container_name = Column(String(64), unique=True, nullable=False)
    container_id = Column(String(64))
    image = Column(String(128))
    status = Column(String(20), nullable=False)
    memory_limit = Column(String(16))
    restart_count = Column(Integer, default=0)
    ports = Column(JSON)
    machine_ip = Column(String(45))
    created_at = Column(DateTime(timezone=True), default=datetime.utcnow)
    updated_at = Column(DateTime(timezone=True), default=datetime.utcnow, onupdate=datetime.utcnow)
    
    binding = relationship("Binding", back_populates="containers")

# QR 历史 & 健康日志 略...
```

### 4.1 为什么不增加组件

纯 asyncpg 能做的事情，SQLAlchemy 2.0 都能做还多了迁移、类型安全、复杂查询：

| 场景 | asyncpg 纯 SQL | SQLAlchemy 2.0 ORM |
|------|---------------|-------------------|
| 新增字段 | 改 SQL + 改 Python | 改 model 类即可 |
| 复杂 JOIN 查询 | 手写 SQL 拼接 | ORM 关系自动关联 |
| 表结构变更 | 手写 ALTER TABLE | Alembic 自动迁移 |
| 测试 Mock | 需 mock 连接 | 内存 SQLite 替换 |
| 代码行数 | 多（每个 SQL 写两遍） | 少（声明一次关系复用） |

## 5. 集成架构

### 5.1 新增模块

```
pool_manager/
├── models.py          # SQLAlchemy ORM 模型（新建）
├── pg_store.py        # PG 操作封装（新建）
├── service.py         # 3 个 FastAPI app（修改启动流程）
├── hot_pool.py        # 热池（修改绑定回调）
├── docker_scheduler.py # （修改容器状态更新）
└── state.py           # 保留 JSON 作为缓存（降级）
```

### 5.2 启动流程

```
pool-bind 启动
  │
  ├── state.load()                           ← 从 JSON 快速恢复
  ├── PgStore.connect()                      ← 连接 PG（失败 = 日志警告）
  ├── PgStore.register_machine()             ← 注册本机到 machines 表
  ├── PgStore.restore_bindings()             ← 从 PG 恢复绑定（覆盖 JSON）
  ├── 扫描 Docker 容器
  │   ├── PgStore.upsert_containers()        ← 同步现有容器到 PG
  │   └── state.restore_from_containers()    ← 更新内存状态
  └── 启动热池
```

### 5.3 绑定流程变化

```
扫码确认（hot_pool._on_confirmed）
    │
    ├── 写入 .env / config.yaml（文件系统）
    ├── Docker.create_container（Docker API）
    ├── PgStore.create_binding(profile, user_id, nickname, avatar_url,
    │         bot_token, machine_ip, machine_hostname, ...)
    ├── PgStore.create_container(binding_id, container_name, ...)
    ├── PgStore.log_qr_event(profile, "confirmed", user_id)
    └── PoolState.mark_bound() + state.save()
```

### 5.4 健康检查回调变化

```
health_check_loop 每 N 秒：
    │
    ├── PgStore.update_container_status(profile, status, restart_count)
    ├── PgStore.update_binding_heartbeat(profile)
    ├── PgStore.log_health_check(profile, status, restart_count, error)
    └── state.touch_active(profile)
```

### 5.5 去重查询

当前去重（纯内存）改为 PG 查询：

```python
# 旧：state.get_docker_user_by_user_id(user_id)
# 新：pg_store.find_binding_by_user_id(user_id)
# 跨机器去重——同一微信用户在任何服务器扫码，都能找到已绑定的容器
async def find_binding_by_user_id(self, user_id: str) -> Optional[dict]:
    stmt = select(Binding).where(
        Binding.user_id == user_id,
        Binding.status == "active"
    )
    result = await self.session.execute(stmt)
    return result.scalar_one_or_none()
```

## 6. 环境变量与依赖

### requirements.txt 新增

```
sqlalchemy[asyncio]>=2.0.0
asyncpg>=0.29.0
alembic>=1.13.0        # 数据库迁移（可选，初期可不用）
```

### .env / docker-compose 新增

```
CLAW_DO_DSN=postgresql+asyncpg://claw_do_user:***@125.67.215.86:5432/claw_do
```

## 7. setup.sh 变化

```bash
# 新增参数 --pg-dsn
bash setup.sh --total 100 --hot-pool 5 --pg-dsn "postgresql+asyncpg://..."

# 生成 .env 时写入 CLAW_DO_DSN
echo "CLAW_DO_DSN=$PG_DSN" >> .env

# 启动时自动注册机器
docker compose up -d
# pool-bind 启动后自动在 machines 表注册本机
```

## 8. 安全性

### 8.1 PG 密码管理

- 密码 32 位随机（`openssl rand -hex 16`）
- 只在 Server-86 创建用户时和 `.env` 文件中出现
- `.env` 文件放在 dosh 服务器上（不提交到 git）
- 每台服务器独立 `.env`，DSN 不同（共享同一个 PG 用户）

### 8.2 bot_token 存储

- bot_token **明文存储**在 bindings 表（决策①）
- 风险：PG 被攻破 → WeChat 凭证泄露
- 缓解：① PG 公网只允许已知服务器 IP；② 密码足够复杂；③ 后续可加列级加密

### 8.3 网络隔离

```
Dosh 服务器              Server-86
    │                        │
    │────── TCP 5432 ───────→│  claw_do_user（仅 claw_do 库）
    │                        │   无法访问 jreye / sim_trade 等其他库
```

## 9. 实施计划

### Phase 1：基础设施（本部署周期）

1. Server-86 上创建 claw_do 数据库 + 用户
2. 生成随机密码，配置 pg_hba.conf
3. 在代码中新增 models.py + pg_store.py
4. 修改启动流程 + 绑定回调
5. 修改 docker-compose.yml 注入 DSN
6. 修改 setup.sh 支持 --pg-dsn
7. 构建新镜像部署
8. 用户重新绑定触发 PG 写入

### Phase 2：扩展（后续）

1. Alembic 迁移支持
2. admin API 增加跨机器查询（全量 bindings + machines）
3. Web 管理面板显示所有服务器状态
4. 心跳超时自动标记机器 offline

## 10. 现有 JSON 文件的角色

| 阶段 | JSON 文件 | PG |
|------|-----------|-----|
| 当前 | 唯一状态存储 | 无 |
| 新架构 | 写入 + 写入 | **权威数据源** |
| 角色 | 快速启动缓存 + 降级备选 | 所有持久化 + 跨机器查询 |

```python
# state.py 的 save() 保持不变（双写）
def save(self):
    # 1. 写入 JSON（快速恢复用）
    json.dump(data, json_file)
    # 2. PG 写入（由 pg_store 完成，不耦合在 state.py 中）
```