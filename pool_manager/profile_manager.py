"""Profile 管理器——创建、列表、绑定、凭证读写。"""

import os
import subprocess
import sys
from typing import Dict, List, Optional

# 从启动模块获取 HERMES_HOME
HERMES_HOME = os.environ.get(
    "HERMES_HOME",
    os.path.expanduser("~/.hermes")
)


def _hermes_bin() -> str:
    # 首选 ~/.local/bin/hermes（标准安装路径），回退 ~/.hermes/bin/hermes
    local_bin = os.path.expanduser("~/.local/bin/hermes")
    if os.path.exists(local_bin):
        return local_bin
    return os.path.expanduser("~/.hermes/bin/hermes")


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


def linux_username(profile: str) -> str:
    """从 profile 名 (weixin-001) 推导 Linux 用户名 (wx001)。"""
    return profile.replace("weixin-", "wx")


def setup_linux_profile(profile: str, credentials: dict, api_env: dict = None) -> bool:
    """在 Linux 用户的 home 下创建完整的 Hermes 配置。

    流程：
    1. 确保 Linux 用户存在
    2. 创建 ~/.hermes/ 目录
    3. 写入 .env（微信凭证 + API Keys）
    4. 写入 config.yaml（平台配置 + 工具权限）
    """
    luser = linux_username(profile)
    hermes_dir = f"/home/{luser}/.hermes"

    # 1. 确保用户存在
    from . import gateway_manager as gm
    ok, msg = gm.create_linux_user(luser)
    if not ok:
        print(f"  [!] 创建 Linux 用户 {luser} 失败: {msg}", file=sys.stderr)
        return False
    print(f"  [OK] Linux 用户 {luser} 就绪")

    # 2. 创建 .hermes/
    ok, msg = gm.ensure_profile_home(luser)
    if not ok:
        print(f"  [!] 创建 .hermes 失败: {msg}", file=sys.stderr)
        return False
    print(f"  [OK] {hermes_dir} 已创建")

    # 3. 写入 .env
    env_vars = {
        "WEIXIN_ACCOUNT_ID": credentials.get("account_id", ""),
        "WEIXIN_TOKEN": credentials.get("token", ""),
        "WEIXIN_ALLOW_ALL_USERS": "true",
        "WEIXIN_BASE_URL": credentials.get("base_url", ""),
        "WEIXIN_HOME_CHANNEL": credentials.get("user_id", ""),
    }
    # 合并 API Key 等全局环境变量
    if api_env:
        for k, v in api_env.items():
            if v:
                env_vars[k] = v

    ok, msg = gm.write_hermes_env(luser, env_vars)
    if not ok:
        print(f"  [!] 写入 .env 失败: {msg}", file=sys.stderr)
        return False
    print(f"  [OK] .env 已写入")

    # 4. 写入 config.yaml
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
        "platform_toolsets": {
            "weixin": ["web", "clarify", "todo", "vision"],
        },
        "agent": {
            "disabled_toolsets": [
                "terminal", "file", "code_execution", "cronjob",
                "delegation", "skills", "messaging", "browser",
                "session_search", "memory",
            ],
        },
    }

    if api_env:
        # 如果有模型配置，写入
        provider = api_env.get("PROVIDER", "")
        model = api_env.get("MODEL", "")
        base_url = api_env.get("BASE_URL", "")
        api_key = api_env.get("API_KEY", "")
        if provider and api_key:
            config["model"] = {
                "provider": provider,
                "default": model or "deepseek-v4-flash",
            }
            # api_key 通过 .env 注入，不在 config.yaml 中明文存储

    ok, msg = gm.write_hermes_config(luser, config)
    if not ok:
        print(f"  [!] 写入 config.yaml 失败: {msg}", file=sys.stderr)
        return False
    print(f"  [OK] config.yaml 已写入")

    return True


    # ── 微信安全工具集配置 ──────────────────────────────────────────────────

_SAFE_TOOLSETS = [
    "web",              # 网络搜索
    "clarify",          # 提问澄清
    "todo",             # 待办管理
    "vision",           # 图片分析
]

_DISABLED_TOOLSETS = [
    "terminal",         # shell 访问 — 禁止
    "file",             # 文件操作 — 禁止
    "code_execution",   # Python 执行 — 禁止
    "cronjob",          # 定时任务 — 禁止
    "delegation",       # 子代理 — 禁止
    "skills",           # 技能管理 — 禁止
    "messaging",        # 跨平台消息 — 禁止
    "browser",          # 浏览器自动化 — 禁止
    "session_search",   # 跨会话搜索 — 禁止（用户隔离）
    "memory",           # 持久记忆 — 禁止（用户隔离）
]


def _write_weixin_config(profile_dir: str):
    """写入/更新 profile 的 config.yaml，包含微信平台配置和工具权限限制。"""
    import yaml
    cfg_path = os.path.join(profile_dir, "config.yaml")
    try:
        if os.path.exists(cfg_path):
            with open(cfg_path, "r") as f:
                cfg = yaml.safe_load(f) or {}
        else:
            cfg = {}
    except Exception:
        cfg = {}

    # 平台配置
    cfg.setdefault("platforms", {})
    cfg["platforms"].setdefault("weixin", {})
    wx = cfg["platforms"]["weixin"]
    wx["enabled"] = True
    wx.setdefault("extra", {})
    wx["extra"]["dm_policy"] = "open"
    wx["extra"]["group_policy"] = "disabled"

    # 工具权限白名单
    cfg.setdefault("platform_toolsets", {})
    cfg["platform_toolsets"]["weixin"] = _SAFE_TOOLSETS

    # 工具权限黑名单（双重保险）
    cfg.setdefault("agent", {})
    cfg["agent"]["disabled_toolsets"] = _DISABLED_TOOLSETS

    try:
        with open(cfg_path, "w") as f:
            yaml.dump(cfg, f, default_flow_style=False, allow_unicode=True)
    except Exception:
        pass


def migrate_profile_configs(prefix: str = "weixin-") -> List[str]:
    """
    扫描所有已绑定的 profile，确保 config.yaml 包含工具权限限制。
    旧 profile（从早期版本绑定）会自动补上新配置。
    返回被修复的 profile 名称列表（可用于后续重启 gateway）。
    """
    import yaml
    fixed = []
    for name in list_profiles(prefix):
        creds = get_weixin_credentials(name)
        if not creds or not creds.get("token"):
            continue  # 未绑定，跳过

        profile_dir = get_profile_dir(name)
        cfg_path = os.path.join(profile_dir, "config.yaml")

        try:
            with open(cfg_path, "r") as f:
                cfg = yaml.safe_load(f) or {}
        except Exception:
            cfg = {}

        # 检查是否已有工具限制（检查 weixin 平台的白名单是否存在）
        pts = cfg.get("platform_toolsets", {})
        existing = pts.get("weixin", [])
        if existing == _SAFE_TOOLSETS:
            continue  # 已是最新，跳过

        _write_weixin_config(profile_dir)
        fixed.append(name)

    return fixed


# ── 查询 ────────────────────────────────────────────────────────────────

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