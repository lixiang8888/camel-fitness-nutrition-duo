"""CAMEL 双智能体角色扮演编排。

这是项目的 CAMEL 核心。用的就是 CAMEL 论文里的 RolePlaying 结构：
一个 assistant agent（负责产出内容）配一个 user agent（负责提出要求、挑刺），
两边各自带着自己的 system message 互相对话。

两个容易踩的坑，这里都绕开了：
1. camel-ai 0.2.90 的 ChatAgent 已经没有 role_name 参数了（老教程里还有）。
2. RolePlaying 默认会在内部另建 agent 并覆盖 system message。
   只有把 assistant_agent / user_agent 显式传进去，人格设定才会被保留。

关于「跑几轮」：camel 的 RolePlaying.step() 本身就是**一轮一调**，它内部不含
循环、也没有 max_turns，跑几轮完全由这里决定。所以 `rounds` 在本模块是**上限**，
不是定额——教练宣布没有新的反对意见就提前收手（见 `_is_concession`）。

顺带一提：`RolePlaying.step()` 返回的 `terminated` 在我们这套配置下**恒为 False**。
camel 的 ChatAgent 默认不装任何 response_terminator，那个标志只会在「上下文超限」
和「外部 stop_event」两种情况下为真，我们两条都没用上。所以下面那句
`if ... .terminated: break` 是一条**保留的安全网**，不是收手机制本身。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Optional

from camel.agents import ChatAgent
from camel.societies import RolePlaying

from .fooddata import build_food_tools
from .personas import COACH, NUTRITIONIST, build_system_message
from .topics import Topic


#: 教练宣布「没有新的反对意见」时说的那句话。看到它这场辩论就收手。
#: 挑刺的一方说没刺可挑了，才算真的到头——营养师是方案提出方，不该主动认输，
#: 所以这条纪律只写进教练的人格（见 personas.COACH）。
NO_MORE_OBJECTION = "【无异议】"

#: 至少跑满这么多轮才允许收手。第 1 轮是「教练挑刺 + 营养师回应」，
#: 是结构上最小的有效交锋，砍了手册那一节就没内容了。
MIN_ROUNDS = 2

#: 判定前从表态两端剥掉的东西：空白、以及模型很爱在句末补的那个句号。
_CONCESSION_PADDING = " \t\n。.！!～~"


@dataclass
class Turn:
    speaker: str
    content: str


@dataclass
class TopicTranscript:
    topic: Topic
    turns: list[Turn] = field(default_factory=list)
    #: 教练表示没有新的反对意见、没跑满就收手了。默认 False，
    #: 既有 `TopicTranscript(topic=...)` 的构造和断言完全不受影响。
    stopped_early: bool = False
    #: 实际跑完的轮数 / 计划的上限轮数。收手那轮的轮号就是 rounds_run。
    rounds_run: int = 0
    planned_rounds: int = 0

    def to_markdown(self) -> str:
        lines = [f"## 议题：{self.topic.section_title}", ""]
        for turn in self.turns:
            lines.append(f"**{turn.speaker}**：{turn.content}")
            lines.append("")
        if self.stopped_early:
            lines.append(
                f"> 教练对当前方案已无新的反对意见，第 {self.rounds_run} 轮后收手"
                f"（上限 {self.planned_rounds} 轮）。"
            )
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


def _is_concession(text: str) -> bool:
    """这一轮发言是不是「没有新的反对意见」的表态。

    **用完全相等，不是包含。** 模型要是写了「【无异议】不过……」这种一长段，
    那说明它其实还有话说——这时候必须当没看见、让辩论继续。两个方向的错代价
    差得很远：漏判只是多花一轮的钱，误判会把一整段发言从手册里抹掉。
    所以这里宁可漏判。

    首尾空白和句末标点忽略掉，因为模型很爱在句末补一个句号。
    """
    return text.strip(_CONCESSION_PADDING) == NO_MORE_OBJECTION


def debate_topic(
    topic: Topic,
    *,
    backend,
    rounds: int = 3,
    on_turn: Optional[Callable[[Turn], None]] = None,
    should_stop: Optional[Callable[[], bool]] = None,
    reader_note: str = "",
    early_stop: bool = False,
    min_rounds: int = MIN_ROUNDS,
) -> TopicTranscript:
    """让两位教练围绕一个议题对谈若干轮，返回完整记录。

    RolePlaying.step() 的语义是：把消息交给 user agent，再把它的回复交给
    assistant agent。所以一轮里时间顺序是「教练先说，营养师后答」。

    `should_stop` 只在**轮次边界**被问到：一次 step() 内部是两次模型调用，
    没法从中间掐断，所以点了停止最坏要等当前这轮 step 返回才收手。

    `reader_note` 是渲染好的读者档案，原样透传给两边的人格。
    本模块**不认识 Profile 这个类型**——低层只收字符串，好单测。

    **`rounds` 是上限，不是定额。** 开着 `early_stop` 时，如果教练明确表示
    对当前方案已经没有新的反对意见（只回一句 `NO_MORE_OBJECTION`），
    这场辩论就到此为止——挑刺的人没刺可挑了，再跑下去只是复读。
    判据和兜底都在 `_is_concession` 里；`min_rounds` 保护第 1 轮那场
    最小有效交锋不被砍掉。

    `early_stop` **默认关**，所以直调本函数的既有测试行为逐字不变；
    pipeline 才是决定开不开的地方（它还要把 mock 模式排除掉）。

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

    transcript = TopicTranscript(topic=topic, planned_rounds=rounds)

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
    for index in range(1, rounds + 1):
        if should_stop is not None and should_stop():
            break

        assistant_response, user_response = society.step(message)

        # 先记教练（user agent），再记营养师（assistant agent），保持时间顺序。
        # **先记录再判断收手**：宣布收手的那一句必须留在 transcript 里，
        # 不能因为「反正要停了」就不记。
        coach_text = _first_text(user_response)
        record(COACH.name, coach_text)
        record(NUTRITIONIST.name, _first_text(assistant_response))
        transcript.rounds_run = index

        # 这条是 camel 自己的终止信号（上下文超限等）。留着当安全网。
        if assistant_response.terminated or user_response.terminated:
            break

        # 教练说没有新的反对意见 → 收手。
        # 注意检查在 step() **之后**：camel 的一轮往返是「教练先说、营养师后答」，
        # 中途掐不断，所以触发收手的那一轮营养师也已经答完了。这样记录里每一轮
        # 都是完整对称的（不会留下半轮），代价只是多一次调用。
        if (
            early_stop
            and index >= min_rounds
            and index < rounds
            and _is_concession(coach_text)
        ):
            transcript.stopped_early = True
            break

    return transcript
