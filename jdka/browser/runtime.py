"""后台静默浏览器运行时。

设计原则对齐 ai-browser-hub，但不依赖 Node：

- **独立 profile**：绝不读取、连接或关闭用户日常 Chrome 的 Profile。
- **随机端口**：不硬编码 9222/19222，避免与用户其它工具抢端口。
- **后台无头**：常驻任务默认 headless；只有登录时才开一个可见窗口，
  且该窗口不带任何自动化参数（降低被平台识别的概率）。
- **pageKey 标签复用**：按 targetId 记录归属，不依赖 ``window.name``
  —— 京准通 SPA 会异步清空 window.name，靠它认领标签必然失效。
- **有界回收**：页面到期或用完即回收，绝不无限增长标签。

后期若切换到 ai-browser-hub，只需另写一个满足 :class:`BrowserRuntime`
协议的实现，上层业务代码不用改。
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from jdka.config import app_dir

# 页面存活上限：超过后在任务边界重建，避免 SPA 长期运行内存泄漏或状态漂移。
PAGE_MAX_TASKS = 200
PAGE_MAX_AGE_SECONDS = 2 * 60 * 60
CHROME_LAUNCH_TIMEOUT = 60


class BrowserUnavailable(RuntimeError):
    """Chrome 不可用（未安装、启动失败或端口连不上）。"""


class LoginRequired(RuntimeError):
    """需要用户在可见窗口中完成登录。"""


class BrowserRuntime(Protocol):
    def acquire(self, page_key: str, url: str) -> Any: ...
    def open_login(self, url: str) -> None: ...
    def shutdown(self) -> None: ...


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def find_chrome() -> str:
    """定位官方稳定版 Chrome。绝不使用用户日常 Profile，只借用可执行文件。"""
    env = os.environ.get("JDKA_CHROME_PATH")
    if env and Path(env).exists():
        return env
    if sys.platform == "darwin":
        candidates = [
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            str(Path.home() / "Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
        ]
    elif sys.platform.startswith("win"):
        candidates = [
            r"C:\Program Files\Google\Chrome\Application\chrome.exe",
            r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        ]
    else:
        candidates = ["/usr/bin/google-chrome", "/usr/bin/chromium"]
    for path in candidates:
        if Path(path).exists():
            return path
    found = shutil.which("google-chrome") or shutil.which("chrome")
    if found:
        return found
    raise BrowserUnavailable(
        "未找到 Google Chrome。请先安装官方稳定版 Chrome 后重试。"
    )


@dataclass
class _ManagedPage:
    page: Any
    created_at: float
    tasks: int = 0

    def expired(self) -> bool:
        return (
            self.tasks >= PAGE_MAX_TASKS
            or (time.time() - self.created_at) >= PAGE_MAX_AGE_SECONDS
            or _closed(self.page)
        )


def _closed(page: Any) -> bool:
    try:
        return bool(page.is_closed())
    except Exception:
        return True


class LocalChromeRuntime:
    """自带的后台浏览器运行时（单进程内使用，非多 App 共享）。"""

    def __init__(self, *, headless: bool = True, profile_dir: Path | None = None) -> None:
        self.headless = headless
        self.profile_dir = profile_dir or (app_dir() / "profile-v1")
        self.state_path = app_dir() / "browser-state.json"
        self._proc: subprocess.Popen[bytes] | None = None
        self._port: int | None = None
        self._pw: Any = None
        self._browser: Any = None
        self._pages: dict[str, _ManagedPage] = {}
        self._login_proc: subprocess.Popen[bytes] | None = None

    # ---------- Chrome 进程 ----------

    def _endpoint_alive(self, port: int) -> bool:
        import urllib.request

        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/json/version", timeout=2
            ) as resp:
                return resp.status == 200
        except Exception:
            return False

    def _reuse_existing(self) -> int | None:
        """复用本应用上次启动、仍然存活的 Chrome，避免重复拉起。"""
        try:
            saved = json.loads(self.state_path.read_text(encoding="utf-8"))
        except Exception:
            return None
        port, pid = saved.get("port"), saved.get("pid")
        if not port or not pid:
            return None
        try:
            os.kill(int(pid), 0)
        except OSError:
            return None
        return int(port) if self._endpoint_alive(int(port)) else None

    def _launch(self) -> int:
        existing = self._reuse_existing()
        if existing:
            self._port = existing
            return existing

        chrome = find_chrome()
        port = _free_port()
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        args = [
            chrome,
            f"--remote-debugging-port={port}",
            f"--user-data-dir={self.profile_dir}",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-background-timer-throttling",
            "--disable-renderer-backgrounding",
            "--disable-backgrounding-occluded-windows",
        ]
        if self.headless:
            # 新版 headless 与有头共用同一套渲染/JS 环境，是签名链最可能通过的模式。
            args.append("--headless=new")
        self._proc = subprocess.Popen(
            args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
        deadline = time.time() + CHROME_LAUNCH_TIMEOUT
        while time.time() < deadline:
            if self._endpoint_alive(port):
                self._port = port
                self.state_path.parent.mkdir(parents=True, exist_ok=True)
                self.state_path.write_text(
                    json.dumps({"port": port, "pid": self._proc.pid}), encoding="utf-8"
                )
                return port
            if self._proc.poll() is not None:
                raise BrowserUnavailable("Chrome 启动后立即退出，请检查是否被安全软件拦截。")
            time.sleep(0.3)
        raise BrowserUnavailable("Chrome 调试端口在超时时间内未就绪。")

    def _connect(self) -> Any:
        if self._browser is not None:
            try:
                if self._browser.is_connected():
                    return self._browser
            except Exception:
                pass
            self._browser = None
            self._pages.clear()
        from playwright.sync_api import sync_playwright

        port = self._launch()
        if self._pw is None:
            self._pw = sync_playwright().start()
        try:
            self._browser = self._pw.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
        except Exception as exc:
            # 端口记录可能已过期，清掉后让下次重新拉起。
            self.state_path.unlink(missing_ok=True)
            raise BrowserUnavailable(f"无法连接后台 Chrome：{exc}") from exc
        return self._browser

    # ---------- 页面租用 ----------

    def acquire(self, page_key: str, url: str) -> Any:
        """取得该 pageKey 的常驻页面；到期或不存在才新建，绝不每次开新标签。"""
        browser = self._connect()
        managed = self._pages.get(page_key)
        if managed is not None and managed.expired():
            self._retire(page_key)
            managed = None
        if managed is None:
            context = (
                browser.contexts[0]
                if browser.contexts
                else browser.new_context()
            )
            page = context.new_page()
            page.set_default_timeout(30_000)
            page.goto(url, wait_until="domcontentloaded", timeout=45_000)
            managed = _ManagedPage(page=page, created_at=time.time())
            self._pages[page_key] = managed
        managed.tasks += 1
        return managed.page

    def _retire(self, page_key: str) -> None:
        managed = self._pages.pop(page_key, None)
        if managed is None:
            return
        try:
            if not _closed(managed.page):
                managed.page.close()
        except Exception:
            pass

    # ---------- 可见登录 ----------

    def open_login(self, url: str) -> None:
        """开一个**不带任何自动化参数**的普通可见 Chrome 供用户登录。

        与后台实例共用同一个 profile，因此登录一次即可。为避免 Chrome
        单例冲突，登录期间后台无头实例会先关闭。
        """
        self.shutdown()
        chrome = find_chrome()
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        self._login_proc = subprocess.Popen(
            [
                chrome,
                f"--user-data-dir={self.profile_dir}",
                "--no-first-run",
                "--no-default-browser-check",
                url,
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    def login_window_open(self) -> bool:
        return self._login_proc is not None and self._login_proc.poll() is None

    # ---------- 生命周期 ----------

    def shutdown(self) -> None:
        for key in list(self._pages):
            self._retire(key)
        try:
            if self._browser is not None:
                self._browser.close()
        except Exception:
            pass
        self._browser = None
        try:
            if self._pw is not None:
                self._pw.stop()
        except Exception:
            pass
        self._pw = None
        if self._proc is not None and self._proc.poll() is None:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=10)
            except Exception:
                self._proc.kill()
        self._proc = None
        self._port = None
        self.state_path.unlink(missing_ok=True)
