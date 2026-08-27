"""应用数据目录与配置。零配置：所有路径从系统标准位置推导。"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass, asdict, field
from pathlib import Path
from typing import Any

APP_NAME = "JD Kuaiche Assistant"
APP_ID = "com.sankaiai.jd-kuaiche-assistant"


def app_dir() -> Path:
    """用户级应用数据目录（跨平台）。可用 JDKA_HOME 覆盖，便于测试。"""
    override = os.environ.get("JDKA_HOME")
    if override:
        path = Path(override).expanduser()
    elif sys.platform == "darwin":
        path = Path.home() / "Library/Application Support/SankaiAI" / APP_NAME
    elif sys.platform.startswith("win"):
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData/Local")
        path = Path(base) / "SankaiAI" / APP_NAME
    else:
        path = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local/share") / "jdka"
    path.mkdir(parents=True, exist_ok=True)
    return path


def state_dir() -> Path:
    path = app_dir() / "state"
    path.mkdir(parents=True, exist_ok=True)
    return path


def config_path() -> Path:
    return app_dir() / "config.json"


@dataclass
class SkuConfig:
    """一个 SKU 的轮换配置。"""

    sku_id: str
    budget: float = 50.0
    target_cpa: str = "50"
    enabled: bool = True
    order_threshold: int = 1
    min_observe_minutes: float = 30.0
    max_rotations_per_day: int = 3
    max_daily_spend: float = 500.0

    @property
    def config_id(self) -> str:
        return f"JD_KC_{self.sku_id}"

    def rotation_config(self) -> dict[str, Any]:
        return {
            "sku_id": self.sku_id,
            "budget": self.budget,
            "target_cpa": self.target_cpa,
            "order_criteria": {
                "field": "total_order_cnt",
                "threshold": self.order_threshold,
            },
            "min_observe_minutes": self.min_observe_minutes,
            "max_rotations_per_day": self.max_rotations_per_day,
            "max_daily_spend": self.max_daily_spend,
        }


@dataclass
class AppConfig:
    """全局配置。

    ``rotate_mode`` 决定出单后如何处理旧计划：

    - ``pause``（**出厂默认**）：只暂停，不删除。可人工复核，可恢复。
    - ``delete``：永久删除，不可撤销。必须用户显式打开。
    """

    skus: list[SkuConfig] = field(default_factory=list)
    poll_interval_seconds: int = 20
    rotate_mode: str = "pause"
    auto_rotate: bool = False
    headless: bool = True
    check_updates: bool = True

    @classmethod
    def load(cls) -> "AppConfig":
        path = config_path()
        if not path.exists():
            return cls()
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return cls()
        skus = [SkuConfig(**s) for s in raw.get("skus", []) if isinstance(s, dict)]
        known = {f.name for f in cls.__dataclass_fields__.values()}  # type: ignore[attr-defined]
        kwargs = {k: v for k, v in raw.items() if k in known and k != "skus"}
        cfg = cls(skus=skus, **kwargs)
        if cfg.rotate_mode not in {"pause", "delete"}:
            cfg.rotate_mode = "pause"
        cfg.poll_interval_seconds = max(10, min(3600, int(cfg.poll_interval_seconds)))
        return cfg

    def save(self) -> None:
        path = config_path()
        payload = asdict(self)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, path)
