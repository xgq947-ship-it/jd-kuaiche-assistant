"""JD Kuaiche campaign creation, status changes, and guarded deletion."""

from __future__ import annotations

import copy
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any, Protocol

from jdka.jd.report import JdKuaicheReportService, normalize_metrics
from jdka.jd.shared import (
    BIDDING_SUGGEST_ENDPOINT,
    BUDGET_SUGGEST_ENDPOINT,
    CAMPAIGN_CREATE_ENDPOINT,
    CAMPAIGN_DELETE_ENDPOINT,
    CAMPAIGN_STATUS_ENDPOINT,
    CID_RECOMMEND_ENDPOINT,
    KEYWORD_RECOMMEND_ENDPOINT,
    PLAN_OPERATE_ENABLE,
    PLAN_OPERATE_PAUSE,
    PLAN_STATUS_ENABLED,
    PLAN_STATUS_PAUSED,
    SAME_SPU_ENDPOINT,
    SKU_INFO_ENDPOINT,
    JdAuthenticationError,
    JdPrepareFailedError,
    JdSkuNotFoundError,
    JdWriteVerificationError,
    PlanCreateFailedError,
    PlanEnableFailedError,
    PlanNameConflictError,
    PlanNameMismatchError,
    PlanNotFoundError,
    PlanNotToolManagedError,
    envelope_data,
    response_login_pin,
)


class _Transport(Protocol):
    def post(self, endpoint: str, body: dict[str, Any], *, write: bool = False) -> dict[str, Any]: ...


def _positive_identifier(value: str | int, option_name: str) -> int:
    try:
        parsed = int(str(value).strip())
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{option_name} 必须是正整数。") from exc
    if parsed <= 0:
        raise ValueError(f"{option_name} 必须是正整数。")
    return parsed


def _positive_decimal(value: str | int | float, option_name: str) -> Decimal:
    try:
        parsed = Decimal(str(value).strip())
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{option_name} 必须是正数。") from exc
    if not parsed.is_finite() or parsed <= 0:
        raise ValueError(f"{option_name} 必须是正数。")
    return parsed


def _decimal_text(value: Decimal) -> str:
    text = format(value, "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def _tool_name(value: str) -> str:
    name = value.strip()
    if not name:
        raise ValueError("plan-name 不能为空。")
    if not name.startswith("AUTO_"):
        raise PlanNotToolManagedError("自动创建的计划名称必须以 AUTO_ 开头。")
    return name


def _prepare_data(transport: _Transport, endpoint: str, body: dict[str, Any]) -> tuple[Any, dict[str, Any]]:
    envelope = transport.post(endpoint, body)
    data = envelope_data(envelope, endpoint, error_type=JdPrepareFailedError)
    return data, envelope


def _build_ad(sku_detail: dict[str, Any]) -> dict[str, Any]:
    ad = copy.deepcopy(sku_detail)
    sku_name = str(sku_detail.get("skuName") or "")
    ad_name = str(sku_detail.get("adName") or "")
    ad["name"] = f"系统推荐-{ad_name}"
    ad["customTitle"] = sku_name
    ad["defaultTitle"] = sku_name
    ad["creativeType"] = 19
    ad["sourceType"] = 4096
    ad["forbitGoldPosPackage"] = False
    ad["_localId"] = 1
    ad.pop("adName", None)
    ad.pop("imagePath", None)
    return ad


def _body_summary(body: dict[str, Any]) -> dict[str, Any]:
    campaign = body["campaignCreateCommand"]
    group = body["adGroupCreateCommand"]
    ad = group["adList"][0]
    deliveries = group["commodityDeliveryList"]
    return {
        "campaign": {
            "name": campaign["name"],
            "day_budget": float(campaign["dayBudget"]),
            "start_time": campaign["startTime"],
            "marketing_objective": campaign["marketingObjective"],
            "marketing_scenario": campaign["marketingScenario"],
            "targeting_type": campaign["targetingType"],
            "increase_budget_switch": campaign["increaseBudgetSwitch"],
            "strategy_ticket_present": bool(campaign["strategyIds"] and campaign["strategyVersionId"]),
        },
        "ad_group": {
            "sku_id": str(ad.get("skuId") or ""),
            "sku_name": ad.get("skuName"),
            "ad_name": ad.get("name"),
            "target_cpa": group["tcpaBid"],
            "keyword_count": len(group["keywordList"]),
            "targeting_category_count": sum(1 for item in deliveries if item.get("matchingType") == 3),
            "targeting_item_count": sum(1 for item in deliveries if item.get("matchingType") == 1),
            "version_ticket_present": bool(group["versionId"]),
            "area_ids": list(group["newAreaIds"]),
            "coupon_enabled": group["adGroupCouponStatus"] == 1,
        },
    }


def _clear_default_targeting(body: dict[str, Any]) -> dict[str, Any]:
    """Clear JD's auto-selected keywords and commodity/category targeting."""

    try:
        group = body["adGroupCreateCommand"]
    except (KeyError, TypeError) as exc:
        raise PlanCreateFailedError("京准通创建请求缺少推广单元结构。") from exc
    if not isinstance(group, dict):
        raise PlanCreateFailedError("京准通创建请求推广单元结构无效。")
    group["keywordList"] = []
    group["commodityDeliveryList"] = []
    return body


def _validate_create_echo(
    body: dict[str, Any],
    *,
    sku_id: int,
    plan_name: str,
    budget: Decimal,
    target_cpa: str,
) -> None:
    try:
        campaign = body["campaignCreateCommand"]
        group = body["adGroupCreateCommand"]
        ad_sku = int(str(group["adList"][0]["skuId"]))
        observed_name = str(campaign["name"])
        observed_budget = Decimal(str(campaign["dayBudget"]))
        observed_target_cpa = str(group["tcpaBid"])
    except (KeyError, IndexError, TypeError, ValueError, InvalidOperation) as exc:
        raise PlanCreateFailedError("京准通创建请求提交前回显结构不完整。") from exc
    matches = {
        "plan_name": observed_name == plan_name,
        "budget": observed_budget == budget,
        "target_cpa": observed_target_cpa == target_cpa,
        "sku_id": ad_sku == sku_id,
    }
    if not all(matches.values()):
        raise PlanCreateFailedError(
            "京准通创建请求提交前回显校验不通过。",
            {
                "field_matches": matches,
                "observed": {
                    "plan_name": observed_name,
                    "budget": float(observed_budget),
                    "target_cpa": observed_target_cpa,
                    "sku_id": str(ad_sku),
                },
            },
        )
    if group.get("keywordList") != [] or group.get("commodityDeliveryList") != []:
        raise PlanCreateFailedError("京准通创建请求仍包含系统默认关键词或定向商品。")
    if not campaign.get("strategyIds") or not campaign.get("strategyVersionId"):
        raise JdPrepareFailedError("京准通创建请求缺少预算策略票据。")
    if not group.get("versionId"):
        raise JdPrepareFailedError("京准通创建请求缺少出价票据。")
    log_param = group.get("logParamExt")
    if not isinstance(log_param, dict) or not str(log_param.get("pin") or "").strip():
        raise JdAuthenticationError("京准通创建请求缺少登录账号标识。")


def _extract_created_ids(data: Any, envelope: dict[str, Any]) -> tuple[str | None, str | None]:
    """Accept documented and harmless wrapper variants without guessing generic IDs."""

    queue: list[Any] = [data, envelope.get("result")]
    seen: set[int] = set()
    plan_id: str | None = None
    ad_group_id: str | None = None
    while queue:
        current = queue.pop(0)
        identity = id(current)
        if identity in seen:
            continue
        seen.add(identity)
        if isinstance(current, dict):
            if current.get("campaignId") not in (None, ""):
                plan_id = str(current["campaignId"])
            if current.get("adGroupId") not in (None, ""):
                ad_group_id = str(current["adGroupId"])
            if plan_id:
                return plan_id, ad_group_id
            for key in ("data", "result", "campaign", "campaignCreateResult", "adGroupCreateResult"):
                if key in current:
                    queue.append(current[key])
        elif isinstance(current, list):
            queue.extend(current[:5])
        elif isinstance(current, (int, str)) and not isinstance(current, bool) and str(current).strip().isdigit():
            plan_id = str(current).strip()
            return plan_id, ad_group_id
    return None, ad_group_id


class JdKuaichePlanService:
    def __init__(self, transport: _Transport) -> None:
        self.transport = transport
        self.report = JdKuaicheReportService(transport)

    def prepare_create(
        self,
        *,
        sku_id: str | int,
        plan_name: str,
        budget: str | int | float,
        target_cpa: str | int | float,
    ) -> dict[str, Any]:
        numeric_sku = _positive_identifier(sku_id, "sku-id")
        name = _tool_name(plan_name)
        budget_value = _positive_decimal(budget, "budget")
        target_value = _positive_decimal(target_cpa, "target-cpa")
        target_text = _decimal_text(target_value)
        capture_create_body = getattr(self.transport, "capture_create_body", None)
        if callable(capture_create_body):
            body = _clear_default_targeting(
                capture_create_body(
                    sku_id=numeric_sku,
                    plan_name=name,
                    budget=_decimal_text(budget_value),
                    target_cpa=target_text,
                )
            )
            _validate_create_echo(
                body,
                sku_id=numeric_sku,
                plan_name=name,
                budget=budget_value,
                target_cpa=target_text,
            )
            summary = _body_summary(body)
            return {
                "sku_id": str(numeric_sku),
                "plan_name": name,
                "budget": float(budget_value),
                "target_cpa": target_text,
                "keyword_count": summary["ad_group"]["keyword_count"],
                "targeting_item_count": summary["ad_group"]["targeting_item_count"],
                "targeting_category_count": summary["ad_group"]["targeting_category_count"],
                "recommend_budget": None,
                "request_body": body,
                "request_summary": summary,
                "body_source": "official_form_capture",
            }
        pin = ""

        same_spu, envelope = _prepare_data(
            self.transport,
            SAME_SPU_ENDPOINT,
            {"skuIds": [numeric_sku], "requestFrom": 0},
        )
        pin = response_login_pin(envelope) or pin
        if not isinstance(same_spu, dict) or str(numeric_sku) not in {str(key) for key in same_spu}:
            raise JdSkuNotFoundError(f"京准通未识别 SKU {numeric_sku}。", {"sku_id": str(numeric_sku)})

        sku_data, envelope = _prepare_data(
            self.transport,
            SKU_INFO_ENDPOINT,
            {
                "skuIds": [numeric_sku],
                "selectRangeType": 1,
                "sourceType": 4096,
                "filterType": None,
                "jdmcAppletUrlFlag": False,
                "businessType": 2,
                "campaignType": 2,
                "validateProductAttributesId": None,
                "requestFrom": 0,
            },
        )
        pin = response_login_pin(envelope) or pin
        if not isinstance(sku_data, dict) or sku_data.get("errorDatas"):
            raise JdSkuNotFoundError(f"SKU {numeric_sku} 不可用于京准通快车推广。", {"sku_id": str(numeric_sku)})
        details = sku_data.get("skuDetails")
        if not isinstance(details, list) or not details or not isinstance(details[0], dict):
            raise JdSkuNotFoundError(f"京准通未返回 SKU {numeric_sku} 的商品详情。", {"sku_id": str(numeric_sku)})
        sku_detail = details[0]

        _, envelope = _prepare_data(
            self.transport,
            KEYWORD_RECOMMEND_ENDPOINT,
            {
                "adKeywordTypes": [],
                "skuId": numeric_sku,
                "devType": 2,
                "adGroupId": None,
                "competitorSkus": [],
                "campaignType": 2,
                "requestFrom": 0,
            },
        )
        pin = response_login_pin(envelope) or pin

        _, envelope = _prepare_data(
            self.transport,
            CID_RECOMMEND_ENDPOINT,
            {
                "skuIds": [numeric_sku],
                "defaultQuery": 1,
                "marketingObjective": 1,
                "marketingScenario": 1,
                "targetingType": 2,
                "requestFrom": 0,
            },
        )
        pin = response_login_pin(envelope) or pin
        bidding, envelope = _prepare_data(
            self.transport,
            BIDDING_SUGGEST_ENDPOINT,
            {
                "retrievalType": 2,
                "businessType": 2,
                "biddingTarget": 16,
                "campaignType": 2,
                "automatedBiddingType": 1,
                "location": "newKcBiddingSuggest",
                "deliveryTarget": 4,
                "coldBootFlag": False,
                "suggestRouter": 1,
                "requestFrom": 0,
            },
        )
        pin = response_login_pin(envelope) or pin
        sid = str(bidding.get("sid") or "") if isinstance(bidding, dict) else ""

        budget_data, envelope = _prepare_data(
            self.transport,
            BUDGET_SUGGEST_ENDPOINT,
            {
                "businessType": 2,
                "location": "newKcBudgetSuggest",
                "campaignBudgetSuggestList": [
                    {
                        "campaignType": 2,
                        "sxuType": 2,
                        "uId": "0",
                        "sxuInfo": [{"sxuId": numeric_sku}],
                        "campaignId": None,
                    }
                ],
                "requestType": 1,
                "requestFrom": 0,
            },
        )
        pin = response_login_pin(envelope) or pin
        budget_item = budget_data[0] if isinstance(budget_data, list) and budget_data else None
        strategy_id = str(budget_item.get("budgetStrategyId") or "") if isinstance(budget_item, dict) else ""
        strategy_version = str(budget_item.get("strategyVersionId") or "") if isinstance(budget_item, dict) else ""

        if not sid or not strategy_id or not strategy_version:
            raise JdPrepareFailedError("京准通推荐接口未返回一次性创建票据。")
        if not pin:
            raise JdAuthenticationError("京准通响应缺少登录账号标识，已中止创建。")

        body = {
            "campaignCreateCommand": {
                "name": name,
                "marketingObjective": 1,
                "marketingScenario": 1,
                "targetingType": 2,
                "dayBudget": float(budget_value),
                "startTime": date.today().isoformat(),
                "endTime": "",
                "timeRangePriceCoef": "",
                "uniformSpeed": 0,
                "dateRange": "",
                "increaseBudgetSwitch": 0,
                "increaseBudgetRadio": 30,
                "increaseBudgetExceptROI": None,
                "increaseBudgetMaxCount": 2,
                "strategyVersionId": strategy_version,
                "strategyIds": [strategy_id],
            },
            "adGroupCreateCommand": {
                "fee": 1,
                "biddingTarget": 16,
                "deliveryTarget": 4,
                "biddingControlType": 4,
                "biddingType": 1,
                "orientationRange": 1,
                "tcpaBid": target_text,
                "versionId": [sid],
                "adGroupCouponStatus": 1,
                "adList": [_build_ad(sku_detail)],
                "skuBlackList": [],
                "newAreaIds": ["0"],
                "commodityDeliveryList": [],
                "dmpCrowdSettings": [],
                "seedsList": [],
                "keywordList": [],
                "premiumOrientationRange": 0,
                "logParamExt": {
                    "pin": pin,
                    "businessType": 2,
                    "recProductRequestFrom": "kcSelection",
                    "automatedBiddingTypeRec": 1,
                    "biddingTargetRec": 16,
                    "tcpaBidRec": target_text,
                    "skuInfos": [{"skuId": numeric_sku, "sourceType": 4096}],
                },
            },
            "requestFrom": 0,
        }
        _validate_create_echo(
            body,
            sku_id=numeric_sku,
            plan_name=name,
            budget=budget_value,
            target_cpa=target_text,
        )
        return {
            "sku_id": str(numeric_sku),
            "plan_name": name,
            "budget": float(budget_value),
            "target_cpa": target_text,
            "keyword_count": 0,
            "targeting_item_count": 0,
            "targeting_category_count": 0,
            "recommend_budget": budget_item.get("recommendBudget") if isinstance(budget_item, dict) else None,
            "request_body": body,
            "request_summary": _body_summary(body),
        }

    def _update_status(self, *, plan_id: str | int, operate_type: int, expected_status: int) -> dict[str, Any]:
        numeric_id = _positive_identifier(plan_id, "plan-id")
        endpoint_body = {
            "campaignSettings": [{"campaignId": numeric_id}],
            "operateType": operate_type,
            "requestFrom": 0,
        }
        envelope = self.transport.post(CAMPAIGN_STATUS_ENDPOINT, endpoint_body, write=True)
        data = envelope_data(envelope, CAMPAIGN_STATUS_ENDPOINT, error_type=PlanEnableFailedError)
        if not isinstance(data, dict) or data.get("success") is not True:
            raise PlanEnableFailedError("京准通计划状态接口未确认成功。", {"plan_id": str(numeric_id)})
        observed = self.report.find_plan(numeric_id)
        if observed is None or observed.get("status") != expected_status:
            raise PlanEnableFailedError(
                "京准通计划状态写入后复核不一致。",
                {"plan_id": str(numeric_id), "expected_status": expected_status},
            )
        return observed

    def create(
        self,
        *,
        sku_id: str | int,
        plan_name: str,
        budget: str | int | float,
        target_cpa: str | int | float,
        auto_enable: bool = True,
        execute: bool = False,
    ) -> dict[str, Any]:
        name = _tool_name(plan_name)
        if self.report.find_plan_by_name(name) is not None:
            raise PlanNameConflictError(f"京准通已存在同名计划：{name}", {"plan_name": name})
        prepared = self.prepare_create(
            sku_id=sku_id,
            plan_name=name,
            budget=budget,
            target_cpa=target_cpa,
        )
        response = {
            key: value
            for key, value in prepared.items()
            if key not in {"request_body"}
        }
        if not execute:
            return {
                **response,
                "plan_id": None,
                "ad_group_id": None,
                "created": False,
                "enabled": False,
                "auto_enabled": auto_enable,
                "executed": False,
                "dry_run": False,
                "simulated": True,
                "required_flag": "--execute",
                "source": "http_preview",
                "auto_retry_allowed": False,
            }

        envelope = self.transport.post(CAMPAIGN_CREATE_ENDPOINT, prepared["request_body"], write=True)
        data = envelope_data(envelope, CAMPAIGN_CREATE_ENDPOINT, error_type=PlanCreateFailedError)
        plan_id, ad_group_id = _extract_created_ids(data, envelope)
        id_recovered_from_list = False
        if plan_id is None:
            observed = self.report.find_plan_by_name(name)
            if observed is not None:
                plan_id = observed["plan_id"]
                id_recovered_from_list = True
        if plan_id is None:
            diagnostics: dict[str, Any] = {
                "response_data_type": type(data).__name__,
                "platform_sub_code": envelope.get("subCode"),
                "platform_message": str(envelope.get("msg") or "")[:160],
            }
            if isinstance(data, dict):
                diagnostics["response_data_keys"] = sorted(str(key) for key in data)[:30]
            raise PlanCreateFailedError("京准通创建接口未返回 campaignId，且列表未发现新计划。", diagnostics)
        enabled = False
        verified = False
        warning = None
        if auto_enable:
            try:
                observed = self._update_status(
                    plan_id=plan_id,
                    operate_type=PLAN_OPERATE_ENABLE,
                    expected_status=PLAN_STATUS_ENABLED,
                )
                enabled = observed.get("status") == PLAN_STATUS_ENABLED
                verified = enabled
            except Exception as exc:
                warning = f"计划已创建但自动启用失败：{exc}"

        return {
            **response,
            "plan_id": plan_id,
            "ad_group_id": ad_group_id,
            "created": True,
            "enabled": enabled,
            "auto_enabled": auto_enable,
            "verified": verified,
            "executed": True,
            "dry_run": False,
            "simulated": False,
            "source": "http",
            "warning": warning,
            "id_recovered_from_list": id_recovered_from_list,
            "auto_retry_allowed": False,
        }

    @staticmethod
    def _guard_plan(item: dict[str, Any] | None, *, plan_id: str | int, expected_plan_name: str) -> dict[str, Any]:
        if item is None:
            raise PlanNotFoundError(f"未找到京准通计划 {plan_id}。", {"plan_id": str(plan_id)})
        observed_name = str(item.get("plan_name") or "")
        expected_name = expected_plan_name.strip()
        if observed_name != expected_name:
            raise PlanNameMismatchError(
                "京准通计划名称与 expected-plan-name 不一致。",
                {"plan_id": str(plan_id), "observed_name": observed_name, "expected_name": expected_name},
            )
        if not observed_name.startswith("AUTO_"):
            raise PlanNotToolManagedError(
                "拒绝操作非 AUTO_ 前缀的人工计划。",
                {"plan_id": str(plan_id), "observed_name": observed_name},
            )
        return item

    def set_enabled(
        self,
        *,
        plan_id: str | int,
        expected_plan_name: str,
        enabled: bool,
        execute: bool = False,
    ) -> dict[str, Any]:
        item = self._guard_plan(
            self.report.find_plan(plan_id),
            plan_id=plan_id,
            expected_plan_name=expected_plan_name,
        )
        desired_status = PLAN_STATUS_ENABLED if enabled else PLAN_STATUS_PAUSED
        if not execute:
            return {
                "plan_id": item["plan_id"],
                "plan_name": item["plan_name"],
                "before_status": item["status"],
                "after_status": desired_status,
                "enabled": enabled,
                "executed": False,
                "simulated": True,
                "verified": False,
                "required_flag": "--execute",
                "auto_retry_allowed": False,
            }
        if item["status"] == desired_status:
            return {
                "plan_id": item["plan_id"],
                "plan_name": item["plan_name"],
                "before_status": item["status"],
                "after_status": desired_status,
                "enabled": enabled,
                "executed": False,
                "simulated": False,
                "verified": True,
                "reason": "计划已处于目标状态",
                "auto_retry_allowed": False,
            }
        observed = self._update_status(
            plan_id=plan_id,
            operate_type=PLAN_OPERATE_ENABLE if enabled else PLAN_OPERATE_PAUSE,
            expected_status=desired_status,
        )
        return {
            "plan_id": observed["plan_id"],
            "plan_name": observed["plan_name"],
            "before_status": item["status"],
            "after_status": observed["status"],
            "enabled": observed["status"] == PLAN_STATUS_ENABLED,
            "executed": True,
            "simulated": False,
            "verified": True,
            "auto_retry_allowed": False,
        }

    def delete(
        self,
        *,
        plan_id: str | int,
        expected_plan_name: str,
        execute: bool = False,
    ) -> dict[str, Any]:
        numeric_id = _positive_identifier(plan_id, "plan-id")
        item = self._guard_plan(
            self.report.find_plan(numeric_id),
            plan_id=numeric_id,
            expected_plan_name=expected_plan_name,
        )
        final_snapshot = self.report.orders(
            plan_id=numeric_id,
            start=item.get("created_date"),
        )
        final_metrics = dict(final_snapshot.get("metrics") or normalize_metrics({}))
        if not execute:
            return {
                "plan_id": item["plan_id"],
                "plan_name": item["plan_name"],
                "final_metrics": final_metrics,
                "executed": False,
                "deleted": False,
                "simulated": True,
                "verified": False,
                "required_flag": "--execute",
                "auto_retry_allowed": False,
            }

        envelope = self.transport.post(
            CAMPAIGN_DELETE_ENDPOINT,
            {"campaignSettings": [{"campaignId": numeric_id}], "requestFrom": 0},
            write=True,
        )
        data = envelope_data(envelope, CAMPAIGN_DELETE_ENDPOINT, error_type=JdWriteVerificationError)
        if not isinstance(data, dict) or data.get("success") is not True:
            raise JdWriteVerificationError(
                "京准通删除接口未确认成功，禁止自动重试。",
                {"plan_id": str(numeric_id)},
            )
        if self.report.find_plan(numeric_id) is not None:
            raise JdWriteVerificationError(
                "京准通删除接口返回成功，但列表复核仍存在该计划。",
                {"plan_id": str(numeric_id)},
            )
        return {
            "plan_id": item["plan_id"],
            "plan_name": item["plan_name"],
            "final_metrics": final_metrics,
            "executed": True,
            "deleted": True,
            "simulated": False,
            "verified": True,
            "auto_retry_allowed": False,
        }


def simulated_create_data(
    *,
    sku_id: str | int,
    plan_name: str,
    budget: str | int | float,
    target_cpa: str | int | float,
    auto_enable: bool,
) -> dict[str, Any]:
    numeric_sku = _positive_identifier(sku_id, "sku-id")
    name = _tool_name(plan_name)
    budget_value = _positive_decimal(budget, "budget")
    target_text = _decimal_text(_positive_decimal(target_cpa, "target-cpa"))
    return {
        "sku_id": str(numeric_sku),
        "plan_id": None,
        "ad_group_id": None,
        "plan_name": name,
        "budget": float(budget_value),
        "target_cpa": target_text,
        "keyword_count": 0,
        "targeting_item_count": 0,
        "targeting_category_count": 0,
        "recommend_budget": None,
        "created": False,
        "enabled": False,
        "auto_enabled": auto_enable,
        "executed": False,
        "dry_run": True,
        "simulated": True,
        "source": "simulation",
        "reason": "dry-run 跳过全部网络请求",
        "auto_retry_allowed": False,
    }


def simulated_status_data(*, plan_id: str | int, expected_plan_name: str, enabled: bool) -> dict[str, Any]:
    return {
        "plan_id": str(_positive_identifier(plan_id, "plan-id")),
        "plan_name": expected_plan_name.strip(),
        "enabled": enabled,
        "executed": False,
        "dry_run": True,
        "simulated": True,
        "verified": False,
        "reason": "dry-run 跳过全部网络请求",
        "auto_retry_allowed": False,
    }


def simulated_delete_data(*, plan_id: str | int, expected_plan_name: str) -> dict[str, Any]:
    return {
        "plan_id": str(_positive_identifier(plan_id, "plan-id")),
        "plan_name": expected_plan_name.strip(),
        "final_metrics": normalize_metrics({}),
        "executed": False,
        "deleted": False,
        "dry_run": True,
        "simulated": True,
        "verified": False,
        "reason": "dry-run 跳过全部网络请求",
        "auto_retry_allowed": False,
    }
