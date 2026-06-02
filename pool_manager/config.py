"""配置加载与合并。

优先级：CLI 参数 > config.yaml > 代码默认值
"""

import os
import yaml
from typing import Dict, Any
from pathlib import Path

DEFAULT_CONFIG_PATH = os.path.expanduser("~/.hermes/wechat-pool/config.yaml")

# ── 代码默认值 ──────────────────────────────────────────────────────────
DEFAULTS: Dict[str, Any] = {
    "pool": {
        "total_profiles": 500,
        "hot_pool_size": 5,
        "max_bound_gateways": 500,
        "profile_prefix": "weixin-",
        "max_qr_refresh": 3,
    },
    "gateway": {
        "idle_timeout_minutes": 1440,
        "restart_on_crash": True,
        "max_restart_attempts": 3,
        "restart_delay_seconds": 10,
        "health_check_interval": 60,
    },
    "frontend": {
        "api_port": 8765,
        "qr_refresh_seconds": 45,
        "host": "0.0.0.0",
        "public_url": "",
    },
    "logging": {
        "level": "INFO",
        "dir": os.path.expanduser("~/.hermes/wechat-pool/logs/"),
        "retain_days": 7,
        "stats_window_hours": 168,
    },
    "ilink": {
        "base_url": "https://ilinkai.weixin.qq.com",
        "qr_timeout_seconds": 480,
        "qr_poll_interval": 1,
    },
}


def _deep_merge(base: dict, override: dict) -> dict:
    """递归合并两个 dict，override 覆盖 base 的同级 key。"""
    result = base.copy()
    for key, val in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(val, dict):
            result[key] = _deep_merge(result[key], val)
        else:
            result[key] = val
    return result


def load_config(path: str = None, cli_overrides: dict = None) -> dict:
    """加载配置，优先级：CLI > 文件 > 默认值。"""
    config = DEFAULTS.copy()

    # 从文件加载
    config_path = path or DEFAULT_CONFIG_PATH
    if os.path.exists(config_path):
        with open(config_path, "r") as f:
            file_config = yaml.safe_load(f) or {}
        config = _deep_merge(config, file_config)

    # CLI 参数覆盖
    if cli_overrides:
        # 扁平 CLI 参数映射到嵌套结构
        cli_map = {
            "total": ("pool", "total_profiles"),
            "hot_pool": ("pool", "hot_pool_size"),
            "max_bound": ("pool", "max_bound_gateways"),
            "prefix": ("pool", "profile_prefix"),
            "port": ("frontend", "api_port"),
        }
        for cli_key, val in cli_overrides.items():
            if val is not None and cli_key in cli_map:
                sec, key = cli_map[cli_key]
                config.setdefault(sec, {})[key] = val

    return config


def resolve_path(p: str) -> str:
    """展开 ~ 和环境变量。"""
    return os.path.expandvars(os.path.expanduser(p))