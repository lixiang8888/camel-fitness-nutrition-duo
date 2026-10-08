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

#: 单次请求的超时（秒）。默认没有超时——网络一出问题就是**无限期挂住**，
#: 页面上只看到计时器在走、没有任何报错，比直接失败难查得多。
#: 实测单次调用约 17 秒，180 秒留了足够余量，同时保证卡住时能报出来。
CALL_TIMEOUT = 180.0


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

    from .backends import OfflineTokenCounter

    return ModelFactory.create(
        model_platform=ModelPlatformType.OPENAI_COMPATIBLE_MODEL,
        model_type=os.getenv("DEEPSEEK_MODEL", DEFAULT_MODEL),
        api_key=api_key,
        url=os.getenv("DEEPSEEK_BASE_URL", DEFAULT_BASE_URL),
        model_config_dict={"temperature": temperature},
        # 必须自己给 token_counter，不能让 camel 去建默认的 OpenAITokenCounter：
        # 它要用 tiktoken 加载 o200k_base 词表，本地没有就联网下载，而那个域名
        # （openaipublic.blob.core.windows.net）在国内连不上——于是建 ChatAgent
        # 那一步就**无限期挂住**，模型调用根本没发出去。现场症状是
        # 「计时器在走，一句发言都没有，也不报错」，而且因为 tiktoken 把词表缓存在
        # /tmp/data-gym-cache，重启一次就复发，看着像「昨天还好好的今天就不行」。
        token_counter=OfflineTokenCounter(),
        timeout=CALL_TIMEOUT,
    )
