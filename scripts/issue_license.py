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
LEDGER = pathlib.Path.home() / "Desktop" / "jd-kuaiche-授权记录.jsonl"


def record(device: str, note: str, key: str) -> None:
    """把签发结果追加到台账。

    客户丢了授权码时可以直接查回来重发，不必重新签发 —— 重新签发本身无害
    （同设备码签出的码等价），但台账还能回答「一共卖给了谁」。
    """
    import datetime
    import json

    entry = {
        "issued_at": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
        "device": device.strip().replace("-", "").lower(),
        "note": note,
        "key": key,
    }
    with LEDGER.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry, ensure_ascii=False) + "\n")


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):
                pass

    parser = argparse.ArgumentParser(description="签发京东快车轮换助手永久授权码")
    parser.add_argument("device", help="买家提供的设备码（64 位十六进制，可含连字符）")
    parser.add_argument("--note", default="", help="备注，例如客户名；会写进授权码")
    parser.add_argument(
        "--private-key",
        type=pathlib.Path,
        default=DEFAULT_KEY,
        help=f"签发私钥路径（默认 {DEFAULT_KEY}）",
    )
    parser.add_argument("--no-record", action="store_true", help="不写签发台账")
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

    if not args.no_record:
        try:
            record(args.device, args.note, key)
        except OSError as exc:
            # 台账写不进去不该让签发失败 —— 授权码本身已经生成好了。
            print(f"（提醒：台账未写入：{exc}）", file=sys.stderr)

    print()
    print("授权码（把下面这一整行发给买家）：")
    print()
    print(key)
    print()
    if args.note:
        print(f"备注：{args.note}")
    if not args.no_record:
        print(f"已记入台账：{LEDGER}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
