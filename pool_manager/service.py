"""Pool Manager 主服务——FastAPI 应用。

启动方式：
    python -m pool_manager.service --config /path/to/config.yaml
"""

import argparse
import asyncio
import json
import logging
import os
import sys
from pathlib import Path
from typing import Optional

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles

from .config import load_config, resolve_path
from .state import PoolState
from .profile_manager import list_profiles, batch_create, get_bound_count
from .hot_pool import HotPool
from . import gateway_manager as gm
from . import profile_manager as pm

# ── 日志配置 ──────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
logger = logging.getLogger("pool_manager")

# ── 全局实例 ──────────────────────────────────────────────────────────
app = FastAPI(title="WeChat Gateway Pool Manager", version="0.1.0")
config: dict = {}
state = PoolState()
hot_pool: Optional[HotPool] = None
pool_task: Optional[asyncio.Task] = None

# ── 静态文件 ──────────────────────────────────────────────────────────
STATIC_DIR = os.path.join(os.path.dirname(__file__), "../static")
if os.path.exists(STATIC_DIR):
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


# ═════════════════════════════════════════════════════════════════════
# API 端点
# ═════════════════════════════════════════════════════════════════════

@app.get("/")
async def root():
    """前端页面。"""
    index_path = os.path.join(STATIC_DIR, "index.html")
    if os.path.exists(index_path):
        return HTMLResponse(open(index_path, encoding="utf-8").read())
    return {"message": "Pool Manager running. Frontend not found."}


@app.get("/api/v1/pool/available")
async def get_available_qr():
    """获取一个可用的二维码。返回热池中最新槽位的 QR 码。"""
    if not hot_pool:
        raise HTTPException(503, "热池未启动")

    slots = hot_pool.get_all_slots()
    if not slots:
        # 没有活跃槽位
        raise HTTPException(503, "当前无可用二维码，请稍后再试")

    # 找一个 waiting 状态的
    for s in slots:
        if s["status"] == "waiting" and s["qr_url"]:
            return {
                "slot_id": s["profile"],
                "qr_url": s["qr_url"],
                "status": s["status"],
            }

    # 退回第一个
    s = slots[0]
    return {
        "slot_id": s["profile"],
        "qr_url": s["qr_url"],
        "status": s["status"],
    }


@app.get("/api/v1/pool/status/{slot_id}")
async def get_slot_status(slot_id: str):
    """查询某个槽位的状态。"""
    if not hot_pool:
        raise HTTPException(503, "热池未启动")

    slots = hot_pool.get_all_slots()
    for s in slots:
        if s["profile"] == slot_id:
            ps = state.profiles.get(slot_id, {})
            return {
                "slot_id": slot_id,
                "qr_status": s["status"],
                "bound": ps.get("status") == "bound_healthy",
                "bound_at": ps.get("bound_at", ""),
                "user_id": ps.get("user_id", ""),
            }

    # 不在热池中，查 state
    ps = state.profiles.get(slot_id, {})
    return {
        "slot_id": slot_id,
        "qr_status": "not_in_pool",
        "bound": ps.get("status", "") in ("bound_healthy", "bound_unhealthy", "bound_idle"),
        "bound_at": ps.get("bound_at", ""),
        "user_id": ps.get("user_id", ""),
    }


@app.get("/api/v1/pool/stats")
async def get_pool_stats():
    """池统计信息。"""
    return state.get_stats()


@app.get("/api/v1/gateways")
async def list_gateways():
    """列出所有 gateway 及其状态。"""
    prefix = config.get("pool", {}).get("profile_prefix", "weixin-")
    profiles = list_profiles(prefix)

    result = []
    for name in profiles:
        ps = state.profiles.get(name, {})
        active, act_str = gm.is_active(name)
        result.append({
            "profile": name,
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
    """手动启动一个 gateway。"""
    ok, msg = gm.start(profile)
    if ok:
        state.set_status(profile, "bound_healthy")
        return {"success": True, "message": f"Gateway {profile} 已启动"}
    raise HTTPException(500, f"启动失败: {msg}")


@app.post("/api/v1/gateway/{profile}/stop")
async def stop_gateway(profile: str):
    """手动停止一个 gateway。"""
    ok, msg = gm.stop(profile)
    if ok:
        state.set_status(profile, "bound_idle")
        return {"success": True, "message": f"Gateway {profile} 已停止"}
    raise HTTPException(500, f"停止失败: {msg}")


@app.post("/api/v1/gateway/{profile}/restart")
async def restart_gateway(profile: str):
    """重启 gateway。"""
    ok, msg = gm.restart(profile)
    if ok:
        state.set_status(profile, "bound_healthy")
        return {"success": True, "message": f"Gateway {profile} 已重启"}
    raise HTTPException(500, f"重启失败: {msg}")


@app.get("/health")
async def health():
    """健康检查。"""
    stats = state.get_stats()
    return {
        "status": "ok",
        "pool_stats": stats,
    }


# ═════════════════════════════════════════════════════════════════════
# 后台任务
# ═════════════════════════════════════════════════════════════════════

async def _health_check_loop():
    """定期健康检查——扫描所有 bound gateway。"""
    interval = config.get("gateway", {}).get("health_check_interval", 60)
    max_restarts = config.get("gateway", {}).get("max_restart_attempts", 3)

    while True:
        await asyncio.sleep(interval)
        prefix = config.get("pool", {}).get("profile_prefix", "weixin-")
        bound = state.get_by_status("bound_healthy") + state.get_by_status("bound_unhealthy")

        for name in bound:
            active, _ = gm.is_active(name)
            ps = state.profiles.get(name, {})
            if not active:
                # 尝试重启
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

        # 保存状态
        state.save()


async def _save_loop():
    """定期持久化状态。"""
    while True:
        await asyncio.sleep(30)
        state.save()


async def _idle_check_loop():
    """空闲检测——扫描 idle gateway。"""
    idle_timeout = config.get("gateway", {}).get("idle_timeout_minutes", 1440)

    while True:
        await asyncio.sleep(300)  # 每 5 分钟
        # 当前版本暂不实现自动 idle 回收（用户可能随时发消息）


# ═════════════════════════════════════════════════════════════════════
# 启动入口
# ═════════════════════════════════════════════════════════════════════

@app.on_event("startup")
async def startup():
    """服务启动时初始化。"""
    global hot_pool, pool_task

    prefix = config.get("pool", {}).get("profile_prefix", "weixin-")
    total = config.get("pool", {}).get("total_profiles", 100)

    # 加载历史状态
    state.load()

    # 初始化 profile 列表
    existing = list_profiles(prefix)
    if not existing:
        logger.info("尚未创建 profile，开始批量创建 %d 个...", total)
        created = batch_create(prefix, total)
        logger.info("已创建 %d 个 profile", len(created))
        existing = created

    state.init_profiles(existing)
    logger.info("池状态已初始化，共 %d 个 profile", len(existing))

    # 启动热池
    hot_pool = HotPool(config, state, pm, gm)
    pool_task = asyncio.create_task(hot_pool.start())

    # 启动后台任务
    asyncio.create_task(_health_check_loop())
    asyncio.create_task(_save_loop())
    asyncio.create_task(_idle_check_loop())

    logger.info("Pool Manager 启动完成")


@app.on_event("shutdown")
async def shutdown():
    """服务关闭时清理。"""
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
    parser.add_argument("--total", type=int, default=None, help="profile 总数")
    parser.add_argument("--hot-pool", type=int, default=None, help="热池大小")
    parser.add_argument("--max-bound", type=int, default=None, help="最大 bound gateway 数")
    parser.add_argument("--port", type=int, default=None, help="API 端口")
    parser.add_argument("--prefix", type=str, default=None, help="profile 名前缀")
    parser.add_argument("--host", type=str, default=None, help="监听地址")

    args = parser.parse_args()

    # 收集 CLI 覆盖
    cli_overrides = {
        "total": args.total,
        "hot_pool": args.hot_pool,
        "max_bound": args.max_bound,
        "port": args.port,
        "prefix": args.prefix,
    }

    global config
    config = load_config(args.config, cli_overrides)

    # 日志配置
    log_dir = resolve_path(config.get("logging", {}).get("dir", "~/.hermes/wechat-pool/logs/"))
    os.makedirs(log_dir, exist_ok=True)

    fh = logging.FileHandler(os.path.join(log_dir, "pool_manager.log"))
    fh.setFormatter(logging.Formatter("%(asctime)s [%(name)s] %(levelname)s: %(message)s"))
    logging.getLogger().addHandler(fh)
    logging.getLogger().setLevel(config.get("logging", {}).get("level", "INFO"))

    host = args.host or config.get("frontend", {}).get("host", "0.0.0.0")
    port = args.port or config.get("frontend", {}).get("api_port", 8765)

    logger.info("启动 Pool Manager: host=%s port=%d", host, port)
    logger.info("配置: total=%d hot_pool=%d max_bound=%d",
                config.get("pool", {}).get("total_profiles"),
                config.get("pool", {}).get("hot_pool_size"),
                config.get("pool", {}).get("max_bound_gateways"))

    uvicorn.run(app, host=host, port=port, log_level=config.get("logging", {}).get("level", "info").lower())


if __name__ == "__main__":
    main()