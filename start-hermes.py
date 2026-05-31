"""Hermes Gateway 启动包装器——过滤生命周期噪声消息。

在 WeChat 通道中抑制 "Gateway shutting down"、"Retrying..." 等
生命周期状态消息，这些消息对 bot 用户无意义。

用法（替代 hermes gateway run）:
    python start-hermes.py
"""
import re
import sys


def _patch_status():
    """Monkey-patch Hermes gateway 的 _emit_status 方法，过滤 lifecycle 消息。"""
    try:
        from hermes_cli.gateway.run import HermesGatewayRunner

        _original = HermesGatewayRunner._emit_status

        # 需要过滤的生命周期消息模式
        _noisy_patterns = re.compile(
            r"^(⚡\s*)?(Gateway shutting down|Stopped|Restarting|Retry)"
            r"|rate limited|waiting \d+s"
            r"|auxiliary \w+ failed"
            r"|fallback context"
            r"|Self-improvement"
            r"|User profile updated",
            re.IGNORECASE,
        )

        def _patched(self, status_type, message, *args, **kwargs):
            # 只过滤 lifecycle 和 status 类型的消息
            if status_type in ("lifecycle", "status"):
                if _noisy_patterns.search(str(message)):
                    return  # 静默丢弃
            return _original(self, status_type, message, *args, **kwargs)

        HermesGatewayRunner._emit_status = _patched
    except Exception as e:
        # 打补丁失败不影响 Hermes 启动
        import logging

        logging.warning("[start-hermes] patch failed: %s", e)


if __name__ == "__main__":
    _patch_status()
    from hermes_cli.main import cli

    sys.exit(cli(["gateway", "run", "--replace"]))
