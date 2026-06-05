---
name: fix-topic-classification-streaming
审核状态: 通过 ✅
审核人: jreye (用户)
审核时间: 2026-06-04 17:30 UTC
审核意见: "执行"
---

# Topic 分类修复 + 代码同步计划

## 目标

修复 `_reasoning_buf` 在流式模式下不积累的 bug，解决 DB 中 topic 与微信元数据不一致的问题。同时将线上运行的 topic 分类逻辑（含 emoji 元数据、`infer_topic` 关键词匹配、`_reasoning_buf` 重分类）同步到 GitHub main 分支，保证代码单源头。

## 变更清单

### 修改: `pool_manager/proxy.py`

| 操作 | 位置 | 内容 |
|------|------|------|
| 删除 | line 112-114 | `_CLASS_EXTRACT`、`_CLASS_STRIP` 正则 — 不再需要 |
| 删除 | line 117-130 | `_inject_topic_classify()` 函数 — 旧的隐身指令方案 |
| 修改 | line 590 | `_inject_topic_classify(body)` → 替换为 `infer_topic(body)` 预分类 |
| 重写 | line 630-770 | `_proxy_stream()` 函数 |
| 重写 | line 782-845 | `_proxy_sync()` 函数 |

### 未修改: `pool_manager/analytics.py`

`analytics.py` 已包含 `infer_topic()`、`infer_topic_from_text()`、`TOPIC_EMOJI` 映射，**无需修改**。

## 实现步骤

### Step 1: 删除废弃代码

删除 `proxy.py` 中的：
- `_CLASS_EXTRACT = re.compile(...)` (line 112)
- `_CLASS_STRIP = re.compile(...)` (line 113-114)
- `def _inject_topic_classify(...)` (line 117-130)

### Step 2: 替换 `_do_proxy_inner` 中的分类调用

```python
# line 589-590 (原)
# 在最后一条用户消息末尾附加分类指令（比 system 消息可靠）
_inject_topic_classify(body)

# 改为:
# 使用关键词匹配做话题分类（从首条用户消息）
topic_pre = analytics.infer_topic(body)
```

### Step 3: 重写 `_proxy_stream()` — 核心修复

#### 3a: SSE 逐行解析（根因修复）

当前 bug：`dec.startswith("data: ") + json.loads()` 的 chunk 级解析在多事件 chunk 中会失败。

修复方案：用字节缓冲 + `

` 分割逐行解析：

```python
buf = b""
_reasoning_buf = ""

async for chunk in resp.aiter_bytes():
    yield chunk
    buf += chunk
    while b"\n\n" in buf:
        event_bytes, _, buf = buf.partition(b"\n\n")
        event_str = event_bytes.decode(errors="replace").strip()
        if not event_str or "[DONE]" in event_str or event_str == "data: [DONE]":
            continue
        if event_str.startswith("data: "):
            try:
                ld = json.loads(event_str[6:])
                choices = ld.get("choices", [])
                if choices:
                    delta = choices[0].get("delta", {})
                    rc = delta.get("reasoning_content", "")
                    content = delta.get("content", "")
                    if rc:
                        _reasoning_buf += rc
                    elif content:
                        _reasoning_buf += content
                u = ld.get("usage", {})
                if u and isinstance(u, dict) and u.get("prompt_tokens") is not None:
                    usage_info = u
            except json.JSONDecodeError:
                pass
```

关键变化：
- `while b"\n\n" in buf` + `partition` — 逐个提取 SSE 事件，不受 chunk 边界影响
- `event_bytes.decode().strip()` — 处理单个事件的字符串
- `ld = json.loads(event_str[6:])` — 只传 `data: ` 后面的 JSON 部分

#### 3b: 初始分类

```python
topic = analytics.infer_topic(body)  # 从首条用户消息关键词匹配
```

放在函数体（closure 外），和 `_reasoning_buf` 一起。

#### 3c: Finally 块 — 重分类 + emoji 元数据 + DB 写入

```python
finally:
    if start_time > 0:
        ...
        # 重分类：初始是"功能咨询"且有推理内容时重新匹配
        if (not topic or topic == "功能咨询") and _reasoning_buf:
            topic = analytics.infer_topic_from_text(_reasoning_buf)
        
        # Emoji 元数据
        emoji = analytics.TOPIC_EMOJI.get(topic, "📌")
        meta = f"\n━━━ {emoji} {topic} · 入 {stream_tokens['prompt_tokens']} · 出 {stream_tokens['completion_tokens']}"
        yield f"data: {json.dumps({'choices':[{'delta':{'content': meta}}]})}\n\n".encode()
        
        # DB 写入 —— 与 metadata 使用同一个 topic 变量
        analytics.enqueue_record(_enrich_record({
            "user_id": ..., "model": ...,
            ...
            "topic": topic,
        }, body, client_ip))
    yield DONE_MARKER
```

### Step 4: 重写 `_proxy_sync()`

```python
async def _proxy_sync(...) -> Response:
    resp = await client.post(url, json=body, headers=headers)
    if start_time > 0:
        ...
        topic = analytics.infer_topic(body)  # 关键词匹配
        emoji = analytics.TOPIC_EMOJI.get(topic, "📌")
        content = rd.get("choices", [{}])[0].get("message", {}).get("content", "")
        if content:
            meta = f"\n━━━ {emoji} {topic} · 入 {pt} · 出 {ct}"
            rd["choices"][0]["message"]["content"] = content + meta
            modified = json.dumps(rd).encode()
            analytics.enqueue_record(_enrich_record({
                ...
                "topic": topic,
            }, body, client_ip))
            return Response(content=modified, ...)
    
    # 错误回退路径也补全 topic
    analytics.enqueue_record(_enrich_record({
        ...
        "topic": topic or "",
    }, body, client_ip))
    return Response(content=resp.content, ...)
```

### Step 5: 去除 `_CLASS_EXTRACT`/`_CLASS_STRIP` 的 import 依赖 (re 已 import)

`import re` 在 proxy.py 中可能还有其他用途，保留 import 行。

## 环境/依赖审计

- 无需新增 Python 依赖
- `analytics.infer_topic` 和 `analytics.TOPIC_EMOJI` 已在 `analytics.py` 中定义
- `analytics` 已 `from pool_manager import analytics` 导入

## 部署影响

### 对线上服务的影响

- **零影响** — 改动仅在 `proxy.py` 内部，API 接口不变（`POST /v1/chat/completions`）
- 元数据格式从 `\n{topic} · 入 X · 出 X` 改为 `\n━━━ {emoji} {topic} · 入 X · 出 X` — 微信用户看到新的 emoji 前缀，属于正向变化
- DB 中 topic 字段将更准确（减少"功能咨询"默认值）

### 部署流程

1. 本地 VM 开发、自测试
2. PR → 合并到 main
3. 在 Server-87 上构建 Docker 镜像（`docker build -t pool-proxy:latest`）
4. 跨服务器传镜像到 dosh（`docker save | ssh ... docker load`）+ `docker compose up -d pool-proxy`
5. 验证容器启动日志

### 回滚方案

回滚到旧镜像即可：
```bash
docker compose down pool-proxy
docker tag pool-proxy:prev pool-proxy:latest  # 如果有备份标签
docker compose up -d pool-proxy
```

## 验证方式

### 开发自测试

1. `ruff check pool_manager/proxy.py` — 语法检查
2. `python3 -c "from pool_manager import proxy, analytics; print('import OK')"` — import 链验证

### 功能验证

部署后：
1. `docker logs pool-proxy --tail 20` — 无启动报错
2. 开发环境 `curl` 调测试接口，检查响应末尾元数据格式为 `\n━━━ 💬 闲聊问候 · 入 ...`（含 emoji）
3. 输入一条无技术关键词的消息，确认 `topic` 不再默认"功能咨询"（应通过 `_reasoning_buf` 重分类）
4. 查 PG `analytics.proxy_api_calls` 新记录的 `topic` 字段是否匹配

## 风险与注意事项

1. **SSE 解析兼容性** — `while b"\n\n" in buf` + `partition` 模式兼容所有 SSE 实现，不会影响正常流式输出
2. **元数据格式变化** — 微信用户元数据从 `主题 · 入 X · 出 X` 变为 `━━━ 💻 主题 · 入 X · 出 X`，视觉上的正向变化
3. **`re` import 保留** — proxy.py 中可能其他地方还在用 re（如 config 解析），不删除 `import re`
4. **`topic_pre` 变量名** — 不要在 closure 内外用相同变量名，函数级 `topic = analytics.infer_topic(body)` 后，closure 内用 `nonlocal topic` 引用
