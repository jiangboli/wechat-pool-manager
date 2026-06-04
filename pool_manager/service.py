"""Pool Manager 主服务——3 个独立 FastAPI 应用。

启动方式（Docker 模式）：
    python -m pool_manager --mode bind     --config /app/config.yaml
    python -m pool_manager --mode admin    --config /app/config.yaml
    python -m pool_manager --mode proxy    --config /app/config.yaml
"""

import argparse
import asyncio
from datetime import datetime, timezone
import io
import json
import logging
import os
import re
import secrets
import sys
from pathlib import Path
from typing import Optional

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, Response, JSONResponse
from fastapi.staticfiles import StaticFiles

from .config import load_config, resolve_path
from .state import PoolState
from .profile_manager import list_linux_users, get_bound_count
from .hot_pool import HotPool
from . import gateway_manager as gm
from . import profile_manager as pm
from . import proxy as llm_proxy
from . import docker_scheduler as ds
from .pg_store import pg_store

# ── 日志 ──────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
logger = logging.getLogger("pool_manager")

# ── 全局实例 ──────────────────────────────────────────────────────────
config: dict = {}
state = PoolState()
hot_pool: Optional[HotPool] = None
pool_task: Optional[asyncio.Task] = None
scheduler: Optional[ds.DockerScheduler] = None
health_task: Optional[asyncio.Task] = None

# ── 排队队列 ──────────────────────────────────────────────────────────────
_binding_queue: list = []          # 排队中的注册请求 [{token, phone, lobster_name, user_name}]
_queue_check_task: Optional[asyncio.Task] = None  # 后台队列分配任务

# ── 三个独立 FastAPI 应用 ────────────────────────────────────────────

# 应用 A：绑定页（对外公开）
bind_app = FastAPI(title="Pool Manager - Binding")

# 应用 B：管理端（127.0.0.1 + Token 认证）
admin_app = FastAPI(title="Pool Manager - Admin")

# 应用 C：LLM Proxy（仅聊天，bot 容器用）
proxy_app = FastAPI(title="Pool Manager - LLM Proxy")

# ── 静态文件 ──────────────────────────────────────────────────────────
STATIC_DIR = os.path.join(os.path.dirname(__file__), "../static")
if os.path.exists(STATIC_DIR):
    bind_app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


# ═════════════════════════════════════════════════════════════════════
# 绑定页 API（bind_app）
# ═════════════════════════════════════════════════════════════════════


@bind_app.get("/")
async def root():
    index_path = os.path.join(STATIC_DIR, "index.html")
    if os.path.exists(index_path):
        return HTMLResponse(content=open(index_path, encoding="utf-8").read(), headers={"Cache-Control": "no-cache, no-store, must-revalidate"})
    return {"message": "Pool Manager running. Frontend not found."}


@bind_app.get("/api/v1/pool/hot-slots")
async def get_hot_slots():
    if not hot_pool:
        raise HTTPException(503, "热池未启动")
    slots = hot_pool.get_all_slots()
    bound_count = 0
    result = []
    for idx, s in enumerate(slots):
        ps = state.profiles.get(s["profile"], {})
        is_bound = ps.get("status") in ("bound_healthy", "bound_unhealthy", "bound_idle")
        if is_bound:
            bound_count += 1
        result.append({
            "profile": s["profile"],
            "slot_index": idx,
            "display_name": f"坑位 {idx + 1:03d}",
            "status": s["status"],
            "qr_url": s["qr_url"],
            "refreshed_at": s.get("refreshed_at", ""),
            "bound": is_bound,
            "user_id": ps.get("user_id", ""),
            "bound_at": ps.get("bound_at", ""),
        })
    return {
        "slots": result,
        "total_slots": len(slots),
        "pool_size": hot_pool.pool_size if hot_pool else len(slots),
        "bound_count": bound_count,
    }


@bind_app.get("/api/v1/bind/check-phone")
async def check_phone(phone: str = ""):
    """验证手机号是否已全局绑定。"""
    if not phone:
        raise HTTPException(400, "手机号必填")
    if not re.match(r'^1[3-9]\d{9}$', phone):
        raise HTTPException(400, "手机号格式错误")
    if pg_store.enabled:
        existing = await pg_store.find_binding_by_phone(phone)
        if existing:
            return {"exists": True, "profile_name": existing["profile_name"], "machine_ip": existing["machine_ip"], "bound_at": existing["bound_at"]}
    return {"exists": False}


@bind_app.post("/api/v1/bind/register")
async def register_binding(req: Request):
    """用户提交信息后，分配二维码槽位。"""
    if not hot_pool:
        raise HTTPException(503, "热池未启动")
    try:
        body = await req.json()
    except Exception:
        raise HTTPException(400, "请求体必须是 JSON")
    phone = (body.get("phone") or "").strip()
    lobster_name = (body.get("lobster_name") or "").strip()
    user_name = (body.get("user_name") or "").strip()
    if not phone or not lobster_name or not user_name:
        raise HTTPException(400, "手机号、龙虾名、用户名均为必填项")
    if not re.match(r'^1[3-9]\d{9}$', phone):
        raise HTTPException(400, "手机号格式错误")
    # 手机号去重检查（全局）
    if pg_store.enabled:
        existing = await pg_store.find_binding_by_phone(phone)
        if existing:
            # ✅ 已绑用户——走 rebind 流程：为原 profile 生成独立 QR
            profile = existing["profile_name"]
            if hot_pool.get_rebind_session(profile):
                hot_pool.cleanup_rebind_session(profile)
            qr_url = await hot_pool.generate_rebind_qr(profile)
            if not qr_url:
                raise HTTPException(503, "生成二维码失败，请重试")
            pending_token = secrets.token_hex(16)
            pg_store.save_pending_binding(pending_token, phone, lobster_name, user_name, profile)
            logger.info("重新绑定: profile=%s phone=%s", profile, phone)
            return {
                "rebind": True,
                "profile_name": profile,
                "qr_url": qr_url,
                "pending_token": pending_token,
            }
    # 找可用槽位
    slots = hot_pool.get_all_slots()
    target = None
    for s in slots:
        if s["status"] == "waiting" and s["qr_url"]:
            target = s
            break
    if not target:
        # 无可用槽位 → 加入排队队列
        pending_token = secrets.token_hex(16)
        pg_store.save_pending_binding(pending_token, phone, lobster_name, user_name, slot_id="__queued__")
        position = len(_binding_queue) + 1
        _binding_queue.append({
            "token": pending_token,
            "phone": phone,
            "lobster_name": lobster_name,
            "user_name": user_name,
        })
        logger.info("排队中: phone=%s user=%s position=%d", phone, user_name, position)
        return {
            "queued": True,
            "pending_token": pending_token,
            "position": position,
        }
    slot_id = target["profile"]
    # 将用户信息绑定到槽位
    hot_pool.set_slot_user_info(slot_id, {
        "phone": phone,
        "lobster_name": lobster_name,
        "user_name": user_name,
    })
    pending_token = secrets.token_hex(16)
    # 也存一份到 pg_store（PG 可用时持久化，但热池的回调中直接用 slot 上的 user_info）
    pg_store.save_pending_binding(pending_token, phone, lobster_name, user_name, slot_id)
    logger.info("绑定注册: slot=%s user=%s lobster=%s", slot_id, user_name, lobster_name)
    return {
        "pending_token": pending_token,
        "slot_id": slot_id,
        "qr_url": target["qr_url"],
    }


@bind_app.get("/api/v1/bind/rebind-status/{pending_token}")
async def get_rebind_status(pending_token: str):
    """轮询 QR 扫码状态（支持 rebind + 排队用户）。"""
    pending = pg_store.get_pending_binding(pending_token)
    if not pending:
        raise HTTPException(404, "无效的 pending_token")
    profile = pending["slot_id"]
    
    # ── 排队中的用户 ──
    if profile == "__queued__":
        position = next((i + 1 for i, q in enumerate(_binding_queue) if q["token"] == pending_token), None)
        if position is not None:
            return {"status": "queued", "position": position}
        # 不在队列中 → 可能已被分配 slot，重新读取 pending（slot_id 可能已更新）
        pending = pg_store.get_pending_binding(pending_token)
        if pending and pending["slot_id"] != "__queued__":
            profile = pending["slot_id"]
            return {"status": "ready", "slot_id": profile, "qr_url": pending.get("qr_url", "")}
        return {"status": "queued", "position": 999}
    
    session = hot_pool.get_rebind_session(profile) if hot_pool else None
    if not session:
        # 会话已清理或不存在
        return {"status": "not_found"}

    result = {"status": session.status}
    if session.status == "confirmed":
        # 确认成功 → 写凭证 + 重启容器 + 更新 PG
        try:
            scheduler = hot_pool.get_scheduler() if hasattr(hot_pool, "get_scheduler") else None
            # 先提取需要的数据
            new_phone = pending.get("phone", "")
            new_lobster = pending.get("lobster_name", "")
            new_user = pending.get("user_name", "")
            # write_env
            if hot_pool._scheduler:
                hot_pool._scheduler.write_env(profile, {
                    "account_id": session.account_id or "",
                    "token": session.token or "",
                    "base_url": session.bot_base_url or "",
                    "user_id": session.user_id or "",
                })
                if new_lobster:
                    hot_pool._scheduler.write_soul(profile, lobster_name=new_lobster)
                hot_pool._scheduler.restart_container(profile)
            # 更新 PG 记录
            if pg_store.enabled:
                await pg_store.update_binding_credentials(
                    profile=profile,
                    account_id=session.account_id or "",
                    bot_token=session.token or "",
                    bot_base_url=session.bot_base_url or "",
                )
                if new_phone or new_lobster or new_user:
                    await pg_store.update_binding_user_info(
                        profile=profile,
                        phone=new_phone,
                        lobster_name=new_lobster,
                        user_name=new_user,
                    )
                await pg_store.log_qr_event(profile, "rebind", session.user_id or "")
            logger.info("[rebind] %s 重新绑定完成，容器已重启", profile)
            result["profile_name"] = profile
        except Exception as e:
            logger.error("[rebind] %s 重新绑定异常: %s", profile, e)
            result["status"] = "error"
            result["error"] = str(e)
        finally:
            hot_pool.cleanup_rebind_session(profile)
    elif session.status in ("expired", "error", "timeout"):
        hot_pool.cleanup_rebind_session(profile)

    return result


@bind_app.get("/api/v1/bind/rebind-qr-image/{profile}")
async def get_rebind_qr_image(profile: str):
    """为 rebind 流程生成 QR 码图片。"""
    if not hot_pool:
        raise HTTPException(503, "热池未启动")
    session = hot_pool.get_rebind_session(profile)
    if not session or not session.qr_url:
        raise HTTPException(404, "未找到该 profile 的 rebind 会话")
    try:
        import qrcode
        qr = qrcode.QRCode(border=2, box_size=10)
        qr.add_data(session.qr_url)
        qr.make(fit=True)
        img = qr.make_image(fill_color="black", back_color="white")
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return Response(content=buf.getvalue(), media_type="image/png",
                        headers={"Cache-Control": "no-cache, no-store, must-revalidate"})
    except Exception as e:
        raise HTTPException(500, f"生成二维码失败: {e}")


@bind_app.get("/api/v1/pool/available")
async def get_available_qr():
    if not hot_pool:
        raise HTTPException(503, "热池未启动")
    slots = hot_pool.get_all_slots()
    if not slots:
        raise HTTPException(503, "当前无可用二维码，请稍后再试")
    for s in slots:
        if s["status"] == "waiting" and s["qr_url"]:
            return {"slot_id": s["profile"], "qr_url": s["qr_url"], "status": s["status"]}
    s = slots[0]
    return {"slot_id": s["profile"], "qr_url": s["qr_url"], "status": s["status"]}


@bind_app.get("/api/v1/pool/status/{slot_id}")
async def get_slot_status(slot_id: str):
    if not hot_pool:
        raise HTTPException(503, "热池未启动")
    slots = hot_pool.get_all_slots()
    for s in slots:
        if s["profile"] == slot_id:
            ps = state.profiles.get(slot_id, {})
            return {
                "slot_id": slot_id,
                "qr_status": s["status"],
                "qr_url": s["qr_url"],
                "qr_refreshed_at": s.get("refreshed_at", ""),
                "bound": ps.get("status") in ("bound_healthy", "bound_unhealthy", "bound_idle"),
                "bound_at": ps.get("bound_at", ""),
                "user_id": ps.get("user_id", ""),
            }
    ps = state.profiles.get(slot_id, {})
    return {
        "slot_id": slot_id,
        "qr_status": "not_in_pool",
        "bound": ps.get("status", "") in ("bound_healthy", "bound_unhealthy", "bound_idle"),
        "bound_at": ps.get("bound_at", ""),
        "user_id": ps.get("user_id", ""),
    }


@bind_app.get("/api/v1/pool/qr-image/{slot_id}")
async def get_qr_image(slot_id: str):
    if not hot_pool:
        raise HTTPException(503, "热池未启动")
    qr_url = hot_pool.get_slot_qr(slot_id)
    if not qr_url:
        ps = state.profiles.get(slot_id, {})
        if ps.get("status") in ("bound_healthy", "bound_unhealthy", "bound_idle"):
            raise HTTPException(410, "该二维码已绑定")
        available = hot_pool.get_all_slots()
        if available:
            qr_url = available[0]["qr_url"]
            slot_id = available[0]["profile"]
        if not qr_url:
            raise HTTPException(503, "当前无可用二维码，请稍后再试")
    try:
        import qrcode
        qr = qrcode.QRCode(border=2, box_size=10)
        qr.add_data(qr_url)
        qr.make(fit=True)
        img = qr.make_image(fill_color="black", back_color="white")
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return Response(content=buf.getvalue(), media_type="image/png",
                        headers={"Cache-Control": "no-cache, no-store, must-revalidate"})
    except Exception as e:
        raise HTTPException(500, f"生成二维码失败: {e}")


@bind_app.get("/api/v1/pool/stats")
async def get_pool_stats():
    mb = config.get("pool", {}).get("max_bound_gateways", 500)
    return state.get_stats(max_bound=mb)


@bind_app.get("/health")
async def bind_health():
    container_count = scheduler.list_containers() if scheduler else []
    return {
        "service": "pool-bind",
        "status": "ok",
        "version": "3.0.0",
        "pool_stats": state.get_stats(max_bound=config.get("pool", {}).get("max_bound_gateways", 500)),
        "containers": len(container_count),
    }


# ═════════════════════════════════════════════════════════════════════
# 管理 API（admin_app）
# ═════════════════════════════════════════════════════════════════════

# ── Admin Token ───────────────────────────────────────────────────
ADMIN_TOKEN = ""


def init_admin_token():
    """初始化 Admin Token。优先级：环境变量 > 文件 > 随机生成。"""
    global ADMIN_TOKEN
    token_path = "/home/data/pool-manager/admin_token"
    os.makedirs(os.path.dirname(token_path), exist_ok=True)

    # 1. 环境变量
    env_token = os.environ.get("ADMIN_TOKEN", "")
    if env_token:
        ADMIN_TOKEN = env_token
        with open(token_path, "w") as f:
            f.write(ADMIN_TOKEN + "\n")
        logger.info("Admin Token 已从环境变量读取")
        return

    # 2. 已有文件
    if os.path.exists(token_path):
        with open(token_path) as f:
            stored = f.read().strip()
        if stored:
            ADMIN_TOKEN = stored
            logger.info("Admin Token 已从文件读取")
            return

    # 3. 随机生成
    ADMIN_TOKEN = "pm_" + secrets.token_hex(16)
    with open(token_path, "w") as f:
        f.write(ADMIN_TOKEN + "\n")
    logger.warning("=" * 60)
    logger.warning("首次启动，Admin Token 已生成:")
    logger.warning("  %s", ADMIN_TOKEN)
    logger.warning("  已写入 %s", token_path)
    logger.warning("=" * 60)


@admin_app.middleware("http")
async def admin_token_middleware(request: Request, call_next):
    """Admin Token 验证中间件。"""
    if request.url.path == "/health" or request.url.path.startswith("/api/v1/health/"):
        return await call_next(request)
    token = request.headers.get("X-Admin-Token", "")
    if not secrets.compare_digest(token, ADMIN_TOKEN):
        logger.warning("Admin 接口未授权访问: %s %s", request.method, request.url.path)
        return JSONResponse(
            status_code=403,
            content={"error": "Forbidden", "message": "需要有效的 X-Admin-Token 头"},
        )
    return await call_next(request)


# 注册 proxy 管理路由（包含 /v1/chat/completions 和 /api/v1/proxy/*）
admin_app.include_router(llm_proxy.router)


@admin_app.get("/api/v1/gateways")
async def list_gateways():
    """列出所有 gateway 及其状态（基于 Docker 容器列表）。"""
    if not scheduler:
        return {"gateways": [], "total": 0}
    containers = scheduler.list_containers()
    result = []
    for c in containers:
        profile = c["profile"]
        ps = state.profiles.get(profile, {})
        result.append({
            "profile": profile,
            "container_name": c.get("container_name", ""),
            "status": ps.get("status", "unknown"),
            "active": c.get("running", False),
            "docker_status": c.get("status", "unknown"),
            "bound_at": ps.get("bound_at", ""),
            "last_active": ps.get("last_active", ""),
            "user_id": ps.get("user_id", ""),
            "error": ps.get("error", ""),
        })
    return {"gateways": result, "total": len(result)}


@admin_app.post("/api/v1/gateway/{profile}/start")
async def start_gateway(profile: str):
    if not scheduler:
        raise HTTPException(503, "DockerScheduler 未初始化")
    ok = scheduler.create_container(profile)
    if ok:
        state.set_status(profile, "bound_healthy")
        return {"success": True, "message": f"Gateway {profile} 已启动"}
    raise HTTPException(500, "启动失败")


@admin_app.post("/api/v1/gateway/{profile}/stop")
async def stop_gateway(profile: str):
    if not scheduler:
        raise HTTPException(503, "DockerScheduler 未初始化")
    ok = scheduler.stop_container(profile)
    if ok:
        state.set_status(profile, "bound_idle")
        return {"success": True, "message": f"Gateway {profile} 已停止"}
    raise HTTPException(500, "停止失败")


@admin_app.post("/api/v1/gateway/{profile}/restart")
async def restart_gateway(profile: str):
    if not scheduler:
        raise HTTPException(503, "DockerScheduler 未初始化")
    ok = scheduler.restart_container(profile)
    if ok:
        state.set_status(profile, "bound_healthy")
        return {"success": True, "message": f"Gateway {profile} 已重启"}
    raise HTTPException(500, "重启失败")


@admin_app.get("/api/v1/gateway/{profile}/logs")
async def get_gateway_logs(profile: str, tail: int = 50):
    if not scheduler:
        raise HTTPException(503, "DockerScheduler 未初始化")
    logs = scheduler.get_container_logs(profile, tail=tail)
    return {"profile": profile, "logs": logs}


@admin_app.post("/api/v1/pool/sync-models")
async def sync_models():
    """一键同步模型配置到所有微信用户。"""
    if not scheduler:
        return {"synced": 0, "message": "DockerScheduler 未初始化"}
    count, msg = gm.sync_model_config()
    return {"synced": count, "message": msg}


@admin_app.get("/health")
async def admin_health():
    container_count = scheduler.list_containers() if scheduler else []
    return {
        "service": "pool-admin",
        "status": "ok",
        "version": "3.0.0",
        "pool_stats": state.get_stats(max_bound=config.get("pool", {}).get("max_bound_gateways", 500)),
        "containers": len(container_count),
    }


@admin_app.get("/api/v1/health/bots")
async def get_bots_health():
    """返回所有 bot 容器的健康状态统计。"""
    if not pg_store.enabled:
        return {"error": "PG 未启用"}
    try:
        rows = await pg_store.get_bindings_with_heartbeat()
        now = datetime.now(timezone.utc)
        healthy, warning, dead, session_expired = [], [], [], []
        for r in rows:
            hb = r.get("last_heartbeat_at")
            mins = (now - hb).total_seconds() / 60 if hb else 999
            bind_status = r.get("status", "")
            
            # 优先检查绑定状态（session_expired 优先于心跳判断）
            if bind_status == "session_expired":
                status = "session_expired"
            elif mins < 10:
                status = "healthy"
            elif mins < 30:
                status = "warning"
            else:
                status = "dead"
            
            bot = {
                "profile": r["profile_name"],
                "user_name": r.get("user_name", ""),
                "lobster_name": r.get("lobster_name", ""),
                "last_heartbeat": hb.isoformat() if hb else None,
                "minutes_since_hb": round(mins, 1),
                "status": status,
            }
            if status == "session_expired":
                session_expired.append(bot)
            elif status == "healthy":
                healthy.append(bot)
            elif status == "warning":
                warning.append(bot)
            else:
                dead.append(bot)
        return {
            "total": len(rows),
            "healthy": len(healthy),
            "warning": len(warning),
            "dead": len(dead),
            "session_expired": len(session_expired),
            "bots": healthy + warning + dead + session_expired,
        }
    except Exception as e:
        raise HTTPException(500, f"查询失败: {e}")


# ═════════════════════════════════════════════════════════════════════
# LLM Proxy 应用（proxy_app）
# ═════════════════════════════════════════════════════════════════════

proxy_app.include_router(llm_proxy.chat_only_router)


@proxy_app.get("/health")
async def proxy_health():
    return {
        "service": "pool-proxy",
        "status": "ok",
        "version": "3.0.0",
    }


# ═════════════════════════════════════════════════════════════════════
# 启动逻辑（按 mode 分别初始化）
# ═════════════════════════════════════════════════════════════════════


async def _start_bind():
    """初始化绑定服务（热池 + DockerScheduler + 状态管理）。"""
    global hot_pool, pool_task, scheduler, health_task

    prefix = config.get("pool", {}).get("profile_prefix", "weixin-")
    total = config.get("pool", {}).get("total_profiles", 500)

    state.load()
    scheduler = ds.DockerScheduler(config)
    gm.set_scheduler(scheduler)
    scheduler.ensure_network()
    if not scheduler.ensure_image():
        logger.warning("hermes-bot 镜像不存在！请先构建: docker build -f Dockerfile.bot -t hermes-bot:latest .")

    profile_names = [f"{prefix}{i:03d}" for i in range(1, total + 1)]
    state.init_profiles(profile_names)
    for name in list(state.profiles.keys()):
        ps = state.profiles[name]
        if ps.get("status") in ("in_hot_pool", "qr_failed"):
            state.mark_available(name)

    # 从现有 Docker 容器恢复绑定状态（状态文件丢失或容器重建时的 fallback）
    try:
        containers = scheduler.list_containers()
        if containers:
            state.restore_from_containers(containers)
            logger.info("已从 %d 个已有容器恢复绑定状态", len(containers))
    except Exception as e:
        logger.warning("扫描已有容器失败（首次部署时正常）: %s", e)

    # ── PG 持久化初始化 ──────────────────────────────────────────
    await pg_store.connect()
    llm_proxy.init_proxy(proxy_config=config.get("proxy", {}), pg_store=pg_store if pg_store.enabled else None)
    if pg_store.enabled:
        try:
            containers = scheduler.list_containers()
            await pg_store.register_machine(total_slots=total, hot_pool_size=config.get("pool", {}).get("hot_pool_size", 20))
            await pg_store.restore_bindings_from_containers(containers)
        except Exception as e:
            logger.warning("PG 初始化异常: %s", e)

    logger.info("池状态已初始化，共 %d 个槽位", len(profile_names))

    hot_pool = HotPool(config, state, pm, gm)
    pool_task = asyncio.create_task(hot_pool.start())

    health_interval = config.get("gateway", {}).get("health_check_interval", 60)
    max_restarts = config.get("gateway", {}).get("max_restart_attempts", 3)
    health_task = asyncio.create_task(scheduler.health_check_loop(
        interval=health_interval, max_restarts=max_restarts,
    ))

    # ── 启动排队队列检查任务 ──
    global _queue_check_task
    _queue_check_task = asyncio.create_task(_queue_check_loop())

    logger.info("Bind 服务启动完成")


# ═════════════════════════════════════════════════════════════════════
# 排队队列分配逻辑
# ═════════════════════════════════════════════════════════════════════


async def _assign_from_queue():
    """从排队队列中取第一个人，分配给可用 slot。"""
    if not _binding_queue or not hot_pool:
        return
    slots = hot_pool.get_all_slots()
    target = None
    for s in slots:
        # 找 status==waiting 且没有 user_info（未被注册用户占用的）slot
        if s["status"] == "waiting" and s["qr_url"]:
            slot_obj = hot_pool.slots.get(s["profile"])
            if slot_obj and not slot_obj.user_info:
                target = s
                break
    if not target:
        return
    item = _binding_queue.pop(0)
    slot_id = target["profile"]
    token = item["token"]
    # 将用户信息绑定到槽位
    hot_pool.set_slot_user_info(slot_id, {
        "phone": item["phone"],
        "lobster_name": item["lobster_name"],
        "user_name": item["user_name"],
    })
    # 更新 pending binding 的 slot_id
    pg_store.update_pending_binding_slot(token, slot_id, target["qr_url"])
    logger.info("排队分配: token=%s slot=%s (剩余 %d 人排队)", token[:8], slot_id, len(_binding_queue))


async def _queue_check_loop():
    """后台任务：每 5 秒检查一次排队队列，有可用 slot 就分配。"""
    logger.info("排队队列检查任务已启动 (interval=5s)")
    try:
        while True:
            await asyncio.sleep(5)
            await _assign_from_queue()
    except asyncio.CancelledError:
        logger.info("排队队列检查任务已停止")


# ═════════════════════════════════════════════════════════════════════
async def _start_admin():
    """初始化管理服务（状态 + DockerScheduler + AdminToken + PG）。"""
    global scheduler

    state.load()
    scheduler = ds.DockerScheduler(config)
    gm.set_scheduler(scheduler)
    init_admin_token()
    await pg_store.connect()
    llm_proxy.init_proxy(proxy_config=config.get("proxy", {}), pg_store=pg_store if pg_store.enabled else None)
    logger.info("Admin 服务启动完成")


async def _start_proxy():
    """初始化 LLM Proxy 服务。"""
    await pg_store.connect()
    llm_proxy.init_proxy(proxy_config=config.get("proxy", {}), pg_store=pg_store if pg_store.enabled else None)
    if pg_store.enabled:
        await llm_proxy.load_from_pg()
        asyncio.create_task(llm_proxy.refresh_loop(interval=30))
        logger.info("Proxy PG 凭证池已加载，后台刷新已启动 (interval=30s)")
    logger.info("Proxy 服务启动完成")


def _setup_lifespan(app_obj, mode):
    """为指定 mode 的应用设置生命周期事件。"""

    @app_obj.on_event("startup")
    async def startup():
        if mode == "bind":
            await _start_bind()
        elif mode == "admin":
            await _start_admin()
        elif mode == "proxy":
            await _start_proxy()

    @app_obj.on_event("shutdown")
    async def shutdown():
        if mode == "bind":
            if hot_pool:
                await hot_pool.stop()
                if pool_task:
                    pool_task.cancel()
            if health_task:
                health_task.cancel()
            if _queue_check_task:
                _queue_check_task.cancel()
            if scheduler:
                scheduler.close()
            state.save()
        elif mode == "admin":
            state.save()
            if scheduler:
                scheduler.close()
        await pg_store.close()
        logger.info("%s 服务已关闭", mode)


# ═════════════════════════════════════════════════════════════════════
# CLI 入口
# ═════════════════════════════════════════════════════════════════════

APP_MAP = {
    "bind": bind_app,
    "admin": admin_app,
    "proxy": proxy_app,
}


def main():
    parser = argparse.ArgumentParser(description="WeChat Gateway Pool Manager")
    parser.add_argument("--mode", type=str, default="bind",
                        choices=["bind", "admin", "proxy"],
                        help="启动模式: bind=绑定页, admin=管理, proxy=LLM代理")
    parser.add_argument("--config", default=os.path.expanduser("~/.hermes/wechat-pool/config.yaml"),
                        help="配置文件路径")
    parser.add_argument("--total", type=int, default=None, help="槽位总数")
    parser.add_argument("--hot-pool", type=int, default=None, help="热池大小")
    parser.add_argument("--max-bound", type=int, default=None, help="最大 bound gateway 数")
    parser.add_argument("--port", type=int, default=None, help="API 端口")
    parser.add_argument("--prefix", type=str, default=None, help="profile 名前缀")
    parser.add_argument("--host", type=str, default=None, help="监听地址")

    args = parser.parse_args()
    cli_overrides = {
        "total": args.total,
        "hot_pool": args.hot_pool,
        "max_bound": args.max_bound,
        "port": args.port,
        "prefix": args.prefix,
    }

    global config
    config = load_config(args.config, cli_overrides)

    log_dir = resolve_path(config.get("logging", {}).get("dir", "~/.hermes/wechat-pool/logs/"))
    os.makedirs(log_dir, exist_ok=True)
    fh = logging.FileHandler(os.path.join(log_dir, "pool_manager.log"))
    fh.setFormatter(logging.Formatter("%(asctime)s [%(name)s] %(levelname)s: %(message)s"))
    logging.getLogger().addHandler(fh)
    logging.getLogger().setLevel(config.get("logging", {}).get("level", "INFO"))

    mode = args.mode
    app_obj = APP_MAP[mode]
    _setup_lifespan(app_obj, mode)

    # mode 默认端口和 host
    ports = {"bind": 8765, "admin": 8766, "proxy": 8767}
    hosts = {"bind": "0.0.0.0", "admin": "0.0.0.0", "proxy": "0.0.0.0"}

    host = args.host or hosts[mode]
    port = args.port or ports[mode]

    logger.info("启动 [%s] 服务: %s:%d", mode, host, port)
    uvicorn.run(app_obj, host=host, port=port,
                log_level=config.get("logging", {}).get("level", "info").lower())


if __name__ == "__main__":
    main()