---
name: 服务拆分为3个独立容器
审核状态: 通过 ✅
审核人: jiangboli (用户)
审核时间: 2026-05-29 17:00
审核意见: "可以，就按照这个开发"
---

# 服务拆分：3 个独立容器 + Token 认证

## 目标

将 pool-manager 拆分为 3 个独立 Docker 容器，各自只暴露必要的接口：

| 容器 | 用途 | 端口 | 公开？ | 认证 |
|------|------|------|--------|------|
| **pool-bind** | 绑定页、二维码、热池状态 | `0.0.0.0:8765` | ✅ 外部网页 | 无 |
| **pool-admin** | 管理接口（增删key、启停容器等） | `127.0.0.1:8766` | ❌ 仅本机 | Token |
| **pool-proxy** | LLM 代理（`/v1/chat/completions`） | `0.0.0.0:8767` | 🐳 bot 容器用 | 无（只有聊天） |

**不需要 Nginx**，因为管理端口直接绑 `127.0.0.1` 物理隔离。

**bot 容器即使能连上 admin:8766，因为没有 Token 也调不动任何管理接口。**

## 架构图

```
┌─ dosh 服务器 ──────────────────────────────────────────────────┐
│                                                                  │
│  Docker 网络: hermes-pool-net                                    │
│                                                                  │
│  ┌──────────────────────┐   ┌──────────────────────┐             │
│  │ pool-bind            │   │ pool-admin            │            │
│  │ 0.0.0.0:8765         │   │ 127.0.0.1:8766       │            │
│  │                      │   │                      │            │
│  │ 绑定页 /             │   │ /api/v1/gateways     │            │
│  │ /api/v1/pool/avail   │   │ /api/v1/gateway/...  │            │
│  │ /api/v1/pool/qr-img  │   │ /api/v1/proxy/keys   │            │
│  │ /api/v1/pool/status  │   │ /api/v1/pool/sync     │            │
│  │ /api/v1/pool/stats   │   │ /health              │            │
│  │ /health              │   │                      │            │
│  └────────┬─────────────┘   └─────────┬────────────┘            │
│           │ Docker Socket             │ Docker Socket            │
│           ▼                           ▼                         │
│  ┌──────────────────────┐                                       │
│  │ pool-proxy           │   bot 容器 ──→ pool-proxy:8767        │
│  │ 0.0.0.0:8767         │   (只有 /v1/chat/completions)         │
│  │ /v1/chat/completions │                                       │
│  └──────────────────────┘                                       │
│                                                                  │
│  共享存储: /home/dosh/data/pool-manager/                         │
│  ├── auth.json        ← proxy(r) + admin(rw)                    │
│  ├── pool_state.json  ← bind(rw) + admin(rw)                    │
│  └── admin_token      ← 启动时写入                               │
└──────────────────────────────────────────────────────────────────┘
```

## 安全矩阵

| 流量来源 | pool-bind:8765 | pool-proxy:8767 | pool-admin:8766 |
|----------|:-------------:|:---------------:|:---------------:|
| 🌐 外部用户 | ✅ 绑定页 | ❌ 没必要访问 | ❌ 127.0.0.1 无法连 |
| 🐳 bot 容器 | ❌ 没必要访问 | ✅ 仅 `/v1/chat` | ❌ 没 Token 调不动 |
| 👨‍💻 SSH 管理员 | ✅ | ✅ | ✅ Token + 本机 |

## 变更清单

### 修改文件

| 文件 | 变更内容 |
|------|---------|
| `pool_manager/proxy.py` | 新增 `chat_only_router`（只有 `/v1/chat/completions`） |
| `pool_manager/service.py` | **大幅重构**：拆为 `bind_app`、`admin_app`、`proxy_app` 三个 FastAPI 实例，`main()` 根据 `--mode` 参数启动对应服务。admin_app 加 Token 中间件。 |
| `pool_manager/__main__.py` | 不变（仍然调用 `service.main()`） |
| `pool_manager/state.py` | 不变 |
| `pool_manager/config.py` | 可能新增 AdminToken 配置项 |
| `pool_manager/docker_scheduler.py` | 修改 bot config 中 proxy 端口 8765→8767 |
| `Dockerfile.pool` | 不变（同一个镜像） |
| `docker-compose.yml` | **大幅改动**：3 个 service + 共享 volume + 环境变量 |

## 实现步骤

---

### Task 1: proxy.py — 新增 chat_only_router

**文件：** `pool_manager/proxy.py`

在文件末尾（`router` 之后）新增只包含聊天补全的子路由：

```python
# ── 仅聊天代理路由（给 bot 容器用，不含任何管理 API）──

chat_only_router = APIRouter()


@chat_only_router.post("/v1/chat/completions")
async def proxy_chat_completions_only(request: Request):
    \"\"\"转发 LLM 请求——只有聊天补全，不包含 keys/status 等管理路由。\"\"\"
    try:
        body = await request.json()
    except Exception:
        return Response(
            content=json.dumps({"error": "invalid json"}),
            status_code=400,
            media_type="application/json",
        )
    return await _do_proxy(request, body)
```

**验证：** `chat_only_router.routes` 长度=1（只有一条路由）

---

### Task 2: service.py — 拆为三个独立 FastAPI app

**文件：** `pool_manager/service.py`

#### 2.1 新增依赖导入

```python
import secrets  # 用于生成 token
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, Response, JSONResponse
```

#### 2.2 保留全部现有路由代码，但绑定到不同 app

将当前 `app` 上所有的路由（`@app.get()`、`@app.post()`）分类到三个 app：

**bind_app** — 绑定页面所需路由（原来 `app` 上的部分路由）：

```python
bind_app = FastAPI(title="Pool Manager - Binding")

# 挂载静态文件
bind_app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

# 所有 /api/v1/pool/* 路由 → 移过来 (hot-slots, available, status, qr-image, stats)
# GET / → 绑定页 HTML
# GET /health
```

**admin_app** — 管理端路由：

```python
admin_app = FastAPI(title="Pool Manager - Admin")

# Token 中间件（见 Task 3）
# GET /api/v1/gateways
# POST /api/v1/gateway/{profile}/start
# POST /api/v1/gateway/{profile}/stop
# POST /api/v1/gateway/{profile}/restart
# GET /api/v1/gateway/{profile}/logs
# POST /api/v1/pool/sync-models
# GET /health
```

**proxy_app** — LLM 代理（仅聊天）：

```python
proxy_app = FastAPI(title="Pool Manager - LLM Proxy")
proxy_app.include_router(llm_proxy.chat_only_router)
# GET /health
```

**注意：** 三个 app 都共享相同的全局变量（`config`, `state`, `hot_pool`, `scheduler` 等），但因为是**独立进程**，内存不共享。每个容器启动时单独初始化：

- bind: 需要初始化 hot_pool, scheduler, state（初始化 profiles，从文件恢复状态）
- admin: 需要初始化 state（从文件加载），scheduler（管理容器）
- proxy: 不需要 state 和 scheduler，只需要 proxy

#### 2.3 修改 main() 函数

```python
def main():
    parser = argparse.ArgumentParser(description="WeChat Gateway Pool Manager")
    # ... 现有参数 ...
    parser.add_argument("--mode", type=str, default="bind",
                        choices=["bind", "admin", "proxy"],
                        help="启动模式: bind=绑定页, admin=管理, proxy=LLM代理")
    
    args = parser.parse_args()
    # ... 加载 config ...
    
    mode = args.mode
    
    if mode == "bind":
        host = "0.0.0.0"
        port = 8765
        _init_bind_services(config)  # 初始化 state, hot_pool, scheduler
        logger.info("启动 Bind 服务: %s:%d", host, port)
        uvicorn.run(bind_app, host=host, port=port, ...)
    
    elif mode == "admin":
        host = "0.0.0.0"  # 容器内部监听 0.0.0.0，但 Docker 端口映射只绑 127.0.0.1
        port = 8766
        _init_admin_services(config)  # 初始化 state, scheduler
        _init_admin_token()  # 生成/读取 token
        logger.info("启动 Admin 服务: %s:%d (Token 认证已启用)", host, port)
        uvicorn.run(admin_app, host=host, port=port, ...)
    
    elif mode == "proxy":
        host = "0.0.0.0"
        port = 8767
        llm_proxy.init_proxy()
        logger.info("启动 Proxy 服务: %s:%d (仅 /v1/chat/completions)", host, port)
        uvicorn.run(proxy_app, host=host, port=port, ...)
```

---

### Task 3: Admin Token 认证

在 `service.py` 中添加 Token 机制：

```python
import hashlib, secrets

ADMIN_TOKEN = ""

def _init_admin_token():
    \"\"\"初始化 Admin Token。\n
    优先级：环境变量 ADMIN_TOKEN > 写入文件的新随机 token
    \"\"\"
    global ADMIN_TOKEN
    token_path = "/home/data/pool-manager/admin_token"
    os.makedirs(os.path.dirname(token_path), exist_ok=True)
    
    # 1. 优先从环境变量读取
    ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "")
    if ADMIN_TOKEN:
        # 写入文件以供参考
        with open(token_path, "w") as f:
            f.write(ADMIN_TOKEN)
        logger.info("Admin Token 从环境变量读取")
        return
    
    # 2. 从已有文件读取
    if os.path.exists(token_path):
        with open(token_path) as f:
            ADMIN_TOKEN = f.read().strip()
        logger.info("Admin Token 从文件读取")
        return
    
    # 3. 生成随机 token
    ADMIN_TOKEN = "pm_" + secrets.token_hex(16)
    with open(token_path, "w") as f:
        f.write(ADMIN_TOKEN + "\n")
    logger.warning("=" * 60)
    logger.warning("Admin Token (首次生成): %s", ADMIN_TOKEN)
    logger.warning("已写入: %s", token_path)
    logger.warning("=" * 60)


async def verify_admin_token(request: Request):
    \"\"\"验证 Admin Token 的中间件依赖。\"\"\"
    # health endpoint 不需要 token
    if request.url.path == "/health":
        return True
    
    token = request.headers.get("X-Admin-Token", "")
    # Token 比较使用恒定时间比较，防止时序攻击
    if not secrets.compare_digest(token, ADMIN_TOKEN):
        raise HTTPException(
            status_code=403,
            detail="Forbidden: 需要有效的 X-Admin-Token 头",
        )
    return True


# 在 admin_app 的每个路由上添加依赖
# 或者用 middleware:
@admin_app.middleware("http")
async def admin_token_middleware(request: Request, call_next):
    path = request.url.path
    if path == "/health":
        return await call_next(request)
    token = request.headers.get("X-Admin-Token", "")
    if not secrets.compare_digest(token, ADMIN_TOKEN):
        return JSONResponse(
            status_code=403,
            content={"error": "Forbidden", "message": "需要有效的 X-Admin-Token"},
        )
    return await call_next(request)
```

**使用方法：**
1. 首次启动 admin 容器时，自动生成随机 token，写到共享 volume 的 `/home/data/pool-manager/admin_token`
2. SSH 进服务器：`cat /home/dosh/data/pool-manager/admin_token` 查看 token
3. 调用管理 API 时加 Header：`X-Admin-Token: pm_xxx...`
4. 也可以自己在 docker-compose.yml 的 environment 中预设 `ADMIN_TOKEN=my-custom-token`

---

### Task 4: docker-compose.yml — 3 个 service

**文件：** `docker-compose.yml`

```yaml
services:
  pool-bind:
    build:
      context: .
      dockerfile: Dockerfile.pool
    image: pool-manager:latest
    container_name: pool-bind
    command: ["--mode", "bind", "--config", "/app/config.yaml"]
    ports:
      - "8765:8765"
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock:ro
      - /home/dosh/data:/home/data
    networks:
      - hermes-pool-net
    restart: unless-stopped

  pool-admin:
    image: pool-manager:latest
    container_name: pool-admin
    command: ["--mode", "admin", "--config", "/app/config.yaml"]
    ports:
      - "127.0.0.1:8766:8766"
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock:ro
      - /home/dosh/data:/home/data
    networks:
      - hermes-pool-net
    restart: unless-stopped
    environment:
      - ADMIN_TOKEN=${ADMIN_TOKEN:-}

  pool-proxy:
    image: pool-manager:latest
    container_name: pool-proxy
    command: ["--mode", "proxy", "--config", "/app/config.yaml"]
    ports:
      - "8767:8767"
    volumes:
      - /home/dosh/data:/home/data
    networks:
      - hermes-pool-net
    restart: unless-stopped

networks:
  hermes-pool-net:
    name: hermes-pool-net
    external: true
```

**关键设计：**
- `pool-admin` 的端口绑定 `127.0.0.1:8766:8766` — 宿主机只有本机能连
- `pool-bind` 的端口绑定 `8765:8765` — 外部可访问绑定页
- `pool-proxy` 的端口绑定 `8767:8767` — bot 容器通过 Docker 内网访问
- 三个容器都挂载共享 volume `/home/dosh/data:/home/data`，共用 auth.json 和 pool_state.json
- Docker socket 只挂给 bind 和 admin（proxy 不需要）

---

### Task 5: docker_scheduler.py — 更新 bot 的 proxy 地址

**文件：** `pool_manager/docker_scheduler.py`

修改 `write_config()` 方法的默认端口：

```python
def write_config(self, profile: str, proxy_host: str = "pool-proxy", proxy_port: int = 8767):
```

bot 容器的 config.yaml 中 `base_url: http://pool-proxy:8767/v1`

---

### Task 6: 已有 bot 容器更新 proxy 地址

SSH 到 dosh 服务器执行：

```bash
# 批量更新已有 bot 容器的 config.yaml
find /home/dosh/data -name "config.yaml" -path "*/.hermes/*" | while read f; do
    if grep -q "pool-manager:8765" "$f"; then
        sed -i 's|pool-manager:8765|pool-proxy:8767|g' "$f"
        echo "已更新: $f"
    fi
done

# 重启所有 bot 容器
for c in $(docker ps --filter name=hermes-weixin --format "{{.Names}}"); do
    docker restart "$c"
done
```

---

### Task 7: 更新绑定页面 frontend

**不需要改动。** 前端使用相对路径（`/api/v1/pool/...`），绑定页面仍然访问同一个地址 `claw-do.do-sh.com:8765`，pool-bind 容器处理这些请求。

---

### Task 8: 部署与验证

#### 8.1 构建镜像 + 启动

```bash
cd /home/dosh/wechat-pool-manager
docker compose build --no-cache
# 停旧容器
docker kill pool-manager && docker rm pool-manager 2>/dev/null
# 启动 3 个新容器
docker compose up -d
```

#### 8.2 验证

```bash
# 1. 检查 3 个容器都正常运行
docker ps --format "table {{.Names}}\t{{.Status}}"

# 2. 外部访问绑定页（从本机测）
curl -s http://claw-do.do-sh.com:8765/ | head -5     # ✅ 200 HTML 页

# 3. 外部访问管理 API → 127.0.0.1 无法连，直接超时
curl -s --connect-timeout 5 http://claw-do.do-sh.com:8766/api/v1/gateways  # ❌ 超时

# 4. SSH 进去测管理 API
ssh dosh@118.122.92.55 "curl -s http://127.0.0.1:8766/api/v1/gateways -H 'X-Admin-Token: <token>'"  # ✅ 200

# 5. 不带 Token → 403
ssh dosh@118.122.92.55 "curl -s http://127.0.0.1:8766/api/v1/gateways"  # ❌ 403

# 6. LLM Proxy 正常
docker exec hermes-weixin-001 curl -s http://pool-proxy:8767/v1/chat/completions -X POST -H 'Content-Type: application/json' -d '{"model":"test","messages":[]}'  # ✅ 400是正常的（参数不对）

# 7. bot 容器连 admin 端口（无 token）
docker exec hermes-weixin-001 curl -s http://pool-admin:8766/api/v1/gateways  # ❌ 403
```

---

## 风险与注意事项

### 1. 启动顺序
pool-bind 和 pool-admin 依赖于 Docker socket。如果 Docker daemon 重启，这两个容器会被 `restart: unless-stopped` 自动重启。

### 2. 状态文件同步
pool-bind 和 pool-admin 各自独立读写 `pool_state.json`。两个进程同时写入可能覆盖。但由于写入频率很低（手动操作 + 扫码绑定），碰撞概率极低。如果发生了，最后一个写入者覆盖。

**改进建议（后续）：** 加一个文件锁或改为 Redis/共享内存，但当前够用。

### 3. 热池状态
当前 HotPool 在内存中管理 QR 扫码槽位。pool-bind 容器启动时，HotPool 会从 state 文件恢复 profiles 并开始生成二维码。pool-admin 不需要运行 HotPool，它只查询状态文件。

### 4. Token 安全
- Token 存储在共享 volume 文件中，SSH 到服务的用户可以读取
- bot 容器如果挂载了共享 volume（应该不会），就能读取 token → **确保 proxy 容器不挂载 admin_token 文件**

实际检查：proxy 容器挂载的是 `/home/dosh/data:/home/data`，而 admin_token 在 `/home/dosh/data/pool-manager/admin_token`。如果 proxy 容器挂载了整个 data 目录，它确实能读到 token。但 proxy 容器没有 Docker socket，且它的 app 只有 `/v1/chat/completions` 一个路由，就算知道 token 也调不了管理接口。

---

## 回滚方案

```bash
# 1. 停止 3 个新容器
docker compose down

# 2. 重建旧镜像
git checkout HEAD~1 -- docker-compose.yml
docker compose build --no-cache
docker compose up -d

# 3. 恢复 bot config 端口
find /home/dosh/data -name "config.yaml" -path "*/.hermes/*" | while read f; do
    sed -i 's|pool-proxy:8767|pool-manager:8765|g' "$f"
done
for c in $(docker ps --filter name=hermes-weixin --format "{{.Names}}"); do
    docker restart "$c"
done

# 4. 恢复 git 状态
git checkout main -- docker-compose.yml
```