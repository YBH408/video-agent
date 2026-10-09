"""Shared HTTP logic copied from the existing Demo, with safe errors."""
from typing import Any

ARK_BASE_URL = "https://ark.cn-beijing.volces.com/api/v3"
MINIMAX_BASE_URL = "https://api.minimax.cn/v2"


class APIError(RuntimeError):
    pass


def _request(method: str, url: str, key: str, *, data=None, timeout=180) -> dict[str, Any]:
    try:
        import requests
    except ImportError:
        raise APIError("真实 API 调用需要安装 requests：pip install -r requirements.txt") from None
    try:
        kwargs = {"headers": {"Authorization": f"Bearer {key}"}, "timeout": timeout}
        if data is not None:
            kwargs["json"] = data
        if method == "post":
            # Never replay a billable POST by following a redirect. No retry loop.
            kwargs["allow_redirects"] = False
        response = getattr(requests, method)(url, **kwargs)
        if method == "post" and 300 <= response.status_code < 400:
            raise APIError("POST 返回重定向，未跟随或重试，请人工核实提交结果。")
        response.raise_for_status()
        result = response.json()
    except (requests.RequestException, ValueError):
        # Do not expose credentials, response bodies or request headers in errors.
        raise APIError("API 请求失败，请检查网络、账户配置和服务状态。") from None
    if not isinstance(result, dict):
        raise APIError("API 响应格式异常：预期 JSON 对象。")
    return result


def post(url: str, key: str, data: dict[str, Any]) -> dict[str, Any]:
    return _request("post", url, key, data=data, timeout=180)


def get(url: str, key: str) -> dict[str, Any]:
    return _request("get", url, key, timeout=90)
