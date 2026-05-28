#!/usr/bin/env python3
"""
批量创建 Linux 用户（替代原来的 hermes profile create）。

用法:
    python3 scripts/create_profiles.py --count 100 --prefix wx

每次会先读取已存在的用户列表，跳过已创建的，只补新的。
"""

import argparse
import os
import subprocess
import sys
from typing import List


def linux_user_exists(username: str) -> bool:
    """检查 Linux 用户是否存在。"""
    try:
        result = subprocess.run(
            ["id", username],
            capture_output=True, text=True, timeout=10)
        return result.returncode == 0
    except Exception:
        return False


def create_linux_user(username: str) -> bool:
    """创建 Linux 用户（通过 sudo，依赖 sudoers 配置）。"""
    try:
        result = subprocess.run(
            ["sudo", "-n", "useradd", "-m", username],
            capture_output=True, text=True, timeout=30)
        if result.returncode == 0:
            return True
        if "already exists" in result.stderr:
            return True
        print(f"  [!] 创建用户 {username} 失败: {result.stderr.strip()}", file=sys.stderr)
        return False
    except Exception as e:
        print(f"  [!] 创建用户 {username} 异常: {e}", file=sys.stderr)
        return False


def write_stub_config(username: str) -> bool:
    """写入空的 Hermes 配置（扫码绑定后会由 setup_linux_profile 覆盖）。"""
    import tempfile
    import yaml

    stub = {
        "platforms": {
            "weixin": {
                "enabled": False,
            },
        },
    }

    hermes_dir = f"/home/{username}/.hermes"
    cfg_path = f"{hermes_dir}/config.yaml"

    # 创建目录
    subprocess.run(
        ["sudo", "-n", "mkdir", "-p", hermes_dir],
        capture_output=True, timeout=10)
    subprocess.run(
        ["sudo", "-n", "chown", f"{username}:{username}", hermes_dir],
        capture_output=True, timeout=10)

    # 写临时文件
    content = yaml.dump(stub, default_flow_style=False, allow_unicode=True)
    yaml_path = f"/tmp/_stub_{username}.yaml"
    with open(yaml_path, "w") as f:
        f.write(content)

    # sudo cp
    subprocess.run(
        ["sudo", "-n", "cp", yaml_path, cfg_path],
        capture_output=True, timeout=10)
    subprocess.run(
        ["sudo", "-n", "chown", f"{username}:{username}", cfg_path],
        capture_output=True, timeout=10)
    os.unlink(yaml_path)
    return True


def batch_create_linux_users(count: int, prefix: str = "wx") -> List[str]:
    """批量创建 Linux 用户。跳过已存在的。"""
    existing = []
    for entry in os.listdir("/home"):
        if entry.startswith(prefix) and os.path.isdir(f"/home/{entry}"):
            existing.append(entry)

    created = []
    for i in range(1, count + 1):
        name = f"{prefix}{i:03d}"
        if name in existing:
            continue
        if create_linux_user(name):
            write_stub_config(name)
            created.append(name)
            print(f"  ✅ {name}")
        else:
            print(f"  ❌ {name}")

    return created


def main():
    parser = argparse.ArgumentParser(description="批量创建 Linux 用户")
    parser.add_argument("--count", type=int, default=100, help="总数")
    parser.add_argument("--prefix", type=str, default="wx", help="用户名前缀")
    args = parser.parse_args()

    print(f"批量创建 Linux 用户: {args.count} 个，前缀: {args.prefix}")
    created = batch_create_linux_users(args.count, args.prefix)
    print(f"完成: 已存在 {args.count - len(created)} 个，新创建 {len(created)} 个")


if __name__ == "__main__":
    main()
