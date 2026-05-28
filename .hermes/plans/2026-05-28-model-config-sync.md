---
name: model-config-sync
审核状态: 通过 ✅
审核人: jiangboli (用户)
审核时间: 2026-05-28
审核意见: 待确认
---

# 凭证池同步机制 — 开发计划（修订版）

## 目标

让每个微信用户都能用上 dosh 主账号的 25 个 deepseek API key 凭证池（round_robin 负载均衡），同时确保 API key 不被微信用户直接读取。

## 架构

```
dosh 凭证池（25 keys）   ──sync──→  每个 wx 用户 ~/.hermes/auth.json
                                          │
                                   load_pool("deepseek")
                                          │
                                  round_robin 分配 key
                                          │
                                   LLM 调用正常 ✅
```

**用户端文件：**

| 文件 | 内容 | 安全说明 |
|------|------|---------|
| `~/.hermes/.env` | 只有微信凭证（account_id, token） | ✅ 无 API key |
| `~/.hermes/config.yaml` | platforms.weixin + model（provider, default, base_url） | ✅ 只有模型名，无 key |
| `~/.hermes/auth.json` | credential_pool.deepseek (25 keys) | ⚠️ 用户可读，但这是必需的——否则用不了 LLM |

**为什么 model 段可以留在 config.yaml？**

model 段只指定：
```yaml
model:
  default: deepseek-v4-flash    # 模型名，不是 key
  provider: deepseek             # 提供商名
  base_url: https://api.deepseek.com/v1  # API 地址
```

这些信息不包含任何凭据，公开也无妨。API key 存在 credential pool 里，由 Hermes 的 `load_pool()` 管理。

## 变更清单

| 文件 | 操作 | 改动 |
|------|------|------|
| `pool_manager/hot_pool.py` | 修改 | `_on_confirmed()` 不再读 `DEEPSEEK_API_KEY` 传进 `api_env` |
| `pool_manager/profile_manager.py` | 修改 | `setup_linux_profile()` 新增：复制凭证池到 wx 用户 auth.json |
| `pool_manager/gateway_manager.py` | 修改 | 新增 `sync_credential_pool()` 方法 |
| `pool_manager/pool_api.py` | 修改 | 新增 `POST /api/v1/pool/sync-models` API 端点 |
| `scripts/setup.sh` | 修改 | 部署时自动执行一次凭证池同步 |

**不涉及：** systemd 模板、`/etc/` 目录、环境变量注入

## 实现步骤

### Step 1: 修复安全漏洞 — `hot_pool.py`

删除从环境变量读 API key 的代码。`setup_linux_profile` 不再接收 `api_env` 参数。

### Step 2: 凭证池同步 — `gateway_manager.py`

新增 `sync_credential_pool()`：

```python
def sync_credential_pool() -> Tuple[int, str]:
    """复制 dosh 的凭证池到所有已创建的 wx 用户。
    
    流程：
    1. 读取 /home/dosh/.hermes/auth.json 的 credential_pool 段
    2. 遍历 /home/wx* 存在的用户
    3. 对每个用户：
       a. 读用户现有的 auth.json（或空 dict）
       b. 注入 credential_pool.deepseek（保留用户原有的其他 provider）  
       c. 写入临时文件 → sudo cp → sudo chown
    4. 返回更新的用户数
    
    为什么用 sudo cp：auth.json 是 wx 用户所有的文件，
    dosh 用户不能直接写。
    """
```

### Step 3: 新用户创建时自动同步 — `profile_manager.py`

在 `setup_linux_profile()` 末尾新增：

```python
# 5. 同步凭证池
from . import gateway_manager as gm
gm.sync_credential_pool_for_user(luser)
```

### Step 4: API 触发同步 — `pool_api.py`

```python
@router.post("/api/v1/pool/sync-models")
async def sync_models():
    """一键同步凭证池到所有微信用户。"""
    count, msg = gateway_manager.sync_credential_pool()
    return {"synced": count, "message": msg}
```

### Step 5: 一键部署 — `scripts/setup.sh`

在 [5/6] 创建 profile 之后新增：

```bash
# [5.5/6] 同步凭证池
python3 -c "
from pool_manager.gateway_manager import sync_credential_pool
n, msg = sync_credential_pool()
print(f'  ✅ 已同步 {n} 个用户的凭证池')
"
```

### Step 6: 清理旧数据（部署时自动执行）

在 dosh 服务器上已存在的 wx 用户：
- 检查 `.env` 中是否有 `DEEPSEEK_API_KEY=` 或 `API_KEY=` → 如果有，删除该行
- 检查 `config.yaml` 中是否有 model 段 → 如果没有，补上（已存在的用户有 model 段，新用户由 profile_manager 写入）

## 风险和注意事项

| 风险 | 应对 |
|------|------|
| auth.json 被 wx 用户修改 | 凭证池的数据来源是 dosh 的 auth.json，wx 用户改自己的不影响 dosh。下次 sync 会覆盖回去 |
| 新加 key 到凭证池 | 跑一下 `POST /sync-models` 或手动 `python3 -c "from pool_manager.gateway_manager import sync_credential_pool; sync_credential_pool()"` |
| 已存在的 wx 用户无 auth.json | `sync_credential_pool` 自动创建（空的 dict → 注入凭证池） |

## 验证方式

1. **安全验证** — 新绑定用户后，`sudo cat /home/wx00X/.hermes/.env` 确认无 `DEEPSEEK_API_KEY`
2. **凭证池可用** — `sudo cat /home/wx00X/.hermes/auth.json` 确认有 `credential_pool.deepseek` 包含 25 个 keys
3. **LLM 调用** — 向 wx 用户发消息，确认正常回复，日志无 "no provider configured"
4. **限流验证** — 给 500 个用户发消息，观察是否有 rate limit 错误（25 keys round_robin 应无问题）
5. **同步验证** — 删掉凭证池一个 key → 调 `/sync-models` → 检查 wx 用户 auth.json 已同步
