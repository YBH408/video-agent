"""Only load this new project's settings when a real API is called."""
import os
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent


class ConfigurationError(ValueError):
    pass


def get_setting(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        # Never search parent folders or read video_img's configuration.
        env_path = PROJECT_DIR / ".env"
        if env_path.is_file():
            try:
                from dotenv import dotenv_values
            except ImportError:
                raise ConfigurationError("读取新项目 .env 需要安装 python-dotenv。") from None
            value = (dotenv_values(env_path).get(name) or "").strip()
    if not value:
        raise ConfigurationError(f"缺少配置 {name}，请在新项目 .env 或环境变量中设置。")
    return value
