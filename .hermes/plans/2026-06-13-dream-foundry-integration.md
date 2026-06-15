---
审核状态: 通过 ✅
审核人: jreye (用户)
审核时间: 2026-06-13
审核意见: "好的，没问题了，执行吧"
---

# 追梦坊 AI API 集成方案

## 目标
将追梦坊（Dream Foundry）的 AI 能力（多模态 LLM + 视频生成）集成到现有 WeChat Pool Manager 架构中，使微信用户能：
1. 发送图片 → 多模态 LLM 分析理解
2. 发出指令 → 生成视频/图片
3. API Key 统一在 `api_keys` 表管理，微信容器 0 接触

---

## 一、现状与问题

### 1.1 当前架构

```
WeChat 容器 (Hermes) → pool-proxy (:8767) → deepseek / alibaba
                         ↑
                  api_keys 表 (PG)
                  provider = deepseek / alibaba
```

### 1.2 模型锁定问题（需修复）

目前有两处硬编码导致模型被锁死：

**① docker_scheduler.py:170 — 容器 config 生成**
```python
# 每个 WeChat 容器启动时生成的 config.yaml
model:
  provider: custom:pool-proxy
  model: deepseek-v4-flash    # ← 写死，全容器统一
```

**② proxy.py:468 — 模型→Provider 路由**
```python
def _get_provider_for_model(model):
    # 只读宿主机 ~/.hermes/config.yaml
    # 不管请求体里的 model 是什么，全转去 deepseek
    return "deepseek"  # 等价于这个效果
```

**后果：** 即使 WeChat 容器发 `model=doubao-seed-2-0-lite-260215`，pool-proxy 依然路由到 deepseek。

---

## 二、目标架构

```
WeChat 容器 (Hermes) → pool-proxy (:8767) ← 统一网关
                         │
                         ├── /v1/chat/completions
                         │     ├── model=deepseek-*   → deepseek
                         │     ├── model=doubao-*     → 追梦坊 LLM（多模态）
                         │     └── model=doubao-seedance* → 追梦坊 LLM
                         │
                         ├── /v1/video/generations ← 新增
                         │     POST → 追梦坊视频生成API (异步)
                         │
                         └── /v1/video/generations/{task_id} ← 新增
                               GET → 轮询视频生成状态
                         
                         所有 key 在 PG api_keys 表统一管理
```

---

## 三、变更清单

### 3.1 proxy.py — 模型路由表（核心改动）

**A. `_get_provider_for_model()` 替换为显式路由表**

```python
def _get_provider_for_model(model: str) -> str:
    """根据模型名路由到正确的 provider。"""
    MODEL_ROUTES = {
        # 现有 LLM
        "deepseek-v4-flash": "deepseek",
        "deepseek-chat": "deepseek",
        "deepseek-reasoner": "deepseek",
        # 追梦坊多模态 LLM
        "doubao-seed-2-0-lite-260215": "dream-foundry",
        "doubao-seed-2-0-pro-260215": "dream-foundry",
        # 追梦坊视频生成
        "doubao-seedance-2.0-fast": "dream-foundry",
    }
    return MODEL_ROUTES.get(model, _config["default_provider"])
```

**B. `_get_base_url()` 加 dream-foundry 映射**

```python
def _get_base_url(provider: str) -> str:
    base_urls = {
        "deepseek": "https://api.deepseek.com/v1",
        "alibaba": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "dream-foundry": "https://ai-api.dreamf.art/v1",  # ← 新增
    }
    ...
```

**C. 新增视频生成路由**

```python
@router.post("/v1/video/generations")
async def create_video_generation(request: Request):
    """提交视频生成任务。"""
    body = await request.json()
    api_key = _select_key("dream-foundry")
    if not api_key:
        return Response(
            content=json.dumps({"error": "dream-foundry 无可用 key"}),
            status_code=502, media_type="application/json",
        )
    client = _get_client()
    resp = await client.post(
        f"{_get_base_url('dream-foundry')}/v1/video/generations",
        json=body,
        headers={"Authorization": f"Bearer {api_key}"},
    )
    return Response(content=resp.text, status_code=resp.status_code,
                    media_type="application/json")


@router.get("/v1/video/generations/{task_id}")
async def get_video_status(task_id: str):
    """查询视频生成进度。"""
    api_key = _select_key("dream-foundry")
    if not api_key:
        return Response(
            content=json.dumps({"error": "dream-foundry 无可用 key"}),
            status_code=502, media_type="application/json",
        )
    client = _get_client()
    resp = await client.get(
        f"{_get_base_url('dream-foundry')}/v1/video/generations/{task_id}",
        headers={"Authorization": f"Bearer {api_key}"},
    )
    return Response(content=resp.text, status_code=resp.status_code,
                    media_type="application/json")
```

**D. 路由注册**

当前 `proxy_app` 注册的是 `chat_only_router`（只有 `/v1/chat/completions`）。

```
proxy_app.include_router(llm_proxy.chat_only_router)
```

视频端的路由加到 `router` 上（admin 端可访问），或者 `chat_only_router` 上也加——取决于是否让 bot 容器直接调视频接口。

**建议：** 视频路由加到 `router`（admin 端）和 `chat_only_router`（bot 端）两处都加。bot 容器可以直接通过 pool-proxy 调视频，无需绕过。

### 3.2 docker_scheduler.py — 不改（容器默认模型不变）

每个 WeChat 容器的 `config.yaml` 中 `model: deepseek-v4-flash` 保持不变——这是 agent 日常聊天的默认模型。

当 agent 需要调追梦坊时（通过技能识别出用户意图），发送请求时显式指定 `model=doubao-seed-2-0-*`，pool-proxy 按路由表分流。

### 3.3 api_keys 表 — 加记录

在 PG `api_keys` 表加一条记录：

| provider | access_token | label | machine_ip | is_active |
|----------|-------------|-------|------------|-----------|
| dream-foundry | sk-uYNHBP... | 追梦坊主Key | 118.122.92.55 | 1 |

Pool-proxy 启动时自动从 PG 加载（每 30 秒刷新），无需手动操作。

### 3.4 Hermes 技能（WeChat 容器侧）

在 WeChat 容器的 skills 目录里放一个轻量技能文件（不含 API key）：

```markdown
# dream-foundry 技能

## 场景1：图片分析/文件解读
当用户发送图片、文件，或要求分析图片内容时：
1. 调 pool-proxy 的 /v1/chat/completions
2. model = "doubao-seed-2-0-lite-260215"
3. messages 中包含 image_url（图片URL或base64）

## 场景2：视频/图片生成
当用户要求生成视频时：
1. POST pool-proxy 的 /v1/video/generations
2. model = "doubao-seedance-2.0-fast"
3. prompt 中使用 @图1 引用参考图
4. 得到 task_id 后轮询 GET /v1/video/generations/{task_id}
5. 视频生成完成后推送给用户
```

技能不含任何 Key，只告诉 agent 怎么调 pool-proxy。

---

## 四、数据流详解

### 4.1 图片理解流程

```
用户发图片到微信
  ↓
WeChat 容器 (Hermes Agent)
  ↓ skill 识别 → "需要多模态分析"
  ↓ 调 pool-proxy，model=doubao-seed-2-0-lite
  ↓
Pool-Proxy
  ↓ _get_provider_for_model("doubao-seed-2-0-lite-260215")
  ↓ → "dream-foundry"
  ↓ _select_key("dream-foundry") → 从内存凭证池拿 key
  ↓ POST https://ai-api.dreamf.art/v1/chat/completions
  ↓ 透传 response 流式返回
  ↓
Agent 收到分析结果 → 回复用户
```

### 4.2 视频生成流程

```
用户：帮我生成一个装修视频，参考这两张图
  ↓
Agent skill 识别 → "视频生成"
  ↓ POST pool-proxy /v1/video/generations
  ↓ 透传到追梦坊
  ↓ 返回 {"task_id": "cgt-2026xxx", "status": "pending"}
  ↓
Agent 告知用户 "正在生成视频..."
  ↓ 后台轮询 GET /v1/video/generations/{task_id}
  ↓ 每 10 秒查一次
  ↓
生成完成 → 拿到视频 URL
  ↓ Agent 下载 → 推送给用户
```

---

## 五、安全分析

| 威胁 | 防护 |
|------|------|
| WeChat bot 获取 API key | Key 只在 pool-proxy 内存，bot 容器零接触 |
| 视频接口被滥用 | 受 pool-proxy 速率限制保护（同 chat/completions） |
| 多模态分析消耗超额 Key | dream-foundry 的单 key 熔断保护复用现有机制 |
| 小白用户发视频生成请求 | API key 在 PG 统一管理，不写入任何文件 |

---

## 六、实施步骤

| 步骤 | 文件 | 说明 |
|------|------|------|
| 1 | `pool_manager/proxy.py` | 改 `_get_provider_for_model` 模型路由表 |
| 2 | `pool_manager/proxy.py` | 改 `_get_base_url` 加 dream-foundry |
| 3 | `pool_manager/proxy.py` | 新增 `/v1/video/generations` + `/{task_id}` 路由 |
| 4 | `pool_manager/service.py` | 视需要更新路由注册 |
| 5 | PG | 加 dream-foundry API key 到 `api_keys` 表 |
| 6 | `~/.hermes/skills/dream-foundry/SKILL.md` | 写 Hermes 技能（不含 key） |
| 7 | 部署 | 重启 pool-proxy，测试全流程 |

---

## 七、验证方式

1. **模型路由验证：** 调 pool-proxy `/v1/chat/completions` 带 `model=doubao-seed-2-0-lite-260215` → 返回追梦坊 LLM 结果
2. **图片理解验证：** 发包含 `image_url` 的请求 → 正确描述图片内容
3. **视频生成验证：** POST `/v1/video/generations` → 返回 `task_id` → 轮询 → 返回视频 URL
4. **Key 安全验证：** WeChat 容器内 `env | grep -i key` 无 dream-foundry key
