#!/usr/bin/env python3
"""QR 码生成工具——为指定 URL 生成二维码 PNG 图片。

用法:
  python scripts/gen_qr.py --url "http://example.com"
  python scripts/gen_qr.py --url "http://example.com" --output /path/to/qr.png

依赖:
  pip install qrcode Pillow
"""

import argparse
import os
import sys

try:
    import qrcode
except ImportError:
    print("错误：缺少依赖，请安装: pip install qrcode Pillow", file=sys.stderr)
    sys.exit(1)


def generate_qr(
    url: str,
    output: str = "qr_code.png",
    box_size: int = 10,
    border: int = 2,
) -> str:
    """生成二维码 PNG 图片。

    Args:
        url: 要编码的 URL
        output: 输出文件路径
        box_size: 每个 QR 模块的像素大小
        border: 边框宽度（模块数）

    Returns:
        输出文件的绝对路径
    """
    qr = qrcode.QRCode(border=border, box_size=box_size)
    qr.add_data(url)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    img.save(output)
    return os.path.abspath(output)


def main():
    parser = argparse.ArgumentParser(
        description="为指定 URL 生成二维码 PNG 图片",
    )
    parser.add_argument(
        "--url",
        required=True,
        help="要编码的 URL（如 http://10.100.11.94:8765/）",
    )
    parser.add_argument(
        "--output",
        default="qr_code.png",
        help="输出图片路径（默认: qr_code.png）",
    )
    parser.add_argument(
        "--box-size",
        type=int,
        default=10,
        help="QR 模块像素大小（默认: 10）",
    )
    parser.add_argument(
        "--border",
        type=int,
        default=2,
        help="边框宽度（默认: 2）",
    )
    args = parser.parse_args()

    path = generate_qr(
        url=args.url,
        output=args.output,
        box_size=args.box_size,
        border=args.border,
    )
    print(f"✅ QR 码已生成: {path}")


if __name__ == "__main__":
    main()
