"""Shared contracts and safety primitives for JD Jingzhuntong HTTP calls."""

from __future__ import annotations

from typing import Any, TypeVar
from urllib.parse import urlsplit


JD_ORIGIN = "https://jzt.jd.com"
JD_HOST = "jzt.jd.com"
JD_HOME_URL = (
    "https://jzt.jd.com/msa/#/list/tab/plan"
    "?objective=item&scenario=normal&targetingType=keyword"
)
JD_CREATE_URL = (
    "https://jzt.jd.com/msa/#/operations/plan-unit/item/normal/keyword?step=1"
)
ATOMS = "https://atoms-api.jd.com"
JZT = "https://jzt-api.jd.com"

CAMPAIGN_LIST_ENDPOINT = f"{ATOMS}/dspad/msa/promolist/item/keyword/campaign"
AD_LIST_ENDPOINT = f"{ATOMS}/dspad/msa/promolist/item/keyword/ad"
SKU_INFO_ENDPOINT = f"{ATOMS}/dspad/common/sku/info"
CID_RECOMMEND_ENDPOINT = f"{ATOMS}/dspad/msa/delivery/cid/recommend"
SKU_RECOMMEND_ENDPOINT = f"{ATOMS}/dspad/msa/delivery/sku/recommend"
SAME_SPU_ENDPOINT = f"{JZT}/common/sku/same/spu/check"
KEYWORD_RECOMMEND_ENDPOINT = f"{JZT}/dspad/keyword/sku/recommend"
BIDDING_SUGGEST_ENDPOINT = f"{JZT}/dspad/bidding/suggest"
BUDGET_SUGGEST_ENDPOINT = f"{JZT}/dspad/common/suggest/campaign/budget"
CAMPAIGN_CREATE_ENDPOINT = f"{ATOMS}/dspad/msa/campaign/item/keyword/add"
CAMPAIGN_STATUS_ENDPOINT = f"{ATOMS}/dspad/material/center/task/campaign/batch/update/status"
CAMPAIGN_DELETE_ENDPOINT = f"{ATOMS}/dspad/material/center/task/campaign/batch/delete"

JD_HEADERS = {
    "content-type": "application/json",
    "language": "zh_CN",
    "loginmode": "0",
    "siteid": "0",
}

# 京准通计划状态取值与直觉相反：2 才是启用，1 是暂停。
# 该结论由真实页面实测确定：发 operateType=2 后列表渲染“有效”且 status=2，
# 发 operateType=1 后渲染“暂停中”且 status=1。请勿凭字面直觉改回去。
PLAN_STATUS_ENABLED = 2
PLAN_STATUS_PAUSED = 1
PLAN_OPERATE_ENABLE = 2
PLAN_OPERATE_PAUSE = 1

JD_READ_ONLY_ENDPOINTS = frozenset(
    {
        CAMPAIGN_LIST_ENDPOINT,
        AD_LIST_ENDPOINT,
        SKU_INFO_ENDPOINT,
        CID_RECOMMEND_ENDPOINT,
        SKU_RECOMMEND_ENDPOINT,
        SAME_SPU_ENDPOINT,
        KEYWORD_RECOMMEND_ENDPOINT,
        BIDDING_SUGGEST_ENDPOINT,
        BUDGET_SUGGEST_ENDPOINT,
    }
)
JD_WRITE_ENDPOINTS = frozenset(
    {
        CAMPAIGN_CREATE_ENDPOINT,
        CAMPAIGN_STATUS_ENDPOINT,
        CAMPAIGN_DELETE_ENDPOINT,
    }
)
JD_ALL_ENDPOINTS = JD_READ_ONLY_ENDPOINTS | JD_WRITE_ENDPOINTS
JD_SIGNED_ENDPOINTS = frozenset(
    {
        SKU_INFO_ENDPOINT,
        SKU_RECOMMEND_ENDPOINT,
        CAMPAIGN_CREATE_ENDPOINT,
    }
)


def endpoint_label(endpoint: str) -> str:
    parts = urlsplit(endpoint)
    return f"{parts.hostname or 'unknown'}{parts.path}"


class JdError(RuntimeError):
    """Base structured JD failure consumed by the common CLI failure mapper."""

    error_code = "PLATFORM_REQUEST_FAILED"
    retryable = True
    recovery_hint: str | None = None

    def __init__(self, message: str, diagnostics: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.response_diagnostics = dict(diagnostics or {})


class JdAuthenticationError(JdError):
    error_code = "AUTH_REQUIRED"
    retryable = True
    recovery_hint = "京准通登录态已失效，请在应用内点击「登录京准通」重新登录。"


class JdSkuNotFoundError(JdError):
    error_code = "JD_SKU_NOT_FOUND"
    retryable = False


class JdPrepareFailedError(JdError):
    error_code = "JD_PREPARE_FAILED"
    retryable = True


class JdRecommendEmptyError(JdError):
    error_code = "JD_RECOMMEND_EMPTY"
    retryable = True


class JdSignRequiredError(JdError):
    error_code = "JD_SIGN_REQUIRED"
    retryable = True
    recovery_hint = "后台页面需要重新加载以安装 h5st 签名钩子，将自动重试。"


class JdSignExpiredError(JdError):
    error_code = "JD_SIGN_EXPIRED"
    retryable = True
    recovery_hint = "只读请求可重新生成签名后再试一次。"


class JdWriteSignExpiredError(JdSignExpiredError):
    retryable = False
    recovery_hint = "写请求禁止自动重试；请先查询计划列表确认平台现状。"


class PlanCreateFailedError(JdError):
    error_code = "PLAN_CREATE_FAILED"
    retryable = False


class PlanEnableFailedError(JdError):
    error_code = "PLAN_ENABLE_FAILED"
    retryable = True


class PlanNameConflictError(JdError):
    error_code = "PLAN_NAME_CONFLICT"
    retryable = False


class PlanNameMismatchError(JdError):
    error_code = "PLAN_NAME_MISMATCH"
    retryable = False


class PlanNotToolManagedError(JdError):
    error_code = "PLAN_NOT_TOOL_MANAGED"
    retryable = False


class PlanNotFoundError(JdError):
    error_code = "PLAN_NOT_FOUND"
    retryable = False


class JdPlatformRequestError(JdError):
    error_code = "PLATFORM_REQUEST_FAILED"
    retryable = True


class JdWriteVerificationError(JdPlatformRequestError):
    """A write may already have happened, so callers must never auto-retry it."""

    retryable = False
    recovery_hint = "写请求结果不确定，禁止自动重试；请先查询计划列表确认平台现状。"


_ErrorT = TypeVar("_ErrorT", bound=JdError)


def _diagnostics(payload: dict[str, Any], endpoint: str) -> dict[str, Any]:
    ext = payload.get("ext")
    trace_id = ext.get("traceId") if isinstance(ext, dict) else None
    result: dict[str, Any] = {
        "endpoint": endpoint_label(endpoint),
        "platform_code": payload.get("code"),
        "platform_sub_code": payload.get("subCode"),
    }
    if trace_id:
        result["trace_id"] = str(trace_id)
    return result


def response_login_pin(payload: dict[str, Any]) -> str:
    system = payload.get("system")
    if not isinstance(system, dict):
        return ""
    return str(system.get("loginPin") or "").strip()


def envelope_data(
    payload: Any,
    endpoint: str,
    *,
    error_type: type[_ErrorT] = JdPlatformRequestError,
) -> Any:
    """Validate the common JD envelope without retaining its raw contents."""

    if not isinstance(payload, dict):
        raise error_type(
            "京准通接口返回结构无效。",
            {"endpoint": endpoint_label(endpoint), "response_type": type(payload).__name__},
        )
    if payload.get("code") == 1:
        return payload.get("data")

    message = str(payload.get("msg") or payload.get("subMsg") or "京准通接口返回失败")
    diagnostics = _diagnostics(payload, endpoint)
    lowered = message.lower()
    if any(marker in message for marker in ("登录", "未登录", "重新登录", "鉴权")) or "login" in lowered:
        raise JdAuthenticationError("京准通登录态失效或请求被鉴权拒绝。", diagnostics)
    raise error_type(f"京准通接口失败：{message}", diagnostics)
