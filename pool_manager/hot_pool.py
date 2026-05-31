"""热池引擎——维护常驻 QR 扫码槽位。

核心逻辑：
1. 保持 N 个未绑定 Linux 用户同时运行 qr_login() 轮询
2. 二维码过期自动刷新（最多 3 次）
3. 检测到确认绑定 → 回调通知调用方（创建 Linux 用户 + 写入凭证 + 启动 gateway）
4. 补充新槽位

去重：同一微信用户二次扫码 → 复用已有 Linux 用户，不创建新用户。
安全：不传递、不写入任何 API key。
"""

import asyncio
import logging
import os
import sys
import time
from typing import Callable, Dict, Optional

from . import docker_scheduler as ds
from . import gateway_manager as gm
from .pg_store import pg_store

logger = logging.getLogger("pool_manager.hot_pool")

# WeChat iLink API 端点
EP_GET_BOT_QR = "ilink/bot/get_bot_qrcode"
EP_GET_QR_STATUS = "ilink/bot/get_qrcode_status"
ILINK_BASE_URL = "https://ilinkai.weixin.qq.com"
QR_TIMEOUT_MS = 35_000


class HotPoolSlot:
    """热池中的一个槽位。"""

    def __init__(self, profile: str, config: dict):
        self.profile = profile
        self.config = config
        self.base_url = ILINK_BASE_URL

        # QR 登录状态
        self.qr_url: str = ""
        self.qr_value: str = ""
        self.status: str = "idle"
        self.refresh_count: int = 0
        self.max_refresh: int = config.get("pool", {}).get("max_qr_refresh", 3)
        self.refreshed_at: str = ""

        # 绑定结果
        self.account_id: Optional[str] = None
        self.token: Optional[str] = None
        self.user_id: Optional[str] = None
        self.bot_base_url: Optional[str] = None

        # 用户提交的信息（表单填写）
        self.user_info: Optional[dict] = None

        self._aiohttp = None
        self._running = False

    async def _api_get(self, url: str, timeout_ms: int = 15000) -> Optional[dict]:
        import aiohttp
        import json as _json
        try:
            timeout = aiohttp.ClientTimeout(total=timeout_ms / 1000)
            async with self._aiohttp.get(url, timeout=timeout, ssl=False) as resp:
                if resp.status == 200:
                    body = await resp.read()
                    return _json.loads(body)
        except Exception as exc:
            logger.warning("[%s] API 调用失败: %s", self.profile, exc)
        return None

    async def run(self, on_confirmed: Callable) -> bool:
        import aiohttp
        self._running = True
        self._consecutive_failures = 0
        conn = aiohttp.TCPConnector(ssl=False)
        async with aiohttp.ClientSession(connector=conn, trust_env=True) as session:
            self._aiohttp = session
            deadline = asyncio.get_event_loop().time() + \
                self.config.get("ilink", {}).get("qr_timeout_seconds", 480)

            while self._running and asyncio.get_event_loop().time() < deadline:
                qr_resp = await self._api_get(
                    f"{self.base_url}/{EP_GET_BOT_QR}?bot_type=3",
                    QR_TIMEOUT_MS,
                )
                if not qr_resp:
                    self._consecutive_failures += 1
                    if self._consecutive_failures > 10:
                        logger.warning("[%s] 连续 %d 次获取二维码失败，放弃当前槽位",
                                       self.profile, self._consecutive_failures)
                        return False
                    await asyncio.sleep(1)
                    continue

                self._consecutive_failures = 0
                self.qr_value = str(qr_resp.get("qrcode") or "")
                self.qr_url = str(qr_resp.get("qrcode_img_content") or "")
                self.refreshed_at = time.strftime("%H:%M:%S")
                if not self.qr_value:
                    await asyncio.sleep(1)
                    continue

                self.status = "waiting"
                logger.info("[%s] 二维码已就绪", self.profile)

                while self._running and asyncio.get_event_loop().time() < deadline:
                    status_resp = await self._api_get(
                        f"{self.base_url}/{EP_GET_QR_STATUS}?qrcode={self.qr_value}",
                        QR_TIMEOUT_MS,
                    )
                    if not status_resp:
                        await asyncio.sleep(
                            self.config.get("ilink", {}).get("qr_poll_interval", 1))
                        continue

                    status = str(status_resp.get("status") or "wait")
                    if status == "wait":
                        pass
                    elif status == "scaned":
                        self.status = "scaned"
                        logger.info("[%s] 已扫码，等待用户确认", self.profile)
                    elif status == "scaned_but_redirect":
                        host = str(status_resp.get("redirect_host") or "")
                        if host:
                            self.base_url = f"https://{host}"
                    elif status == "expired":
                        self.refresh_count += 1
                        if self.refresh_count > self.max_refresh:
                            logger.warning("[%s] 二维码多次过期（%d次）", self.profile, self.refresh_count)
                            self.status = "expired"
                            return False
                        break
                    elif status == "confirmed":
                        self.account_id = str(status_resp.get("ilink_bot_id") or "")
                        self.token = str(status_resp.get("bot_token") or "")
                        self.bot_base_url = str(status_resp.get("baseurl") or self.base_url)
                        self.user_id = str(status_resp.get("ilink_user_id") or "")
                        if not self.account_id or not self.token:
                            logger.error("[%s] QR 确认但凭证不完整", self.profile)
                            return False

                        # 检查用户信息是否完整（必须经过表单提交才能绑定）
                        if not self.user_info or not self.user_info.get("phone"):
                            logger.warning("[%s] QR 被直接扫描（未经过表单提交），忽略此次绑定，重新生成二维码",
                                          self.profile)
                            self.status = "expired"
                            self.refresh_count += 1
                            if self.refresh_count > self.max_refresh:
                                return False
                            break  # 跳出内层循环，重新获取新二维码

                        self.status = "confirmed"
                        logger.info("[%s] 绑定成功！user_id=%s", self.profile, self.user_id)

                        await on_confirmed({
                            "profile": self.profile,
                            "account_id": self.account_id,
                            "token": self.token,
                            "base_url": self.bot_base_url,
                            "user_id": self.user_id,
                        })
                        return True

                    await asyncio.sleep(
                        self.config.get("ilink", {}).get("qr_poll_interval", 1))
        return False

    def stop(self):
        self._running = False


class RebindSession:
    """已绑定用户的重新扫码会话——直接为指定 profile 生成 QR，不占热池槽位。"""

    def __init__(self, profile: str):
        self.profile = profile
        self.config = {}
        self.base_url = ILINK_BASE_URL
        self.qr_url = ""
        self.qr_value = ""
        self.status = "idle"
        self.account_id = None
        self.token = None
        self.user_id = None
        self.bot_base_url = None
        self._aiohttp = None
        self._running = False

    async def _api_get(self, url: str, timeout_ms: int = 15000):
        import aiohttp
        import json
        try:
            timeout = aiohttp.ClientTimeout(total=timeout_ms / 1000)
            async with self._aiohttp.get(url, timeout=timeout, ssl=False) as resp:
                if resp.status == 200:
                    body = await resp.read()
                    return json.loads(body)
        except Exception as exc:
            logger.warning("[rebind:%s] API 调用失败: %s", self.profile, exc)
        return None

    async def start(self, config: dict) -> bool:
        """调用 iLink 生成 QR 并轮询扫码状态。返回 True=确认成功。"""
        import aiohttp
        import asyncio
        self.config = config
        self._running = True
        conn = aiohttp.TCPConnector(ssl=False)
        async with aiohttp.ClientSession(connector=conn, trust_env=True) as session:
            self._aiohttp = session
            deadline = asyncio.get_event_loop().time() +                 config.get("ilink", {}).get("qr_timeout_seconds", 480)

            # Step 1: 获取二维码
            qr_resp = await self._api_get(
                f"{self.base_url}/{EP_GET_BOT_QR}?bot_type=3",
                QR_TIMEOUT_MS,
            )
            if not qr_resp:
                self.status = "error"
                logger.error("[rebind:%s] 获取二维码失败", self.profile)
                return False

            self.qr_value = str(qr_resp.get("qrcode") or "")
            self.qr_url = str(qr_resp.get("qrcode_img_content") or "")
            self.status = "waiting"
            logger.info("[rebind:%s] 二维码已就绪", self.profile)

            # Step 2: 轮询扫码状态
            while self._running and asyncio.get_event_loop().time() < deadline:
                status_resp = await self._api_get(
                    f"{self.base_url}/{EP_GET_QR_STATUS}?qrcode={self.qr_value}",
                    QR_TIMEOUT_MS,
                )
                if not status_resp:
                    await asyncio.sleep(
                        config.get("ilink", {}).get("qr_poll_interval", 1))
                    continue

                status = str(status_resp.get("status") or "wait")
                if status == "confirmed":
                    self.account_id = str(status_resp.get("ilink_bot_id") or "")
                    self.token = str(status_resp.get("bot_token") or "")
                    self.bot_base_url = str(status_resp.get("baseurl") or self.base_url)
                    self.user_id = str(status_resp.get("ilink_user_id") or "")
                    if not self.account_id or not self.token:
                        logger.error("[rebind:%s] QR 确认但凭证不完整", self.profile)
                        self.status = "error"
                        return False
                    self.status = "confirmed"
                    logger.info("[rebind:%s] 重新绑定成功！user_id=%s", self.profile, self.user_id)
                    return True
                elif status == "expired":
                    self.status = "expired"
                    logger.warning("[rebind:%s] 二维码已过期", self.profile)
                    return False
                elif status == "scaned":
                    logger.info("[rebind:%s] 已扫码，等待确认", self.profile)

                await asyncio.sleep(
                    config.get("ilink", {}).get("qr_poll_interval", 1))

            self.status = "timeout"
            logger.warning("[rebind:%s] 二维码等待超时", self.profile)
            return False

    def stop(self):
        self._running = False


class HotPool:
    """热池管理器——保持 N 个活跃 QR 扫码槽位。"""

    def __init__(self, config: dict, state_ref, profile_manager_ref, gateway_manager_ref):
        self.config = config
        self.state = state_ref
        self.pm = profile_manager_ref
        self.gm = gateway_manager_ref
        self._scheduler = gm._scheduler  # 全局 DockerScheduler 实例

        self.pool_size = config.get("pool", {}).get("hot_pool_size", 5)
        self.prefix = config.get("pool", {}).get("profile_prefix", "weixin-")
        self.slots: Dict[str, HotPoolSlot] = {}
        self._tasks: Dict[str, asyncio.Task] = {}
        self._rebind_sessions: Dict[str, RebindSession] = {}
        self._running = False

    async def start(self):
        self._running = True
        logger.info("热池启动，目标槽位数: %d", self.pool_size)
        while self._running:
            await self._tick()
            await asyncio.sleep(3)
        logger.info("热池已停止")

    async def stop(self):
        self._running = False
        for name, task in self._tasks.items():
            task.cancel()
            if name in self.slots:
                self.slots[name].stop()
        self._tasks.clear()
        self.slots.clear()

    async def _tick(self):
        active = len(self.slots)
        needed = self.pool_size - active
        if needed > 0:
            available = self.state.get_unbound()
            for profile in available[:needed]:
                if profile not in self.slots:
                    self._start_slot(profile)

    def _start_slot(self, profile: str):
        slot = HotPoolSlot(profile, self.config)
        self.slots[profile] = slot
        self.state.mark_hot_pool(profile, qr_url="获取中...")
        logger.info("[%s] 加入热池", profile)

        async def _run_slot():
            try:
                success = await slot.run(self._on_confirmed)
                if success:
                    phone = (slot.user_info or {}).get("phone", "")

                    # 全局手机号去重：查 PG 是否已有绑定
                    if phone and pg_store.enabled:
                        existing = await pg_store.find_binding_by_phone(phone)
                        if existing:
                            logger.info("[%s] 手机号 %s 已在 %s 上绑定（profile=%s），复用已有容器",
                                        profile, phone, existing["machine_ip"], existing["profile_name"])
                            self.state.mark_bound(profile, slot.user_id or "")
                            # 更新凭证 + 重启容器
                            self._scheduler.write_env(existing["profile_name"], {
                                "account_id": slot.account_id or "",
                                "token": slot.token or "",
                                "base_url": slot.bot_base_url or "",
                            })
                            # 更新 lobster_name（SOUL.md）
                            old_lobster = (slot.user_info or {}).get("lobster_name", "")
                            if old_lobster:
                                self._scheduler.write_soul(existing["profile_name"], lobster_name=old_lobster)
                            self._scheduler.restart_container(existing["profile_name"])
                            # 清理旧 session/会话数据，让新用户从零开始
                            self._scheduler.delete_runtime_data(existing["profile_name"])
                            # 记录事件 + 从热池中移除
                            await pg_store.log_qr_event(profile, "rebind", slot.user_id or "")
                            return

                    self.state.mark_bound(profile, slot.user_id)
                    logger.info("[%s] 绑定完成，创建 Docker 容器", profile)
                    # 创建并启动 Docker 容器
                    lobster_name = (slot.user_info or {}).get("lobster_name", "")
                    ok = self._scheduler.create_container(profile, {
                        "account_id": slot.account_id,
                        "token": slot.token,
                        "base_url": slot.bot_base_url or "",
                        "user_id": slot.user_id or "",
                    }, lobster_name=lobster_name) if self._scheduler else False
                    if ok:
                        logger.info("[%s] Docker 容器已创建并启动", profile)
                        # ── PG 持久化 ──
                        if pg_store.enabled:
                            container_name = f"hermes-{profile}"
                            ui = slot.user_info or {}
                            binding_id = await pg_store.create_binding(
                                profile=profile,
                                user_id=slot.user_id or "",
                                account_id=slot.account_id or "",
                                bot_token=slot.token or "",
                                bot_base_url=slot.bot_base_url or "",
                                phone=ui.get("phone", ""),
                                lobster_name=ui.get("lobster_name", ""),
                                user_name=ui.get("user_name", ""),
                            )
                            if binding_id:
                                await pg_store.create_container(
                                    binding_id=binding_id,
                                    container_name=container_name,
                                    container_id="",
                                    image="hermes-bot:latest",
                                    status="running",
                                    memory_limit="2g",
                                )
                            await pg_store.log_qr_event(profile, "confirmed", slot.user_id)
                    else:
                        logger.error("[%s] Docker 容器创建失败", profile)
                        self.state.mark_unhealthy(profile, "容器创建失败")
                else:
                    if slot.status == "expired":
                        self.state.mark_qr_failed(profile)
                    else:
                        self.state.mark_qr_failed(profile)
                    await asyncio.sleep(5)
                    self.state.mark_available(profile)
            except asyncio.CancelledError:
                pass
            except Exception as e:
                logger.error("[%s] 槽位异常: %s", profile, e)
                self.state.mark_qr_failed(profile)
            finally:
                self.slots.pop(profile, None)
                self._tasks.pop(profile, None)
                slot.stop()

        task = asyncio.create_task(_run_slot())
        self._tasks[profile] = task

    async def _on_confirmed(self, result: dict):
        """QR 确认回调——记录确认信息，实际容器创建在 _run_slot 中处理。"""
        profile = result["profile"]
        slot = self.slots.get(profile)
        if slot:
            slot.account_id = result.get("account_id", slot.account_id)
            slot.token = result.get("token", slot.token)
            slot.bot_base_url = result.get("base_url", slot.bot_base_url)
            slot.user_id = result.get("user_id", slot.user_id)
        logger.info("[%s] 扫码确认成功 user_id=%s", profile, result.get("user_id", ""))

    def get_slot_qr(self, profile: str) -> Optional[str]:
        slot = self.slots.get(profile)
        return slot.qr_url if slot else None

    def get_all_slots(self) -> list:
        result = []
        for name, slot in self.slots.items():
            result.append({
                "profile": name,
                "status": slot.status,
                "qr_url": slot.qr_url,
                "refreshed_at": slot.refreshed_at,
            })
        return result

    async def generate_rebind_qr(self, profile: str) -> Optional[str]:
        """为已绑定 profile 生成独立 QR 码（不占热池槽位）。"""
        session = RebindSession(profile)
        self._rebind_sessions[profile] = session
        asyncio.create_task(session.start(self.config))
        # 等待 QR 就绪（最多等 15 秒）
        for i in range(30):
            await asyncio.sleep(0.5)
            if session.qr_url:
                logger.info("[rebind:%s] QR 已就绪", profile)
                return session.qr_url
            if session.status in ("error", "expired", "timeout"):
                logger.warning("[rebind:%s] QR 生成失败: %s", profile, session.status)
                self.cleanup_rebind_session(profile)
                return None
        logger.warning("[rebind:%s] QR 生成超时", profile)
        self.cleanup_rebind_session(profile)
        return None

    def get_rebind_session(self, profile: str) -> Optional[RebindSession]:
        """获取 rebind 会话状态。"""
        return self._rebind_sessions.get(profile)

    def cleanup_rebind_session(self, profile: str):
        """清理已完成的 rebind 会话。"""
        session = self._rebind_sessions.pop(profile, None)
        if session:
            session.stop()

    def set_slot_user_info(self, profile: str, user_info: dict):
        """将用户提交的信息绑定到指定槽位。"""
        slot = self.slots.get(profile)
        if slot:
            slot.user_info = user_info
            logger.info("[%s] 用户信息已绑定: %s", profile, user_info.get("user_name", ""))

