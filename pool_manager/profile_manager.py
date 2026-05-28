"""Profile 管理器——管理 Docker 容器的凭证和配置。

不再创建 Linux 用户。每个微信用户对应一个 Docker 容器，
数据通过 volume mount 持久化在 /home/data/{尾数}/{profile}/.hermes/。

安全设计：
- .env 仅包含微信凭证（account_id, token），无任何 API key
- config.yaml 包含 platform + model 配置，LLM 指向 pool manager proxy
- API key 只在 pool manager 进程内存中
"""

import os
import sys
from typing import Dict, List, Optional, Tuple


def _get_scheduler():
    """获取全局 DockerScheduler 实例。"""
    from . import gateway_manager as gm
    return gm._scheduler


def setup_linux_profile(profile: str, credentials: dict) -> bool:
    """创建 Docker 容器的配置目录并写入凭证。"""
    s = _get_scheduler()
    if not s:
        print(f"  [!] DockerScheduler 未初始化", file=sys.stderr)
        return False
    try:
        s.write_env(profile, credentials)
        s.write_config(profile)
        print(f"  [OK] {profile} 配置完成")
        return True
    except Exception as e:
        print(f"  [!] 配置 {profile} 失败: {e}", file=sys.stderr)
        return False


def update_credentials(profile: str, credentials: dict) -> bool:
    """更新已存在的 Docker 容器的微信凭证。"""
    s = _get_scheduler()
    if not s:
        return False
    try:
        s.write_env(profile, credentials)
        print(f"  [OK] {profile} 凭证已更新")
        return True
    except Exception as e:
        print(f"  [!] 更新凭证失败: {e}", file=sys.stderr)
        return False


# ── 查询 ────────────────────────────────────────────────────────────


def list_linux_users(prefix: str = "") -> List[str]:
    """返回 profile 名列表（兼容旧接口名称）。

    Docker 模式下，返回所有 managed_by=pool-manager 的容器对应的 profile。
    """
    s = _get_scheduler()
    if not s:
        return []
    try:
        containers = s.list_containers()
        return [c["profile"] for c in containers]
    except Exception:
        return []


def get_bound_count(prefix: str = "") -> int:
    """统计已绑定（容器 running）的 profile 数。"""
    s = _get_scheduler()
    if not s:
        return 0
    try:
        containers = s.list_containers()
        return len([c for c in containers if c.get("running")])
    except Exception:
        return 0


def linux_username(profile: str) -> str:
    """兼容旧接口——Docker 模式下 profile 名不变。"""
    return profile
