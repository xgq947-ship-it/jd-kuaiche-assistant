"""轮换引擎：把纯策略判断落到平台动作上。

安全立场（与自用脚本的关键差异）：

- **出厂默认只暂停不删除**。删除是不可逆的，而本项目的历史已经证明
  「接口回执全绿而现实是错的」这种失效模式在京准通真实存在，所以默认
  不把不可逆动作交给自动化。
- **删除前必留快照**，写入 ``history``，便于人工重建。
- **只碰 AUTO_ 前缀且名称完全匹配**的计划，沿用平台层三重保护。
- **写请求永不自动重试**。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any, Callable

from jdka.config import AppConfig, SkuConfig, state_dir
from jdka.core.naming import build_plan_name
from jdka.core.policy import PLAN_STATUS_ENABLED, PLAN_STATUS_PAUSED, decide
from jdka.core.state import load_state, save_state
from jdka.jd.plan import JdKuaichePlanService
from jdka.jd.report import JdKuaicheReportService, normalize_metrics
from jdka.jd.shared import JdError, PlanNameConflictError, PlanNotFoundError

MAX_NAME_ATTEMPTS = 3


@dataclass
class CycleResult:
    """一个 SKU 一轮的结果，直接喂给 UI。"""

    config_id: str
    sku_id: str
    status: str  # watching | action | skipped | error
    plan_id: str | None = None
    plan_name: str | None = None
    plan_status: int | None = None
    metrics: dict[str, Any] = field(default_factory=dict)
    action: str | None = None
    reason: str | None = None
    message: str | None = None
    error_code: str | None = None
    at: str = field(default_factory=lambda: datetime.now().astimezone().isoformat(timespec="seconds"))

    def to_dict(self) -> dict[str, Any]:
        return {
            "config_id": self.config_id,
            "sku_id": self.sku_id,
            "status": self.status,
            "plan_id": self.plan_id,
            "plan_name": self.plan_name,
            "plan_status": self.plan_status,
            "enabled": self.plan_status == PLAN_STATUS_ENABLED
            if self.plan_status is not None
            else None,
            "metrics": self.metrics,
            "action": self.action,
            "reason": self.reason,
            "message": self.message,
            "error_code": self.error_code,
            "at": self.at,
        }


def _spend_today(persisted: dict[str, Any], metrics: dict[str, Any], business_date: str) -> float:
    total = 0.0
    for entry in persisted.get("history") or []:
        if not isinstance(entry, dict):
            continue
        if not str(entry.get("retired_at") or "").startswith(business_date):
            continue
        final = entry.get("final_metrics")
        if isinstance(final, dict):
            try:
                total += float(final.get("cost") or 0)
            except (TypeError, ValueError):
                pass
    try:
        total += float(metrics.get("cost") or 0)
    except (TypeError, ValueError):
        pass
    return round(total, 2)


class RotationEngine:
    def __init__(
        self,
        plan_service: JdKuaichePlanService,
        report_service: JdKuaicheReportService,
        config: AppConfig,
        *,
        execute: bool = False,
        log: Callable[[str], None] | None = None,
    ) -> None:
        self.plans = plan_service
        self.report = report_service
        self.config = config
        self.execute = execute
        self.log = log or (lambda _msg: None)

    def state_path(self, sku: SkuConfig) -> Path:
        return state_dir() / f"{sku.config_id}.json"

    # ------------------------------------------------------------------

    def run_once(self, sku: SkuConfig) -> CycleResult:
        """跑一个 SKU 的一轮。异常在这里收敛成结果，不向上抛。"""
        base = {"config_id": sku.config_id, "sku_id": sku.sku_id}
        if not sku.enabled:
            return CycleResult(**base, status="skipped", reason="配置未启用")
        try:
            return self._run_once(sku)
        except JdError as exc:
            return CycleResult(
                **base,
                status="error",
                message=str(exc),
                error_code=getattr(exc, "error_code", None),
            )
        except Exception as exc:  # noqa: BLE001 - 单个 SKU 失败不影响其它 SKU
            return CycleResult(
                **base, status="error", message=f"{type(exc).__name__}: {exc}"
            )

    def _run_once(self, sku: SkuConfig) -> CycleResult:
        base = {"config_id": sku.config_id, "sku_id": sku.sku_id}
        path = self.state_path(sku)
        today = date.today()
        persisted = load_state(path, today=today)

        current, metrics = self._resolve_current(persisted)

        policy_state = dict(persisted)
        policy_state["spend_today"] = _spend_today(
            persisted, metrics, today.isoformat()
        )
        decision = decide(
            config=sku.rotation_config(),
            state=policy_state,
            current_plan=current,
            metrics=metrics,
            now=datetime.now().astimezone(),
        )
        action = str(decision.get("action"))
        reason = str(decision.get("reason"))

        if action == "none":
            self._persist_observation(path, persisted, current, metrics)
            return CycleResult(
                **base,
                status="watching",
                plan_id=current["plan_id"] if current else None,
                plan_name=current["plan_name"] if current else None,
                plan_status=current.get("status") if current else None,
                metrics=metrics,
                action="none",
                reason=reason,
            )

        if not self.execute:
            return CycleResult(
                **base,
                status="skipped",
                action=action,
                reason=reason,
                metrics=metrics,
                plan_id=current["plan_id"] if current else None,
                plan_name=current["plan_name"] if current else None,
                message="预览模式：未执行任何写操作",
            )

        if action == "enable":
            return self._do_enable(sku, path, persisted, current, metrics, reason)
        if action in {"create", "rotate"}:
            return self._do_rotate(
                sku, path, persisted, current, metrics, reason, retire=action == "rotate"
            )
        return CycleResult(**base, status="watching", action=action, reason=reason)

    # ------------------------------------------------------------------

    def _resolve_current(
        self, persisted: dict[str, Any]
    ) -> tuple[dict[str, Any] | None, dict[str, Any]]:
        """以平台为准刷新当前计划；平台上已不存在则转为重建。"""
        plan_id = persisted.get("current_plan_id")
        plan_name = persisted.get("current_plan_name")
        if not plan_id or not plan_name:
            return None, {}
        try:
            observed = self.report.orders(plan_id=plan_id)
        except PlanNotFoundError:
            self.log(f"计划 {plan_id} 在平台已不存在，转为重建")
            return None, {}
        return (
            {
                "plan_id": str(observed["plan_id"]),
                "plan_name": str(observed["plan_name"] or plan_name),
                "status": observed.get("status"),
                "created_at": persisted.get("created_at"),
            },
            dict(observed.get("metrics") or {}),
        )

    def _persist_observation(
        self,
        path: Path,
        persisted: dict[str, Any],
        current: dict[str, Any] | None,
        metrics: dict[str, Any],
    ) -> None:
        if current is None:
            return
        persisted["current_plan_id"] = current["plan_id"]
        persisted["current_plan_name"] = current["plan_name"]
        persisted["status"] = current.get("status")
        persisted["spend_today"] = _spend_today(
            persisted, metrics, date.today().isoformat()
        )
        persisted["last_executed_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
        save_state(path, persisted)

    def _do_enable(
        self,
        sku: SkuConfig,
        path: Path,
        persisted: dict[str, Any],
        current: dict[str, Any] | None,
        metrics: dict[str, Any],
        reason: str,
    ) -> CycleResult:
        assert current is not None
        result = self.plans.set_enabled(
            plan_id=current["plan_id"],
            expected_plan_name=current["plan_name"],
            enabled=True,
            execute=True,
        )
        persisted["status"] = PLAN_STATUS_ENABLED if result.get("enabled") else PLAN_STATUS_PAUSED
        persisted["last_executed_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
        save_state(path, persisted)
        return CycleResult(
            config_id=sku.config_id,
            sku_id=sku.sku_id,
            status="action",
            plan_id=current["plan_id"],
            plan_name=current["plan_name"],
            plan_status=persisted["status"],
            metrics=metrics,
            action="enable",
            reason=reason,
            message="已重新启用被暂停的计划",
        )

    def _retire_plan(
        self,
        current: dict[str, Any],
        metrics: dict[str, Any],
        persisted: dict[str, Any],
    ) -> str:
        """让旧计划退场。默认暂停；仅在显式配置 delete 时才永久删除。"""
        snapshot = {
            "plan_id": current["plan_id"],
            "plan_name": current["plan_name"],
            "retired_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "final_metrics": dict(metrics or normalize_metrics({})),
            "mode": self.config.rotate_mode,
        }
        if self.config.rotate_mode == "delete":
            self.plans.delete(
                plan_id=current["plan_id"],
                expected_plan_name=current["plan_name"],
                execute=True,
            )
            note = "已删除旧计划"
        else:
            self.plans.set_enabled(
                plan_id=current["plan_id"],
                expected_plan_name=current["plan_name"],
                enabled=False,
                execute=True,
            )
            note = "已暂停旧计划（未删除，可人工复核）"
        persisted.setdefault("history", []).append(snapshot)
        persisted["rotations_today"] = int(persisted.get("rotations_today") or 0) + 1
        return note

    def _do_rotate(
        self,
        sku: SkuConfig,
        path: Path,
        persisted: dict[str, Any],
        current: dict[str, Any] | None,
        metrics: dict[str, Any],
        reason: str,
        *,
        retire: bool,
    ) -> CycleResult:
        note = ""
        if retire and current is not None:
            note = self._retire_plan(current, metrics, persisted)
            # 旧计划的指标快照先落盘：即便随后创建失败也不丢账。
            for key in ("current_plan_id", "current_plan_name", "status", "created_at"):
                persisted[key] = None
            save_state(path, persisted)

        base_round = int(persisted.get("round") or 0) + 1
        last_error: Exception | None = None
        for offset in range(MAX_NAME_ATTEMPTS):
            candidate_round = base_round + offset
            candidate = build_plan_name(sku.sku_id, candidate_round)
            try:
                created = self.plans.create(
                    sku_id=sku.sku_id,
                    plan_name=candidate,
                    budget=sku.budget,
                    target_cpa=sku.target_cpa,
                    auto_enable=True,
                    execute=True,
                )
            except PlanNameConflictError as exc:
                last_error = exc
                continue
            persisted["current_plan_id"] = str(created["plan_id"])
            persisted["current_plan_name"] = candidate
            persisted["status"] = (
                PLAN_STATUS_ENABLED if created.get("enabled") else PLAN_STATUS_PAUSED
            )
            persisted["created_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
            persisted["round"] = candidate_round
            persisted["last_executed_at"] = persisted["created_at"]
            save_state(path, persisted)
            message = "、".join(x for x in (note, f"已创建 {candidate}") if x)
            if created.get("warning"):
                message += f"（{created['warning']}）"
            return CycleResult(
                config_id=sku.config_id,
                sku_id=sku.sku_id,
                status="action",
                plan_id=str(created["plan_id"]),
                plan_name=candidate,
                plan_status=persisted["status"],
                metrics={},
                action="rotate" if retire else "create",
                reason=reason,
                message=message,
            )
        raise PlanNameConflictError(
            f"连续 {MAX_NAME_ATTEMPTS} 个轮次名称均冲突，已停止创建。"
        ) from last_error
