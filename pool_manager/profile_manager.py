"""Profile 管理：创建、查找、状态跟踪。"""

import os
import subprocess
import sys
from typing import List, Optional

from .config import resolve_path

HERMES_HOME = os.path.expanduser("~/.hermes")


def _hermes_bin() -> str:
    """自动定位 hermes 命令。"""
    candidate = os.path.join(HERMES_HOME, "bin", "hermes")
    if os.path.exists(candidate):
        return candidate
    return "hermes"


def create_profile(name: str, clone_from: str = "") -> bool:
    """创建一个新的 Hermes profile。

    Args:
        name: profile 名称（如 weixin-001）
        clone_from: 从哪个现有 profile 克隆（空=全新）

    Returns:
        是否创建成功
    """
    cmd = [_hermes_bin(), "profile", "create", name, "--no-alias"]
    if clone_from:
        cmd += ["--clone-from", clone_from]

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=60,
            env={**os.environ, "HERMES_HOME": HERMES_HOME},
        )
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
        print(f"  hermes 命令未找到，请确认 Hermes 已安装", file=sys.stderr)
        return False


def batch_create(prefix: str, count: int, clone_from: str = "default") -> List[str]:
    """批量创建 N 个 profile。

    Args:
        prefix: profile 名前缀（如 weixin-）
        count: 创建数量
        clone_from: 克隆源 profile

    Returns:
        成功创建的 profile 名称列表
    """
    created = []
    for i in range(1, count + 1):
        name = f"{prefix}{i:03d}"
        if create_profile(name, clone_from):
            created.append(name)
        else:
            print(f"  跳过 {name}")
    return created


def list_profiles(prefix: str = "") -> List[str]:
    """列出所有符合条件的 profile。"""
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
    """获取 profile 目录路径。"""
    return os.path.join(HERMES_HOME, "profiles", name)


def profile_exists(name: str) -> bool:
    """检查 profile 是否存在。"""
    return os.path.isdir(get_profile_dir(name))


def set_weixin_credentials(profile: str, account_id: str, token: str, base_url: str = "") -> bool:
    """向 profile 的 .env 写入 WeChat 凭证。

    写入后 gateway 启动时会自动加载 WEIXIN_ACCOUNT_ID / WEIXIN_TOKEN。
    """
    env_path = os.path.join(get_profile_dir(profile), ".env")
    if not os.path.exists(env_path):
        return False

    try:
        with open(env_path, "r") as f:
            lines = f.readlines()
    except Exception:
        lines = []

    # 移除旧的 WEIXIN 配置行
    keep_keys = {"WEIXIN_ACCOUNT_ID=", "WEIXIN_TOKEN=", "WEIXIN_BASE_URL="}
    lines = [l for l in lines if not any(l.startswith(k) for k in keep_keys)]

    # 追加新配置
    lines.append(f"\n# WeChat credentials (auto-set by pool manager)\n")
    lines.append(f"WEIXIN_ACCOUNT_ID={account_id}\n")
    lines.append(f"WEIXIN_TOKEN={token}\n")
    if base_url:
        lines.append(f"WEIXIN_BASE_URL={base_url}\n")

    with open(env_path, "w") as f:
        f.writelines(lines)
    return True


def get_weixin_credentials(profile: str) -> Optional[dict]:
    """从 profile 的 .env 读取 WeChat 凭证。"""
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
    """统计已绑定的 gateway 数量。"""
    count = 0
    for name in list_profiles(prefix):
        creds = get_weixin_credentials(name)
        if creds and creds.get("token"):
            count += 1
    return count