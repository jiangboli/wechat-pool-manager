#!/usr/bin/env python3
"""
批量创建 Hermes profile。

用法：
    python scripts/create_profiles.py --count 100 --prefix weixin-
    python scripts/create_profiles.py --count 50 --clone-from default --prefix wx-
"""

import argparse
import os
import sys

# 将项目根目录加入 sys.path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pool_manager.profile_manager import batch_create


def main():
    parser = argparse.ArgumentParser(description="批量创建 Hermes profile")
    parser.add_argument("--count", type=int, default=100, help="创建数量（默认 100）")
    parser.add_argument("--prefix", type=str, default="weixin-", help="profile 名前缀（默认 weixin-）")
    parser.add_argument("--clone-from", type=str, default="default", help="克隆源 profile（默认 default）")
    args = parser.parse_args()

    print(f"准备创建 {args.count} 个 profile（前缀: {args.prefix}）")
    created = batch_create(args.prefix, args.count, args.clone_from)
    print(f"\n完成！成功创建 {len(created)} 个 profile")

    if created:
        print(f"范围: {created[0]} ~ {created[-1]}")


if __name__ == "__main__":
    main()