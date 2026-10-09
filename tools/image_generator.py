"""Seedream request logic retained for a later execution phase."""
from config import get_setting
from tools._http import APIError, ARK_BASE_URL, post


def generate_image(prompt: str) -> str:
    """Calling this function makes a real, potentially billable image request."""
    if not isinstance(prompt, str) or not prompt.strip():
        raise ValueError("图片 prompt 不能为空。")
    result = post(
        ARK_BASE_URL + "/images/generations",
        get_setting("ARK_API_KEY"),
        {
            "model": get_setting("SEEDREAM_MODEL_ID"),
            "prompt": prompt,
            "size": "2K",
            "response_format": "url",
            "sequential_image_generation": "disabled",
            "stream": False,
        },
    )
    try:
        url = result["data"][0]["url"]
    except (KeyError, IndexError, TypeError):
        raise APIError("图片响应缺少 data[0].url。") from None
    if not isinstance(url, str) or not url.strip():
        raise APIError("图片响应中的 url 无效。")
    return url
