#!/usr/bin/env python3
"""签发授权码（**只在作者机器上运行**，需要私钥）。

用法：

    python scripts/issue_license.py <设备码> [--note 客户名] \
        [--private-key ~/Desktop/jd-kuaiche-签发私钥-请妥善保管.pem]

买家在应用的激活页复制设备码发来，用本脚本签发后把授权码回给他。
授权码永久有效，且只在那一台设备上可用。

私钥绝不能进仓库，也不要发给任何人 —— 泄露等于任何人都能自行签发。
"""

from __future__ import annotations

import argparse
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from jdka.license import issue  # noqa: E402

DEFAULT_KEY = pathlib.Path.home() / "Desktop" / "jd-kuaiche-签发私钥-请妥善保管.pem"


def main() -> int:
    parser = argparse.ArgumentParser(description="签发京东快车轮换助手永久授权码")
    parser.add_argument("device", help="买家提供的设备码（64 位十六进制，可含连字符）")
    parser.add_argument("--note", default="", help="备注，例如客户名；会写进授权码")
    parser.add_argument(
        "--private-key",
        type=pathlib.Path,
        default=DEFAULT_KEY,
        help=f"签发私钥路径（默认 {DEFAULT_KEY}）",
    )
    args = parser.parse_args()

    if not args.private_key.exists():
        print(f"找不到私钥：{args.private_key}", file=sys.stderr)
        return 1

    try:
        key = issue(
            device=args.device,
            private_key_pem=args.private_key.read_bytes(),
            note=args.note,
        )
    except ValueError as exc:
        print(f"签发失败：{exc}", file=sys.stderr)
        return 1

    print()
    print("授权码（把下面这一整行发给买家）：")
    print()
    print(key)
    print()
    if args.note:
        print(f"备注：{args.note}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
