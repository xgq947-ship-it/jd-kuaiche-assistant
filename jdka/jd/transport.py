"""京准通 HTTP 传输层：在已登录页面上下文内发请求，不导出 Cookie。

与被抽取的旧实现相比有两处关键差异：

1. **不驱动 UI**。旧实现靠 Playwright 点京东表单再拦截创建请求，依赖 9 个
   中文界面文案，京东改版即全量失效。这里只暴露 ``post``，不提供
   ``capture_create_body``，因此 :meth:`JdKuaichePlanService.prepare_create`
   会走已验证可行的程序化组装分支。
2. **浏览器来自 BrowserRuntime**，后台无头、独立 profile、随机端口。

请求始终由页面上下文发出（``credentials: 'include'``），签名由页面自身的
h5st 钩子生成。本模块从不读取、导出或落盘 Cookie / Token。
"""

from __future__ import annotations

import copy
from typing import Any
from urllib.parse import urlsplit

from jdka.browser.runtime import BrowserRuntime, LoginRequired
from jdka.jd.shared import (
    JD_ALL_ENDPOINTS,
    JD_HEADERS,
    JD_HOME_URL,
    JD_READ_ONLY_ENDPOINTS,
    JD_SIGNED_ENDPOINTS,
    JD_WRITE_ENDPOINTS,
    JdAuthenticationError,
    JdPlatformRequestError,
    JdSignExpiredError,
    JdSignRequiredError,
    JdWriteSignExpiredError,
    endpoint_label,
)

PAGE_KEY = "jd:kuaiche"

# 登录判定：SPA 冷启动时页面尚未挂载，绝不能据此判成掉登录。
# 这一条是踩过坑换来的——Google Flow 上出现过同样的冷启动误判。
_LOGIN_URL_MARKERS = ("passport.jd.com", "login.jd.com", "plogin.m.jd.com")

_XHR_SCRIPT = """async ({endpoint, headers, body}) => await new Promise((resolve) => {
    const xhr = new XMLHttpRequest();
    xhr.open('POST', endpoint, true);
    xhr.withCredentials = true;
    xhr.timeout = 30000;
    for (const [name, value] of Object.entries(headers)) {
        xhr.setRequestHeader(name, value);
    }
    const finish = (networkError = '') => {
        let payload = null;
        try { payload = JSON.parse(xhr.responseText || ''); } catch (_) {}
        resolve({
            httpStatus: xhr.status || 0,
            finalUrl: xhr.responseURL || '',
            contentType: xhr.getResponseHeader('content-type') || '',
            signaturePresent: (() => {
                try { return new URL(xhr.responseURL).searchParams.has('h5st'); }
                catch (_) { return false; }
            })(),
            payload,
            networkError,
        });
    };
    xhr.onload = () => finish();
    xhr.onerror = () => finish('error');
    xhr.ontimeout = () => finish('timeout');
    xhr.onabort = () => finish('abort');
    xhr.send(JSON.stringify(body));
})"""

_FETCH_SCRIPT = """async ({endpoint, headers, body}) => {
    try {
        const response = await fetch(endpoint, {
            method: 'POST', credentials: 'include', headers,
            body: JSON.stringify(body),
        });
        let payload = null;
        try { payload = await response.json(); } catch (_) {}
        return {
            httpStatus: response.status || 0,
            finalUrl: response.url || '',
            contentType: response.headers.get('content-type') || '',
            payload, networkError: '',
        };
    } catch (error) {
        return {
            httpStatus: 0, finalUrl: '', contentType: '', payload: null,
            networkError: error && error.name ? error.name : 'error',
        };
    }
}"""


class JdTransport:
    """只暴露 ``post``；刻意不实现 ``capture_create_body``。"""

    def __init__(self, runtime: BrowserRuntime) -> None:
        self.runtime = runtime

    # ---------- 页面与登录 ----------

    def page(self) -> Any:
        page = self.runtime.acquire(PAGE_KEY, JD_HOME_URL)
        self.assert_logged_in(page)
        return page

    @staticmethod
    def assert_logged_in(page: Any) -> None:
        """只在**明确跳到登录页**时判定掉登录。

        页面还没渲染完不算掉登录 —— 冷启动误判会导致后台反复弹登录窗口。
        """
        url = str(getattr(page, "url", "") or "")
        host = urlsplit(url).hostname or ""
        if any(marker in url for marker in _LOGIN_URL_MARKERS):
            raise JdAuthenticationError(
                "京准通登录态已失效，请在应用内点击「登录京准通」。",
                {"observed_host": host},
            )
        if host and not host.endswith(".jd.com"):
            raise JdAuthenticationError(
                "后台页面已离开京东站点。", {"observed_host": host}
            )

    # ---------- 请求 ----------

    @staticmethod
    def _validate(endpoint: str, body: dict[str, Any], *, write: bool) -> None:
        if endpoint not in JD_ALL_ENDPOINTS:
            raise JdPlatformRequestError(
                "拒绝请求未列入白名单的京准通接口。",
                {"endpoint": endpoint_label(endpoint)},
            )
        if endpoint in JD_WRITE_ENDPOINTS and not write:
            raise JdPlatformRequestError(
                "京准通写接口必须显式标记 write=True。",
                {"endpoint": endpoint_label(endpoint)},
            )
        if endpoint in JD_READ_ONLY_ENDPOINTS and write:
            raise JdPlatformRequestError(
                "京准通只读接口不能标记为写请求。",
                {"endpoint": endpoint_label(endpoint)},
            )
        if body.get("requestFrom") != 0:
            raise JdPlatformRequestError(
                "京准通请求体必须包含 requestFrom=0。",
                {"endpoint": endpoint_label(endpoint)},
            )

    def post(self, endpoint: str, body: dict[str, Any], *, write: bool = False) -> dict[str, Any]:
        self._validate(endpoint, body, write=write)
        page = self.page()
        signed = endpoint in JD_SIGNED_ENDPOINTS
        script = _XHR_SCRIPT if signed else _FETCH_SCRIPT
        try:
            response = page.evaluate(
                script,
                {
                    "endpoint": endpoint,
                    "headers": copy.deepcopy(JD_HEADERS),
                    "body": copy.deepcopy(body),
                },
            )
        except Exception as exc:
            raise JdPlatformRequestError(
                "京准通页面内请求失败。",
                {"endpoint": endpoint_label(endpoint), "error_type": type(exc).__name__},
            ) from exc

        if not isinstance(response, dict):
            raise JdPlatformRequestError(
                "京准通返回结构无效。", {"endpoint": endpoint_label(endpoint)}
            )
        status = int(response.get("httpStatus") or 0)
        expected = urlsplit(endpoint)
        final = urlsplit(str(response.get("finalUrl") or ""))
        final_host = final.hostname
        redirected = final_host is not None and (
            final_host != expected.hostname or final.path != expected.path
        )
        diagnostics = {"endpoint": endpoint_label(endpoint), "http_status": status}
        if response.get("networkError"):
            diagnostics["network_error_type"] = str(response["networkError"])
            raise JdPlatformRequestError("京准通页面内请求失败。", diagnostics)
        if (
            redirected
            or status in {301, 302, 303, 307, 308, 401, 403}
            or (final_host is not None and not final_host.endswith(".jd.com"))
        ):
            raise JdAuthenticationError("京准通登录态失效或请求被重定向。", diagnostics)
        if status < 200 or status >= 300:
            raise JdPlatformRequestError("京准通 HTTP 请求返回失败状态。", diagnostics)
        payload = response.get("payload")
        if not isinstance(payload, dict):
            diagnostics["content_type"] = str(response.get("contentType") or "").split(";", 1)[0]
            raise JdAuthenticationError("京准通返回非 JSON，登录态可能已失效。", diagnostics)
        if signed and payload.get("code") != 1:
            if not bool(response.get("signaturePresent")):
                raise JdSignRequiredError("京准通页面未生成 h5st 签名材料。", diagnostics)
            message = str(payload.get("msg") or payload.get("subMsg") or "")
            if payload.get("code") == -402 or any(
                marker in message for marker in ("签名", "校验失败", "异常请求")
            ):
                error_type = JdWriteSignExpiredError if write else JdSignExpiredError
                raise error_type("京准通 h5st 签名无效或已过期。", diagnostics)
        return payload
