---
name: wechat-pool-manager-architecture
version: 2.0.0
---

# WeChat Gateway Pool Manager — 架构方案 v2

## 架构总览

```
┌─────────────────────────────────────────────────────────┐
│                Pool Manager (:8765)                      │
│  ┌──────────────────────────────────────────────────┐   │
│  │  FastAPI Service                                 │   │
│  │  ┌──────────┐  ┌──────────┐  ┌──────────────┐   │   │
│  │  │ 热池引擎  │  │ 用户管理  │  │ LLM Proxy    │   │   │
│  │  │ HotPool  │  │ UserMgmt │  │ /v1/chat/... │   │   │
│  │  └──────────┘  └──────────┘  └──────┬───────┘   │   │
│  │                                      │           │   │
│  │                             credential pool      │   │
│  │                             25 deepseek keys     │   │
│  │                             round_robin          │   │
│  └──────────────────────────────────────────────────┘   │
│                    ▲ dosh 用户进程                        │
└────────────────────┼────────────────────────────────────┘
                     │
        ┌────────────┼────────────┐
        ▼            ▼            ▼
┌────────────┐ ┌────────────┐ ┌────────────┐
│ wx001 gw  │ │ wx002 gw  │ │ wx003 gw  │ ...
│ Linux user │ │ Linux user │ │ Linux user │
│ :8001      │ │ :8002      │ │ :8003      │
│ base_url=  │ │ base_url=  │ │ base_url=  │
│ localhost  │ │ localhost  │ │ localhost  │
│ :8765/v1   │ │ :8765/v1   │ │ :8765/v1   │
└────────────┘ └────────────┘ └────────────┘
     │              │              │
     └──────────────┼──────────────┘
                    ▼
             微信消息              ← iLink API
```

## 核心组件

### 1. 热池引擎 (HotPool)

- **管理对象：** Linux 用户（非 Hermes profile）
- **功能：** 保持 N 个 QR 码槽位在线，用户扫码即绑定
- **去重：** 同一个微信用户二次扫码 → 更新凭证 + 重启 gateway，不创建冗余用户
- **创建方式：** `useradd wx001 + 写空壳 ~/.hermes/config.yaml`，不再 `hermes profile create`

### 2. Linux 用户管理 (UserMgmt)

每个微信用户对应一个独立 Linux 用户，Unix 文件权限实现用户间数据隔离：

```
/home/wx001/.hermes/
├── .env          → 仅微信凭证（account_id, token），无 API key
├── config.yaml   → platforms + model（无工具集限制）
│                     model.provider = custom
│                     model.base_url = http://127.0.0.1:8765/v1
│                     model.api_key: 不需要填
├── auth.json     → 不需要（proxy 管 key）
└── logs/
```

**工具集限制：** 全部不设（`platform_toolsets` 和 `disabled_toolsets` 都不写），wx 用户有完整权限。

### 3. LLM Proxy（核心安全组件）

- **位置：** pool manager 内的一个 FastAPI 路由（30 行代码）
- **功能：** 接收 wx 用户的 `/v1/chat/completions` 请求
  - 读取请求中的 `model` 字段
  - 读对应的 credential pool（如 deepseek 的 25 keys）
  - round_robin 选一个 key
  - 转发到真实 API（`api.deepseek.com/v1`）
  - 流式返回结果
- **API key 生命周期：** 只在 pool manager 进程内存中，永不落盘
- **性能：** 异步非阻塞（asyncio），500 用户同时请求不排队

```
客户端请求 → proxy (asyncio.await) → deepseek API (5s)
                     ↑                ↑
client2 → proxy (asyncio.await) → deepseek API (5s)
             不排队，同时处理
```

### 4. 凭证池（Credential Pool）

- 25 个 deepseek API keys，存于 dosh 的 `~/.hermes/auth.json`
- proxy 运行时从 auth.json 读取到内存
- round_robin 分配，一个 key 被限流自动换下一个
- 加 key/删 key → pool manager 热重载（不需要重启）

### 5. 模型切换（换 provider）

改了 `~/.hermes/config.yaml` 的模型配置后，proxy 自动感知（每请求读一次 config，开销忽略不计）。

同时需要同步 wx 用户的 `config.yaml` 中的 `model.default`（影响 Hermes 的行为），通过一条命令：

```bash
python3 -c "from pool_manager.gateway_manager import sync_model_config; sync_model_config('deepseek-v3')"
```

也可以调 API：`POST /api/v1/pool/sync-models`

## 改动清单

| 文件 | 操作 | 说明 |
|------|------|------|
| `pool_manager/hot_pool.py` | 修改 | `_on_confirmed()` 删除 `api_env`；新增微信用户去重逻辑 |
| `pool_manager/profile_manager.py` | 修改 | `setup_linux_profile()` 改为只写微信凭证 + platforms；不写 API key、工具集限制；不再需要 Hermes profile |
| `pool_manager/gateway_manager.py` | 修改 | 新增 `sync_credential_pool()`、`sync_model_config()`；新增 `update_credentials()` |
| `pool_manager/proxy.py` | **新建** | OpenAI 兼容的 LLM 转发代理，读取 credential pool，round_robin 分配 key |
| `pool_manager/service.py` | 修改 | 启动时注册 proxy 路由 |
| `pool_manager/state.py` | 修改 | 新增 `get_linux_user_by_user_id()`、`record_binding()` |
| `scripts/create_profiles.py` | 修改 | 从 `hermes profile create` 改为 `useradd + 写空壳 config.yaml` |
| `scripts/setup.sh` | 修改 | 部署时自动同步凭证池到 proxy |

## 安全设计

| 威胁 | 防护 |
|------|------|
| wx001 读 wx002 的数据 | Linux 用户隔离，不可穿越 `/home/` |
| wx 用户获取 API key | key 在 pool manager 进程内存，不落盘 |
| 25 keys 负载 | round_robin 分配 |
| proxy 单点瓶颈 | asyncio 异步非阻塞，500 用户并行不排队 |
| 模型切换 | proxy 实时读取 config + 一条命令同步 wx 用户 |

## 部署流程（新机器）

```bash
git clone https://github.com/jiangboli/wechat-pool-manager.git
cd wechat-pool-manager
bash scripts/setup.sh --total 100 --hot-pool 5 --max-bound 80
```

setup.sh 自动：
1. 安装 Hermes
2. 创建 Linux 用户（100 个）
3. 创建热池槽位（5 个）
4. 启动 pool manager（含 proxy）
5. 凭证池同步
