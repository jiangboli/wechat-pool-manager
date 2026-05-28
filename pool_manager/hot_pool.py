"""热池引擎——维护常驻 QR 扫码槽位。

核心逻辑：
1. 保持 N 个未绑定 profile 同时运行 qr_login() 轮询
2. 二维码过期自动刷新（最多 3 次）
3. 检测到确认绑定 → 回调通知调用方
4. 补充新槽位
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
        """调用 iLink API。"""
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
        """运行 QR 登录流程。返回 True=绑定成功, False=失败。"""
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
                    logger.warning("[%s] 获取二维码失败（第%d次），1秒后重试",
                                   self.profile, self._consecutive_failures)
                    await asyncio.sleep(1)
                    continue

                self._consecutive_failures = 0
                self.qr_value = str(qr_resp.get("qrcode") or "")
                self.qr_url = str(qr_resp.get("qrcode_img_content") or "")
                self.refreshed_at = time.strftime("%H:%M:%S")
                if not self.qr_value:
                    logger.warning("[%s] 二维码响应缺少 qrcode 字段", self.profile)
                    await asyncio.sleep(1)
                    continue

                self.status = "waiting"
                logger.info("[%s] 二维码已就绪", self.profile)

                # 轮询扫码状态
                while self._running and asyncio.get_event_loop().time() < deadline:
                    status_resp = await self._api_get(
                        f"{self.base_url}/{EP_GET_QR_STATUS}?qrcode={self.qr_value}",
                        QR_TIMEOUT_MS,
                    )
                    if not status_resp:
                        await asyncio.sleep(
                            self.config.get("ilink", {}).get("qr_poll_interval", 1)
                        )
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
                            logger.info("[%s] 重定向到 %s", self.profile, self.base_url)
                    elif status == "expired":
                        self.refresh_count += 1
                        if self.refresh_count > self.max_refresh:
                            logger.warning("[%s] 二维码多次过期（%d次）",
                                           self.profile, self.refresh_count)
                            self.status = "expired"
                            return False
                        logger.info("[%s] 二维码过期，刷新（%d/%d）",
                                    self.profile, self.refresh_count, self.max_refresh)
                        break  # 重新获取 QR
                    elif status == "confirmed":
                        self.account_id = str(status_resp.get("ilink_bot_id") or "")
                        self.token = str(status_resp.get("bot_token") or "")
                        self.bot_base_url = str(status_resp.get("baseurl") or self.base_url)
                        self.user_id = str(status_resp.get("ilink_user_id") or "")
                        if not self.account_id or not self.token:
                            logger.error("[%s] QR 确认但凭证不完整", self.profile)
                            return False

                        self.status = "confirmed"
                        logger.info("[%s] 绑定成功！account_id=%s",
                                    self.profile, self.account_id)

                        await on_confirmed({
                            "profile": self.profile,
                            "account_id": self.account_id,
                            "token": self.token,
                            "base_url": self.bot_base_url,
                            "user_id": self.user_id,
                        })
                        return True

                    await asyncio.sleep(
                        self.config.get("ilink", {}).get("qr_poll_interval", 1)
                    )

        return False

    def stop(self):
        """停止槽位。"""
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
        """启动热池主循环。"""
        self._running = True
        logger.info("热池启动，目标槽位数: %d", self.pool_size)
        while self._running:
            await self._tick()
            await asyncio.sleep(3)
        logger.info("热池已停止")

    async def stop(self):
        """停止热池。"""
        self._running = False
        for name, task in self._tasks.items():
            task.cancel()
            if name in self.slots:
                self.slots[name].stop()
        self._tasks.clear()
        self.slots.clear()

    async def _tick(self):
        """每 tick 补充槽位。"""
        # 注意：_run_slot 的 finally 块已负责清理，这里只负责计数和补充
        active = len(self.slots)
        needed = self.pool_size - active
        if needed > 0:
            available = self.state.get_unbound()
            for profile in available[:needed]:
                if profile not in self.slots:
                    self._start_slot(profile)

    def _start_slot(self, profile: str):
        """启动一个 QR 槽位。"""
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
        """QR 确认回调——创建 Linux 用户 + 配置 Hermes 环境 + 写入凭证。"""
        profile = result["profile"]
        credentials = {
            "account_id": result["account_id"],
            "token": result["token"],
            "base_url": result.get("base_url", ""),
            "user_id": result.get("user_id", ""),
        }
        # 从主 Hermes config 读取模型配置（credential pool 处理 api_key）
        import yaml
        main_config_path = os.path.expanduser("~/.hermes/config.yaml")
        try:
            with open(main_config_path) as _f:
                main_cfg = yaml.safe_load(_f)
            model_cfg = main_cfg.get("model", {})
        except Exception:
            model_cfg = {}

        api_env = {
            "PROVIDER": model_cfg.get("provider", ""),
            "MODEL": model_cfg.get("default", ""),
            "BASE_URL": model_cfg.get("base_url", ""),
            "API_KEY": os.environ.get("DEEPSEEK_API_KEY", ""),
        }
        ok = self.pm.setup_linux_profile(profile, credentials, api_env)
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