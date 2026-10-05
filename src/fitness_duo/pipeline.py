"""完整流程的编排：对谈 → 整理 → 出手册。

这个模块把「跑什么」和「怎么展示」分开。它自己不打印任何东西、也不碰浏览器，
只按顺序调用 society / digest，并在关键节点上回调 `PipelineHooks` 里挂的函数。

于是同一套编排可以有两个前端：

    cli.py       把 hooks 接到 print 上     → 命令行
    launcher.py  把 hooks 接到 SSE 广播上   → 网页

分层红线：本模块**不 import cli，也不 import launcher**。
反向（cli / launcher import pipeline）才是允许的方向。

为什么 `backend` 由调用方构建、而不是这里自己 build_backend：
`MissingApiKey` 的处理方式两个前端不一样——CLI 要退 2 并在 stderr 上印一段提示，
网页要在 worker 里把首行放进状态栏、完整堆栈留在终端。放在外面，两边各自说了算。
测试也方便：直接注入 ScriptedBackend，完全不需要 .env。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Optional, Sequence

from .config import OUTPUT_DIR
from .personas import COACH, NUTRITIONIST
from .topics import CLOSING_TOPIC, TOPICS, Topic, get_topic

if TYPE_CHECKING:  # 只为类型标注，运行时不导入（避免拖进 camel）
    from .society import TopicTranscript, Turn


class RunStopped(RuntimeError):
    """用户中途喊停。

    走到这里就说明这一轮作废——**一个文件都不写**。半成品手册比没有手册更危险：
    它看起来是完整成果，但缺了后面几个议题，容易被当成成品拿走。
    """


@dataclass(frozen=True)
class RunResult:
    transcript_path: Path
    handbook_path: Path
    handbook: str
    transcripts: "tuple[TopicTranscript, ...]"
    sections: tuple[str, ...]
    closing: str


@dataclass
class PipelineHooks:
    """流程各节点的可选回调。全是 None 时就是一个安静的 run。

    每个回调都是一个具名事件，而不是 `on_event(kind, payload)` 那种大分发——
    因为 CLI 也要复用它，写成大分发的话终端那边会退化成一长串 `if kind == ...`，
    还丢掉类型。具名回调让每个 print 直接挂在对应节点上，最容易保证输出逐字不变。
    """

    # (议题总数, 每个议题轮数)
    on_run_start: Optional[Callable[[int, int], None]] = None
    # (第几个·从 1 开始, 总数, 议题)
    on_topic_start: Optional[Callable[[int, int, Topic], None]] = None
    # (议题, 本轮发言) —— 带上议题，网页要按议题分组
    on_turn: Optional[Callable[[Topic, "Turn"], None]] = None
    # (议题, 该议题的完整记录)
    on_topic_done: Optional[Callable[[Topic, "TopicTranscript"], None]] = None
    # (议题, 是不是收尾议题) —— 网页用它显示「整理中…」
    on_section_start: Optional[Callable[[Topic, bool], None]] = None
    # (议题, 是不是收尾议题, 整理出来的 markdown)
    on_section_done: Optional[Callable[[Topic, bool, str], None]] = None
    # (最终结果)
    on_run_done: Optional[Callable[[RunResult], None]] = None


def select_topics(only: str = "") -> list[Topic]:
    """CLI 用的议题选择：`--only a,b` 的语义。

    行为**逐字复刻**原来的 cli._cmd_run，包括一个既有小坑：
    `--only closing` 会得到 [closing, closing]。因为收尾议题是无条件 append 的。
    这个不改——改了就破坏「CLI 输出逐字不变」。网页走 resolve_topics 那条路。
    """
    topics = [get_topic(k) for k in only.split(",")] if only else list(TOPICS)
    topics.append(CLOSING_TOPIC)
    return topics


class InvalidTopic(ValueError):
    """网页提交的自定义议题不合法。"""


#: 自定义议题各字段的长度上限。挡的不是攻击（网页在本机、只影响自己这一次跑），
#: 是「顺手粘一整篇文章进去」——议题描述每轮都要进 system message 发给两个 agent，
#: 撑大了成本是成倍翻的，而且会挤掉真正有用的上下文。
MAX_TITLE = 60
MAX_BRIEF = 400
MAX_GOAL = 200
MAX_OPENING = 1000

DEFAULT_GOAL = "收敛出读者可以直接照做的结论；两位教练没谈拢的地方要如实保留，不要和稀泥。"

DEFAULT_OPENING = "【结论先行】关于「{title}」，我先摆出我的方案。\n\n{brief}\n\n欢迎挑刺。"


def _field(raw: Mapping, name: str, label: str, limit: int) -> str:
    value = raw.get(name)
    if value is None or value == "":
        return ""
    if not isinstance(value, str):
        raise InvalidTopic(f"{label}必须是文本。")
    value = value.strip()
    if len(value) > limit:
        raise InvalidTopic(f"{label}太长了：上限 {limit} 字，你给了 {len(value)} 字。")
    return value


def make_custom_topic(raw: Mapping, *, index: int = 1) -> Topic:
    """把网页表单来的裸数据变成一个 Topic。

    只有「标题」和「要讨论什么」是必填的——另外两项留空就用默认值：
    收敛目标用通用说法，开场白则由议题描述合成一段，免得空着开场把对话卡在第一步。
    """
    if not isinstance(raw, Mapping):
        raise InvalidTopic("自定义议题的格式不对。")

    title = _field(raw, "title", "标题", MAX_TITLE)
    brief = _field(raw, "brief", "要讨论什么", MAX_BRIEF)
    goal = _field(raw, "goal", "收敛目标", MAX_GOAL)
    opening = _field(raw, "opening", "开场白", MAX_OPENING)

    if not title:
        raise InvalidTopic("自定义议题缺标题。")
    if not brief:
        raise InvalidTopic(f"自定义议题「{title}」没有写要讨论什么。")

    return Topic(
        key=f"custom-{index}",
        section_title=title,
        brief=brief,
        goal=goal or DEFAULT_GOAL,
        opening=opening or DEFAULT_OPENING.format(title=title, brief=brief),
    )


def resolve_topics(
    keys: Optional[Sequence[str]] = None,
    custom: Optional[Sequence[Mapping]] = None,
) -> list[Topic]:
    """网页用的议题选择。

    - `keys=None` 表示「没指定」→ 用全部内置议题；给 `[]` 才是「一个内置的都不要」
    - 自定义议题排在内置议题之后

    收尾议题**无条件挪到最后**，而且只保留一个。这不是排版偏好：它要把前面所有
    议题的对话一起读进去才写得出「分歧备忘」，位置不对内容就是错的。调用方把
    它放进 keys 里（网页上那个固定勾选的框就会这么干）也照样会被挪走。
    """
    base = list(TOPICS) if keys is None else [get_topic(k) for k in keys]
    if custom:
        base = [*base, *(make_custom_topic(raw, index=i) for i, raw in enumerate(custom, 1))]
    body = [t for t in base if t.key != CLOSING_TOPIC.key]
    return [*body, CLOSING_TOPIC]


def run_pipeline(
    *,
    backend,
    rounds: int = 3,
    topics: Optional[Sequence[Topic]] = None,
    out_dir: Optional[Path] = None,
    mock: bool = False,
    hooks: Optional[PipelineHooks] = None,
    should_stop: Optional[Callable[[], bool]] = None,
) -> RunResult:
    """跑完整流程。产物全部跑完才落盘，中途抛异常或喊停都不留半成品。

    `should_stop` 的检查点只有两处：议题开始前、某个议题对谈结束后。
    所以喊停最坏要等当前这一轮对谈跑完（society.step() 内部是两次模型调用，
    没法从中间掐断）。
    """
    # 懒 import：这两个模块会拖进 camel，而 `fitness-duo topics` 这种子命令
    # 不该为一个议题表付 camel 的导入开销。原来的 cli._cmd_run 也是这么做的。
    from . import digest, society

    topics = list(topics) if topics else select_topics("")
    total = len(topics)
    h = hooks or PipelineHooks()

    if h.on_run_start:
        h.on_run_start(total, rounds)

    transcripts: list["TopicTranscript"] = []
    sections: list[str] = []
    # 只有收尾议题会赋真值；先给个初值，免得议题表不以收尾结尾时
    # 末尾 assemble(sections, closing) 直接 NameError。
    closing = ""

    for index, topic in enumerate(topics, 1):
        if should_stop and should_stop():
            raise RunStopped()

        is_closing = topic.key == CLOSING_TOPIC.key
        if h.on_topic_start:
            h.on_topic_start(index, total, topic)

        transcript = society.debate_topic(
            topic,
            backend=backend,
            rounds=rounds,
            # 默认参数绑住当前 topic，不然闭包会捕获循环变量
            on_turn=(lambda t, _topic=topic: h.on_turn(_topic, t)) if h.on_turn else None,
            should_stop=should_stop,
        )
        transcripts.append(transcript)
        if h.on_topic_done:
            h.on_topic_done(topic, transcript)

        if should_stop and should_stop():
            raise RunStopped()

        if h.on_section_start:
            h.on_section_start(topic, is_closing)

        if is_closing:
            # 收尾议题要把前面所有议题的结论一起喂进去，否则写不出「分歧备忘」
            context = "\n\n".join(t.as_dialogue() for t in transcripts)
            closing = digest.digest_closing(context, backend=backend)
            produced = closing
        else:
            produced = digest.digest_section(topic, transcript.as_dialogue(), backend=backend)
            sections.append(produced)

        if h.on_section_done:
            h.on_section_done(topic, is_closing, produced)

    # ---- 落盘 ----
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    out = Path(out_dir) if out_dir else OUTPUT_DIR
    out.mkdir(parents=True, exist_ok=True)

    suffix = ".mock" if mock else ""
    transcript_path = out / f"transcript-{stamp}{suffix}.md"
    handbook_path = out / f"handbook-{stamp}{suffix}.md"

    # 只用称呼，不带上 title——「营养师（循证运动营养师，负责…）」这种自我重复读着别扭
    header = digest.HEADER.format(a=NUTRITIONIST.name, b=COACH.name)
    if mock:
        header = "> ⚠️ **这是模拟模式生成的占位手册，不含任何真实营养学内容。**\n\n" + header

    handbook = digest.assemble(sections, closing, header=header)

    transcript_path.write_text(
        "\n\n".join(t.to_markdown() for t in transcripts), encoding="utf-8"
    )
    handbook_path.write_text(handbook, encoding="utf-8")

    result = RunResult(
        transcript_path=transcript_path,
        handbook_path=handbook_path,
        handbook=handbook,
        transcripts=tuple(transcripts),
        sections=tuple(sections),
        closing=closing,
    )
    if h.on_run_done:
        h.on_run_done(result)
    return result
