"""命令行入口。"""

from __future__ import annotations

import argparse
import sys

from jdka import __version__


def _force_utf8_output() -> None:
    """Windows 控制台默认用 GBK/cp1252，打印中文会直接抛 UnicodeEncodeError。

    桌面外壳读取 stdout 的握手行，一旦这里崩掉后端就起不来，因此在做任何
    输出之前先固定成 UTF-8。
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):
                pass


def main(argv: list[str] | None = None) -> int:
    _force_utf8_output()
    parser = argparse.ArgumentParser(
        prog="jdka", description="京东快车轮换助手"
    )
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="command")

    ui = sub.add_parser("ui", help="启动本地控制面板（默认）")
    ui.add_argument("--port", type=int, default=0, help="指定端口，默认随机")
    ui.add_argument("--no-open", action="store_true", help="不自动打开浏览器")
    ui.add_argument("--emit-endpoint", action="store_true",
                    help="在 stdout 打印端口与令牌，供桌面外壳读取")

    sub.add_parser("preview", help="跑一轮只读预览检查后退出")
    sub.add_parser("doctor", help="环境自检")

    args = parser.parse_args(argv)
    command = args.command or "ui"

    if command == "ui":
        from jdka.server import serve

        serve(
            port=getattr(args, "port", 0),
            open_browser=not getattr(args, "no_open", False),
            emit_endpoint=getattr(args, "emit_endpoint", False),
        )
        return 0

    if command == "preview":
        from jdka.service import MonitorService

        service = MonitorService()
        try:
            if not service.config.skus:
                print("尚未配置任何 SKU，请先运行 `jdka ui` 添加。")
                return 1
            for item in service.run_preview():
                print(f"{item['sku_id']}: {item['status']} {item.get('action') or ''} "
                      f"{item.get('reason') or ''} {item.get('message') or ''}".rstrip())
        finally:
            service.shutdown()
        return 0

    if command == "doctor":
        from jdka.browser.runtime import BrowserUnavailable, find_chrome
        from jdka.config import app_dir

        print(f"版本      : {__version__}")
        print(f"数据目录  : {app_dir()}")
        try:
            print(f"Chrome    : {find_chrome()}")
        except BrowserUnavailable as exc:
            print(f"Chrome    : ❌ {exc}")
            return 1
        try:
            import playwright  # noqa: F401
            print("Playwright: 已安装")
        except ImportError:
            print("Playwright: ❌ 未安装，请执行 pip install -e .")
            return 1
        return 0

    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
