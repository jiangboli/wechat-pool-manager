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


class HotPool:
    """热池管理器——保持 N 个活跃 QR 扫码槽位。"""

    def __init__(self, config: dict, state_ref, profile_manager_ref, gateway_manager_ref):
        self.config = config
        self.state = state_ref
        self.pm = profile_manager_ref
        self.gm = gateway_manager_ref

        self.pool_size = config.get("pool", {}).get("hot_pool_size", 5)
        self.prefix = config.get("pool", {}).get("profile_prefix", "weixin-")
        self.slots: Dict[str, HotPoolSlot] = {}
        self._tasks: Dict[str, asyncio.Task] = {}
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
                    self.state.mark_bound(profile, slot.user_id)
                    logger.info("[%s] 绑定完成，启动 gateway", profile)
                    ok, msg = self.gm.start(profile)
                    if ok:
                        logger.info("[%s] gateway 已启动", profile)
                    else:
                        logger.error("[%s] gateway 启动失败: %s", profile, msg)
                        self.state.mark_unhealthy(profile, msg)
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
        """QR 确认回调——创建/重用 Linux 用户 + 写入凭证 + 启动 gateway。

        安全设计：
        - .env 只写微信凭证（account_id, token），不写任何 API key
        - config.yaml 不写 api_key
        - 模型配置（provider, base_url）指向 proxy 地址
        """
        profile = result["profile"]
        credentials = {
            "account_id": result["account_id"],
            "token": result["token"],
            "base_url": result.get("base_url", ""),
            "user_id": result.get("user_id", ""),
        }

        user_id = credentials["user_id"]

        # 去重：同一个微信用户二次扫码 → 复用已有 Linux 用户
        existing_luser = self.state.get_linux_user_by_user_id(user_id)
        if existing_luser:
            logger.info("[%s] 用户 %s 已存在（user_id=%s），更新凭证 + 重启 gateway",
                        profile, existing_luser, user_id)
            ok = self.pm.update_credentials(existing_luser, credentials)
            if ok:
                self.state.set_status(profile, "bound_healthy",
                                      bound_at=time.strftime("%Y-%m-%dT%H:%M:%S"),
                                      user_id=user_id)
                self.gm.restart(existing_luser)
            else:
                logger.error("[%s] 更新凭证失败", profile)
            return

        # 新用户：创建 Linux 用户 + 写入配置
        ok = self.pm.setup_linux_profile(profile, credentials)
        if ok:
            logger.info("[%s] Linux 用户配置完成", profile)
        else:
            logger.error("[%s] Linux 用户配置失败！", profile)

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
