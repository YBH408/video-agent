"""Seed chat API. Only the planner's prompt and output budget change."""
from config import get_setting
from tools._http import APIError, ARK_BASE_URL, post


def generate_text(system_prompt: str, user_prompt: str) -> str:
    result = post(
        ARK_BASE_URL + "/chat/completions",
        get_setting("ARK_API_KEY"),
        {
            "model": get_setting("SEED_MODEL_ID"),
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "thinking": {"type": "disabled"},
            "temperature": 0,
            # The Demo used 10 for classification; storyboard JSON needs more room.
            "max_tokens": 4096,
        },
    )
    try:
        content = result["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        raise APIError("LLM 响应缺少 choices[0].message.content。") from None
    if not isinstance(content, str) or not content.strip():
        raise APIError("LLM 返回了空文本或非文本内容。")
    return content
