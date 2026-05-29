---
name: 安全隔离-端口分离与反向代理
审核状态: 待审核
审核人:
审核时间:
---

# 安全隔离：端口分离 + Nginx 反向代理

## 目标

将 pool-manager 的 LLM Proxy（bot 容器使用）和管理 API（管理员使用）**物理隔离到不同端口**，外部公网只能访问绑定页面必需的接口，杜绝敏感 API 被外部或 bot 容器恶意访问的风险。

## 架构变化

```
部署前：
  pool-manager:8765 (0.0.0.0)
    ├── /v1/chat/completions  ← bot 容器用
    ├── /api/v1/gateways      ← 危险！bot也能访问
    ├── /api/v1/gateway/...   ← 危险！bot也能访问
    └── / (绑定页面)           ← 外部用户能访问

部署后：
  端口 8766 (0.0.0.0) — LLM Proxy Only
    └── /v1/chat/completions  ← 只有 bot 容器能访问

  端口 8765 (127.0.0.1) — 全部管理API（只有本机能连）
    └── 所有 API（完整功能）

  Nginx :8765 (0.0.0.0) — 外部用户入口
    ├── ✅ /                 → 代理到 127.0.0.1:8765
    ├── ✅ /api/v1/pool/*    → 代理到 127.0.0.1:8765
    ├── ✅ /health           → 代理到 127.0.0.1:8765
    └── ❌ 其他路径           → 403 Forbidden
```

## 安全矩阵

| 流量来源 | 能访问哪些接口 |
|----------|--------------|
| 🌐 外部用户 (`claw-do.do-sh.com:8765`) | 绑定页 `/` + `/api/v1/pool/available\|status\|qr-image\|stats` + `/health` |
| 🐳 hermes-bot 容器 (`pool-manager:8766`) | 只有 `/v1/chat/completions` |
| 👨‍💻 管理员（SSH后 127.0.0.1:8765） | 全部（gateways、gateway管理、proxy key管理、同步等） |

## 变更清单

### 修改文件

| 文件 | 变更内容 |
|------|---------|
| `pool_manager/service.py` | 新增 `llm_app`（仅 `/v1/chat/completions`）、main 入口启动双进程 |
| `pool_manager/proxy.py` | 新增 `chat_only_router`（仅聊天代理路由） |
| `pool_manager/docker_scheduler.py` | 修改 bot config 中 proxy 端口 8765→8766 |
| `Dockerfile.pool` | 新增 EXPOSE 8766、CMD 改为 shell 脚本启动双进程 |
| `docker-compose.yml` | 8765 绑定 127.0.0.1、新增 8766 端口映射 |

### 新增文件

| 文件 | 用途 |
|------|------|
| 服务器（dosh）Nginx 配置 `/etc/nginx/sites-available/pool-manager` | 反向代理 + 路径白名单 |

### 暂不删除/不改

- `pool_manager/state.py` — 不变
- `pool_manager/hot_pool.py` — 不变
- `pool_manager/gateway_manager.py` — 不变
- `static/index.html` — 不变（前端代码兼容）

## 实现步骤

---

### Task 1: proxy.py 新增 chat_only_router

**文件：** `pool_manager/proxy.py`

在文件末尾（router 定义之后）新增：

```python
# ── 仅转发代理路由（给 bot 容器用，不含管理 API）──

chat_only_router = APIRouter()


@chat_only_router.post("/v1/chat/completions")
async def proxy_chat_completions_only(request: Request):
    \"\"\"转发 LLM 请求——只有聊天补全，不含管理 API。\"\"\"
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

**验证：**
- `python -c "from pool_manager.proxy import chat_only_router; print(len(chat_only_router.routes))"` → 输出 1（只有一条路由）

---

### Task 2: service.py 创建 llm_app + 双进程启动

**文件：** `pool_manager/service.py`

#### 2.1 在 `app` 定义之后，新增 llm_app：

```python
# ── LLM Proxy 只读应用（给 bot 容器用，不含管理 API）──
llm_app = FastAPI(title="Pool Manager - LLM Proxy Only")
llm_app.include_router(llm_proxy.chat_only_router)
```

（加在 `llm_proxy.init_proxy()` 调用之前，约 line 57 之后）

#### 2.2 修改 `main()` 函数，启动双进程：

```python
def main():
    parser = argparse.ArgumentParser(description="WeChat Gateway Pool Manager (Docker)")
    # ... 已有的参数 ...
    parser.add_argument("--proxy-port", type=int, default=None, help="LLM Proxy 端口")

    args = parser.parse_args()
    # ... 已有的配置加载 ...

    host = args.host or config.get("frontend", {}).get("host", "0.0.0.0")
    port = args.port or config.get("frontend", {}).get("api_port", 8765)
    proxy_port = args.proxy_port or config.get("frontend", {}).get("proxy_port", 8766)

    logger.info("启动 Pool Manager v3 (安全隔离模式)")
    logger.info("  管理 API:  %s:%d", host, port)
    logger.info("  LLM Proxy: 0.0.0.0:%d", proxy_port)

    import multiprocessing
    p1 = multiprocessing.Process(target=uvicorn.run,
                                  args=(app,),
                                  kwargs={"host": "127.0.0.1", "port": port,
                                          "log_level": config.get("logging", {}).get("level", "info").lower()},
                                  daemon=True)
    p2 = multiprocessing.Process(target=uvicorn.run,
                                  args=(llm_app,),
                                  kwargs={"host": "0.0.0.0", "port": proxy_port,
                                          "log_level": config.get("logging", {}).get("level", "info").lower()},
                                  daemon=True)
    p1.start()
    p2.start()
    logger.info("双进程已启动，等待...")
    p1.join()
    p2.join()
```

**注意：** 使用 `multiprocessing` 是因为 asyncio 事件循环不能直接在同一个进程跑两个 uvicorn。
需在文件顶部增加 `import multiprocessing`。

---

### Task 3: 更新 Dockerfile.pool

**文件：** `Dockerfile.pool`

```dockerfile
FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt docker

COPY pool_manager/ /app/pool_manager/
COPY static/ /app/static/
COPY config.yaml /app/config.yaml

EXPOSE 8765
EXPOSE 8766

CMD ["python", "-m", "pool_manager", "--config", "/app/config.yaml", "--host", "127.0.0.1", "--proxy-port", "8766"]
```

变更：
- 新增 `EXPOSE 8766`
- CMD 中指定 `--host 127.0.0.1 --proxy-port 8766`（管理 API 只在本地监听，proxy 开放在 `0.0.0.0:8766`）

---

### Task 4: 更新 docker-compose.yml

**文件：** `docker-compose.yml`

```yaml
services:
  pool-manager:
    # ... 现有配置 ...
    ports:
      - "127.0.0.1:8765:8765"    # 管理 API — 只本机可连
      - "8766:8766"               # LLM Proxy — bot 容器用
    # ... 其余不变 ...
```

**验证：**
- `docker compose config` 输出中端口映射正确

---

### Task 5: 更新 bot 容器的 proxy 端口

**文件：** `pool_manager/docker_scheduler.py`

修改 `write_config()` 方法中的 proxy_port 默认值：

```python
# 第 145 行附近
def write_config(self, profile: str, proxy_host: str = "pool-manager", proxy_port: int = 8766):
```

同时需要将所有**已有 bot 容器**的 config.yaml 中的端口从 8765 更新为 8766（见 Task 8）。

**验证：**
- 新创建容器后，`cat /home/dosh/data/{尾数}/{profile}/.hermes/config.yaml` 中 `base_url: http://pool-manager:8766/v1`

---

### Task 6: 在 dosh 服务器安装 Nginx + 配置

**操作：** SSH 到 dosh 服务器执行

```bash
# 安装 Nginx
sudo apt update && sudo apt install -y nginx

# 配置
sudo cat > /etc/nginx/sites-available/pool-manager << 'NGINXEOF'
server {
    listen 8765;
    server_name claw-do.do-sh.com;

    # 绑定页面
    location / {
        proxy_pass http://127.0.0.1:8765;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    }

    # 绑定页所需的公开 API
    location /api/v1/pool/ {
        proxy_pass http://127.0.0.1:8765;
        proxy_set_header Host $host;
    }

    location /health {
        proxy_pass http://127.0.0.1:8765;
        proxy_set_header Host $host;
    }

    # 所有其他 API 路径 → 403
    location /api/ {
        return 403;
    }

    location /v1/ {
        return 403;
    }
}
NGINXEOF

# 启用站点
sudo ln -sf /etc/nginx/sites-available/pool-manager /etc/nginx/sites-enabled/
sudo rm -f /etc/nginx/sites-enabled/default
sudo nginx -t && sudo systemctl reload nginx
```

**注意事项：**
- dosh 服务器上 Nginx 是否可用需 SSH 上去检查（之前没装过）
- Nginx 监听 8765 端口，但 pool-manager 也需要 8765（127.0.0.1），两者不冲突（不同 interface）
- Nginx 配置 `/api/` 的 deny-all 必须在 `/api/v1/pool/` 之后（Nginx 按最长前缀匹配）

---

### Task 7: 部署 pool-manager 新镜像

```bash
# 在 dosh 服务器执行
cd /home/dosh/wechat-pool-manager
docker compose build --no-cache pool-manager
docker kill pool-manager && docker rm pool-manager
docker compose up -d pool-manager
```

**验证：**
- `docker logs pool-manager --tail 5`
  - 应看到 `管理 API: 127.0.0.1:8765`
  - 应看到 `LLM Proxy: 0.0.0.0:8766`

---

### Task 8: 更新已有 bot 容器的 config.yaml + 重启

**已被绑定的 bot 容器**的 config.yaml 中 `proxy_port` 还是旧的 8765，需要批量更新。

**方法：** SSH 到 dosh 服务器，遍历所有数据目录，将 config.yaml 中的 `8765/v1` 替换为 `8766/v1`：

```bash
# 查找所有 bot 容器的 config.yaml
find /home/dosh/data -name "config.yaml" -path "*/.hermes/*" | while read f; do
    # 检查是否包含旧的 proxy 端口
    if grep -q "pool-manager:8765" "$f"; then
        sed -i 's|pool-manager:8765|pool-manager:8766|g' "$f"
        echo "已更新: $f"
    fi
done

# 重启所有 bot 容器
for container in $(docker ps --filter name=hermes-weixin --format "{{.Names}}"); do
    echo "重启: $container"
    docker restart "$container"
done
```

**验证：**
- 随机抽查几个容器的日志：`docker logs hermes-weixin-001 --tail 5` 应看到 LLM 请求正常

---

### Task 9: 测试安全隔离

#### 9.1 从外部（本机）测试公开 API

```bash
# 公开 API → 应通过
curl -s http://claw-do.do-sh.com:8765/                        # → 200 (绑定页)
curl -s http://claw-do.do-sh.com:8765/api/v1/pool/stats        # → 200
curl -s http://claw-do.do-sh.com:8765/api/v1/pool/available    # → 200

# 敏感 API → 应 403
curl -s http://claw-do.do-sh.com:8765/api/v1/gateways          # → 403
curl -s -X POST http://claw-do.do-sh.com:8765/api/v1/gateway/...  # → 403

# Proxy API (bot路径) → 应 403
curl -s http://claw-do.do-sh.com:8765/v1/chat/completions      # → 403
```

#### 9.2 从服务器本地测试管理 API

```bash
ssh dosh@118.122.92.55 "..."
curl -s http://127.0.0.1:8765/api/v1/gateways                  # → 200
```

#### 9.3 从 bot 容器测试 LLM Proxy

```bash
docker exec hermes-weixin-001 curl -s http://pool-manager:8766/v1/chat/completions -X POST -d '{}'  # → 400 (正常，缺参数)
docker exec hermes-weixin-001 curl -s http://pool-manager:8766/api/v1/gateways  # → 404（路由不存在）
```

---

## 风险与注意事项

### 1. 双进程退出风险
`multiprocessing.Process` 中一个进程崩溃不会自动重启另一个。pool-manager 容器已有 `restart: unless-stopped`，容器重启时两个进程都会重新启动。

### 2. 已有 bot 容器更新顺序
必须先更新 pool-manager（开放 8766 端口）→ 再更新 bot 容器的 config.yaml + 重启。如果先重启 bot（还连旧端口），bot 会连不上 proxy。

### 3. 启动时序
pool-manager 容器启动时需要时间初始化 LLM Proxy（加载 auth.json）。如果 bot 容器在 pool-manager 完全启动前就尝试连接 8766，可能会失败。但 bot 容器有 Hermes 的重试机制，短时间不可用后会自动恢复。

### 4. Nginx 安装依赖
dosh 服务器需 `sudo apt install nginx`。如果 apt 卡住（香港服务器网络），用 `apt install -y --no-install-recommends nginx-light`（更小）。

## 回滚方案

如果部署后出问题：

```bash
# 恢复 docker-compose.yml
git checkout main -- docker-compose.yml

# 恢复 docker_scheduler.py
git checkout main -- pool_manager/docker_scheduler.py

# 重建 + 重启
docker compose build --no-cache pool-manager
docker kill pool-manager && docker rm pool-manager
docker compose up -d pool-manager

# 恢复 bot config.yaml 的端口
find /home/dosh/data -name "config.yaml" -path "*/.hermes/*" | while read f; do
    sed -i 's|pool-manager:8766|pool-manager:8765|g' "$f"
done

# 重启所有 bot
docker restart $(docker ps --filter name=hermes-weixin --format "{{.Names}}")

# 恢复 Nginx 配置（如果 Nginx 改坏了）
rm /etc/nginx/sites-enabled/pool-manager
systemctl reload nginx
```