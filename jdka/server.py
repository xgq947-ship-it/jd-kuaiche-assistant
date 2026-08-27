"""本地控制面：仅监听 127.0.0.1，带随机访问令牌。

前后端通过一组小 JSON 接口交互，后续用 Tauri 套壳时可以直接复用同一套
接口，不必重写业务层。
"""

from __future__ import annotations

import json
import secrets
import threading
import webbrowser
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from jdka import __version__
from jdka import license as jdka_license
from jdka.config import AppConfig, SkuConfig, app_dir
from jdka.service import MonitorService
from jdka.update import REPO as UPDATE_REPO
from jdka.update import check as check_update
from jdka.update import list_releases

UI_DIR = Path(__file__).parent / "ui"

# 桌面外壳里页面源是 tauri://localhost（Windows 为 http://tauri.localhost），
# 访问 127.0.0.1 属跨域，必须显式放行；只认这两个固定源，不用通配符。
# 真正的访问控制仍然是每个请求都要带令牌。
ALLOWED_ORIGINS = frozenset(
    {"tauri://localhost", "http://tauri.localhost", "https://tauri.localhost"}
)


class _Handler(BaseHTTPRequestHandler):
    service: MonitorService
    token: str

    def log_message(self, *args: Any) -> None:  # noqa: D102 - 静默访问日志
        pass

    # ---------- 基础 ----------

    def _cors(self) -> None:
        origin = self.headers.get("Origin", "")
        if origin in ALLOWED_ORIGINS:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")

    def _send(self, payload: Any, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self._cors()
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self) -> None:  # noqa: N802 - 预检请求
        origin = self.headers.get("Origin", "")
        if origin not in ALLOWED_ORIGINS:
            self.send_response(403)
            self.end_headers()
            return
        self.send_response(204)
        self._cors()
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Token")
        self.send_header("Access-Control-Max-Age", "600")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _authorized(self, query: dict[str, list[str]]) -> bool:
        supplied = (query.get("token") or [""])[0] or self.headers.get("X-Token", "")
        return secrets.compare_digest(supplied, self.token)

    def _licensed(self) -> bool:
        """授权闸门放在服务端：藏掉界面不算保护，接口本身必须拒绝。"""
        status = jdka_license.load_status()
        if status.licensed:
            return True
        self._send(
            {"error": "license_required", "message": status.reason, **status.to_dict()},
            402,
        )
        return False

    def _body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return {}

    # ---------- 路由 ----------

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)

        if parsed.path == "/":
            html = (UI_DIR / "index.html").read_text(encoding="utf-8")
            html = html.replace("__TOKEN__", self.token).replace("__VERSION__", __version__)
            body = html.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if not self._authorized(query):
            self._send({"error": "unauthorized"}, 403)
            return

        # 授权状态本身必须能在未激活时读到，否则激活页无从显示。
        if parsed.path == "/api/license":
            self._send(jdka_license.load_status().to_dict())
            return
        if parsed.path == "/api/about":
            self._send(
                {
                    "version": __version__,
                    "repository": f"https://github.com/{UPDATE_REPO}",
                    "data_dir": str(app_dir()),
                }
            )
            return

        if not self._licensed():
            return

        if parsed.path == "/api/status":
            payload = self.service.status()
            payload["version"] = __version__
            self._send(payload)
        elif parsed.path == "/api/config":
            cfg = self.service.config
            self._send(asdict(cfg))
        elif parsed.path == "/api/update":
            self._send(check_update().to_dict())
        elif parsed.path == "/api/releases":
            self._send({"releases": list_releases()})
        else:
            self._send({"error": "not found"}, 404)

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        if not self._authorized(query):
            self._send({"error": "unauthorized"}, 403)
            return
        body = self._body()
        service = self.service

        # 激活是未授权状态下唯一允许的写操作。
        if parsed.path == "/api/license/activate":
            status = jdka_license.activate(str(body.get("key") or ""))
            self._send(
                {"ok": status.licensed, "message": status.reason, **status.to_dict()}
            )
            return

        if not self._licensed():
            return

        if parsed.path == "/api/start":
            self._send(service.start())
        elif parsed.path == "/api/stop":
            self._send(service.stop())
        elif parsed.path == "/api/preview":
            self._send({"ok": True, "results": service.run_preview()})
        elif parsed.path == "/api/login":
            self._send(service.open_login())
        elif parsed.path == "/api/login/confirm":
            self._send(service.confirm_login())
        elif parsed.path == "/api/sku/add":
            self._send(self._add_sku(body))
        elif parsed.path == "/api/sku/remove":
            self._send(self._remove_sku(body))
        elif parsed.path == "/api/settings":
            self._send(self._save_settings(body))
        else:
            self._send({"error": "not found"}, 404)

    # ---------- 操作 ----------

    def _add_sku(self, body: dict[str, Any]) -> dict[str, Any]:
        sku_id = str(body.get("sku_id") or "").strip()
        if not sku_id.isdigit() or len(sku_id) < 6:
            return {"ok": False, "message": "SKU ID 必须是至少 6 位数字"}
        cfg = self.service.config
        if any(s.sku_id == sku_id for s in cfg.skus):
            return {"ok": False, "message": "该 SKU 已存在"}
        try:
            sku = SkuConfig(
                sku_id=sku_id,
                budget=float(body.get("budget") or 50),
                target_cpa=str(body.get("target_cpa") or "50"),
                order_threshold=int(body.get("order_threshold") or 1),
                min_observe_minutes=float(body.get("min_observe_minutes") or 30),
                max_rotations_per_day=int(body.get("max_rotations_per_day") or 3),
                max_daily_spend=float(body.get("max_daily_spend") or 500),
            )
        except (TypeError, ValueError):
            return {"ok": False, "message": "参数格式不正确"}
        if sku.budget <= 0 or float(sku.target_cpa) <= 0:
            return {"ok": False, "message": "日预算和目标成交成本必须大于 0"}
        cfg.skus.append(sku)
        cfg.save()
        return {"ok": True}

    def _remove_sku(self, body: dict[str, Any]) -> dict[str, Any]:
        sku_id = str(body.get("sku_id") or "")
        cfg = self.service.config
        before = len(cfg.skus)
        cfg.skus = [s for s in cfg.skus if s.sku_id != sku_id]
        if len(cfg.skus) == before:
            return {"ok": False, "message": "未找到该 SKU"}
        cfg.save()
        self.service.items.pop(f"JD_KC_{sku_id}", None)
        return {"ok": True}

    def _save_settings(self, body: dict[str, Any]) -> dict[str, Any]:
        cfg = self.service.config
        if "poll_interval_seconds" in body:
            try:
                cfg.poll_interval_seconds = max(10, min(3600, int(body["poll_interval_seconds"])))
            except (TypeError, ValueError):
                return {"ok": False, "message": "轮询间隔必须是整数"}
        if "rotate_mode" in body:
            mode = str(body["rotate_mode"])
            if mode not in {"pause", "delete"}:
                return {"ok": False, "message": "轮换方式只能是 pause 或 delete"}
            cfg.rotate_mode = mode
        if "headless" in body:
            cfg.headless = bool(body["headless"])
        cfg.save()
        return {"ok": True, "config": asdict(cfg)}


def serve(
    *,
    port: int = 0,
    open_browser: bool = True,
    emit_endpoint: bool = False,
) -> None:
    """启动本地控制面。

    ``emit_endpoint`` 供桌面外壳使用：在 stdout 打一行 JSON 告知实际端口与
    访问令牌，外壳读到后才认为后端就绪。端口固定为 0（随机）时尤其必要。
    """
    service = MonitorService()
    token = secrets.token_urlsafe(24)
    handler = type("Handler", (_Handler,), {"service": service, "token": token})
    server = ThreadingHTTPServer(("127.0.0.1", port), handler)
    actual = server.server_address[1]
    url = f"http://127.0.0.1:{actual}/?token={token}"
    if emit_endpoint:
        # 单独一行、带前缀，避免与其它输出混淆。
        print(
            "JDKA_ENDPOINT "
            + json.dumps({"port": actual, "token": token, "version": __version__}),
            flush=True,
        )
    else:
        print(f"京东快车轮换助手 v{__version__}")
        print(f"控制面板：{url}")
    if open_browser:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n正在停止…")
    finally:
        service.shutdown()
        server.shutdown()
