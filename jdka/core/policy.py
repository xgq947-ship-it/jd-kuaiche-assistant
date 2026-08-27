"""Pure decision policy for one hourly JD rotation tick."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any


# 京准通计划状态取值与直觉相反：2 是启用，1 是暂停（平台实测结论）。
PLAN_STATUS_ENABLED = 2
PLAN_STATUS_PAUSED = 1


def _as_float(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _as_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _parse_datetime(value: Any) -> datetime | None:
    if not value:
        return None
    text = str(value).strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def decide(
    *,
    config: dict[str, Any],
    state: dict[str, Any],
    current_plan: dict[str, Any] | None,
    metrics: dict[str, Any] | None,
    now: datetime | None = None,
) -> dict[str, Any]:
    current_time = now or datetime.now(timezone.utc)
    if current_time.tzinfo is None:
        current_time = current_time.replace(tzinfo=timezone.utc)
    spend_today = _as_float(state.get("spend_today"))
    rotations_today = _as_int(state.get("rotations_today"))
    max_daily_spend = _as_float(config.get("max_daily_spend"))
    max_rotations = _as_int(config.get("max_rotations_per_day"))

    if max_daily_spend > 0 and spend_today >= max_daily_spend:
        return {"action": "none", "reason": "daily_spend_capped", "should_notify": True}
    if max_rotations > 0 and rotations_today >= max_rotations:
        return {"action": "none", "reason": "rotation_capped", "should_notify": True}
    if current_plan is None:
        return {"action": "create", "reason": "no_current_plan", "should_notify": False}

    status = _as_int(current_plan.get("status"))
    if status == PLAN_STATUS_PAUSED:
        return {"action": "enable", "reason": "current_plan_paused", "should_notify": True}

    created_at = _parse_datetime(current_plan.get("created_at"))
    if created_at is None:
        created_at = current_time
    elapsed_minutes = max(0.0, (current_time - created_at.astimezone(current_time.tzinfo)).total_seconds() / 60)
    min_observe = _as_float(config.get("min_observe_minutes"))
    if elapsed_minutes < min_observe:
        return {
            "action": "none",
            "reason": "observing",
            "should_notify": False,
            "observed_minutes": round(elapsed_minutes, 2),
            "required_minutes": min_observe,
        }

    field = str((config.get("order_criteria") or {}).get("field") or "total_order_cnt")
    threshold = _as_float((config.get("order_criteria") or {}).get("threshold") or 1)
    observed = _as_float((metrics or {}).get(field))
    if observed >= threshold:
        return {
            "action": "rotate",
            "reason": "order_threshold_reached",
            "should_notify": True,
            "field": field,
            "observed": observed,
            "threshold": threshold,
        }
    return {"action": "none", "reason": "no_order", "should_notify": False}
