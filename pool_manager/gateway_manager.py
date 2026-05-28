"""Gateway 生命周期管理——通过 sudo systemctl 管理系统级服务。

架构变更 (2026-05-28):
- 从 systemd --user (所有 profile 跑在 dosh 下) 改为系统级 systemd 服务
- 每个 profile 对应一个 Linux 用户 (wxNNN)
- 使用 passwordless sudo (已配置 /etc/sudoers.d/hermes-pool)
- 实现用户间的文件系统隔离
"""

import os
import subprocess
import sys
import time
from typing import Tuple, Optional

SERVICE_TEMPLATE = "hermes-gateway@%s"
POOL_SERVICE = "hermes-pool"

# 密码缓存（首次 sudo 时设置，后续复用）
_SUDO_PASSWORD: Optional[str] = None

def _get_sudo_env() -> dict:
    """返回给 subprocess 的环境——确保 sudo -n 能工作。"""
    env = os.environ.copy()
    # 确保 sudo 不使用 TTY（passwordless sudo 已配置）
    env.setdefault("SUDO_ASKPASS", "/usr/bin/false")
    return env


def _sudo(*args: str) -> Tuple[int, str]:
    """通过 sudo 执行命令（passwordless via sudoers）。"""
    try:
        result = subprocess.run(
            ["sudo", "-n"] + list(args),
            capture_output=True,
            text=True,
            timeout=30,
            env=_get_sudo_env(),
        )
        return result.returncode, (result.stdout + result.stderr).strip()
    except subprocess.TimeoutExpired:
        return -1, "timeout"
    except FileNotFoundError:
        return -2, "sudo not found"
    except Exception as e:
        return -3, str(e)


def _sudo_popen(args: list, timeout: int = 30) -> Tuple[int, str]:
    """通过 sudo 执行命令，返回输出。"""
    try:
        result = subprocess.run(
            ["sudo", "-n"] + args,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=_get_sudo_env(),
        )
        return result.returncode, (result.stdout + result.stderr).strip()
    except subprocess.TimeoutExpired:
        return -1, "timeout"


# ── Linux 用户管理 ──────────────────────────────────────────────────


def create_linux_user(username: str) -> Tuple[bool, str]:
    """创建一个 Linux 用户用于 gateway 隔离。"""
    # 先检查是否已存在
    rc, out = _sudo("id", username)
    if rc == 0:
        return True, f"用户 {username} 已存在"

    rc, out = _sudo("useradd", "-m", username)
    if rc == 0:
        return True, f"用户 {username} 已创建"
    return False, f"创建用户失败: {out}"


def delete_linux_user(username: str) -> Tuple[bool, str]:
    """删除 Linux 用户及其 home。"""
    rc, out = _sudo("userdel", "-r", username)
    if rc == 0:
        # 清理遗留目录
        _sudo("rm", "-rf", f"/home/{username}")
        return True, f"用户 {username} 已删除"
    return False, f"删除用户失败: {out}"


def linux_user_exists(username: str) -> bool:
    rc, _ = _sudo("id", username)
    return rc == 0


# ── Profile 目录管理 ────────────────────────────────────────────────


def ensure_profile_home(username: str) -> Tuple[bool, str]:
    """确保用户的 .hermes 目录存在且权限正确。"""
    hermes_dir = f"/home/{username}/.hermes"
    # 创建目录
    rc, out = _sudo("mkdir", "-p", hermes_dir)
    if rc != 0:
        return False, f"创建 .hermes 失败: {out}"
    # 设置 owner
    rc, out = _sudo("chown", "-R", f"{username}:{username}", f"/home/{username}")
    if rc != 0:
        return False, f"chown 失败: {out}"
    return True, hermes_dir


def write_hermes_env(username: str, env_vars: dict) -> Tuple[bool, str]:
    """向用户的 .env 写入环境变量。"""
    env_path = f"/home/{username}/.hermes/.env"
    lines = []
    for key, value in env_vars.items():
        if value:
            lines.append(f"{key}={value}")

    content = "\n".join(lines) + "\n"

    # 写入临时文件然后 sudo cp
    import tempfile
    with tempfile.NamedTemporaryFile(mode='w', suffix='.env', delete=False) as f:
        f.write(content)
        tmp = f.name

    rc, out = _sudo("cp", tmp, env_path)
    os.unlink(tmp)
    if rc != 0:
        return False, f"写入 .env 失败: {out}"

    # chown
    rc, _ = _sudo("chown", f"{username}:{username}", env_path)
    return rc == 0, env_path


def write_hermes_config(username: str, config_data: dict) -> Tuple[bool, str]:
    """向用户写入 config.yaml。"""
    import yaml
    import tempfile

    cfg_path = f"/home/{username}/.hermes/config.yaml"
    content = yaml.dump(config_data, default_flow_style=False, allow_unicode=True)

    with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as f:
        f.write(content)
        tmp = f.name

    rc, out = _sudo("cp", tmp, cfg_path)
    os.unlink(tmp)
    if rc != 0:
        return False, f"写入 config.yaml 失败: {out}"

    rc, _ = _sudo("chown", f"{username}:{username}", cfg_path)
    return rc == 0, cfg_path


# ── Gateway 生命周期 ────────────────────────────────────────────────


def _linux_user_from_profile(profile: str) -> str:
    """从 profile 名 (weixin-001) 推导 Linux 用户名 (wx001)。"""
    return profile.replace("weixin-", "wx")


def start(profile: str) -> Tuple[bool, str]:
    """启动指定 profile 的 gateway（以 Linux 用户身份）。"""
    luser = _linux_user_from_profile(profile)

    # 确保 Linux 用户存在
    if not linux_user_exists(luser):
        ok, msg = create_linux_user(luser)
        if not ok:
            return False, msg

    # 启动系统服务
    svc = SERVICE_TEMPLATE % luser
    rc, out = _sudo("systemctl", "start", svc)
    if rc == 0:
        time.sleep(3)
        ok, _ = is_active(profile)
        return ok, out
    return False, out


def stop(profile: str) -> Tuple[bool, str]:
    luser = _linux_user_from_profile(profile)
    svc = SERVICE_TEMPLATE % luser
    rc, out = _sudo("systemctl", "stop", svc)
    return rc == 0, out


def restart(profile: str) -> Tuple[bool, str]:
    luser = _linux_user_from_profile(profile)
    svc = SERVICE_TEMPLATE % luser
    rc, out = _sudo("systemctl", "restart", svc)
    time.sleep(2)
    ok, _ = is_active(profile)
    return ok, out


def is_active(profile: str) -> Tuple[bool, str]:
    luser = _linux_user_from_profile(profile)
    svc = SERVICE_TEMPLATE % luser
    rc, out = _sudo("systemctl", "is-active", svc)
    return rc == 0 and out.strip() == "active", out.strip()


def status_detail(profile: str) -> dict:
    luser = _linux_user_from_profile(profile)
    svc = SERVICE_TEMPLATE % luser
    rc, out = _sudo("systemctl", "status", svc, "--no-pager", "--lines=5")
    active, state_str = is_active(profile)
    return {
        "profile": profile,
        "linux_user": luser,
        "active": active,
        "state": state_str,
        "raw_output": out[:1000],
    }


# ── 旧兼容：保持签名一致 ────────────────────────────────────────────


def is_bound_gateway_running(profile: str) -> bool:
    active, _ = is_active(profile)
    return active


def start_pool_manager() -> Tuple[bool, str]:
    rc, out = _sudo("systemctl", "start", POOL_SERVICE)
    return rc == 0, out


def stop_pool_manager() -> Tuple[bool, str]:
    rc, out = _sudo("systemctl", "stop", POOL_SERVICE)
    return rc == 0, out


def daemon_reload() -> Tuple[bool, str]:
    rc, out = _sudo("systemctl", "daemon-reload")
    return rc == 0, out


def enable_template_service() -> Tuple[bool, str]:
    rc, out = _sudo("systemctl", "enable", SERVICE_TEMPLATE % "")
    return rc == 0, out