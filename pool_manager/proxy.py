"""LLM 转发代理——将 wx 用户的 LLM 请求转发到真实 API。

增强功能（v3）：
1. 多 Provider 负载均衡（round-robin）
2. 动态凭证管理 API（热增删，不重启）
3. 熔断保护（连续错误 N 次后暂停 key）
4. 调用量统计（provider/key 维度）
5. Fallback provider 链
6. 流式/非流式透明转发
"""

import asyncio
import json
import logging
import os
import random
import time
from collections import defaultdict
from typing import Dict, List, Optional

import httpx
from fastapi import APIRouter, Request, Response
from fastapi.responses import StreamingResponse

logger = logging.getLogger("pool_manager.proxy")

# OpenAI 兼容的流式响应结束标记
DONE_MARKER = b"data: [DONE]\n\n"

# 共享 HTTPX 客户端（连接池 + keepalive）
_http_client: Optional[httpx.AsyncClient] = None

# 并发控制——最多同时处理 N 个 LLM 请求
_request_semaphore: Optional[asyncio.Semaphore] = None

# 速率限制——每 IP 每秒/分钟允许的请求数
_rate_limiter: Dict[str, list] = {}
RATE_LIMIT_MAX = 5         # 窗口内最大请求数
RATE_LIMIT_WINDOW = 60     # 窗口秒数
QUEUE_TIMEOUT = 30         # 排队超时秒数
MAX_CONCURRENT = 50        # 最大同时处理请求数


def _get_client() -> httpx.AsyncClient:
    global _http_client
    if _http_client is None:
        limits = httpx.Limits(max_keepalive_connections=50, max_connections=200)
        _http_client = httpx.AsyncClient(
            timeout=httpx.Timeout(300.0, read=120.0),
            limits=limits,
        )
    return _http_client


def _init_rate_limiter(max_concurrent: int = 50, rate_limit: int = 5):
    """初始化并发控制和速率限制。"""
    global _request_semaphore, RATE_LIMIT_MAX, MAX_CONCURRENT
    MAX_CONCURRENT = max_concurrent
    RATE_LIMIT_MAX = rate_limit
    _request_semaphore = asyncio.Semaphore(max_concurrent)
    logger.info(
        "速率限制初始化: max_concurrent=%d rate_limit=%d req/%ds",
        max_concurrent, rate_limit, RATE_LIMIT_WINDOW,
    )


def _check_rate_limit(client_ip: str) -> bool:
    """检查客户端 IP 是否超过速率限制。"""
    now = time.time()
    window = RATE_LIMIT_WINDOW

    # 清理过期记录
    if client_ip in _rate_limiter:
        _rate_limiter[client_ip] = [
            t for t in _rate_limiter[client_ip]
            if now - t < window
        ]

    timestamps = _rate_limiter.get(client_ip, [])
    if len(timestamps) >= RATE_LIMIT_MAX:
        return False  # 超限

    # 记录本次请求
    timestamps.append(now)
    _rate_limiter[client_ip] = timestamps
    return True

# ── 凭证池（内存） ───────────────────────────────────────────────────

_credential_pool: Dict[str, list] = {}       # provider → [key_info, ...]
_pool_index: Dict[str, int] = {}              # provider → 当前 round_robin 索引
_pg_store: Optional[object] = None            # PG 存储实例（可选）
_refresh_task: Optional[asyncio.Task] = None  # 后台刷新任务
_stats: Dict[str, dict] = {}                  # provider → 统计
_circuit_breakers: Dict[str, dict] = {}       # key_id → 熔断状态

# 配置
_config = {
    "default_provider": "deepseek",
    "fallback_providers": [],
    "circuit_breaker_max_errors": 5,
    "circuit_breaker_recovery_sec": 60,
}

# ── 凭证管理 ────────────────────────────────────────────────────────


def init_proxy(proxy_config: dict = None, pg_store=None):
    """初始化 proxy。

    Args:
        proxy_config: 可选配置 dict
        pg_store: PgStore 实例（启用时从 PG 加载，否则回退 auth.json）
    """
    global _pg_store
    if proxy_config:
        _config.update(proxy_config)
    _pg_store = pg_store

    # 解析嵌套的 circuit_breaker 配置
    cb = _config.get("circuit_breaker", {})
    if isinstance(cb, dict):
        _config["circuit_breaker_max_errors"] = cb.get("max_errors", _config.get("circuit_breaker_max_errors", 5))
        _config["circuit_breaker_recovery_sec"] = cb.get("recovery_window", _config.get("circuit_breaker_recovery_sec", 60))

    # 初始化速率限制
    max_concurrent = _config.get("max_concurrent_requests", 50)
    rate_limit = _config.get("rate_limit_per_ip", 5)
    _init_rate_limiter(max_concurrent=max_concurrent, rate_limit=rate_limit)

    if pg_store and pg_store.enabled:
        logger.info("LLM Proxy: 使用 PG 凭证池（启动后异步加载）")
    else:
        _load_from_auth_json()

    logger.info("LLM Proxy v3 就绪: %d 个 provider", len(_credential_pool))
    for provider, keys in _credential_pool.items():
        logger.info("  %s: %d keys", provider, len(keys))


def _auth_json_path() -> str:
    """返回持久化 auth.json 路径（volume 挂载的目录，重启不丢）。"""
    return "/home/data/pool-manager/auth.json"


def _load_from_auth_json():
    """从 auth.json 加载初始凭证。"""
    try:
        auth_path = _auth_json_path()
        if not os.path.exists(auth_path):
            logger.warning("auth.json 不存在: %s", auth_path)
            return
        with open(auth_path) as f:
            data = json.load(f)
        pool = data.get("credential_pool", {})
        for provider, keys in pool.items():
            for key in keys:
                _add_key_internal(provider, key)
    except Exception as e:
        logger.error("读取 auth.json 失败: %s", e)


# ── PG 凭证池 ──────────────────────────────────────────────────────


async def load_from_pg():
    """从 PG 加载本机所有 API Key 到内存（启动时调用）。"""
    if not _pg_store or not _pg_store.enabled:
        return
    try:
        keys = await _pg_store.load_api_keys()
        _credential_pool.clear()
        _pool_index.clear()
        for k in keys:
            key_info = {
                "access_token": k["access_token"],
                "label": k.get("label", ""),
                "_pg_id": k["id"],
            }
            _add_key_internal(k["provider"], key_info, persist=False)
        logger.info("从 PG 加载 %d 个 API Key", len(keys))
    except Exception as e:
        logger.warning("从 PG 加载 API Key 失败: %s", e)


async def refresh_loop(interval: int = 30):
    """后台定时从 PG 刷新凭证池（新增/删除同步）。

    Args:
        interval: 刷新间隔秒数（默认 30）
    """
    logger.info("凭证池后台刷新已启动 (interval=%ds)", interval)
    while True:
        await asyncio.sleep(interval)
        try:
            await _sync_pg_to_memory()
        except Exception as e:
            logger.warning("凭证池刷新失败: %s", e)


async def _sync_pg_to_memory():
    """PG → 内存增量同步。

    新增的 key 自动加入，已删除的 key 自动移除，未变动的保留（熔断状态不丢）。
    """
    pg_keys = await _pg_store.load_api_keys()
    pg_id_set = {k["id"] for k in pg_keys}

    # 收集当前内存中的 PG key ID
    mem_pg_ids = set()
    for provider, keys in _credential_pool.items():
        for k in keys:
            pid = k.get("_pg_id")
            if pid is not None:
                mem_pg_ids.add(pid)

    # 新增: PG 有但内存没有
    new_keys = [k for k in pg_keys if k["id"] not in mem_pg_ids]
    for nk in new_keys:
        key_info = {
            "access_token": nk["access_token"],
            "label": nk.get("label", ""),
            "_pg_id": nk["id"],
        }
        _add_key_internal(nk["provider"], key_info, persist=False)
        logger.info("[sync] 新增 key: provider=%s pg_id=%d", nk["provider"], nk["id"])

    # 删除: 内存有但 PG 没有
    removed_ids = mem_pg_ids - pg_id_set
    if removed_ids:
        for provider in list(_credential_pool.keys()):
            keys = _credential_pool[provider]
            remaining = [k for k in keys if k.get("_pg_id") not in removed_ids]
            for removed in keys:
                if removed.get("_pg_id") in removed_ids:
                    logger.info("[sync] 删除 key: provider=%s pg_id=%s",
                                provider, removed.get("_pg_id"))
            if remaining:
                _credential_pool[provider] = remaining
            else:
                del _credential_pool[provider]
                _pool_index.pop(provider, None)


def _save_credential_pool():
    """将当前凭证池写入持久化存储。

    PG 启用时每步操作已直接写入 PG，此函数不做额外操作。
    无 PG 时回退 auth.json 写入。
    """
    if _pg_store and _pg_store.enabled:
        return  # PG 模式：每步操作已独立写入 PG
    try:
        auth_path = _auth_json_path()
        os.makedirs(os.path.dirname(auth_path), exist_ok=True)
        pool = {}
        for provider, keys in _credential_pool.items():
            pool[provider] = [
                {"access_token": k["access_token"], "label": k.get("label", "")}
                for k in keys
            ]
        with open(auth_path, "w") as f:
            json.dump({"credential_pool": pool}, f, indent=2, ensure_ascii=False)
        logger.info("[proxy] 凭证池已持久化到 %s (%d providers)", auth_path, len(pool))
    except Exception as e:
        logger.error("[proxy] 持久化凭证池失败: %s", e)


def _add_key_internal(provider: str, key_info: dict, persist: bool = True) -> str:
    """内部添加 key，返回 key_id。

    Args:
        provider: provider 名
        key_info: key 信息 dict
        persist: 是否持久化到存储（启动加载时传 False，管理 API 调用时传 True）
    """
    if provider not in _credential_pool:
        _credential_pool[provider] = []
        _pool_index[provider] = 0

    key_id = f"{provider}_{len(_credential_pool[provider])}_{int(time.time())}"
    key_info["_key_id"] = key_id
    key_info["_added_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    key_info["_healthy"] = True
    key_info["_error_count"] = 0
    _credential_pool[provider].append(key_info)
    logger.info("[proxy] 添加 key: provider=%s label=%s id=%s", provider, key_info.get("label", ""), key_id)
    if persist:
        _save_credential_pool()  # 自动持久化
    return key_id


def _remove_key_internal(key_id: str) -> bool:
    """内部删除 key。"""
    for provider in list(_credential_pool.keys()):
        keys = _credential_pool[provider]
        for i, k in enumerate(keys):
            if k.get("_key_id") == key_id:
                keys.pop(i)
                logger.info("[proxy] 删除 key: provider=%s id=%s", provider, key_id)
                if not keys:
                    del _credential_pool[provider]
                    _pool_index.pop(provider, None)
                return True
    return False


# ── 熔断保护 ──────────────────────────────────────────────────────


def _record_error(key_info: dict):
    """记录一次错误，触发熔断检查。"""
    key_info["_error_count"] = key_info.get("_error_count", 0) + 1
    max_errors = _config["circuit_breaker_max_errors"]
    if key_info["_error_count"] >= max_errors and key_info.get("_healthy", True):
        key_info["_healthy"] = False
        key_info["_circuit_open_at"] = time.time()
        key_id = key_info.get("_key_id", "unknown")
        logger.warning("[proxy] 熔断触发: key=%s (%d 连续错误)", key_id, max_errors)


def _check_circuit_breaker(key_info: dict) -> bool:
    """检查 key 是否可用（熔断未触发或已恢复）。"""
    if key_info.get("_healthy", True):
        return True
    # 检查恢复窗口
    open_at = key_info.get("_circuit_open_at", 0)
    recovery_sec = _config["circuit_breaker_recovery_sec"]
    if time.time() - open_at >= recovery_sec:
        key_info["_healthy"] = True
        key_info["_error_count"] = 0
        key_id = key_info.get("_key_id", "unknown")
        logger.info("[proxy] 熔断恢复: key=%s", key_id)
        return True
    return False


# ── Key 选择（round_robin + 熔断感知） ────────────────────────────


def _select_key(provider: str) -> Optional[str]:
    """round_robin 从凭证池选一个健康的 key。"""
    keys = _credential_pool.get(provider, [])
    if not keys:
        return None

    # 过滤健康的 key
    healthy_keys = [k for k in keys if _check_circuit_breaker(k)]
    if not healthy_keys:
        return None

    idx = _pool_index.get(provider, 0) % len(healthy_keys)
    key = healthy_keys[idx]["access_token"]
    _pool_index[provider] = (idx + 1) % len(healthy_keys)
    return key


# ── Provider 路由 ─────────────────────────────────────────────────


def _get_provider_for_model(model: str) -> str:
    """根据模型名判断使用哪个 provider。"""
    # 尝试从 config.yaml 读取
    try:
        import yaml
        cfg_path = os.path.expanduser("~/.hermes/config.yaml")
        if os.path.exists(cfg_path):
            with open(cfg_path) as f:
                cfg = yaml.safe_load(f) or {}
            model_cfg = cfg.get("model", {})
            if isinstance(model_cfg, str):
                return model_cfg
            return model_cfg.get("provider", _config["default_provider"])
    except Exception:
        pass
    return _config["default_provider"]


def _get_base_url(provider: str) -> str:
    """获取 provider 的 API base URL。"""
    base_urls = {
        "deepseek": "https://api.deepseek.com/v1",
        "alibaba": "https://dashscope.aliyuncs.com/compatible-mode/v1",
    }
    try:
        import yaml
        cfg_path = os.path.expanduser("~/.hermes/config.yaml")
        if os.path.exists(cfg_path):
            with open(cfg_path) as f:
                cfg = yaml.safe_load(f) or {}
            model_cfg = cfg.get("model", {})
            if isinstance(model_cfg, dict) and model_cfg.get("base_url"):
                return model_cfg["base_url"].rstrip("/")
    except Exception:
        pass
    return base_urls.get(provider, "https://api.deepseek.com/v1")


# ── 转发逻辑 ──────────────────────────────────────────────────────


async def _do_proxy(request: Request, body: dict) -> Response:
    """执行代理转发（含 fallback 链 + 速率限制 + 排队）。"""
    # ── 速率限制 ──
    client_ip = request.client.host if request.client else "unknown"
    if not _check_rate_limit(client_ip):
        retry_after = RATE_LIMIT_WINDOW
        logger.warning("[proxy] 速率限制触发: ip=%s limit=%d req/%ds",
                       client_ip, RATE_LIMIT_MAX, retry_after)
        return Response(
            content=json.dumps({
                "error": f"请求过于频繁，请 {retry_after} 秒后再试",
                "retry_after": retry_after,
            }),
            status_code=429,
            media_type="application/json",
            headers={"Retry-After": str(retry_after)},
        )

    # ── 并发排队 ──
    sem = _request_semaphore
    if sem is not None:
        try:
            async with asyncio.timeout(QUEUE_TIMEOUT):
                await sem.acquire()
        except asyncio.TimeoutError:
            logger.warning("[proxy] 排队超时: ip=%s (max_concurrent=%d)", client_ip, MAX_CONCURRENT)
            return Response(
                content=json.dumps({
                    "error": "系统繁忙，请稍后再试",
                    "retry_after": 10,
                }),
                status_code=503,
                media_type="application/json",
                headers={"Retry-After": "10"},
            )
    else:
        # Semaphore not initialized — create a default one
        global _request_semaphore
        _request_semaphore = asyncio.Semaphore(MAX_CONCURRENT)
        sem = _request_semaphore
        await sem.acquire()

    try:
        return await _do_proxy_inner(request, body, client_ip)
    finally:
        if sem is not None:
            sem.release()


async def _do_proxy_inner(request: Request, body: dict, client_ip: str) -> Response:
    """实际的代理转发逻辑（不含速率限制和排队）。"""
    model = body.get("model", "")
    providers_to_try = []

    # 主 provider
    primary = _get_provider_for_model(model)
    providers_to_try.append(primary)

    # Fallback provider 链
    for fb in _config.get("fallback_providers", []):
        if fb != primary:
            providers_to_try.append(fb)

    last_error = None
    for provider in providers_to_try:
        api_key = _select_key(provider)
        if not api_key:
            logger.warning("[proxy] provider=%s 无可用 key（可能已全部熔断）", provider)
            if len(providers_to_try) > 1:
                logger.info("[proxy] 尝试 fallback provider: %s", provider)
            continue

        base_url = _get_base_url(provider)
        target_url = f"{base_url}/chat/completions"

        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        for h in ("User-Agent", "Accept", "X-Request-Id"):
            if h in request.headers:
                headers[h] = request.headers[h]

        stream = body.get("stream", False)
        try:
            client = _get_client()
            if stream:
                result = await _proxy_stream(client, target_url, body, headers)
                # 记录成功
                _record_success(provider)
                return result
            else:
                result = await _proxy_sync(client, target_url, body, headers)
                _record_success(provider)
                return result
        except Exception as e:
            logger.warning("[proxy] provider=%s 转发失败: %s", provider, e)
            _record_error_for_provider(provider)
            last_error = e
            continue

    # 所有 provider 都失败
    return Response(
        content=json.dumps({"error": f"所有 provider 均不可用: {last_error}"}),
        status_code=502,
        media_type="application/json",
    )


def _record_success(provider: str):
    """记录一次成功调用。"""
    if provider not in _stats:
        _stats[provider] = {"total_calls": 0, "success_calls": 0, "error_calls": 0, "last_success": ""}
    _stats[provider]["total_calls"] += 1
    _stats[provider]["success_calls"] += 1
    _stats[provider]["last_success"] = time.strftime("%H:%M:%S")


def _record_error_for_provider(provider: str):
    """记录一次错误（会对该 provider 的所有 key 记录错误）。"""
    if provider not in _stats:
        _stats[provider] = {"total_calls": 0, "success_calls": 0, "error_calls": 0}
    _stats[provider]["total_calls"] += 1
    _stats[provider]["error_calls"] += 1

    # 对该 provider 的所有 key 记录错误（触发熔断）
    for key_info in _credential_pool.get(provider, []):
        _record_error(key_info)


async def _proxy_stream(client: httpx.AsyncClient, url: str,
                        body: dict, headers: dict) -> StreamingResponse:
    """流式转发——SSE 逐 token 返回。"""
    async def generate():
        try:
            async with client.stream("POST", url, json=body, headers=headers) as resp:
                async for chunk in resp.aiter_bytes():
                    yield chunk
        except Exception as e:
            yield f"data: {json.dumps({'error': str(e)})}\n\n".encode()
            yield DONE_MARKER
    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


async def _proxy_sync(client: httpx.AsyncClient, url: str,
                      body: dict, headers: dict) -> Response:
    """非流式转发——完整返回。"""
    resp = await client.post(url, json=body, headers=headers)
    return Response(
        content=resp.content,
        status_code=resp.status_code,
        media_type=resp.headers.get("content-type", "application/json"),
    )


# ── FastAPI 路由 ────────────────────────────────────────────────────

router = APIRouter()


@router.post("/v1/chat/completions")
async def proxy_chat_completions(request: Request):
    """转发 LLM 请求——支持流式和非流式。"""
    try:
        body = await request.json()
    except Exception:
        return Response(
            content=json.dumps({"error": "invalid json"}),
            status_code=400,
            media_type="application/json",
        )

    return await _do_proxy(request, body)


# ── 仅聊天代理路由（给 bot 容器用，不含管理 API）──

chat_only_router = APIRouter()


@chat_only_router.post("/v1/chat/completions")
async def proxy_chat_completions_only(request: Request):
    """转发 LLM 请求——只有聊天补全，不含 keys/status 管理路由。"""
    try:
        body = await request.json()
    except Exception:
        return Response(
            content=json.dumps({"error": "invalid json"}),
            status_code=400,
            media_type="application/json",
        )
    return await _do_proxy(request, body)


# ── 管理 API ────────────────────────────────────────────────────────


@router.post("/api/v1/proxy/keys")
async def add_key(request: Request):
    """添加 API key。

    Body:
        {"provider": "deepseek", "key": "sk-xxx", "label": "主key-1"}

    有 PG 时同时持久化到 PG，仅 auth.json 回退时写文件。
    """
    try:
        data = await request.json()
    except Exception:
        return Response(
            content=json.dumps({"error": "invalid json"}),
            status_code=400,
            media_type="application/json",
        )

    provider = data.get("provider", "")
    api_key = data.get("key", "")
    label = data.get("label", "")

    if not provider or not api_key:
        return Response(
            content=json.dumps({"error": "provider and key are required"}),
            status_code=400,
            media_type="application/json",
        )

    # 写入内存
    key_id = _add_key_internal(provider, {
        "access_token": api_key,
        "label": label or f"{provider}-key",
    })

    # 写入 PG（如启用）
    if _pg_store and _pg_store.enabled:
        pg_id = await _pg_store.create_api_key(provider, api_key, label)
        if pg_id:
            # 标记内存中的 key 有 PG id，后续刷新可识别
            for keys in _credential_pool.values():
                for k in keys:
                    if k.get("_key_id") == key_id:
                        k["_pg_id"] = pg_id
                        break

    return {
        "success": True,
        "key_id": key_id,
        "provider": provider,
        "message": f"Key {label} 已添加到 {provider}",
    }


@router.delete("/api/v1/proxy/keys/{key_id}")
async def delete_key(key_id: str):
    """删除 API key。

    同时删除 PG 记录（如启用）和内存中的 key。
    """
    # 先删内存
    ok = _remove_key_internal(key_id)
    if not ok:
        return Response(
            content=json.dumps({"error": f"key {key_id} not found"}),
            status_code=404,
            media_type="application/json",
        )

    # 尝试按 PG id 删除（key_id 可能是 _pg_id 或 _key_id）
    if _pg_store and _pg_store.enabled:
        try:
            pg_id = int(key_id.split("_")[-1]) if "_" in key_id else int(key_id)
            await _pg_store.delete_api_key(pg_id)
        except (ValueError, TypeError):
            pass

    return {"success": True, "message": f"Key {key_id} 已删除"}


@router.get("/api/v1/proxy/status")
async def proxy_status():
    """查看 proxy 状态——各 provider 的 key/统计/熔断。"""
    result = []
    for provider, keys in _credential_pool.items():
        key_list = []
        for k in keys:
            key_list.append({
                "key_id": k.get("_key_id"),
                "label": k.get("label", ""),
                "healthy": k.get("_healthy", True),
                "error_count": k.get("_error_count", 0),
                "added_at": k.get("_added_at", ""),
            })
            circuit_breaker = k.get("_circuit_open_at")
            if circuit_breaker:
                key_list[-1]["circuit_open_at"] = time.strftime(
                    "%H:%M:%S", time.localtime(circuit_breaker))
                key_list[-1]["recovery_in"] = max(
                    0, _config["circuit_breaker_recovery_sec"] - (time.time() - circuit_breaker))

        result.append({
            "provider": provider,
            "keys": key_list,
            "stats": _stats.get(provider, {}),
            "total_keys": len(keys),
            "healthy_keys": sum(1 for k in keys if k.get("_healthy", True)),
        })

    return {
        "providers": result,
        "config": {
            "default_provider": _config["default_provider"],
            "fallback_providers": _config["fallback_providers"],
            "circuit_breaker_max_errors": _config["circuit_breaker_max_errors"],
            "circuit_breaker_recovery_sec": _config["circuit_breaker_recovery_sec"],
        },
        "total_providers": len(result),
    }
