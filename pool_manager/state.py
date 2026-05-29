"""状态持久化——将池管理器状态保存到 JSON 文件。"""

import json
import logging
import os
import time
from typing import Dict, List, Optional, Any
from pathlib import Path

# 状态文件路径（存储在持久化数据卷 /home/data/pool-manager/ 下）
# 容器重建后状态不丢失
STATE_FILE = "/home/data/pool-manager/pool_state.json"

# Profile 状态枚举
STATUS_AVAILABLE = "available"        # 已创建但从未入热池
STATUS_IN_HOT_POOL = "in_hot_pool"   # 正在热池中轮询 QR
STATUS_BOUND_HEALTHY = "bound_healthy"   # 已绑定，容器运行正常
STATUS_BOUND_UNHEALTHY = "bound_unhealthy" # 已绑定但容器异常
STATUS_BOUND_IDLE = "bound_idle"      # 已绑定但已因空闲被停
STATUS_QR_FAILED = "qr_failed"       # QR 多次过期
STATUS_EXPIRED = "expired"            # WeChat session 过期


class PoolState:
    """线程安全的池状态（单线程 asyncio，无需锁）。"""

    def __init__(self):
        self.profiles: Dict[str, dict] = {}  # profile_name → state dict
        self.stats: Dict[str, Any] = {
            "total_bound": 0,
            "total_available": 0,
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "last_saved": "",
        }
        self._dirty = False

    def init_profiles(self, names: List[str]):
        """初始化 profile 列表（设为 available）。"""
        for name in names:
            if name not in self.profiles:
                self.profiles[name] = {
                    "status": STATUS_AVAILABLE,
                    "bound_at": "",
                    "last_active": "",
                    "error": "",
                    "restart_count": 0,
                    "qr_url": "",
                    "user_id": "",
                }
        self._dirty = True

    def get_available(self) -> List[str]:
        """获取所有 available 的 profile 名称。"""
        return [n for n, p in self.profiles.items() if p["status"] == STATUS_AVAILABLE]

    def get_unbound(self) -> List[str]:
        """获取所有未绑定的 profile（available + qr_failed）。"""
        return [n for n, p in self.profiles.items()
                if p["status"] in (STATUS_AVAILABLE, STATUS_QR_FAILED)]

    def set_status(self, name: str, status: str, **extra):
        """设置 profile 状态和额外字段。"""
        if name in self.profiles:
            self.profiles[name]["status"] = status
            for k, v in extra.items():
                self.profiles[name][k] = v
            self._dirty = True

    def mark_bound(self, name: str, user_id: str = ""):
        """标记 profile 为已绑定。"""
        self.set_status(name, STATUS_BOUND_HEALTHY,
                        bound_at=time.strftime("%Y-%m-%dT%H:%M:%S"),
                        last_active=time.strftime("%Y-%m-%dT%H:%M:%S"),
                        user_id=user_id,
                        error="")

    def mark_unhealthy(self, name: str, error: str = ""):
        self.set_status(name, STATUS_BOUND_UNHEALTHY, error=error)

    def mark_idle(self, name: str):
        self.set_status(name, STATUS_BOUND_IDLE)

    def mark_qr_failed(self, name: str):
        self.set_status(name, STATUS_QR_FAILED)

    def mark_available(self, name: str):
        self.set_status(name, STATUS_AVAILABLE, qr_url="", error="")

    def mark_hot_pool(self, name: str, qr_url: str = ""):
        self.set_status(name, STATUS_IN_HOT_POOL, qr_url=qr_url)

    def touch_active(self, name: str):
        """更新最后活跃时间。"""
        if name in self.profiles:
            self.profiles[name]["last_active"] = time.strftime("%Y-%m-%dT%H:%M:%S")

    def get_by_status(self, status: str) -> List[str]:
        return [n for n, p in self.profiles.items() if p["status"] == status]

    def get_stats(self) -> dict:
        """计算统计数据。"""
        total = len(self.profiles)
        bound = len(self.get_by_status(STATUS_BOUND_HEALTHY)) + \
                len(self.get_by_status(STATUS_BOUND_UNHEALTHY)) + \
                len(self.get_by_status(STATUS_BOUND_IDLE))
        available = len(self.get_available())
        hot_pool = len(self.get_by_status(STATUS_IN_HOT_POOL))
        unhealthy = len(self.get_by_status(STATUS_BOUND_UNHEALTHY))
        expired = len(self.get_by_status(STATUS_EXPIRED))
        return {
            "total_profiles": total,
            "bound": bound,
            "available": available,
            "hot_pool": hot_pool,
            "unhealthy": unhealthy,
            "expired": expired,
        }

    def save(self, path: str = None):
        """持久化到 JSON 文件。"""
        if not self._dirty:
            return
        save_path = path or STATE_FILE
        os.makedirs(os.path.dirname(save_path), exist_ok=True)
        self.stats.update(self.get_stats())
        self.stats["last_saved"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        data = {
            "profiles": self.profiles,
            "stats": self.stats,
        }
        with open(save_path, "w") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        self._dirty = False

    # ── 微信用户去重 ──────────────────────────────────────────────────

    def get_docker_user_by_user_id(self, user_id: str) -> Optional[str]:
        """通过微信 user_id 查找已绑定的 Docker 容器对应的 profile。"""
        for name, ps in self.profiles.items():
            if ps.get("user_id") == user_id and ps.get("status", "").startswith("bound"):
                return name
        return None

    def record_binding(self, name: str, user_id: str):
        """记录绑定关系。"""
        if name in self.profiles:
            self.profiles[name]["user_id"] = user_id
            self._dirty = True

    def load(self, path: str = None):
        """从 JSON 文件恢复状态。"""
        load_path = path or STATE_FILE
        if not os.path.exists(load_path):
            return
        try:
            with open(load_path, "r") as f:
                data = json.load(f)
            self.profiles.update(data.get("profiles", {}))
            self.stats.update(data.get("stats", {}))
        except (json.JSONDecodeError, IOError):
            pass

    def restore_from_containers(self, containers: List[dict]):
        """从现有 Docker 容器恢复绑定状态（状态文件丢失时的 fallback）。

        containers: scheduler.list_containers() 返回的列表，每项含 profile, status, running 等。
        """
        for c in containers:
            profile = c.get("profile")
            if profile and profile in self.profiles:
                status = c.get("status", "")
                if status == "running":
                    self.set_status(profile, STATUS_BOUND_HEALTHY,
                                    last_active=time.strftime("%Y-%m-%dT%H:%M:%S"))
                else:
                    self.set_status(profile, STATUS_BOUND_IDLE,
                                    last_active=c.get("created_at", ""))
        if containers:
            logger = logging.getLogger("pool_manager.state")
            logger.info("从 %d 个现有 Docker 容器恢复绑定状态", len(containers))
