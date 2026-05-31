import logging
import os
import socket
from datetime import datetime, timezone
from typing import Optional, List, Dict, Any

from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from sqlalchemy import select, update, delete, text
from sqlalchemy.exc import OperationalError

from .models import Base, Machine, Binding, DockerContainer, QrHistory, GatewayHealthLog, ApiKey

logger = logging.getLogger("pool_manager.pg_store")

# ── 机器信息（启动时自动检测）──
_MACHINE_IP = ""
_MACHINE_HOSTNAME = ""


def _detect_machine_info():
    """自动检测本机 IP 和主机名。
    
    优先级: MACHINE_IP 环境变量 > socket 探测 > "unknown"
    在 Docker 容器内 socket 探测会返回容器内部 IP（如 172.18.0.x），
    可通过环境变量 MACHINE_IP 覆盖为宿主机真实 IP。
    """
    global _MACHINE_IP, _MACHINE_HOSTNAME
    _MACHINE_HOSTNAME = socket.gethostname()
    
    # 优先环境变量（Docker 部署用）
    _MACHINE_IP = os.environ.get("MACHINE_IP", "")
    if not _MACHINE_IP:
        try:
            # 取第一个非 127.0.0.1 的 IP
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.settimeout(0)
            s.connect(("10.254.254.254", 1))
            _MACHINE_IP = s.getsockname()[0]
            s.close()
        except Exception:
            _MACHINE_IP = "unknown"
    logger.info("本机信息: IP=%s hostname=%s", _MACHINE_IP, _MACHINE_HOSTNAME)


class PgStore:
    """PG 持久化层。"""

    # 内存中的待绑定用户信息（PG 降级时用）
    _pending_bindings: Dict[str, dict] = {}

    def __init__(self):
        self.dsn: str = ""
        self.engine = None
        self.session_factory = None
        self._connected = False
        self._enabled = False
        _detect_machine_info()

    @property
    def enabled(self) -> bool:
        return self._enabled

    async def connect(self, dsn: str = None):
        """连接 PG。失败时不阻止启动，只写日志。"""
        self.dsn = dsn or os.environ.get("CLAW_DO_DSN", "")
        if not self.dsn:
            logger.warning("CLAW_DO_DSN 未设置，PG 功能禁用")
            return

        try:
            self.engine = create_async_engine(self.dsn, pool_size=5, max_overflow=10,
                                              connect_args={"timeout": 5})
            self.session_factory = async_sessionmaker(self.engine, expire_on_commit=False)
            # 测试连接
            async with self.session_factory() as session:
                await session.execute(select(1))
            self._connected = True
            self._enabled = True
            logger.info("PG 连接成功: %s", self.dsn.split("@")[-1] if "@" in self.dsn else self.dsn)
            await self._ensure_schema()
        except OperationalError as e:
            logger.warning("PG 连接失败（服务正常启动，PG 功能禁用）: %s", e)
            self._connected = False
            self._enabled = False
        except Exception as e:
            logger.warning("PG 初始化异常（PG 功能禁用）: %s", e)
            self._connected = False
            self._enabled = False

    async def _ensure_schema(self):
        """幂等创建所有表 + 迁移新列。"""
        try:
            async with self.engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
                # 迁移：确保新列存在（兼容旧库）
                for col in ["phone", "lobster_name", "user_name"]:
                    sql = text(
                        "ALTER TABLE bindings ADD COLUMN IF NOT EXISTS "
                        f"{col} VARCHAR(64)"
                    )
                    await conn.execute(sql)
            logger.info("PG schema 已就绪")
        except Exception as e:
            # 多容器同时 create_all 可能触发 pg_type 唯一约束冲突，忽略
            if "UniqueViolationError" in type(e).__name__ or "duplicate key" in str(e).lower():
                logger.info("PG schema 已存在（并发创建检测到）")
            else:
                raise

    async def close(self):
        """关闭连接池。"""
        if self.engine:
            await self.engine.dispose()
            self._connected = False
            self._enabled = False

    def _session(self):
        """获取异步会话。"""
        return self.session_factory()

    # ── 机器管理 ────────────────────────────────────────────────────

    async def register_machine(self, total_slots: int = 100, hot_pool_size: int = 5,
                                version: str = ""):
        """注册/更新本机信息。"""
        if not self._enabled:
            return
        try:
            async with self._session() as session:
                stmt = select(Machine).where(Machine.machine_ip == _MACHINE_IP)
                result = await session.execute(stmt)
                machine = result.scalar_one_or_none()
                if machine:
                    machine.hostname = _MACHINE_HOSTNAME
                    machine.status = "online"
                    machine.total_slots = total_slots
                    machine.hot_pool_size = hot_pool_size
                    machine.version = version
                    machine.last_heartbeat_at = datetime.now(timezone.utc)
                else:
                    machine = Machine(
                        machine_ip=_MACHINE_IP,
                        hostname=_MACHINE_HOSTNAME,
                        total_slots=total_slots,
                        hot_pool_size=hot_pool_size,
                        version=version,
                        status="online",
                        last_heartbeat_at=datetime.now(timezone.utc),
                    )
                    session.add(machine)
                await session.commit()
                logger.info("机器已注册: IP=%s slots=%d", _MACHINE_IP, total_slots)
        except Exception as e:
            logger.warning("机器注册失败: %s", e)

    # ── 绑定管理 ────────────────────────────────────────────────────

    def save_pending_binding(self, token: str, phone: str, lobster_name: str, user_name: str, slot_id: str):
        """在用户提交表单后、扫码确认前，暂存用户信息。"""
        self._pending_bindings[token] = {
            "phone": phone,
            "lobster_name": lobster_name,
            "user_name": user_name,
            "slot_id": slot_id,
        }
        logger.info("待绑定信息已暂存: token=%s slot=%s", token[:8], slot_id)

    def get_pending_binding(self, token: str) -> Optional[dict]:
        """扫码确认后获取暂存的用户信息。"""
        return self._pending_bindings.pop(token, None)

    async def create_binding(self, profile: str, user_id: str, account_id: str,
                              bot_token: str, bot_base_url: str,
                              nickname: str = "", avatar_url: str = "",
                              phone: str = "", lobster_name: str = "",
                              user_name: str = "") -> Optional[int]:
        """创建绑定记录。"""
        if not self._enabled:
            return None
        try:
            async with self._session() as session:
                binding = Binding(
                    profile_name=profile,
                    machine_ip=_MACHINE_IP,
                    machine_hostname=_MACHINE_HOSTNAME,
                    user_id=user_id,
                    account_id=account_id,
                    nickname=nickname or "",
                    avatar_url=avatar_url or "",
                    phone=phone or "",
                    lobster_name=lobster_name or "",
                    user_name=user_name or "",
                    bot_token=bot_token,
                    bot_base_url=bot_base_url,
                    status="active",
                    bound_at=datetime.now(timezone.utc),
                    last_active_at=datetime.now(timezone.utc),
                )
                session.add(binding)
                await session.commit()
                logger.info("绑定记录已创建: profile=%s user_id=%s lobster=%s",
                           profile, user_id, lobster_name or "(未设置)")
                return binding.id
        except Exception as e:
            logger.warning("创建绑定记录失败: %s", e)
            return None



    async def find_binding_by_user_id(self, user_id: str) -> Optional[dict]:
        """按微信 user_id 查找绑定（跨机器去重）。"""
        if not self._enabled:
            return None
        try:
            async with self._session() as session:
                stmt = select(Binding).where(
                    Binding.user_id == user_id, Binding.status == "active"
                )
                result = await session.execute(stmt)
                binding = result.scalar_one_or_none()
                if binding:
                    return {
                        "id": binding.id,
                        "profile_name": binding.profile_name,
                        "machine_ip": binding.machine_ip,
                        "user_id": binding.user_id,
                        "status": binding.status,
                    }
                return None
        except Exception as e:
            logger.warning("查询绑定失败: %s", e)
            return None

    async def find_binding_by_phone(self, phone: str) -> Optional[dict]:
        """全局查询：该手机号是否已在任何服务器上绑定了。"""
        if not self._enabled or not phone:
            return None
        try:
            async with self._session() as session:
                stmt = select(Binding).where(
                    Binding.phone == phone,
                    Binding.status == "active",
                )
                result = await session.execute(stmt)
                binding = result.scalars().first()
                if binding:
                    return {
                        "id": binding.id,
                        "profile_name": binding.profile_name,
                        "machine_ip": binding.machine_ip,
                        "user_id": binding.user_id,
                        "phone": binding.phone,
                        "status": binding.status,
                        "bound_at": str(binding.bound_at) if binding.bound_at else "",
                    }
                return None
        except Exception as e:
            logger.warning("手机号去重查询失败: %s", e)
            return None

    async def list_bindings(self) -> List[Dict[str, Any]]:
        """列出所有绑定。"""
        if not self._enabled:
            return []
        try:
            async with self._session() as session:
                stmt = select(Binding).order_by(Binding.profile_name)
                result = await session.execute(stmt)
                bindings = result.scalars().all()
                return [
                    {
                        "id": b.id,
                        "profile_name": b.profile_name,
                        "machine_ip": b.machine_ip,
                        "user_id": b.user_id,
                        "nickname": b.nickname,
                        "status": b.status,
                        "bound_at": b.bound_at.isoformat() if b.bound_at else "",
                    }
                    for b in bindings
                ]
        except Exception as e:
            logger.warning("列出绑定失败: %s", e)
            return []

    # ── 容器管理 ────────────────────────────────────────────────────

    async def create_container(self, binding_id: int, container_name: str,
                                container_id: str, image: str, status: str,
                                memory_limit: str = "", ports: dict = None) -> Optional[int]:
        """创建容器记录。"""
        if not self._enabled:
            return None
        try:
            async with self._session() as session:
                container = DockerContainer(
                    binding_id=binding_id,
                    container_name=container_name,
                    container_id=container_id,
                    image=image,
                    status=status,
                    memory_limit=memory_limit or "",
                    restart_count=0,
                    ports=ports or {},
                    machine_ip=_MACHINE_IP,
                )
                session.add(container)
                await session.commit()
                return container.id
        except Exception as e:
            logger.warning("创建容器记录失败: %s", e)
            return None

    async def update_container_status(self, container_name: str, status: str,
                                       restart_count: int = 0):
        """更新容器状态。"""
        if not self._enabled:
            return
        try:
            async with self._session() as session:
                stmt = select(DockerContainer).where(
                    DockerContainer.container_name == container_name
                )
                result = await session.execute(stmt)
                container = result.scalar_one_or_none()
                if container:
                    container.status = status
                    container.restart_count = restart_count
                    container.updated_at = datetime.now(timezone.utc)
                    await session.commit()
        except Exception as e:
            logger.warning("更新容器状态失败: %s", e)

    async def restore_bindings_from_containers(self, containers: List[Dict]) -> int:
        """从 Docker 容器列表恢复/创建绑定和容器记录。

        适用于首次部署 PG 或状态文件丢失时，扫描现有容器写入 PG。
        返回恢复的绑定数量。
        """
        if not self._enabled:
            return 0
        count = 0
        try:
            for c in containers:
                profile = c.get("profile", "")
                container_name = c.get("container_name", "")
                if not profile or not container_name:
                    continue
                async with self._session() as session:
                    # 检查是否已存在
                    stmt = select(Binding).where(Binding.profile_name == profile)
                    result = await session.execute(stmt)
                    existing = result.scalar_one_or_none()
                    if existing:
                        # 更新容器状态
                        dc_stmt = select(DockerContainer).where(
                            DockerContainer.container_name == container_name
                        )
                        dc_result = await session.execute(dc_stmt)
                        dc = dc_result.scalar_one_or_none()
                        if dc:
                            dc.status = c.get("status", "running")
                            dc.updated_at = datetime.now(timezone.utc)
                        continue

                    # 新绑定
                    binding = Binding(
                        profile_name=profile,
                        machine_ip=_MACHINE_IP,
                        machine_hostname=_MACHINE_HOSTNAME,
                        status="active",
                        bound_at=datetime.now(timezone.utc),
                    )
                    session.add(binding)
                    await session.flush()

                    container = DockerContainer(
                        binding_id=binding.id,
                        container_name=container_name,
                        container_id=c.get("short_id", ""),
                        image="hermes-bot:latest",
                        status=c.get("status", "running"),
                        machine_ip=_MACHINE_IP,
                    )
                    session.add(container)
                    count += 1
                await session.commit()
            if count:
                logger.info("从 %d 个容器恢复绑定", count)
        except Exception as e:
            logger.warning("从容器恢复绑定失败: %s", e)
        return count

    # ── QR 历史 ─────────────────────────────────────────────────────

    async def log_qr_event(self, profile: str, status: str, user_id: str = ""):
        """记录扫码事件。"""
        if not self._enabled:
            return
        try:
            async with self._session() as session:
                event = QrHistory(
                    profile_name=profile,
                    machine_ip=_MACHINE_IP,
                    user_id=user_id or "",
                    status=status,
                    resolved_at=datetime.now(timezone.utc) if status in (
                        "confirmed", "expired", "failed") else None,
                )
                session.add(event)
                await session.commit()
        except Exception as e:
            logger.warning("记录扫码事件失败: %s", e)

    # ── 健康检查 ─────────────────────────────────────────────────────

    async def log_health_check(self, profile: str, status: str,
                                restart_count: int = 0, error: str = ""):
        """记录健康检查结果。"""
        if not self._enabled:
            return
        try:
            async with self._session() as session:
                log = GatewayHealthLog(
                    profile_name=profile,
                    machine_ip=_MACHINE_IP,
                    status=status,
                    restart_count=restart_count,
                    error_message=error or None,
                )
                session.add(log)
                # 同时更新 binding 的心跳时间
                stmt = select(Binding).where(Binding.profile_name == profile)
                result = await session.execute(stmt)
                binding = result.scalar_one_or_none()
                if binding:
                    binding.last_heartbeat_at = datetime.now(timezone.utc)
                await session.commit()
        except Exception as e:
            logger.warning("记录健康检查失败: %s", e)

    async def update_binding_heartbeat(self, profile: str):
        """更新绑定心跳时间。"""
        if not self._enabled:
            return
        try:
            async with self._session() as session:
                stmt = select(Binding).where(Binding.profile_name == profile)
                result = await session.execute(stmt)
                binding = result.scalar_one_or_none()
                if binding:
                    binding.last_heartbeat_at = datetime.now(timezone.utc)
                    await session.commit()
        except Exception as e:
            logger.warning("更新心跳失败: %s", e)

    async def update_binding_credentials(self, profile: str, account_id: str = "",
                                          bot_token: str = "", bot_base_url: str = "") -> bool:
        """更新绑定记录中的微信凭证。"""
        if not self._enabled:
            return False
        try:
            async with self._session() as session:
                stmt = select(Binding).where(Binding.profile_name == profile)
                result = await session.execute(stmt)
                binding = result.scalar_one_or_none()
                if binding:
                    if account_id:
                        binding.account_id = account_id
                    if bot_token:
                        binding.bot_token = bot_token
                    if bot_base_url:
                        binding.bot_base_url = bot_base_url
                    binding.updated_at = datetime.now(timezone.utc)
                    await session.commit()
                    logger.info("绑定凭证已更新: profile=%s", profile)
                    return True
                logger.warning("更新凭证失败: profile=%s 未找到", profile)
                return False
        except Exception as e:
            logger.warning("更新绑定凭证失败: %s", e)
            return False

    async def update_binding_user_info(self, profile: str, phone: str = "",
                                        lobster_name: str = "", user_name: str = "") -> bool:
        """更新绑定记录中的用户信息。"""
        if not self._enabled:
            return False
        try:
            async with self._session() as session:
                stmt = select(Binding).where(Binding.profile_name == profile)
                result = await session.execute(stmt)
                binding = result.scalar_one_or_none()
                if binding:
                    if phone:
                        binding.phone = phone
                    if lobster_name:
                        binding.lobster_name = lobster_name
                    if user_name:
                        binding.user_name = user_name
                    binding.updated_at = datetime.now(timezone.utc)
                    await session.commit()
                    logger.info("绑定用户信息已更新: profile=%s", profile)
                    return True
                return False
        except Exception as e:
            logger.warning("更新绑定用户信息失败: %s", e)
            return False

    # ── API Key 管理 ────────────────────────────────────────────────

    async def load_api_keys(self) -> list[dict]:
        """加载本机的所有有效 API Key（启动时 + 定时刷新用）。"""
        if not self._enabled:
            return []
        try:
            async with self._session() as session:
                stmt = select(ApiKey).where(
                    ApiKey.machine_ip == _MACHINE_IP,
                    ApiKey.is_active == 1,
                )
                result = await session.execute(stmt)
                keys = result.scalars().all()
                return [
                    {
                        "id": k.id,
                        "provider": k.provider,
                        "access_token": k.access_token,
                        "label": k.label or "",
                    }
                    for k in keys
                ]
        except Exception as e:
            logger.warning("加载 API Key 失败: %s", e)
            return []

    async def create_api_key(self, provider: str, access_token: str,
                               label: str = "") -> Optional[int]:
        """创建 API Key 记录，返回 id。"""
        if not self._enabled:
            return None
        try:
            async with self._session() as session:
                key = ApiKey(
                    provider=provider,
                    access_token=access_token,
                    label=label or "",
                    machine_ip=_MACHINE_IP,
                    is_active=1,
                )
                session.add(key)
                await session.commit()
                logger.info("API Key 已创建: provider=%s label=%s id=%d",
                            provider, label or "(未命名)", key.id)
                return key.id
        except Exception as e:
            logger.warning("创建 API Key 失败: %s", e)
            return None

    async def delete_api_key(self, key_id: int) -> bool:
        """软删除 API Key（is_active=0）。"""
        if not self._enabled:
            return False
        try:
            async with self._session() as session:
                stmt = select(ApiKey).where(
                    ApiKey.id == key_id,
                    ApiKey.machine_ip == _MACHINE_IP,
                )
                result = await session.execute(stmt)
                key = result.scalar_one_or_none()
                if not key:
                    return False
                key.is_active = 0
                key.updated_at = datetime.now(timezone.utc)
                await session.commit()
                logger.info("API Key 已删除: id=%d provider=%s", key_id, key.provider)
                return True
        except Exception as e:
            logger.warning("删除 API Key 失败: %s", e)
            return False


# ── 全局单例 ──────────────────────────────────────────────────────
pg_store = PgStore()