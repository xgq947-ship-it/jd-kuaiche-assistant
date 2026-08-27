"""JSON state persistence for JD campaign rotation; no database dependency."""

from __future__ import annotations

import json
import os
from datetime import date
from pathlib import Path
from typing import Any


STATE_VERSION = 1


def empty_state(*, today: date | None = None) -> dict[str, Any]:
    business_date = (today or date.today()).isoformat()
    return {
        "version": STATE_VERSION,
        "business_date": business_date,
        "current_plan_id": None,
        "current_plan_name": None,
        "status": None,
        "created_at": None,
        "round": 0,
        "rotations_today": 0,
        "spend_today": 0.0,
        "last_executed_at": None,
        "history": [],
    }


def normalize_state(payload: Any, *, today: date | None = None) -> dict[str, Any]:
    state = empty_state(today=today)
    if isinstance(payload, dict):
        for key in state:
            if key in payload:
                state[key] = payload[key]
    if not isinstance(state.get("history"), list):
        state["history"] = []
    current_date = (today or date.today()).isoformat()
    if state.get("business_date") != current_date:
        state["business_date"] = current_date
        state["rotations_today"] = 0
        state["spend_today"] = 0.0
    state["version"] = STATE_VERSION
    return state


def load_state(path: Path, *, today: date | None = None) -> dict[str, Any]:
    if not path.exists():
        return empty_state(today=today)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"京东快车轮换 state 无法读取：{path}") from exc
    return normalize_state(payload, today=today)


def save_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temp.write_text(json.dumps(normalize_state(state), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temp, path)
