"""Pool Manager 主服务——FastAPI 应用。

启动方式（Docker 模式）：
    python -m pool_manager.service --config /path/to/config.yaml

架构变化（v3.0）：
    每个微信用户一个 Docker 容器，由 DockerScheduler 管理
    Pool Manager 自身也在 Docker 中运行
"""

import argparse
import asyncio
import io
import json
import logging
import os
import sys
from pathlib import Path
from typing import Optional

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, Response
from fastapi.staticfiles import StaticFiles

from .config import load_config, resolve_path
from .state import PoolState
from .profile_manager import list_linux_users, get_bound_count
from .hot_pool import HotPool
from . import gateway_manager as gm
from . import profile_manager as pm
from . import proxy as llm_proxy
from . import docker_scheduler as ds

# ── 日志 ──────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
logger = logging.getLogger("pool_manager")

# ── 全局实例 ──────────────────────────────────────────────────────────
app = FastAPI(title="WeChat Gateway Pool Manager", version="3.0.0")
config: dict = {}
state = PoolState()
hot_pool: Optional[HotPool] = None
pool_task: Optional[asyncio.Task] = None
scheduler: Optional[ds.DockerScheduler] = None
health_task: Optional[asyncio.Task] = None

# ── 静态文件 ──────────────────────────────────────────────────────────
STATIC_DIR = os.path.join(os.path.dirname(__file__), "../static")
if os.path.exists(STATIC_DIR):
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

# ── 注册 proxy 路由 ─────────────────────────────────────────────────
app.include_router(llm_proxy.router)


# ═════════════════════════════════════════════════════════════════════
# API 端点
# ═════════════════════════════════════════════════════════════════════


@app.get("/")
async def root():
    index_path = os.path.join(STATIC_DIR, "index.html")
    if os.path.exists(index_path):
        return HTMLResponse(open(index_path, encoding="utf-8").read())
    return {"message": "Pool Manager running. Frontend not found."}


@app.get("/api/v1/pool/hot-slots")
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


@app.get("/api/v1/pool/available")
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


@app.get("/api/v1/pool/status/{slot_id}")
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


@app.get("/api/v1/pool/qr-image/{slot_id}")
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


@app.get("/api/v1/pool/stats")
async def get_pool_stats():
    return state.get_stats()


@app.get("/api/v1/gateways")
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


@app.post("/api/v1/gateway/{profile}/start")
async def start_gateway(profile: str):
    if not scheduler:
        raise HTTPException(503, "DockerScheduler 未初始化")
    ok = scheduler.create_container(profile)
    if ok:
        state.set_status(profile, "bound_healthy")
        return {"success": True, "message": f"Gateway {profile} 已启动"}
    raise HTTPException(500, "启动失败")


@app.post("/api/v1/gateway/{profile}/stop")
async def stop_gateway(profile: str):
    if not scheduler:
        raise HTTPException(503, "DockerScheduler 未初始化")
    ok = scheduler.stop_container(profile)
    if ok:
        state.set_status(profile, "bound_idle")
        return {"success": True, "message": f"Gateway {profile} 已停止"}
    raise HTTPException(500, "停止失败")


@app.post("/api/v1/gateway/{profile}/restart")
async def restart_gateway(profile: str):
    if not scheduler:
        raise HTTPException(503, "DockerScheduler 未初始化")
    ok = scheduler.restart_container(profile)
    if ok:
        state.set_status(profile, "bound_healthy")
        return {"success": True, "message": f"Gateway {profile} 已重启"}
    raise HTTPException(500, "重启失败")


@app.get("/api/v1/gateway/{profile}/logs")
async def get_gateway_logs(profile: str, tail: int = 50):
    if not scheduler:
        raise HTTPException(503, "DockerScheduler 未初始化")
    logs = scheduler.get_container_logs(profile, tail=tail)
    return {"profile": profile, "logs": logs}


@app.post("/api/v1/pool/sync-models")
async def sync_models():
    """一键同步模型配置到所有微信用户。"""
    if not scheduler:
        return {"synced": 0, "message": "DockerScheduler 未初始化"}
    count, msg = gm.sync_model_config()
    return {"synced": count, "message": msg}


@app.get("/health")
async def health():
    container_count = scheduler.list_containers() if scheduler else []
    return {
        "status": "ok",
        "version": "3.0.0",
        "pool_stats": state.get_stats(),
        "containers": len(container_count),
    }


# ═════════════════════════════════════════════════════════════════════
# 后台任务
# ═════════════════════════════════════════════════════════════════════


@app.on_event("startup")
async def startup():
    global hot_pool, pool_task, scheduler, health_task

    prefix = config.get("pool", {}).get("profile_prefix", "weixin-")
    total = config.get("pool", {}).get("total_profiles", 100)

    # 加载历史状态
    state.load()

    # ── 初始化 DockerScheduler ──
    scheduler = ds.DockerScheduler(config)
    gm.set_scheduler(scheduler)

    # 确保 Docker 网络和镜像
    scheduler.ensure_network()
    if not scheduler.ensure_image():
        logger.warning("hermes-bot 镜像不存在！请先构建: docker build -f Dockerfile.bot -t hermes-bot:latest .")

    # ── 初始化 profile 列表（按编号生成）──
    profile_names = []
    for i in range(1, total + 1):
        profile = f"{prefix}{i:03d}"
        profile_names.append(profile)
    state.init_profiles(profile_names)

    # 重置过期热池状态
    for name in list(state.profiles.keys()):
        ps = state.profiles[name]
        if ps.get("status") in ("in_hot_pool", "qr_failed"):
            state.mark_available(name)
    logger.info("池状态已初始化，共 %d 个槽位", len(profile_names))

    # 启动 LLM Proxy
    llm_proxy.init_proxy()
    logger.info("LLM Proxy 就绪")

    # 启动热池
    hot_pool = HotPool(config, state, pm, gm)
    pool_task = asyncio.create_task(hot_pool.start())

    # 启动 Docker 健康检查循环
    health_interval = config.get("gateway", {}).get("health_check_interval", 60)
    max_restarts = config.get("gateway", {}).get("max_restart_attempts", 3)
    health_task = asyncio.create_task(scheduler.health_check_loop(
        interval=health_interval,
        max_restarts=max_restarts,
    ))

    logger.info("Pool Manager v3 (Docker 模式) 启动完成")


@app.on_event("shutdown")
async def shutdown():
    if hot_pool:
        await hot_pool.stop()
        if pool_task:
            pool_task.cancel()
    if health_task:
        health_task.cancel()
    if scheduler:
        scheduler.close()
    state.save()
    logger.info("Pool Manager 已关闭")


# ═════════════════════════════════════════════════════════════════════
# CLI 入口
# ═════════════════════════════════════════════════════════════════════


def main():
    parser = argparse.ArgumentParser(description="WeChat Gateway Pool Manager (Docker)")
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

    host = args.host or config.get("frontend", {}).get("host", "0.0.0.0")
    port = args.port or config.get("frontend", {}).get("api_port", 8765)

    logger.info("启动 Pool Manager v3 (Docker 模式): host=%s port=%d", host, port)

    uvicorn.run(app, host=host, port=port,
                log_level=config.get("logging", {}).get("level", "info").lower())


if __name__ == "__main__":
    main()
