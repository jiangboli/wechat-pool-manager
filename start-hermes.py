"""Hermes Gateway 启动包装器——过滤生命周期噪声消息。

在 WeChat 通道中抑制 "Gateway shutting down"、"Retrying..." 等
生命周期状态消息，这些消息对 bot 用户无意义。

用法（替代 hermes gateway run）:
    python start-hermes.py
"""
import os
import re
import subprocess
import sys


def _patch_status():
    """Monkey-patch Hermes gateway 的 _emit_status 方法，过滤 lifecycle 消息。"""
    try:
        from hermes_cli.gateway.run import HermesGatewayRunner

        _original = HermesGatewayRunner._emit_status

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
            if status_type in ("lifecycle", "status"):
                if _noisy_patterns.search(str(message)):
                    return
            return _original(self, status_type, message, *args, **kwargs)

        HermesGatewayRunner._emit_status = _patched
    except Exception:
        pass  # patch 失败不影响启动


if __name__ == "__main__":
    _patch_status()
    # 使用 subprocess 调用 hermes CLI，兼容不同版本
    hermes_bin = os.environ.get("HERMES_BIN", "hermes")
    sys.exit(subprocess.call([hermes_bin, "gateway", "run", "--replace"]))
