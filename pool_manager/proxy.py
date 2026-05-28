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
from typing import Dict, List, Optional

import httpx
from fastapi import APIRouter, Request, Response
from fastapi.responses import StreamingResponse

logger = logging.getLogger("pool_manager.proxy")

# OpenAI 兼容的流式响应结束标记
DONE_MARKER = b"data: [DONE]\n\n"

# ── 凭证池（内存） ───────────────────────────────────────────────────

_credential_pool: Dict[str, list] = {}       # provider → [key_info, ...]
_pool_index: Dict[str, int] = {}              # provider → 当前 round_robin 索引
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


def init_proxy(proxy_config: dict = None):
    """初始化 proxy（从 auth.json 加载初始凭证池）。"""
    if proxy_config:
        _config.update(proxy_config)
    _load_from_auth_json()
    logger.info("LLM Proxy v3 就绪: %d 个 provider", len(_credential_pool))
    for provider, keys in _credential_pool.items():
        logger.info("  %s: %d keys", provider, len(keys))


def _load_from_auth_json():
    """从 auth.json 加载初始凭证。"""
    try:
        auth_path = os.path.expanduser("~/.hermes/auth.json")
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


def _add_key_internal(provider: str, key_info: dict) -> str:
    """内部添加 key，返回 key_id。"""
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
    """执行代理转发（含 fallback 链）。"""
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
        client = None
        try:
            client = httpx.AsyncClient(timeout=300)
            if stream:
                result = await _proxy_stream(client, target_url, body, headers)
                # 记录成功
                _record_success(provider)
                return result
            else:
                result = await _proxy_sync(client, target_url, body, headers)
                _record_success(provider)
                await client.aclose()
                return result
        except Exception as e:
            logger.warning("[proxy] provider=%s 转发失败: %s", provider, e)
            _record_error_for_provider(provider)
            last_error = e
            if client:
                try:
                    await client.aclose()
                except Exception:
                    pass
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
        finally:
            await client.aclose()
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


# ── 管理 API ────────────────────────────────────────────────────────


@router.post("/api/v1/proxy/keys")
async def add_key(request: Request):
    """添加 API key。

    Body:
        {"provider": "deepseek", "key": "sk-xxx", "label": "主key-1"}
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

    key_id = _add_key_internal(provider, {
        "access_token": api_key,
        "label": label or f"{provider}-key",
    })

    return {
        "success": True,
        "key_id": key_id,
        "provider": provider,
        "message": f"Key {label} 已添加到 {provider}",
    }


@router.delete("/api/v1/proxy/keys/{key_id}")
async def delete_key(key_id: str):
    """删除 API key。"""
    ok = _remove_key_internal(key_id)
    if not ok:
        return Response(
            content=json.dumps({"error": f"key {key_id} not found"}),
            status_code=404,
            media_type="application/json",
        )
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
