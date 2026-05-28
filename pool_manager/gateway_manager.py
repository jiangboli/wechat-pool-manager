"""Gateway 生命周期管理——通过 sudo systemctl 管理系统级服务。

架构：
- 每个微信用户对应一个独立 Linux 用户（wxNNN）
- 系统级 systemd 服务 hermes-gateway@wxNNN.service
- 使用 passwordless sudo（/etc/sudoers.d/hermes-pool）
- API key 通过 proxy（pool manager :8765）管理，不写入用户文件

安全设计：
- write_hermes_env 仅写入微信凭证，不写入任何 API key
- 模型配置的 base_url 指向本地 proxy（127.0.0.1:8765/v1）
- 凭证池同步仅用于管理目的，API key 不进入 wx 用户的任何文件
"""

import json
import logging
import os
import subprocess
import sys
import time
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger("pool_manager.gateway_manager")

SERVICE_TEMPLATE = "hermes-gateway@%s"
POOL_SERVICE = "hermes-pool"

SUDOERS_RULES = [
    "/usr/bin/systemctl",
    "/usr/sbin/useradd",
    "/usr/sbin/userdel",
    "/usr/bin/mkdir",
    "/usr/bin/chown",
    "/usr/bin/chmod",
    "/usr/bin/cp",
    "/usr/bin/rm",
    "/usr/bin/cat",
    "/usr/bin/ln",
    "/usr/bin/id",
]


def _get_sudo_env() -> dict:
    env = os.environ.copy()
    env.setdefault("SUDO_ASKPASS", "/usr/bin/false")
    return env


def _sudo(*args: str) -> Tuple[int, str]:
    try:
        result = subprocess.run(
            ["sudo", "-n"] + list(args),
            capture_output=True, text=True, timeout=30,
            env=_get_sudo_env(),
        )
        return result.returncode, (result.stdout + result.stderr).strip()
    except subprocess.TimeoutExpired:
        return -1, "timeout"
    except FileNotFoundError:
        return -2, "sudo not found"
    except Exception as e:
        return -3, str(e)


# ── Linux 用户管理 ──────────────────────────────────────────────────


def create_linux_user(username: str) -> Tuple[bool, str]:
    rc, out = _sudo("id", username)
    if rc == 0:
        return True, f"用户 {username} 已存在"
    rc, out = _sudo("useradd", "-m", username)
    if rc == 0:
        return True, f"用户 {username} 已创建"
    return False, f"创建用户失败: {out}"


def delete_linux_user(username: str) -> Tuple[bool, str]:
    rc, out = _sudo("userdel", "-r", username)
    if rc == 0:
        _sudo("rm", "-rf", f"/home/{username}")
        return True, f"用户 {username} 已删除"
    return False, f"删除用户失败: {out}"


def linux_user_exists(username: str) -> bool:
    rc, _ = _sudo("id", username)
    return rc == 0


# ── Profile 目录管理 ────────────────────────────────────────────────


def ensure_profile_home(username: str) -> Tuple[bool, str]:
    hermes_dir = f"/home/{username}/.hermes"
    rc, out = _sudo("mkdir", "-p", hermes_dir)
    if rc != 0:
        return False, f"创建 .hermes 失败: {out}"
    rc, out = _sudo("chown", "-R", f"{username}:{username}", f"/home/{username}")
    if rc != 0:
        return False, f"chown 失败: {out}"
    return True, hermes_dir


def write_hermes_env(username: str, env_vars: dict) -> Tuple[bool, str]:
    """写入 .env 文件——仅限微信凭证，不写 API key。"""
    env_path = f"/home/{username}/.hermes/.env"
    lines = []
    for key, value in env_vars.items():
        if value:
            lines.append(f"{key}={value}")

    content = "\n".join(lines) + "\n"

    import tempfile
    with tempfile.NamedTemporaryFile(mode='w', suffix='.env', delete=False) as f:
        f.write(content)
        tmp = f.name

    rc, out = _sudo("cp", tmp, env_path)
    os.unlink(tmp)
    if rc != 0:
        return False, f"写入 .env 失败: {out}"
    rc, _ = _sudo("chown", f"{username}:{username}", env_path)
    return rc == 0, env_path


def write_hermes_config(username: str, config_data: dict) -> Tuple[bool, str]:
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
    return profile.replace("weixin-", "wx")


def start(profile: str) -> Tuple[bool, str]:
    luser = _linux_user_from_profile(profile)
    if not linux_user_exists(luser):
        ok, msg = create_linux_user(luser)
        if not ok:
            return False, msg
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
    active, state_str = is_active(profile)
    return {
        "profile": profile,
        "linux_user": luser,
        "active": active,
        "state": state_str,
    }


# ── 凭证池同步 ──────────────────────────────────────────────────────


def sync_credential_pool() -> Tuple[int, str]:
    """将 dosh 的 credential pool 复制到所有已创建的 Linux 用户。

    在 pool manager 进程中（dosh 下运行），dosh 的 auth.json 完全可读。
    但 wx 用户的 gateway 进程运行在各自 Linux 用户下，无法读 dosh 的 auth.json。

    由于我们改用 proxy 架构（API key 在 pool manager 内存），
    wx 用户不再需要 credential pool。此方法保留用于：
    1. 状态检查
    2. 后续扩展

    返回 (用户数, 状态说明)。
    """
    logger.warning("sync_credential_pool: 当前架构使用 proxy 管理 key，不需要同步到用户")
    return 0, "proxy 模式，不需要同步凭证池"


def sync_model_config(new_model: str = "") -> Tuple[int, str]:
    """更新所有 wx 用户的 config.yaml 中的模型名。

    用于切换模型时同步 wx 用户的 model.default。

    Args:
        new_model: 新模型名。为空时从 dosh 的 config.yaml 读取。

    Returns: (更新的用户数, 状态说明)
    """
    import yaml

    # 读取 dosh 的模型配置
    main_config_path = os.path.expanduser("~/.hermes/config.yaml")
    if not os.path.exists(main_config_path):
        return 0, "dosh config.yaml 不存在"

    with open(main_config_path) as f:
        main_cfg = yaml.safe_load(f) or {}

    model_cfg = main_cfg.get("model", {})
    if isinstance(model_cfg, str):
        model_cfg = {"default": model_cfg}

    model_default = new_model or model_cfg.get("default", "deepseek-v4-flash")

    # 遍历所有 wx 用户
    users = _list_wx_users()
    count = 0

    for luser in users:
        cfg_path = f"/home/{luser}/.hermes/config.yaml"
        raw = _sudo_read(cfg_path)
        if raw is None:
            continue

        try:
            cfg = yaml.safe_load(raw) or {}
        except Exception:
            continue

        # 更新 model.default
        if "model" not in cfg:
            cfg["model"] = {}
        if isinstance(cfg["model"], str):
            cfg["model"] = {"default": cfg["model"]}
        cfg["model"]["default"] = model_default
        cfg["model"]["provider"] = "custom"
        cfg["model"]["base_url"] = "http://127.0.0.1:8765/v1"

        # 写入
        tmp = f"/tmp/_sync_model_{luser}.yaml"
        with open(tmp, "w") as f:
            yaml.dump(cfg, f, default_flow_style=False, allow_unicode=True)
        rc, _ = _sudo("cp", tmp, cfg_path)
        os.unlink(tmp)
        if rc == 0:
            _sudo("chown", f"{luser}:{luser}", cfg_path)
            count += 1

        # 重启 gateway
        _sudo("systemctl", "restart", SERVICE_TEMPLATE % luser)

    return count, f"已同步 {count} 个用户的模型配置"


def _list_wx_users() -> List[str]:
    """列出所有 wx Linux 用户。"""
    if not os.path.isdir("/home"):
        return []
    return sorted([d for d in os.listdir("/home") if d.startswith("wx")])


def _sudo_read(path: str) -> Optional[str]:
    """通过 sudo cat 读取文件内容。"""
    rc, out = _sudo("cat", path)
    if rc == 0:
        return out
    return None


# ── Systemd ─────────────────────────────────────────────────────────


def daemon_reload() -> Tuple[bool, str]:
    rc, out = _sudo("systemctl", "daemon-reload")
    return rc == 0, out


def enable_template_service() -> Tuple[bool, str]:
    rc, out = _sudo("systemctl", "enable", SERVICE_TEMPLATE % "")
    return rc == 0, out
