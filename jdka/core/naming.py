"""Pure naming helpers for tool-managed JD campaigns."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any


_PATTERN = re.compile(r"^AUTO_(?P<sku_suffix>\d{6})_(?P<timestamp>\d{10})_R(?P<round>\d{3})$")


def build_plan_name(sku_id: str | int, round_number: int, *, now: datetime | None = None) -> str:
    sku_text = str(sku_id).strip()
    if not sku_text.isdigit() or len(sku_text) < 6:
        raise ValueError("sku_id 必须是至少 6 位数字。")
    if round_number < 0 or round_number > 999:
        raise ValueError("round_number 必须在 0 到 999 之间。")
    stamp = (now or datetime.now()).strftime("%m%d%H%M%S")
    return f"AUTO_{sku_text[-6:]}_{stamp}_R{round_number:03d}"


def is_tool_managed(name: str) -> bool:
    return _PATTERN.fullmatch(name.strip()) is not None


def parse_plan_name(name: str) -> dict[str, Any] | None:
    match = _PATTERN.fullmatch(name.strip())
    if match is None:
        return None
    return {
        "sku_suffix": match.group("sku_suffix"),
        "timestamp": match.group("timestamp"),
        "round": int(match.group("round")),
    }
