"""Pool Manager 主服务——FastAPI 应用。

启动方式：
    python -m pool_manager.service --config /path/to/config.yaml
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

# ── 日志 ──────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
logger = logging.getLogger("pool_manager")

# ── 全局实例 ──────────────────────────────────────────────────────────
app = FastAPI(title="WeChat Gateway Pool Manager", version="2.0.0")
config: dict = {}
state = PoolState()
hot_pool: Optional[HotPool] = None
pool_task: Optional[asyncio.Task] = None

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
    """列出所有 gateway 及其状态（基于 Linux 用户列表）。"""
    users = list_linux_users("wx")
    result = []
    for luser in users:
        profile = f"weixin-{luser.lstrip('wx')}"  # wx001 → weixin-001
        ps = state.profiles.get(profile, {})
        active, act_str = gm.is_active(profile)
        result.append({
            "profile": profile,
            "linux_user": luser,
            "status": ps.get("status", "unknown"),
            "active": active,
            "state": act_str,
            "bound_at": ps.get("bound_at", ""),
            "last_active": ps.get("last_active", ""),
            "user_id": ps.get("user_id", ""),
            "error": ps.get("error", ""),
        })
    return {"gateways": result, "total": len(result)}


@app.post("/api/v1/gateway/{profile}/start")
async def start_gateway(profile: str):
    ok, msg = gm.start(profile)
    if ok:
        state.set_status(profile, "bound_healthy")
        return {"success": True, "message": f"Gateway {profile} 已启动"}
    raise HTTPException(500, f"启动失败: {msg}")


@app.post("/api/v1/gateway/{profile}/stop")
async def stop_gateway(profile: str):
    ok, msg = gm.stop(profile)
    if ok:
        state.set_status(profile, "bound_idle")
        return {"success": True, "message": f"Gateway {profile} 已停止"}
    raise HTTPException(500, f"停止失败: {msg}")


@app.post("/api/v1/gateway/{profile}/restart")
async def restart_gateway(profile: str):
    ok, msg = gm.restart(profile)
    if ok:
        state.set_status(profile, "bound_healthy")
        return {"success": True, "message": f"Gateway {profile} 已重启"}
    raise HTTPException(500, f"重启失败: {msg}")


@app.post("/api/v1/pool/sync-models")
async def sync_models():
    """一键同步凭证池和模型配置到所有微信用户。"""
    count, msg = gm.sync_model_config()
    return {"synced": count, "message": msg}


@app.get("/health")
async def health():
    return {"status": "ok", "pool_stats": state.get_stats()}


# ═════════════════════════════════════════════════════════════════════
# 后台任务
# ═════════════════════════════════════════════════════════════════════


async def _health_check_loop():
    interval = config.get("gateway", {}).get("health_check_interval", 60)
    max_restarts = config.get("gateway", {}).get("max_restart_attempts", 3)
    while True:
        await asyncio.sleep(interval)
        bound = state.get_by_status("bound_healthy") + state.get_by_status("bound_unhealthy")
        for name in bound:
            active, _ = gm.is_active(name)
            ps = state.profiles.get(name, {})
            if not active:
                restart_count = ps.get("restart_count", 0) + 1
                state.profiles[name]["restart_count"] = restart_count
                if restart_count <= max_restarts:
                    logger.warning("[%s] gateway 不活跃，尝试重启（%d/%d）", name, restart_count, max_restarts)
                    ok, _ = gm.restart(name)
                    if ok:
                        state.set_status(name, "bound_healthy", restart_count=restart_count)
                    else:
                        state.set_status(name, "bound_unhealthy", error=f"重启失败 ({restart_count}/{max_restarts})")
                else:
                    state.set_status(name, "bound_unhealthy", error="超过最大重启次数")
        state.save()


async def _save_loop():
    while True:
        await asyncio.sleep(30)
        state.save()


async def _idle_check_loop():
    while True:
        await asyncio.sleep(300)


# ═════════════════════════════════════════════════════════════════════
# 启动入口
# ═════════════════════════════════════════════════════════════════════


@app.on_event("startup")
async def startup():
    global hot_pool, pool_task

    prefix = config.get("pool", {}).get("profile_prefix", "weixin-")
    total = config.get("pool", {}).get("total_profiles", 100)

    # 加载历史状态
    state.load()

    # 初始化 profile 列表（从 Linux 用户列表读取）
    existing_users = list_linux_users("wx")
    if not existing_users:
        logger.info("尚未创建 Linux 用户，开始批量创建 %d 个...", total)
        import subprocess
        script = os.path.join(os.path.dirname(__file__), "../scripts/create_profiles.py")
        subprocess.run(
            [sys.executable, script, "--count", str(total), "--prefix", "wx"],
            capture_output=True, timeout=120)
        existing_users = list_linux_users("wx")
        logger.info("已创建 %d 个 Linux 用户", len(existing_users))

    # 将 Linux 用户名转为 profile 名用于状态管理
    profile_names = []
    for luser in existing_users:
        # wx001 → weixin-001
        suffix = luser.lstrip("wx")
        profile = f"weixin-{suffix}"
        profile_names.append(profile)
    state.init_profiles(profile_names)

    # 重置过期热池状态
    for name in list(state.profiles.keys()):
        ps = state.profiles[name]
        if ps.get("status") in ("in_hot_pool", "qr_failed"):
            state.mark_available(name)
    logger.info("池状态已初始化，共 %d 个槽位", len(existing_users))

    # 启动 LLM Proxy
    llm_proxy.init_proxy()
    logger.info("LLM Proxy 就绪")

    # 启动热池
    hot_pool = HotPool(config, state, pm, gm)
    pool_task = asyncio.create_task(hot_pool.start())

    # 启动后台任务
    asyncio.create_task(_health_check_loop())
    asyncio.create_task(_save_loop())
    asyncio.create_task(_idle_check_loop())

    logger.info("Pool Manager v2 启动完成")


@app.on_event("shutdown")
async def shutdown():
    if hot_pool:
        await hot_pool.stop()
        if pool_task:
            pool_task.cancel()
    state.save()
    logger.info("Pool Manager 已关闭")


# ═════════════════════════════════════════════════════════════════════
# CLI 入口
# ═════════════════════════════════════════════════════════════════════


def main():
    parser = argparse.ArgumentParser(description="WeChat Gateway Pool Manager")
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

    logger.info("启动 Pool Manager v2: host=%s port=%d", host, port)

    uvicorn.run(app, host=host, port=port,
                log_level=config.get("logging", {}).get("level", "info").lower())


if __name__ == "__main__":
    main()
