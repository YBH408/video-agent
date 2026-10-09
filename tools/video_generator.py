"""MiniMax submission/query API; no automatic submission, retries or polling."""
from typing import Any

from config import get_setting
from tools._http import APIError, MINIMAX_BASE_URL, get, post


VIDEO_DURATION_SECONDS = 5
VIDEO_ASPECT_RATIO = "16:9"
VIDEO_RESOLUTION = "768P"


def generate_video(prompt: str, *, duration_seconds: int = VIDEO_DURATION_SECONDS,
                   aspect_ratio: str = VIDEO_ASPECT_RATIO, model_id: str | None = None) -> str:
    """Submit ONE real, potentially billable task and return its task ID."""
    if not isinstance(prompt, str) or not prompt.strip():
        raise ValueError("视频 prompt 不能为空。")
    if type(duration_seconds) is not int or duration_seconds != VIDEO_DURATION_SECONDS or aspect_ratio != VIDEO_ASPECT_RATIO:
        raise ValueError("本轮仅支持每镜头 5 秒、16:9。")
    if model_id is not None and (not isinstance(model_id, str) or not model_id.strip()):
        raise ValueError("视频模型 ID 不能为空。")
    result = post(
        MINIMAX_BASE_URL + "/video_generation",
        get_setting("MINIMAX_API_KEY"),
        {
            "model": model_id.strip() if model_id is not None else get_setting("MINIMAX_VIDEO_MODEL_ID"),
            "content": [{"type": "text", "text": prompt}],
            "duration": duration_seconds,
            "resolution": VIDEO_RESOLUTION,
            "ratio": aspect_ratio,
        },
    )
    task_id = result.get("task_id")
    if not task_id:
        task = result.get("task")
        task_id = task.get("id") if isinstance(task, dict) else None
    if not isinstance(task_id, str) or not task_id.strip():
        raise APIError("视频响应缺少有效的 task_id 或 task.id。")
    return task_id


def query_video(task_id: str) -> dict[str, Any]:
    """Query an existing task once using fields verified in the original Demo."""
    if not isinstance(task_id, str) or not task_id.strip():
        raise ValueError("task_id 不能为空。")
    from urllib.parse import quote

    result = get(
        MINIMAX_BASE_URL + "/query/video_generation/" + quote(task_id, safe=""),
        get_setting("MINIMAX_API_KEY"),
    )
    task = result.get("task")
    if not isinstance(task, dict) or not isinstance(task.get("status"), str):
        raise APIError("查询响应缺少 task.status。")
    if task["status"] == "succeeded":
        content = task.get("content")
        if not isinstance(content, dict) or not isinstance(content.get("url"), str) or not content["url"].strip():
            raise APIError("成功任务的响应缺少 task.content.url。")
    return task
