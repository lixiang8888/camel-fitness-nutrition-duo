"""离线模拟后端。

本模块还提供一个 `OfflineTokenCounter`，**真实模式也在用**（见 config.build_backend）：
camel 默认的 token 计数器要联网下载 tiktoken 词表，在国内会无限期挂住。

目的：让你在还没有 API key 的情况下，也能把 CAMEL 的整条编排链路跑通
（建 agent → init_chat → 多轮 step → 落盘 → 整理成手册），
确认是「模型没配」而不是「代码有 bug」。

它不是假的模型调用：CAMEL 的 ModelPlatformType.STUB 只会回一句 "Lorem Ipsum"，
没法验证多轮对话。这里通过继承 BaseModelBackend 实现一个真正的后端，
回复里会带上上一轮内容的摘录，方便你肉眼确认对话确实在按顺序流转。

注意：模拟模式产出的手册是占位内容，没有任何营养学参考价值。
"""

from __future__ import annotations

from typing import Any, List, Optional, Type

from camel.models import BaseModelBackend
from camel.utils import BaseTokenCounter
from camel.types import ModelType
from openai.types.chat import ChatCompletion, ChatCompletionMessage
from openai.types.chat.chat_completion import Choice

MOCK_BANNER = "[离线模拟回复 · 非真实内容]"


class OfflineTokenCounter(BaseTokenCounter):
    """粗糙的字符数估算。只为满足接口，不追求准确。

    **这个类同时在两处用**：离线模拟后端，以及 config.build_backend 造真实后端时。
    真实模式也要给，是因为 camel 默认会去建 `OpenAITokenCounter`，而它要用 tiktoken
    加载 o200k_base 词表——本地没有就去 openaipublic.blob.core.windows.net 下载。
    那个域名在国内连不上，于是**无限期挂住**：不抛异常、不超时、日志里什么都没有。
    表现是页面上「计时器在走，但一句发言都不出」。
    tiktoken 默认把词表缓存在 `/tmp/data-gym-cache`，重启一次就没了，所以这毛病
    会「昨天还好好的，今天就不行」。

    估算值偏小（字符数除以 2），对本项目的对话长度绰绰有余——
    DeepSeek 的上下文是 64k 量级，而一个议题的对话也就几千 token。
    """

    def count_tokens_from_messages(self, messages: List[Any]) -> int:
        total = 0
        for message in messages:
            content = message.get("content") if isinstance(message, dict) else message
            total += len(str(content))
        return total // 2 + 1

    def encode(self, text: str) -> List[int]:
        return list(text.encode("utf-8"))

    def decode(self, token_ids: List[int]) -> str:
        return bytes(token_ids).decode("utf-8", errors="ignore")


class ScriptedBackend(BaseModelBackend):
    """按固定剧本回话的假后端，用于离线自测。"""

    def __init__(self) -> None:
        super().__init__(model_type=ModelType.STUB, model_config_dict={})
        self._counter = OfflineTokenCounter()
        self._calls = 0

    @property
    def token_counter(self) -> BaseTokenCounter:
        return self._counter

    def _reply(self, messages: List[Any]) -> str:
        self._calls += 1

        # 找出最后一条非 system 消息，摘一句出来，证明轮次确实在推进。
        excerpt = ""
        for message in reversed(messages):
            if not isinstance(message, dict):
                continue
            if message.get("role") in ("user", "assistant"):
                excerpt = str(message.get("content", "")).replace("\n", " ")[:60]
                break

        return (
            f"{MOCK_BANNER} 第 {self._calls} 次调用。\n\n"
            f"【接住上一轮】你刚才说到「{excerpt}…」。\n\n"
            f"【模拟观点】这里本应是结合人格与议题展开的具体论证，"
            f"模拟模式下不产生任何真实营养学结论。\n\n"
            f"【下一步】把 DEEPSEEK_API_KEY 填进 .env 后重新运行，"
            f"这一段会变成真实的专业发言。"
        )

    def _build(self, messages: List[Any]) -> ChatCompletion:
        return ChatCompletion(
            id=f"mock-{self._calls}",
            created=0,
            model="offline-mock",
            object="chat.completion",
            choices=[
                Choice(
                    finish_reason="stop",
                    index=0,
                    message=ChatCompletionMessage(
                        role="assistant", content=self._reply(messages)
                    ),
                )
            ],
        )

    def _run(
        self,
        messages: List[Any],
        response_format: Optional[Type[Any]] = None,
        tools: Optional[List[Any]] = None,
    ) -> ChatCompletion:
        return self._build(messages)

    async def _arun(
        self,
        messages: List[Any],
        response_format: Optional[Type[Any]] = None,
        tools: Optional[List[Any]] = None,
    ) -> ChatCompletion:
        return self._build(messages)

    @property
    def token_limit(self) -> int:
        return 1_000_000
