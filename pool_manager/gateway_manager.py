"""Gateway 生命周期管理——通过 systemctl --user 控制 per-profile gateway。"""

import os
import subprocess
import time
from typing import Tuple

SERVICE_TEMPLATE = "hermes-gateway@%s"
POOL_SERVICE = "hermes-pool"


def _systemctl(*args: str) -> Tuple[int, str]:
    """执行 systemctl --user 命令。"""
    try:
        result = subprocess.run(
            ["systemctl", "--user"] + list(args),
            capture_output=True,
            text=True,
            timeout=30,
        )
        return result.returncode, result.stdout + result.stderr
    except subprocess.TimeoutExpired:
        return -1, "timeout"
    except FileNotFoundError:
        return -2, "systemctl not found"


def start(profile: str) -> Tuple[bool, str]:
    """启动指定 profile 的 gateway 服务。"""
    svc = SERVICE_TEMPLATE % profile
    rc, out = _systemctl("start", svc)
    if rc == 0:
        # 等两秒确认启动
        time.sleep(2)
        ok, _ = is_active(profile)
        return ok, out
    return False, out


def stop(profile: str) -> Tuple[bool, str]:
    """停止指定 profile 的 gateway。"""
    svc = SERVICE_TEMPLATE % profile
    rc, out = _systemctl("stop", svc)
    return rc == 0, out


def restart(profile: str) -> Tuple[bool, str]:
    """重启 gateway。"""
    svc = SERVICE_TEMPLATE % profile
    rc, out = _systemctl("restart", svc)
    time.sleep(2)
    ok, _ = is_active(profile)
    return ok, out


def is_active(profile: str) -> Tuple[bool, str]:
    """检查 gateway 是否 active。"""
    svc = SERVICE_TEMPLATE % profile
    rc, out = _systemctl("is-active", svc)
    return rc == 0 and out.strip() == "active", out.strip()


def status_detail(profile: str) -> dict:
    """获取 gateway 详细状态。"""
    svc = SERVICE_TEMPLATE % profile
    rc, out = _systemctl("status", svc, "--no-pager", "--lines=5")

    active, state_str = is_active(profile)

    # 从 status 输出中提取重启次数
    restart_count = 0
    for line in out.splitlines():
        if "Active:" in line and "restart" in line:
            import re
            m = re.search(r"(\d+)\s+(s \w+ ago|min \w+ ago)", line)
            # Not exact but gives us a rough idea
        if "Main PID:" in line:
            pass

    return {
        "profile": profile,
        "active": active,
        "state": state_str,
        "raw_output": out[:1000],
    }


def is_bound_gateway_running(profile: str) -> bool:
    """快速检查 gateway 是否运行中。"""
    active, _ = is_active(profile)
    return active


def start_pool_manager() -> Tuple[bool, str]:
    """启动 Pool Manager 自身服务。"""
    rc, out = _systemctl("start", POOL_SERVICE)
    return rc == 0, out


def stop_pool_manager() -> Tuple[bool, str]:
    """停止 Pool Manager。"""
    rc, out = _systemctl("stop", POOL_SERVICE)
    return rc == 0, out


def enable_template_service() -> Tuple[bool, str]:
    """启用 systemd 模板服务，使 @.service 可被实例化。"""
    rc, out = _systemctl("enable", SERVICE_TEMPLATE % "")
    return rc == 0, out


def daemon_reload() -> Tuple[bool, str]:
    """重新加载 systemd 用户配置。"""
    rc, out = _systemctl("daemon-reload")
    return rc == 0, out