"""JD Kuaiche campaign listing and order metric normalization."""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from typing import Any, Protocol

from jdka.jd.shared import (
    CAMPAIGN_LIST_ENDPOINT,
    PLAN_STATUS_ENABLED,
    JdPlatformRequestError,
    PlanNotFoundError,
    envelope_data,
)


CUSTOM_COLUMNS = [
    "campaignName",
    "campaignId",
    "status",
    "dayBudgetStr",
    "time",
    "timeRange",
    "impressions",
    "clicks",
    "CTR",
    "cost",
    "CPM",
    "CPC",
    "directOrderCnt",
    "directOrderSum",
    "indirectOrderCnt",
    "indirectOrderSum",
    "totalOrderCnt",
    "totalOrderSum",
    "totalPresaleOrderCnt",
    "totalPresaleOrderSum",
    "totalCartCnt",
    "totalOrderCVS",
    "CPA",
    "totalOrderROI",
]


class _Transport(Protocol):
    def post(self, endpoint: str, body: dict[str, Any], *, write: bool = False) -> dict[str, Any]: ...


def _iso_date(value: str | None, *, default: date) -> date:
    if not value:
        return default
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"日期格式错误：{value}，应为 YYYY-MM-DD") from exc


def resolve_date_range(start: str | None, end: str | None) -> tuple[date, date]:
    end_date = _iso_date(end, default=date.today())
    start_date = _iso_date(start, default=end_date - timedelta(days=14))
    if start_date > end_date:
        raise ValueError("start 不能晚于 end。")
    return start_date, end_date


def build_campaign_list_body(
    *,
    start: date,
    end: date,
    page: int = 1,
    page_size: int = 10,
    status: str = "",
) -> dict[str, Any]:
    if page < 1:
        raise ValueError("page 必须大于等于 1。")
    if page_size < 1 or page_size > 100:
        raise ValueError("page-size 必须在 1 到 100 之间。")
    return {
        "page": page,
        "pageSize": page_size,
        "status": status,
        "filters": [],
        "obys": "",
        "startDay": start.isoformat(),
        "endDay": end.isoformat(),
        "clickOrOrderCaliber": 0,
        "clickOrOrderDay": 15,
        "giftFlag": 0,
        "orderStatusCategory": 1,
        "customColumns": list(CUSTOM_COLUMNS),
        "requestFrom": 0,
    }

def _int_value(value: Any) -> int:
    if value in (None, ""):
        return 0
    try:
        return int(float(str(value).replace(",", "")))
    except (TypeError, ValueError):
        return 0


def _float_value(value: Any) -> float:
    if value in (None, ""):
        return 0.0
    if isinstance(value, dict):
        value = value.get("value")
    try:
        return float(str(value).replace(",", "").replace("%", ""))
    except (TypeError, ValueError):
        return 0.0


def normalize_metrics(row: dict[str, Any]) -> dict[str, int | float]:
    return {
        "cost": _float_value(row.get("cost")),
        "impressions": _int_value(row.get("impressions")),
        "clicks": _int_value(row.get("clicks")),
        "ctr": _float_value(row.get("CTR")),
        "cpc": _float_value(row.get("CPC")),
        "cpm": _float_value(row.get("CPM")),
        "direct_order_cnt": _int_value(row.get("directOrderCnt")),
        "direct_order_sum": _float_value(row.get("directOrderSum")),
        "indirect_order_cnt": _int_value(row.get("indirectOrderCnt")),
        "indirect_order_sum": _float_value(row.get("indirectOrderSum")),
        "total_order_cnt": _int_value(row.get("totalOrderCnt")),
        "total_order_sum": _float_value(row.get("totalOrderSum")),
        "total_presale_order_cnt": _int_value(row.get("totalPresaleOrderCnt")),
        "cpa": _float_value(row.get("CPA")),
        "total_order_roi": _float_value(row.get("totalOrderROI")),
    }


def _first_date(value: Any) -> str | None:
    if value is None:
        return None
    match = re.search(r"\d{4}-\d{2}-\d{2}", str(value))
    if match:
        return match.group(0)
    return None


def infer_created_date(row: dict[str, Any], *, today: date | None = None) -> str | None:
    for key in ("startTime", "startDay", "createTime", "createdAt", "time", "timeRange"):
        found = _first_date(row.get(key))
        if found:
            return found

    name = str(row.get("campaignName") or "")
    match = re.fullmatch(r"AUTO_\d{6}_(\d{2})(\d{2})\d{6}_R\d{3}", name)
    if not match:
        return None
    reference = today or date.today()
    try:
        candidate = date(reference.year, int(match.group(1)), int(match.group(2)))
    except ValueError:
        return None
    if candidate > reference + timedelta(days=1):
        candidate = date(reference.year - 1, candidate.month, candidate.day)
    return candidate.isoformat()


def normalize_plan(row: Any) -> dict[str, Any] | None:
    if not isinstance(row, dict):
        return None
    campaign_id = row.get("campaignId")
    if campaign_id in (None, ""):
        return None
    status = _int_value(row.get("status"))
    metrics = normalize_metrics(row)
    return {
        "plan_id": str(campaign_id),
        "plan_name": str(row.get("campaignName") or ""),
        "status": status,
        "enabled": status == PLAN_STATUS_ENABLED,
        "budget": _float_value(row.get("budget", row.get("dayBudgetStr"))),
        "created_date": infer_created_date(row),
        "metrics": metrics,
        "has_order": int(metrics["total_order_cnt"]) >= 1,
    }


def _rows_and_paginator(data: Any) -> tuple[list[dict[str, Any]], dict[str, Any], dict[str, Any]]:
    if not isinstance(data, dict):
        raise JdPlatformRequestError("京准通计划列表缺少 data 对象。")
    raw_rows = data.get("data")
    rows = [row for row in raw_rows if isinstance(row, dict)] if isinstance(raw_rows, list) else []
    paginator = data.get("paginator") if isinstance(data.get("paginator"), dict) else {}
    summary = data.get("ext") if isinstance(data.get("ext"), dict) else {}
    return rows, paginator, summary


def _paginator_total(paginator: dict[str, Any]) -> int | None:
    for key in ("items", "total", "totalCount", "records"):
        value = paginator.get(key)
        if isinstance(value, (int, float, str)) and str(value).strip():
            try:
                return int(value)
            except (TypeError, ValueError):
                continue
    return None


class JdKuaicheReportService:
    def __init__(self, transport: _Transport) -> None:
        self.transport = transport

    def _query_page(
        self,
        *,
        start: date,
        end: date,
        page: int,
        page_size: int,
        status: str = "",
    ) -> tuple[list[dict[str, Any]], dict[str, Any], dict[str, Any], dict[str, Any]]:
        body = build_campaign_list_body(start=start, end=end, page=page, page_size=page_size, status=status)
        envelope = self.transport.post(CAMPAIGN_LIST_ENDPOINT, body)
        data = envelope_data(envelope, CAMPAIGN_LIST_ENDPOINT)
        rows, paginator, summary = _rows_and_paginator(data)
        return rows, paginator, summary, envelope

    def list(
        self,
        *,
        start: str | None = None,
        end: str | None = None,
        page: int = 1,
        page_size: int = 10,
        status: str = "",
        name_prefix: str | None = None,
        plan_id: str | int | None = None,
    ) -> dict[str, Any]:
        start_date, end_date = resolve_date_range(start, end)
        wanted_id = str(plan_id) if plan_id is not None else None
        prefix = (name_prefix or "").strip()
        if not wanted_id and not prefix:
            rows, paginator, summary, _ = self._query_page(
                start=start_date,
                end=end_date,
                page=page,
                page_size=page_size,
                status=status,
            )
            items = [item for row in rows if (item := normalize_plan(row)) is not None]
            return {
                "query": {
                    "start": start_date.isoformat(),
                    "end": end_date.isoformat(),
                    "page": page,
                    "page_size": page_size,
                    "status": status,
                },
                "items": items,
                "paginator": paginator,
                "summary": normalize_metrics(summary),
                "total": _paginator_total(paginator) if _paginator_total(paginator) is not None else len(items),
            }

        matched: list[dict[str, Any]] = []
        scan_page = 1
        scan_size = 100
        while scan_page <= 100:
            rows, paginator, _summary, _ = self._query_page(
                start=start_date,
                end=end_date,
                page=scan_page,
                page_size=scan_size,
                status=status,
            )
            for row in rows:
                item = normalize_plan(row)
                if item is None:
                    continue
                if wanted_id and item["plan_id"] != wanted_id:
                    continue
                if prefix and not item["plan_name"].startswith(prefix):
                    continue
                matched.append(item)
            total = _paginator_total(paginator)
            if not rows or len(rows) < scan_size or (total is not None and scan_page * scan_size >= total):
                break
            scan_page += 1
        return {
            "query": {
                "start": start_date.isoformat(),
                "end": end_date.isoformat(),
                "name_prefix": prefix or None,
                "plan_id": wanted_id,
                "status": status,
            },
            "items": matched,
            "total": len(matched),
            "paginator": {"scanned_pages": scan_page},
            "summary": {},
        }

    def find_plan(
        self,
        plan_id: str | int,
        *,
        start: str | None = None,
        end: str | None = None,
    ) -> dict[str, Any] | None:
        result = self.list(start=start, end=end, plan_id=plan_id)
        return result["items"][0] if result["items"] else None

    def find_plan_by_name(self, plan_name: str) -> dict[str, Any] | None:
        result = self.list(name_prefix=plan_name)
        return next((item for item in result["items"] if item["plan_name"] == plan_name), None)

    def orders(
        self,
        *,
        plan_id: str | int,
        start: str | None = None,
        end: str | None = None,
    ) -> dict[str, Any]:
        start_date, end_date = resolve_date_range(start, end)
        item = self.find_plan(plan_id, start=start_date.isoformat(), end=end_date.isoformat())
        if item is None:
            raise PlanNotFoundError(f"未找到京准通计划 {plan_id}。", {"plan_id": str(plan_id)})

        inferred_start = item.get("created_date")
        if start is None and inferred_start and inferred_start != start_date.isoformat():
            refreshed = self.find_plan(plan_id, start=inferred_start, end=end_date.isoformat())
            if refreshed is not None:
                item = refreshed
                start_date = date.fromisoformat(inferred_start)

        metrics = dict(item["metrics"])
        return {
            "plan_id": item["plan_id"],
            "plan_name": item["plan_name"],
            "status": item["status"],
            "enabled": item["enabled"],
            "date_range": {"start": start_date.isoformat(), "end": end_date.isoformat()},
            "caliber": {"clickOrOrderCaliber": 0, "clickOrOrderDay": 15},
            "metrics": metrics,
            "has_order": int(metrics.get("total_order_cnt") or 0) >= 1,
            "order_criteria": "total_order_cnt>=1",
            "attribution_note": "点击口径，15天归因窗口，存在回填延迟",
        }


def simulated_list_data() -> dict[str, Any]:
    today = date.today().isoformat()
    return {
        "query": {"start": today, "end": today},
        "items": [],
        "total": 0,
        "summary": {},
        "simulated": True,
        "dry_run": True,
        "source": "simulation",
    }


def simulated_orders_data(plan_id: str | int) -> dict[str, Any]:
    today = date.today().isoformat()
    metrics = normalize_metrics({})
    return {
        "plan_id": str(plan_id),
        "plan_name": None,
        "status": 0,
        "enabled": False,
        "date_range": {"start": today, "end": today},
        "caliber": {"clickOrOrderCaliber": 0, "clickOrOrderDay": 15},
        "metrics": metrics,
        "has_order": False,
        "order_criteria": "total_order_cnt>=1",
        "attribution_note": "点击口径，15天归因窗口，存在回填延迟",
        "simulated": True,
        "dry_run": True,
        "source": "simulation",
    }
