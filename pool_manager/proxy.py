"""LLM 转发代理——将 wx 用户的 LLM 请求转发到真实 API。

工作方式：
1. 接收 wx 用户的 /v1/chat/completions 请求
2. 从请求中解析 model 名
3. 读 dosh 的 credential pool（同一个进程，无权限问题）
4. round_robin 选一个 API key
5. 替换 Authorization header → 转发到真实 API
6. 流式返回结果

这样 API key 只在 pool manager 进程内存中，wx 用户永远看不到。
"""

import asyncio
import json
import logging
import random
import os
from typing import Optional

import httpx
from fastapi import APIRouter, Request, Response
from fastapi.responses import StreamingResponse

logger = logging.getLogger("pool_manager.proxy")

# OpenAI 兼容的流式响应结束标记
DONE_MARKER = b"data: [DONE]\n\n"

# ── 凭证池缓存 ──────────────────────────────────────────────────────────
# 每个 provider 的 key 列表，从 dosh 的 auth.json 读取后缓存到内存
_credential_pool: dict = {}
_pool_index: dict = {}  # provider → 当前 round_robin 索引


def _load_credential_pool() -> dict:
    """从 dosh 的 auth.json 加载 credential pool。"""
    try:
        import yaml
        auth_path = os.path.expanduser("~/.hermes/auth.json")
        if not os.path.exists(auth_path):
            logger.warning("auth.json 不存在: %s", auth_path)
            return {}
        with open(auth_path) as f:
            data = json.load(f)
        pool = data.get("credential_pool", {})
        return pool
    except Exception as e:
        logger.error("读取 credential pool 失败: %s", e)
        return {}


def _reload_pool():
    """重载凭证池到内存。"""
    global _credential_pool
    _credential_pool = _load_credential_pool()
    logger.info("凭证池已重载: %d 个 provider", len(_credential_pool))
    for provider, keys in _credential_pool.items():
        logger.info("  %s: %d keys", provider, len(keys))


def _select_key(provider: str) -> Optional[str]:
    """round_robin 从凭证池选一个 key。"""
    keys = _credential_pool.get(provider, [])
    if not keys:
        return None
    
    idx = _pool_index.get(provider, 0)
    key = keys[idx]["access_token"]
    _pool_index[provider] = (idx + 1) % len(keys)
    return key


def _get_provider_for_model(model: str) -> Optional[str]:
    """根据模型名判断使用哪个 provider。
    
    读取 dosh 的 config.yaml 获取 model→provider 映射。
    """
    import yaml
    try:
        cfg_path = os.path.expanduser("~/.hermes/config.yaml")
        if not os.path.exists(cfg_path):
            return "deepseek"  # 默认
        with open(cfg_path) as f:
            cfg = yaml.safe_load(f) or {}
        
        model_cfg = cfg.get("model", {})
        if isinstance(model_cfg, str):
            return model_cfg
        return model_cfg.get("provider", "deepseek")
    except Exception:
        return "deepseek"


def _get_base_url(provider: str) -> str:
    """获取 provider 的 API base URL。"""
    base_urls = {
        "deepseek": "https://api.deepseek.com/v1",
        "alibaba": "https://dashscope.aliyuncs.com/compatible-mode/v1",
    }
    
    # 尝试从 config.yaml 读
    import yaml
    try:
        cfg_path = os.path.expanduser("~/.hermes/config.yaml")
        with open(cfg_path) as f:
            cfg = yaml.safe_load(f) or {}
        model_cfg = cfg.get("model", {})
        if isinstance(model_cfg, dict) and model_cfg.get("base_url"):
            return model_cfg["base_url"].rstrip("/")
    except Exception:
        pass
    
    return base_urls.get(provider, "https://api.deepseek.com/v1")


# ── FastAPI 路由 ────────────────────────────────────────────────────────

router = APIRouter()


@router.post("/v1/chat/completions")
async def proxy_chat_completions(request: Request):
    """转发 LLM 请求——wx 用户的 Hermes 发到此端点。"""
    try:
        body = await request.json()
    except Exception:
        return Response(
            content=json.dumps({"error": "invalid json"}),
            status_code=400,
            media_type="application/json",
        )
    
    model = body.get("model", "")
    provider = _get_provider_for_model(model) or "deepseek"
    
    # 选 key
    api_key = _select_key(provider)
    if not api_key:
        logger.error("[proxy] provider=%s 无可用的 API key", provider)
        return Response(
            content=json.dumps({"error": f"no available key for {provider}"}),
            status_code=503,
            media_type="application/json",
        )
    
    base_url = _get_base_url(provider)
    target_url = f"{base_url}/chat/completions"
    
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    # 保留除 Authorization 外的客户端 headers
    for h in ("User-Agent", "Accept", "X-Request-Id"):
        if h in request.headers:
            headers[h] = request.headers[h]
    
    stream = body.get("stream", False)
    
    try:
        async with httpx.AsyncClient(timeout=300) as client:
            if stream:
                return await _proxy_stream(client, target_url, body, headers)
            else:
                return await _proxy_sync(client, target_url, body, headers)
    except Exception as e:
        logger.error("[proxy] 转发失败: model=%s provider=%s error=%s",
                      model, provider, e)
        return Response(
            content=json.dumps({"error": str(e)}),
            status_code=502,
            media_type="application/json",
        )


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


# ── 初始化 ──────────────────────────────────────────────────────────────

def init_proxy():
    """初始化 proxy——加载凭证池。"""
    _reload_pool()
    logger.info("LLM Proxy 就绪")
