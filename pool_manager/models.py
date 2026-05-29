"""SQLAlchemy ORM 模型——Claw DO 中心存储。"""

from datetime import datetime, timezone
from sqlalchemy import Column, Integer, String, Text, DateTime, ForeignKey, JSON, BigInteger
from sqlalchemy.orm import DeclarativeBase, relationship


class Base(DeclarativeBase):
    pass


class Machine(Base):
    """服务器注册表——每台运行 pool-manager 的机器。"""
    __tablename__ = "machines"

    id = Column(Integer, primary_key=True)
    machine_ip = Column(String(45), unique=True, nullable=False, comment="服务器公网 IP")
    hostname = Column(String(64), comment="主机名")
    location = Column(String(64), comment="机房位置，如 香港/广州")
    total_slots = Column(Integer, default=100, comment="总槽位数")
    hot_pool_size = Column(Integer, default=5, comment="热池大小")
    status = Column(String(20), default="online", comment="online/offline/maintenance")
    version = Column(String(32), comment="pool-manager 版本号")
    last_heartbeat_at = Column(DateTime(timezone=True), comment="最后心跳")
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc),
                        onupdate=lambda: datetime.now(timezone.utc))


class Binding(Base):
    """微信绑定记录——用户与 Docker 容器的关联。"""
    __tablename__ = "bindings"

    id = Column(Integer, primary_key=True)
    profile_name = Column(String(32), unique=True, nullable=False, comment="配置名，如 weixin-001")
    machine_ip = Column(String(45), nullable=False, comment="运行此容器的服务器 IP")
    machine_hostname = Column(String(64), comment="服务器主机名")
    user_id = Column(String(128), comment="微信用户 ID (ilink_user_id)")
    account_id = Column(String(128), comment="机器人账号 ID (ilink_bot_id)")
    nickname = Column(String(128), comment="微信昵称（有则存）")
    avatar_url = Column(Text, comment="微信头像 URL（有则存）")
    bot_token = Column(String(256), comment="机器人 Token（明文存储）")
    bot_base_url = Column(String(256), comment="机器人 API 地址")
    status = Column(String(20), default="active", comment="active/inactive/expired")
    bound_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc),
                      comment="首次绑定时间")
    last_active_at = Column(DateTime(timezone=True), comment="最后活跃时间")
    last_heartbeat_at = Column(DateTime(timezone=True), comment="最后健康心跳")
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc),
                        onupdate=lambda: datetime.now(timezone.utc))

    containers = relationship("DockerContainer", back_populates="binding",
                              cascade="all, delete-orphan")


class DockerContainer(Base):
    """Docker 容器详情。"""
    __tablename__ = "docker_containers"

    id = Column(Integer, primary_key=True)
    binding_id = Column(Integer, ForeignKey("bindings.id"), nullable=False, comment="关联绑定")
    container_name = Column(String(64), unique=True, nullable=False, comment="容器名 hermes-weixin-001")
    container_id = Column(String(64), comment="Docker 容器 ID")
    image = Column(String(128), comment="镜像名")
    status = Column(String(20), nullable=False, comment="running/stopped/exited")
    memory_limit = Column(String(16), comment="内存限制，如 2g")
    restart_count = Column(Integer, default=0, comment="重启次数")
    ports = Column(JSON, comment="端口映射 JSON")
    machine_ip = Column(String(45), comment="运行此容器的服务器 IP")
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc),
                        onupdate=lambda: datetime.now(timezone.utc))

    binding = relationship("Binding", back_populates="containers")


class QrHistory(Base):
    """二维码扫码历史。"""
    __tablename__ = "qr_history"

    id = Column(Integer, primary_key=True)
    profile_name = Column(String(32), nullable=False, comment="哪个槽位")
    machine_ip = Column(String(45), comment="扫码发生在哪个服务器")
    user_id = Column(String(128), comment="扫码者（有则存）")
    status = Column(String(16), nullable=False, comment="created/scanned/confirmed/expired/failed")
    refresh_count = Column(Integer, default=0, comment="此 session 刷新次数")
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    resolved_at = Column(DateTime(timezone=True), comment="最终结果时间")


class GatewayHealthLog(Base):
    """网关健康检查日志。"""
    __tablename__ = "gateway_health_log"

    id = Column(Integer, primary_key=True)
    profile_name = Column(String(32), nullable=False, comment="哪个 profile")
    machine_ip = Column(String(45), comment="哪个服务器")
    status = Column(String(20), nullable=False, comment="healthy/unhealthy")
    restart_count = Column(Integer, default=0, comment="此时重启次数")
    memory_usage = Column(String(32), comment="内存用量")
    error_message = Column(Text, comment="异常信息")
    checked_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))