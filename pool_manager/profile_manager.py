"""Profile 管理器——创建 Linux 用户、写入配置。

不再使用 Hermes profile 模式。每个微信用户对应一个独立 Linux 用户，
以 Unix 文件权限实现用户间数据隔离。

安全设计：
- .env 仅包含微信凭证（account_id, token），无任何 API key
- config.yaml 仅包含 platforms + model 配置，无 api_key
- 模型请求通过本地 proxy 转发（base_url=http://localhost:8765/v1）
- API key 只在 pool manager 进程内存中
- 不设任何工具集限制（platform_toolsets / disabled_toolsets 均不写）
"""

import os
import subprocess
import sys
from typing import Dict, List, Optional, Tuple

# 从启动模块获取 HERMES_HOME
HERMES_HOME = os.environ.get(
    "HERMES_HOME",
    os.path.expanduser("~/.hermes")
)


def linux_username(profile: str) -> str:
    """从 profile 名 (weixin-001) 推导 Linux 用户名 (wx001)。"""
    return profile.replace("weixin-", "wx")


def setup_linux_profile(profile: str, credentials: dict) -> bool:
    """在 Linux 用户的 home 下创建完整的 Hermes 配置。

    流程：
    1. 确保 Linux 用户存在
    2. 创建 ~/.hermes/ 目录
    3. 写入 .env（仅微信凭证）
    4. 写入 config.yaml（platforms + model，无工具限制，无 API key）
    """
    from . import gateway_manager as gm

    luser = linux_username(profile)
    hermes_dir = f"/home/{luser}/.hermes"

    # 1. 确保用户存在
    ok, msg = gm.create_linux_user(luser)
    if not ok:
        print(f"  [!] 创建 Linux 用户 {luser} 失败: {msg}", file=sys.stderr)
        return False

    # 2. 创建 .hermes/
    ok, msg = gm.ensure_profile_home(luser)
    if not ok:
        print(f"  [!] 创建 .hermes 失败: {msg}", file=sys.stderr)
        return False

    # 3. 写入 .env — 仅微信凭证，无 API key
    env_vars = {
        "WEIXIN_ACCOUNT_ID": credentials.get("account_id", ""),
        "WEIXIN_TOKEN": credentials.get("token", ""),
        "WEIXIN_ALLOW_ALL_USERS": "true",
        "WEIXIN_BASE_URL": credentials.get("base_url", ""),
        "WEIXIN_HOME_CHANNEL": credentials.get("user_id", ""),
    }

    ok, msg = gm.write_hermes_env(luser, env_vars)
    if not ok:
        print(f"  [!] 写入 .env 失败: {msg}", file=sys.stderr)
        return False

    # 4. 写入 config.yaml
    #    不设 platform_toolsets 和 agent.disabled_toolsets（全权限放开）
    #    不包含任何 api_key
    config = {
        "platforms": {
            "weixin": {
                "enabled": True,
                "extra": {
                    "dm_policy": "open",
                    "group_policy": "disabled",
                },
            },
        },
        "model": {
            "default": "deepseek-v4-flash",
            "provider": "custom",
            "base_url": "http://127.0.0.1:8765/v1",
        },
    }

    ok, msg = gm.write_hermes_config(luser, config)
    if not ok:
        print(f"  [!] 写入 config.yaml 失败: {msg}", file=sys.stderr)
        return False

    # 5. 从 Linux 用户列表初始化状态
    print(f"  [OK] {luser} 配置完成（微信凭证 + proxy 模式）")
    return True


def update_credentials(profile: str, credentials: dict) -> bool:
    """更新已存在的 Linux 用户的微信凭证。

    用于去重场景：同一个微信用户二次扫码时复用已有用户。
    """
    from . import gateway_manager as gm

    luser = linux_username(profile)
    env_vars = {
        "WEIXIN_ACCOUNT_ID": credentials.get("account_id", ""),
        "WEIXIN_TOKEN": credentials.get("token", ""),
        "WEIXIN_ALLOW_ALL_USERS": "true",
        "WEIXIN_BASE_URL": credentials.get("base_url", ""),
        "WEIXIN_HOME_CHANNEL": credentials.get("user_id", ""),
    }

    ok, msg = gm.write_hermes_env(luser, env_vars)
    if ok:
        print(f"  [OK] {luser} 凭证已更新")
        return True
    print(f"  [!] {luser} 更新凭证失败: {msg}", file=sys.stderr)
    return False


# ── 查询 ────────────────────────────────────────────────────────────────


def list_linux_users(prefix: str = "wx") -> List[str]:
    """列出已创建的 Linux 用户。"""
    if not os.path.isdir("/home"):
        return []
    result = []
    for name in sorted(os.listdir("/home")):
        if name.startswith(prefix):
            # 确认是系统用户（有 /home 目录）
            if os.path.isdir(f"/home/{name}"):
                result.append(name)
    return result


def get_weixin_credentials(profile: str) -> Optional[dict]:
    """从 Linux 用户的 .env 读取微信凭证。"""
    luser = linux_username(profile)
    env_path = f"/home/{luser}/.hermes/.env"
    if not os.path.exists(env_path):
        return None
    try:
        with open(env_path) as f:
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


def get_bound_count(prefix: str = "wx") -> int:
    """统计已绑定（有凭证）的 Linux 用户数。"""
    count = 0
    for name in list_linux_users(prefix):
        luser = name  # list_linux_users 直接返回 wx001 格式
        env_path = f"/home/{luser}/.hermes/.env"
        if os.path.exists(env_path):
            with open(env_path) as f:
                if "WEIXIN_TOKEN=" in f.read():
                    count += 1
    return count
