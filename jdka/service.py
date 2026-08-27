"""常驻监控服务：在后台线程里按间隔轮询，并把状态暴露给 UI。

生命周期与安全：

- 单实例：同一时刻只有一个轮询线程。
- 急停：``stop()`` 会立刻打断等待，不必等到下一个间隔。
- 登录失效：暂停轮询并把状态标成 ``login_required``，不反复弹窗骚扰用户。
- 浏览器懒启动：只有真正开始轮换时才拉起后台 Chrome。
"""

from __future__ import annotations

import threading
from collections import deque
from datetime import datetime
from typing import Any

from jdka import license as jdka_license
from jdka.browser.runtime import BrowserUnavailable, LocalChromeRuntime
from jdka.config import AppConfig
from jdka.engine import CycleResult, RotationEngine
from jdka.jd.plan import JdKuaichePlanService
from jdka.jd.report import JdKuaicheReportService
from jdka.jd.shared import JD_HOME_URL, JdAuthenticationError

MAX_BACKOFF_SECONDS = 300


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


class MonitorService:
    def __init__(self) -> None:
        self.config = AppConfig.load()
        self._thread: threading.Thread | None = None
        self._wake = threading.Event()
        self._running = False
        self._lock = threading.Lock()
        self._runtime: LocalChromeRuntime | None = None
        self.cycles = 0
        self.failure_streak = 0
        self.login_required = False
        self.last_error: str | None = None
        self.started_at: str | None = None
        self.items: dict[str, dict[str, Any]] = {}
        self.log: deque[dict[str, str]] = deque(maxlen=200)

    # ---------- 状态 ----------

    def _note(self, text: str) -> None:
        self.log.appendleft({"at": _now(), "text": text})

    def status(self) -> dict[str, Any]:
        return {
            "running": self._running,
            "login_required": self.login_required,
            "cycles": self.cycles,
            "failure_streak": self.failure_streak,
            "last_error": self.last_error,
            "started_at": self.started_at,
            "interval_seconds": self.config.poll_interval_seconds,
            "rotate_mode": self.config.rotate_mode,
            "sku_count": len([s for s in self.config.skus if s.enabled]),
            "items": list(self.items.values()),
            "log": list(self.log)[:50],
        }

    # ---------- 浏览器 ----------

    def runtime(self) -> LocalChromeRuntime:
        with self._lock:
            if self._runtime is None:
                self._runtime = LocalChromeRuntime(headless=self.config.headless)
            return self._runtime

    def open_login(self) -> dict[str, Any]:
        """开可见窗口让用户登录。会先停掉轮询，避免与登录窗口抢 profile。"""
        was_running = self._running
        self.stop()
        try:
            self.runtime().open_login(JD_HOME_URL)
        except BrowserUnavailable as exc:
            return {"ok": False, "message": str(exc)}
        self._note("已打开登录窗口，请在浏览器中完成登录后点击「我已登录」")
        return {"ok": True, "was_running": was_running}

    def confirm_login(self) -> dict[str, Any]:
        """用户点「我已登录」后重新校验，成功则清掉 login_required。"""
        with self._lock:
            if self._runtime is not None:
                self._runtime.shutdown()
                self._runtime = None
        try:
            services = self._services()
            services[1].list(page_size=1)
        except JdAuthenticationError as exc:
            self.login_required = True
            return {"ok": False, "message": str(exc)}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "message": f"{type(exc).__name__}: {exc}"}
        self.login_required = False
        self.last_error = None
        self._note("登录态校验通过")
        return {"ok": True}

    def _services(self) -> tuple[JdKuaichePlanService, JdKuaicheReportService]:
        from jdka.jd.transport import JdTransport

        transport = JdTransport(self.runtime())
        return JdKuaichePlanService(transport), JdKuaicheReportService(transport)

    # ---------- 轮询 ----------

    def start(self) -> dict[str, Any]:
        if self._running:
            return {"ok": True, "message": "已在运行"}
        # 纵深防御：HTTP 网关已经拦过一道，这里再独立校验一次。
        # 只在一个地方判断，意味着改掉那一个函数就能全线放行；而这里是真正
        # 开始花钱的入口，值得单独确认。
        if not jdka_license.load_status().licensed:
            return {"ok": False, "message": "尚未激活，无法开始自动轮换"}
        if not [s for s in self.config.skus if s.enabled]:
            return {"ok": False, "message": "请先添加至少一个启用的 SKU"}
        self._running = True
        self._wake.clear()
        self.started_at = _now()
        self.failure_streak = 0
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        self._note(
            f"开始自动轮换，间隔 {self.config.poll_interval_seconds} 秒，"
            f"旧计划处理方式：{'删除' if self.config.rotate_mode == 'delete' else '暂停'}"
        )
        return {"ok": True}

    def stop(self) -> dict[str, Any]:
        if not self._running:
            return {"ok": True, "message": "未在运行"}
        self._running = False
        self._wake.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=15)
        self._thread = None
        self._note("已停止自动轮换")
        return {"ok": True}

    def run_preview(self) -> list[dict[str, Any]]:
        """预览检查：只读，不做任何写操作。"""
        results = self._cycle(execute=False)
        return [r.to_dict() for r in results]

    def _cycle(self, *, execute: bool) -> list[CycleResult]:
        plans, report = self._services()
        engine = RotationEngine(
            plans, report, self.config, execute=execute, log=self._note
        )
        results: list[CycleResult] = []
        for sku in self.config.skus:
            result = engine.run_once(sku)
            results.append(result)
            self.items[sku.config_id] = result.to_dict()
            if result.status == "action" and result.message:
                self._note(f"[{sku.sku_id}] {result.message}")
            if result.error_code == "AUTH_REQUIRED":
                self.login_required = True
        return results

    def _loop(self) -> None:
        while self._running:
            self.cycles += 1
            try:
                results = self._cycle(execute=True)
                failed = [r for r in results if r.status == "error"]
                if failed:
                    self.failure_streak += 1
                    self.last_error = failed[0].message
                else:
                    self.failure_streak = 0
                    self.last_error = None
            except BrowserUnavailable as exc:
                self.failure_streak += 1
                self.last_error = str(exc)
                self._note(f"浏览器不可用：{exc}")
            except Exception as exc:  # noqa: BLE001 - 循环绝不能因单次异常退出
                self.failure_streak += 1
                self.last_error = f"{type(exc).__name__}: {exc}"

            if self.login_required:
                self._note("登录态失效，已暂停自动轮换，请重新登录")
                self._running = False
                break

            delay = self.config.poll_interval_seconds
            if self.failure_streak:
                delay = min(MAX_BACKOFF_SECONDS, delay * (2 ** min(self.failure_streak, 6)))
            self._wake.wait(delay)
            self._wake.clear()

    def shutdown(self) -> None:
        self.stop()
        with self._lock:
            if self._runtime is not None:
                self._runtime.shutdown()
                self._runtime = None
