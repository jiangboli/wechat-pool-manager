"""Docker 容器调度器。

管理 hermes-bot 容器的全生命周期：
- create/start/stop/restart/remove
- 资源限制（CPU/Memory）
- 健康检查
- 日志管理

数据目录规范：
  /home/data/{尾数}/{profile}/.hermes/   ← 尾数 = profile 编号末位 (0-9)
"""

import asyncio
import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Optional

import docker
from docker.errors import DockerException, ImageNotFound, NotFound

from .pg_store import pg_store

logger = logging.getLogger("pool_manager.docker_scheduler")

# 默认配置
DEFAULT_IMAGE = "hermes-bot:latest"
DEFAULT_NETWORK = "hermes-pool-net"
DEFAULT_DATA_ROOT = "/home/data/"
DEFAULT_MEMORY_LIMIT = "512m"
DEFAULT_MEMORY_RESERVATION = "128m"
DEFAULT_CPU_SHARES = 256
DEFAULT_CPU_QUOTA = 50000
DEFAULT_MAX_CONTAINERS = 500
DEFAULT_PIDS_LIMIT = 1024
DEFAULT_ULIMIT_NOFILE = 2048
DEFAULT_RESTART_POLICY = {"Name": "unless-stopped"}


def _profile_to_tail(profile: str) -> str:
    """提取 profile 编号的末位（0-9）。

    weixin-001 → "1"  (001 末位)
    wx001 → "1"       (001 末位)
    """
    m = re.search(r"(\d+)$", profile)
    if not m:
        return "0"
    num_str = m.group(1)
    return num_str[-1]  # 取末位


def _profile_dir(profile: str, data_root: str = DEFAULT_DATA_ROOT) -> str:
    """计算 profile 的数据目录路径。

    >>> _profile_dir("weixin-001")
    /home/data/1/weixin-001/
    """
    tail = _profile_to_tail(profile)
    return os.path.join(data_root, tail, profile)


def _hermes_dir(profile: str, data_root: str = DEFAULT_DATA_ROOT) -> str:
    """返回 profile 的 .hermes 目录路径。"""
    return os.path.join(_profile_dir(profile, data_root), ".hermes")


def _container_name(profile: str) -> str:
    """返回容器名。"""
    # weixin-001 → hermes-weixin-001
    return f"hermes-{profile}"


def _profile_from_container_name(name: str) -> str:
    """从容器名还原 profile。"""
    # hermes-weixin-001 → weixin-001
    return name[len("hermes-"):]


class DockerScheduler:
    """Docker 容器调度器。"""

    def __init__(self, config: dict):
        self.config = config
        docker_cfg = config.get("docker", {})
        defaults = docker_cfg.get("container_defaults", {})

        self.image = docker_cfg.get("image", DEFAULT_IMAGE)
        self.network = docker_cfg.get("network", DEFAULT_NETWORK)
        self.data_root = docker_cfg.get("data_root", DEFAULT_DATA_ROOT)
        self.data_root_host = docker_cfg.get("data_root_host", self.data_root)
        self.max_containers = docker_cfg.get("max_containers", DEFAULT_MAX_CONTAINERS)
        self.memory_limit = defaults.get("memory_limit", DEFAULT_MEMORY_LIMIT)
        self.memory_reservation = defaults.get("memory_reservation", DEFAULT_MEMORY_RESERVATION)
        self.cpu_shares = defaults.get("cpu_shares", DEFAULT_CPU_SHARES)
        self.cpu_quota = defaults.get("cpu_quota", DEFAULT_CPU_QUOTA)
        self.pids_limit = defaults.get("pids_limit", DEFAULT_PIDS_LIMIT)
        self.ulimit_nofile = defaults.get("ulimit_nofile", DEFAULT_ULIMIT_NOFILE)
        self.restart_policy = DEFAULT_RESTART_POLICY

        self._client: Optional[docker.DockerClient] = None

    # ── 客户端 ──────────────────────────────────────────────────

    @property
    def client(self) -> docker.DockerClient:
        if self._client is None:
            self._client = docker.from_env()
        return self._client

    # ── 数据目录 ────────────────────────────────────────────────

    def _chown_recursive_up(self, path: str, uid: int = 1000):
        """递归向上 chown 父目录，直到 data_root 边界。"""
        path = os.path.normpath(path)
        root = os.path.normpath(self.data_root)
        while path.startswith(root) and path != root:
            try:
                os.chown(path, uid, -1)
            except PermissionError:
                break
            path = os.path.dirname(path)

    def ensure_data_dir(self, profile: str) -> str:
        """确保 profile 的数据目录存在并返回 hermes 目录路径。"""
        hdir = _hermes_dir(profile, self.data_root)
        os.makedirs(hdir, exist_ok=True)
        # 容器内的 hermes 用户 (UID 1000) 需要写 .hermes/logs/ .hermes/sessions.db
        # chown 到 UID 1000 让容器内 hermes 用户可正常写入，不开放权限给其他人
        os.chown(hdir, 1000, -1)
        # 父目录也改 owner，方便宿主机用户直接编辑
        self._chown_recursive_up(hdir, uid=1000)
        return hdir

    def write_env(self, profile: str, credentials: dict):
        """写入容器 ~/.hermes/.env 文件（微信凭证，不含 API key）。"""
        hdir = self.ensure_data_dir(profile)
        env_path = os.path.join(hdir, ".env")
        with open(env_path, "w") as f:
            f.write(f"WEIXIN_ACCOUNT_ID={credentials.get('account_id', '')}\n")
            f.write(f"WEIXIN_TOKEN={credentials.get('token', '')}\n")
            f.write(f"WEIXIN_BASE_URL={credentials.get('base_url', '')}\n")
            f.write(f"WEIXIN_ALLOW_ALL_USERS=true\n")
            f.write(f"WEIXIN_HOME_CHANNEL={credentials.get('user_id', '')}\n")
        logger.info("[%s] 凭证已写入 %s", profile, env_path)
        os.chown(env_path, 1000, -1)

    def write_config(self, profile: str, proxy_host: str = "pool-proxy", proxy_port: int = 8767):
        """写入容器 ~/.hermes/config.yaml。

        platform 配置 + LLM proxy 地址。
        """
        hdir = self.ensure_data_dir(profile)
        cfg_path = os.path.join(hdir, "config.yaml")
        cfg = f"""# Hermes 配置文件（由 Pool Manager 自动生成）
# 微信网关平台
gateway:
  mode: websocket

platforms:
  weixin:
    allow_all_users: true

# LLM 配置——所有请求经过 Pool Manager Proxy
model:
  provider: custom:pool-proxy
  model: deepseek-v4-flash

custom_providers:
  - name: pool-proxy
    base_url: http://{proxy_host}:{proxy_port}/v1

agent:
  auto_send_tool_results: true

# 自动审批——无需用户手动 approve
delegation:
  subagent_auto_approve: true

# 完全禁用审批弹窗（auto 模式=无需人工确认）
approvals:
  mode: off
  destructive_slash_confirm: false

# hooks 自动接受（防止 Hermes 重写配置后弹窗）
hooks_auto_accept: true

# 禁用预执行安全扫描（bot 用户即主人，无需审批）
security:
  tirith_enabled: false

# 显示控制——关闭所有内置中间消息噪音
display:
  interim_assistant_messages: false
  tool_progress: off

# 启用 send_message 工具，用于 agent 自主发业务进度通知
enabled_toolsets:
  - messaging
"""
        with open(cfg_path, "w") as f:
            f.write(cfg)
        logger.info("[%s] 配置文件已写入 %s", profile, cfg_path)
        os.chown(cfg_path, 1000, -1)
        # 写入 .managed 标记——防止 Hermes 自动重写 config.yaml
        managed_path = os.path.join(hdir, ".managed")
        if not os.path.exists(managed_path):
            with open(managed_path, "w") as f:
                f.write("managed by pool-manager\n")
            os.chown(managed_path, 1000, -1)
            logger.info("[%s] .managed 标记已创建", profile)

        # .managed 文件触发 Hermes 检查所需的子目录是否存在
        memories_dir = os.path.join(hdir, "memories")
        if not os.path.exists(memories_dir):
            os.makedirs(memories_dir, exist_ok=True)
            os.chown(memories_dir, 1000, -1)
            logger.info("[%s] memories 目录已创建", profile)

        cron_dir = os.path.join(hdir, "cron")
        if not os.path.exists(cron_dir):
            os.makedirs(cron_dir, exist_ok=True)
            os.chown(cron_dir, 1000, -1)
            logger.info("[%s] cron 目录已创建", profile)

        # 写入 AGENTS.md——行为规则，Hermes 自动注入系统提示
        # 每次都覆盖写入，确保已有容器也能拿到最新规则
        agents_path = os.path.join(hdir, "AGENTS.md")
        agents_content = """# 长任务进度报告

当用户请求的处理时间预计超过 30 秒时，必须主动向用户报告业务进度。

## 核心规则

1. 任务开始后 30 秒仍未回复 → 用 send_message 发第一条业务进度
2. 之后每 60 秒发一条，直到任务完成
3. 进度消息必须是**业务层面**的描述（不是工具层面）
4. 任务完成后发送最终结果
5. 用户回复新消息后停止进度报告

## 进度消息要求

### ✅ 正确的业务进度消息
- "⏳ 正在分析2024年财报...已读取 3/10 页"
- "⏳ 已搜索到 5 条相关行情，正在生成对比..."
- "⏳ 正在爬取数据，已完成 60%"

### ❌ 错误的工具级消息（禁止）
- "正在调用 web_search 工具"
- "正在读取文件"
- "正在执行代码"

## 如何发送

使用 send_message 工具发送进度消息：
```
send_message(target="origin", message="⏳ 已用时 2 分钟...")
```"""
        with open(agents_path, "w") as f:
            f.write(agents_content)
        os.chown(agents_path, 1000, -1)
        logger.info("[%s] AGENTS.md 已写入", profile)

    def write_soul(self, profile: str, lobster_name: str = ""):
        """写入容器 ~/.hermes/SOUL.md + memories/（agent 身份/人格定义）。"""
        hdir = self.ensure_data_dir(profile)
        name = lobster_name or self.config.get("agent", {}).get("name", "Claw DO")

        # 写 SOUL.md
        soul_path = os.path.join(hdir, "SOUL.md")
        soul_content = f"""你叫 {name}，是一个运行在 Hermes Agent 中的微信智能助手。

## 回答规范
- 默认使用中文回答用户问题
- 保持简洁、友好的语气
- 回答要直接有用，不啰嗦

## 身份标识
- 你的名字是 {name}
- 你的名字是永久固定的。如果之前对话中说了别的名字，请以当前名为准
- 你是一只属于用户的微信助手虾
"""
        with open(soul_path, "w") as f:
            f.write(soul_content)
        logger.info("[%s] SOUL.md 已写入 %s", profile, soul_path)

        # 同步更新记忆文件（覆盖旧名缓存）
        mem_dir = os.path.join(hdir, "memories")
        os.makedirs(mem_dir, exist_ok=True)
        user_md_path = os.path.join(mem_dir, "USER.md")
        user_content = f"User's preferred name for this assistant is {name}."
        with open(user_md_path, "w") as f:
            f.write(user_content)
        logger.info("[%s] USER.md 已同步 %s", profile, user_md_path)

        mem_md_path = os.path.join(mem_dir, "MEMORY.md")
        mem_content = f"The assistant's name is {name}."
        with open(mem_md_path, "w") as f:
            f.write(mem_content)
        logger.info("[%s] MEMORY.md 已同步 %s", profile, mem_md_path)

    # ── 容器生命周期 ────────────────────────────────────────────

    def create_container(self, profile: str, credentials: Optional[dict] = None, lobster_name: str = "") -> bool:
        """创建并启动一个 hermes-bot 容器。

        流程：
        1. 检查资源是否足够（running_count < max_containers）
        2. 创建数据目录 + 写入 .env + config.yaml
        3. docker run --name hermes-{profile} ...
        """
        # 检查容器是否已存在
        name = _container_name(profile)
        try:
            existing = self.client.containers.get(name)
            logger.info("[%s] 容器 %s 已存在，状态: %s", profile, name, existing.status)
            if existing.status != "running":
                existing.start()
                logger.info("[%s] 已启动已存在的容器", profile)
            return True
        except NotFound:
            pass

        # 检查运行容器数量
        running = self._count_running()
        if running >= self.max_containers:
            logger.warning("[%s] 超出最大容器数 (%d/%d)，无法创建", profile, running, self.max_containers)
            return False

        # 创建数据目录
        hdir = self.ensure_data_dir(profile)

        # 写入凭证和配置
        if credentials:
            self.write_env(profile, credentials)
        self.write_config(profile)
        self.write_soul(profile, lobster_name=lobster_name)

        # 计算宿主机路径（给 Docker API 挂载用）
        hdir_host = _hermes_dir(profile, self.data_root_host)

        # 启动容器
        try:
            container = self.client.containers.run(
                image=self.image,
                name=name,
                network=self.network,
                detach=True,
                restart_policy={"Name": "unless-stopped"},  # type: ignore[arg-type]
                mem_limit=self.memory_limit,
                mem_reservation=self.memory_reservation,
                cpu_shares=self.cpu_shares,
                cpu_quota=self.cpu_quota,
                pids_limit=self.pids_limit,
                ulimits=[docker.types.Ulimit(name="nofile", soft=self.ulimit_nofile, hard=self.ulimit_nofile)],
                environment={"TZ": "Asia/Shanghai"},
                volumes={
                                    hdir_host: {
                        "bind": "/home/hermes/.hermes",
                        "mode": "rw",
                    }
                },
                hostname=name,
                labels={
                    "managed_by": "pool-manager",
                    "profile": profile,
                },
            )
            logger.info("[%s] 容器 %s 已创建 (id=%s)", profile, name, container.short_id)
            return True
        except DockerException as e:
            logger.error("[%s] 创建容器失败: %s", profile, e)
            return False

    def start_container(self, profile: str) -> bool:
        """启动已存在的容器。"""
        name = _container_name(profile)
        try:
            container = self.client.containers.get(name)
            container.start()
            logger.info("[%s] 容器已启动", profile)
            return True
        except NotFound:
            logger.warning("[%s] 容器 %s 不存在", profile, name)
            return False
        except DockerException as e:
            logger.error("[%s] 启动容器失败: %s", profile, e)
            return False

    def stop_container(self, profile: str) -> bool:
        """停止容器。"""
        name = _container_name(profile)
        try:
            container = self.client.containers.get(name)
            container.stop(timeout=10)
            logger.info("[%s] 容器已停止", profile)
            return True
        except NotFound:
            logger.warning("[%s] 容器 %s 不存在", profile, name)
            return False
        except DockerException as e:
            logger.error("[%s] 停止容器失败: %s", profile, e)
            return False

    def restart_container(self, profile: str) -> bool:
        """重启容器。"""
        name = _container_name(profile)
        try:
            container = self.client.containers.get(name)
            container.restart(timeout=10)
            logger.info("[%s] 容器已重启", profile)
            return True
        except NotFound:
            # 容器不存在则创建
            logger.info("[%s] 容器不存在，尝试创建", profile)
            return self.create_container(profile)
        except DockerException as e:
            logger.error("[%s] 重启容器失败: %s", profile, e)
            return False

    def remove_container(self, profile: str, keep_data: bool = True) -> bool:
        """删除容器。"""
        name = _container_name(profile)
        try:
            container = self.client.containers.get(name)
            container.remove(force=True)
            logger.info("[%s] 容器 %s 已删除", profile, name)
            return True
        except NotFound:
            logger.warning("[%s] 容器 %s 不存在，无需删除", profile, name)
            return True
        except DockerException as e:
            logger.error("[%s] 删除容器失败: %s", profile, e)
            return False

    # ── 查询 ────────────────────────────────────────────────────

    def get_container_status(self, profile: str) -> dict:
        """获取容器详细状态。"""
        name = _container_name(profile)
        try:
            container = self.client.containers.get(name)
            container.reload()
            return {
                "profile": profile,
                "container_name": name,
                "status": container.status,
                "running": container.status == "running",
                "restart_count": container.attrs.get("RestartCount", 0),
                "created_at": container.attrs.get("Created", ""),
                "started_at": container.attrs.get("State", {}).get("StartedAt", ""),
                "health": container.attrs.get("State", {}).get("Health", {}).get("Status", "unknown"),
                "image": container.image.tags[0] if container.image and container.image.tags else "unknown",
            }
        except NotFound:
            return {
                "profile": profile,
                "container_name": name,
                "status": "not_found",
                "running": False,
            }
        except DockerException as e:
            return {
                "profile": profile,
                "status": "error",
                "error": str(e),
            }

    def list_containers(self) -> list[dict]:
        """列出所有由 pool-manager 管理的容器。"""
        containers = self.client.containers.list(
            all=True,
            filters={"label": "managed_by=pool-manager"},
        )
        result = []
        for c in containers:
            labels = c.labels or {}
            profile = labels.get("profile", "") or _profile_from_container_name(c.name or "")
            result.append({
                "profile": profile,
                "container_name": c.name,
                "status": c.status,
                "running": c.status == "running",
                "short_id": c.short_id,
                "created_at": c.attrs.get("Created", ""),
            })
        return sorted(result, key=lambda x: x["profile"])

    def get_container_logs(self, profile: str, tail: int = 50) -> str:
        """获取容器日志。"""
        name = _container_name(profile)
        try:
            container = self.client.containers.get(name)
            return container.logs(tail=tail).decode("utf-8", errors="replace")
        except NotFound:
            return f"[容器 {name} 不存在]"
        except DockerException as e:
            return f"[获取日志失败: {e}]"

    # ── 内部辅助 ────────────────────────────────────────────────

    def _count_running(self) -> int:
        """计算当前运行的容器数。"""
        try:
            containers = self.client.containers.list(
                filters={"label": "managed_by=pool-manager"},
            )
            return len([c for c in containers if c.status == "running"])
        except DockerException:
            return 0

    def ensure_network(self) -> bool:
        """确保共享网络存在。"""
        try:
            networks = self.client.networks.list(names=[self.network])
            if not networks:
                self.client.networks.create(self.network, driver="bridge")
                logger.info("Docker 网络 %s 已创建", self.network)
            return True
        except DockerException as e:
            logger.error("创建网络失败: %s", e)
            return False

    def ensure_image(self) -> bool:
        """确保 hermes-bot 镜像存在。"""
        try:
            self.client.images.get(self.image)
            logger.info("镜像 %s 已存在", self.image)
            return True
        except ImageNotFound:
            logger.warning("镜像 %s 不存在，需要先构建", self.image)
            return False
        except DockerException as e:
            logger.error("检查镜像失败: %s", e)
            return False

    def close(self):
        """关闭 Docker 客户端。"""
        if self._client:
            self._client.close()
            self._client = None

    # ── 健康检查 ────────────────────────────────────────────────

    async def health_check_loop(self, interval: int = 60, max_restarts: int = 3):
        """定期检查所有容器状态，自动重启异常的容器。

        在 Service startup 中作为 asyncio task 启动。
        """
        logger.info("Docker 健康检查循环启动 (interval=%ds, max_restarts=%d)", interval, max_restarts)
        while True:
            try:
                await self._health_check_tick(max_restarts)
            except Exception as e:
                logger.error("健康检查异常: %s", e)
            await asyncio.sleep(interval)

    async def _health_check_tick(self, max_restarts: int):
        """单次健康检查 tick。"""
        containers = self.client.containers.list(
            all=True,
            filters={"label": "managed_by=pool-manager"},
        )
        for c in containers:
            try:
                c.reload()
                status = c.status
                restart_count = c.attrs.get("RestartCount", 0)

                if status == "running":
                    # 检查 Docker HEALTHCHECK 状态
                    health = c.attrs.get("State", {}).get("Health", {}).get("Status", "")
                    if health == "unhealthy":
                        logger.warning("[%s] 容器健康检查不通过，重启...", c.name)
                        c.restart(timeout=10)
                    # 更新 PG 心跳（容器在跑即视为健康）
                    profile = c.labels.get("profile", "")
                    if profile and pg_store.enabled:
                        await pg_store.update_binding_heartbeat(profile)

                    # 检查微信连接状态（扫描最近 5 分钟日志）
                    if profile:
                        try:
                            recent_logs = c.logs(since=int(time.time() - 300)).decode("utf-8", errors="replace")
                            if "Session expired" in recent_logs:
                                logger.error("[%s] 微信 Session 已过期，需重新扫码", profile)
                                if pg_store.enabled:
                                    await pg_store.update_binding_status(profile, "session_expired")
                            elif "getUpdates failed" in recent_logs or "poll error" in recent_logs:
                                logger.warning("[%s] 微信连接异常，自动重启容器...", profile)
                                c.restart(timeout=10)
                            elif pg_store.enabled and profile:
                                # 没有异常日志 → 如果之前标记过 session_expired，自动恢复为 active
                                await pg_store.restore_binding_if_expired(profile)
                        except Exception as e:
                            logger.warning("[%s] 检查微信连接日志失败: %s", profile, e)

                    # 检查容器存储大小（可写层 + 数据目录 > 10GB 则重建）
                    try:
                        size_rw = c.attrs.get("SizeRw", 0) or 0
                        data_dir = _profile_dir(profile)
                        if os.path.exists(data_dir):
                            import subprocess
                            result = subprocess.run(
                                ["du", "-sb", data_dir],
                                capture_output=True, text=True, timeout=5
                            )
                            data_size = int(result.stdout.split()[0]) if result.returncode == 0 else 0
                        else:
                            data_size = 0
                        total_gb = (size_rw + data_size) / (1024**3)
                        STORAGE_LIMIT_GB = 10
                        if total_gb > STORAGE_LIMIT_GB:
                            logger.warning(
                                "[%s] 存储超限: 可写层 %.2fGB + 数据 %.2fGB = %.2fGB (上限 %dGB)，重建容器...",
                                c.name, size_rw/(1024**3), data_size/(1024**3), total_gb, STORAGE_LIMIT_GB
                            )
                            c.kill()
                            c.remove()
                            self.create_container(profile)
                    except Exception as e:
                        logger.warning("[%s] 检查容器存储大小失败: %s", c.name, e)

                    # 检查异常网络连接（TCP 连接数超过阈值则重建）
                    try:
                        import subprocess
                        proc = subprocess.run(
                            ["docker", "exec", c.name, "cat", "/proc/net/tcp"],
                            capture_output=True, text=True, timeout=5
                        )
                        if proc.returncode == 0:
                            lines = proc.stdout.strip().split("\n")
                            established = 0
                            for line in lines[1:]:
                                parts = line.strip().split()
                                if len(parts) >= 4 and parts[3] == "01":
                                    established += 1
                            if established > 200:
                                logger.warning(
                                    "[%s] 网络连接异常: %d 个 TCP 连接 (正常 < 20)，重建容器...",
                                    c.name, established
                                )
                                c.kill()
                                c.remove()
                                self.create_container(profile)
                            elif established > 50:
                                logger.warning(
                                    "[%s] 网络连接偏高: %d 个 TCP 连接 (正常 < 20)",
                                    c.name, established
                                )
                    except Exception as e:
                        logger.warning("[%s] 检查容器网络连接失败: %s", c.name, e)
                    continue

                if status == "exited":
                    # 已退出——尝试重启（如果重启次数未超限）
                    if restart_count < max_restarts:
                        logger.warning("[%s] 容器已退出，尝试重启 (%d/%d)",
                                       c.name, restart_count + 1, max_restarts)
                        c.start()
                    else:
                        logger.error("[%s] 容器已退出且超过最大重启次数 (%d)",
                                     c.name, max_restarts)
            except NotFound:
                pass
            except DockerException as e:
                logger.error("[%s] 健康检查失败: %s", c.name, e)
