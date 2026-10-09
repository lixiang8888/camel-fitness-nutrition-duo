"""CAMEL 双智能体角色扮演编排。

这是项目的 CAMEL 核心。用的就是 CAMEL 论文里的 RolePlaying 结构：
一个 assistant agent（负责产出内容）配一个 user agent（负责提出要求、挑刺），
两边各自带着自己的 system message 互相对话。

两个容易踩的坑，这里都绕开了：
1. camel-ai 0.2.90 的 ChatAgent 已经没有 role_name 参数了（老教程里还有）。
2. RolePlaying 默认会在内部另建 agent 并覆盖 system message。
   只有把 assistant_agent / user_agent 显式传进去，人格设定才会被保留。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Optional

from camel.agents import ChatAgent
from camel.societies import RolePlaying

from .fooddata import build_food_tools
from .personas import COACH, NUTRITIONIST, build_system_message
from .topics import Topic


@dataclass
class Turn:
    speaker: str
    content: str


@dataclass
class TopicTranscript:
    topic: Topic
    turns: list[Turn] = field(default_factory=list)

    def to_markdown(self) -> str:
        lines = [f"## 议题：{self.topic.section_title}", ""]
        for turn in self.turns:
            lines.append(f"**{turn.speaker}**：{turn.content}")
            lines.append("")
        return "\n".join(lines)

    def as_dialogue(self) -> str:
        """给整理阶段用的纯对话文本。"""
        return "\n\n".join(f"{t.speaker}：{t.content}" for t in self.turns)


def _first_text(response) -> str:
    """从 ChatAgentResponse 里安全取文本。"""
    msgs = getattr(response, "msgs", None) or []
    if not msgs:
        return ""
    return str(msgs[0].content).strip()


def debate_topic(
    topic: Topic,
    *,
    backend,
    rounds: int = 3,
    on_turn: Optional[Callable[[Turn], None]] = None,
    should_stop: Optional[Callable[[], bool]] = None,
    reader_note: str = "",
) -> TopicTranscript:
    """让两位教练围绕一个议题对谈若干轮，返回完整记录。

    RolePlaying.step() 的语义是：把消息交给 user agent，再把它的回复交给
    assistant agent。所以一轮里时间顺序是「教练先说，营养师后答」。

    `should_stop` 只在**轮次边界**被问到：一次 step() 内部是两次模型调用，
    没法从中间掐断，所以点了停止最坏要等当前这轮 step 返回才收手。

    `reader_note` 是渲染好的读者档案，原样透传给两边的人格。
    本模块**不认识 Profile 这个类型**——低层只收字符串，好单测。

    两位各配一套 `fooddata` 的查表工具（见 fooddata.build_food_tools）。
    工具调用是模型自己发起的，所以**离线模拟后端不会产生任何工具调用**——
    ScriptedBackend 只回固定文本。档案、议题、工具三者互不依赖，
    这一点让「模拟模式跑通整条链路」这个保证在加了工具之后依然成立。
    """
    # 两位各自拿一份工具，不共用实例。成分表是只读的，共用本来也没事，
    # 但 CAMEL 的 tool 对象在 agent 内部会被引用，分开建省得将来出怪事。
    assistant_agent = ChatAgent(
        system_message=build_system_message(
            NUTRITIONIST, topic.brief, topic.goal, reader_note=reader_note
        ),
        model=backend,
        tools=build_food_tools(),
    )
    user_agent = ChatAgent(
        system_message=build_system_message(
            COACH, topic.brief, topic.goal, reader_note=reader_note
        ),
        model=backend,
        tools=build_food_tools(),
    )

    society = RolePlaying(
        assistant_role_name=NUTRITIONIST.name,
        user_role_name=COACH.name,
        task_prompt=topic.brief,
        # 关掉这两个：它们会在内部再建 agent，超出「只有两位智能体」的设定
        with_task_specify=False,
        with_task_planner=False,
        with_critic_in_the_loop=False,
        model=backend,
        assistant_agent=assistant_agent,
        user_agent=user_agent,
    )

    transcript = TopicTranscript(topic=topic)

    def record(speaker: str, content: str) -> None:
        if not content:
            return
        turn = Turn(speaker=speaker, content=content)
        transcript.turns.append(turn)
        if on_turn:
            on_turn(turn)

    # init_chat 返回的是 assistant（营养师）的开场白
    opening = society.init_chat(init_msg_content=topic.opening)
    record(NUTRITIONIST.name, _first_text(opening) or topic.opening)

    message = opening
    for _ in range(rounds):
        if should_stop is not None and should_stop():
            break

        assistant_response, user_response = society.step(message)

        # 先记教练（user agent），再记营养师（assistant agent），保持时间顺序
        record(COACH.name, _first_text(user_response))
        record(NUTRITIONIST.name, _first_text(assistant_response))

        if assistant_response.terminated or user_response.terminated:
            break

    return transcript
