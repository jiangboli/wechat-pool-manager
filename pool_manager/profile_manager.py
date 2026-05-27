"""Profile 管理：创建、查找、状态跟踪。"""

import os
import subprocess
import sys
from typing import List, Optional

from .config import resolve_path

HERMES_HOME = os.path.expanduser("~/.hermes")


def _hermes_bin() -> str:
    candidate = os.path.join(HERMES_HOME, "bin", "hermes")
    if os.path.exists(candidate):
        return candidate
    return "hermes"


def create_profile(name: str, clone_from: str = "") -> bool:
    cmd = [_hermes_bin(), "profile", "create", name, "--no-alias"]
    if clone_from:
        cmd += ["--clone-from", clone_from]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60,
                                env={**os.environ, "HERMES_HOME": HERMES_HOME})
        if result.returncode != 0:
            if "already exists" in result.stderr:
                return True
            print(f"  创建 profile {name} 失败: {result.stderr.strip()}", file=sys.stderr)
            return False
        return True
    except subprocess.TimeoutExpired:
        print(f"  创建 profile {name} 超时", file=sys.stderr)
        return False
    except FileNotFoundError:
        print(f"  hermes 命令未找到", file=sys.stderr)
        return False


def batch_create(prefix: str, count: int, clone_from: str = "default") -> List[str]:
    created = []
    for i in range(1, count + 1):
        name = f"{prefix}{i:03d}"
        if create_profile(name, clone_from):
            created.append(name)
    return created


def list_profiles(prefix: str = "") -> List[str]:
    profiles_dir = os.path.join(HERMES_HOME, "profiles")
    if not os.path.isdir(profiles_dir):
        return []
    result = []
    for name in sorted(os.listdir(profiles_dir)):
        if prefix and not name.startswith(prefix):
            continue
        if os.path.isdir(os.path.join(profiles_dir, name)):
            result.append(name)
    return result


def get_profile_dir(name: str) -> str:
    return os.path.join(HERMES_HOME, "profiles", name)


def profile_exists(name: str) -> bool:
    return os.path.isdir(get_profile_dir(name))


def set_weixin_credentials(profile: str, account_id: str, token: str, base_url: str = "") -> bool:
    """向 profile 写入 WeChat 凭证和配置。"""
    profile_dir = get_profile_dir(profile)

    # 1. 更新 .env
    env_path = os.path.join(profile_dir, ".env")
    try:
        with open(env_path, "r") as f:
            lines = f.readlines()
    except Exception:
        lines = []
    keep = {"WEIXIN_ACCOUNT_ID=", "WEIXIN_TOKEN=", "WEIXIN_BASE_URL=", "WEIXIN_DM_POLICY="}
    lines = [l for l in lines if not any(l.startswith(k) for k in keep)]
    lines.append("")
    lines.append("# WeChat credentials (auto-set by pool manager)")
    lines.append("WEIXIN_ACCOUNT_ID=" + account_id)
    lines.append("WEIXIN_TOKEN=" + token)
    lines.append("WEIXIN_DM_POLICY=open")
    if base_url:
        lines.append("WEIXIN_BASE_URL=" + base_url)
    with open(env_path, "w") as f:
        for line in lines:
            f.write(line + "\n")

    # 2. 更新 config.yaml
    cfg_path = os.path.join(profile_dir, "config.yaml")
    try:
        import yaml
        if os.path.exists(cfg_path):
            with open(cfg_path, "r") as f:
                cfg = yaml.safe_load(f) or {}
        else:
            cfg = {}
        if "platforms" not in cfg:
            cfg["platforms"] = {}
        if "weixin" not in cfg["platforms"]:
            cfg["platforms"]["weixin"] = {}
        wx = cfg["platforms"]["weixin"]
        wx["enabled"] = True
        if "extra" not in wx:
            wx["extra"] = {}
        wx["extra"]["dm_policy"] = "open"
        wx["extra"]["group_policy"] = "disabled"
        with open(cfg_path, "w") as f:
            yaml.dump(cfg, f, default_flow_style=False, allow_unicode=True)
    except Exception:
        pass
    return True


def get_weixin_credentials(profile: str) -> Optional[dict]:
    env_path = os.path.join(get_profile_dir(profile), ".env")
    if not os.path.exists(env_path):
        return None
    try:
        with open(env_path, "r") as f:
            content = f.read()
    except Exception:
        return None
    result = {}
    for line in content.splitlines():
        line = line.strip()
        if line.startswith("WEIXIN_ACCOUNT_ID="):
            result["account_id"] = line.split("=", 1)[1]
        elif line.startswith("WEIXIN_TOKEN="):
            result["token"] = line.split("=", 1)[1]
        elif line.startswith("WEIXIN_BASE_URL="):
            result["base_url"] = line.split("=", 1)[1]
    return result or None


def get_bound_count(prefix: str = "weixin-") -> int:
    count = 0
    for name in list_profiles(prefix):
        creds = get_weixin_credentials(name)
        if creds and creds.get("token"):
            count += 1
    return count
