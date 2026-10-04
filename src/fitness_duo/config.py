"""配置与模型工厂。"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[2]
OUTPUT_DIR = ROOT / "outputs"

# 读 .env（不存在也不报错，模拟模式不需要 key）
load_dotenv(ROOT / ".env")

DEFAULT_BASE_URL = "https://api.deepseek.com/v1"
DEFAULT_MODEL = "deepseek-chat"


class MissingApiKey(RuntimeError):
    """没配 key 时给出人话提示，而不是让 openai 抛一长串栈。"""


def build_backend(*, mock: bool = False, temperature: float = 0.75):
    """返回 CAMEL 的模型后端。

    统一走 OpenAI 兼容协议，所以换 Kimi / 通义 / 本地 Ollama
    只需要改 .env 里的 BASE_URL 和 MODEL，代码不用动。
    """
    if mock:
        from .backends import ScriptedBackend

        return ScriptedBackend()

    api_key = os.getenv("DEEPSEEK_API_KEY", "").strip()
    if not api_key:
        raise MissingApiKey(
            "没有找到 DEEPSEEK_API_KEY。\n"
            "  1) cp .env.example .env\n"
            "  2) 把 key 填进 .env\n"
            "  想先不花钱跑通流程的话，加上 --mock：\n"
            "  uv run fitness-duo run --mock"
        )

    from camel.models import ModelFactory
    from camel.types import ModelPlatformType

    return ModelFactory.create(
        model_platform=ModelPlatformType.OPENAI_COMPATIBLE_MODEL,
        model_type=os.getenv("DEEPSEEK_MODEL", DEFAULT_MODEL),
        api_key=api_key,
        url=os.getenv("DEEPSEEK_BASE_URL", DEFAULT_BASE_URL),
        model_config_dict={"temperature": temperature},
    )
