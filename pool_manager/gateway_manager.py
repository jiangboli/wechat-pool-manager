"""Gateway 生命周期管理——通过 DockerScheduler 管理容器。

架构变化：
- 旧：每个微信用户一个 Linux 用户 + systemd system 服务
- 新：每个微信用户一个 Docker 容器，由 DockerScheduler 管理

数据目录：
  /home/data/{尾数}/{profile}/.hermes/   ← 尾数 = profile 编号末位
"""

import json
import logging
import os
import time
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("pool_manager.gateway_manager")

# DockerScheduler 实例（由 service.py 在 startup 时设置）
_scheduler = None  # type: ignore[type-arg]


def set_scheduler(scheduler):
    """设置 DockerScheduler 实例（在 service startup 时调用）。"""
    global _scheduler
    _scheduler = scheduler


def _check_scheduler():
    if _scheduler is None:
        raise RuntimeError("DockerScheduler 未初始化 — 请先调用 set_scheduler()")


# ── Gateway 生命周期 ────────────────────────────────────────────────


def start(profile: str) -> Tuple[bool, str]:
    """启动 gateway（创建并启动 Docker 容器）。"""
    _check_scheduler()
    try:
        ok = _scheduler.create_container(profile)
        return (ok, f"容器 {profile} 已{'创建' if ok else '创建失败'}")
    except Exception as e:
        return False, str(e)


def stop(profile: str) -> Tuple[bool, str]:
    """停止 gateway（停止 Docker 容器）。"""
    _check_scheduler()
    try:
        ok = _scheduler.stop_container(profile)
        return (ok, f"容器 {profile} 已{'停止' if ok else '停止失败'}")
    except Exception as e:
        return False, str(e)


def restart(profile: str) -> Tuple[bool, str]:
    """重启 gateway（重启 Docker 容器）。"""
    _check_scheduler()
    try:
        ok = _scheduler.restart_container(profile)
        return (ok, f"容器 {profile} 已{'重启' if ok else '重启失败'}")
    except Exception as e:
        return False, str(e)


def is_active(profile: str) -> Tuple[bool, str]:
    """检查 gateway 是否活跃（容器是否 running）。"""
    _check_scheduler()
    try:
        status = _scheduler.get_container_status(profile)
        running = status.get("running", False)
        state_str = status.get("status", "unknown")
        return running, state_str
    except Exception:
        return False, "unknown"


def status_detail(profile: str) -> dict:
    """获取 gateway 详细状态。"""
    _check_scheduler()
    try:
        return _scheduler.get_container_status(profile)
    except Exception as e:
        return {"profile": profile, "status": "error", "error": str(e)}


def remove(profile: str) -> Tuple[bool, str]:
    """删除 gateway（删除 Docker 容器，保留数据）。"""
    _check_scheduler()
    try:
        ok = _scheduler.remove_container(profile, keep_data=True)
        return (ok, f"容器 {profile} 已{'删除' if ok else '删除失败'}")
    except Exception as e:
        return False, str(e)


# ── 凭证池同步 ──────────────────────────────────────────────────────


def sync_model_config(new_model: str = "") -> Tuple[int, str]:
    """同步模型配置——在 Docker 模式下，模型配置在容器创建时已设置。
    
    如需切换模型，重新生成 config.yaml 并重启容器即可。
    """
    _check_scheduler()
    if not new_model:
        new_model = _get_default_model()
    
    containers = _scheduler.list_containers()
    count = 0
    for c in containers:
        profile = c["profile"]
        if c.get("running"):
            _scheduler.write_config(profile)
            count += 1
    
    return count, f"已同步 {count} 个运行容器的配置"


def sync_credential_pool() -> Tuple[int, str]:
    """凭证池同步——Docker 模式不需要，proxy 管理 key。"""
    return 0, "proxy 模式，不需要同步凭证池"


def _get_default_model() -> str:
    """从主配置读取默认模型名。"""
    try:
        import yaml
        cfg_path = os.path.expanduser("~/.hermes/config.yaml")
        if os.path.exists(cfg_path):
            with open(cfg_path) as f:
                cfg = yaml.safe_load(f) or {}
            model_cfg = cfg.get("model", {})
            if isinstance(model_cfg, str):
                return model_cfg
            return model_cfg.get("default", "deepseek-v4-flash")
    except Exception:
        pass
    return "deepseek-v4-flash"


# ── 容器管理（用于旧 API 兼容） ────────────────────────────────────


def list_all() -> List[dict]:
    """列出所有管理的容器。"""
    _check_scheduler()
    return _scheduler.list_containers()


def get_logs(profile: str, tail: int = 50) -> str:
    """获取容器日志。"""
    _check_scheduler()
    return _scheduler.get_container_logs(profile, tail)
