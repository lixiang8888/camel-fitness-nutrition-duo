"""离线自测：不联网、不需要 API key，验证编排链路本身是对的。

跑法：uv run pytest
"""

from __future__ import annotations

import pytest

from fitness_duo import digest, society
from fitness_duo.backends import ScriptedBackend
from fitness_duo.config import MissingApiKey, build_backend
from fitness_duo.personas import COACH, NUTRITIONIST, build_system_message
from fitness_duo.topics import CLOSING_TOPIC, TOPICS, get_topic


@pytest.fixture
def backend() -> ScriptedBackend:
    return ScriptedBackend()


def test_two_personas_are_actually_different():
    """人格设定是这个项目的前提，两者必须真的不同。"""
    assert NUTRITIONIST.name != COACH.name
    assert NUTRITIONIST.system_message != COACH.system_message


def test_topics_are_wellformed():
    keys = [t.key for t in (*TOPICS, CLOSING_TOPIC)]
    assert len(keys) == len(set(keys)), "议题 key 不能重复"
    for topic in (*TOPICS, CLOSING_TOPIC):
        assert topic.opening.strip(), f"{topic.key} 缺少开场白"
        assert topic.goal.strip(), f"{topic.key} 缺少收敛目标"


def test_get_topic_rejects_unknown_key():
    with pytest.raises(KeyError):
        get_topic("不存在的议题")


def test_build_system_message_is_unchanged_without_a_reader_note():
    """加了 reader_note 参数之后，不传它必须还是原来那三段。

    这是黄金回归的一部分：档案功能不能改变「没填档案」那条路径上的任何一个字节。
    """
    expected = (
        f"{NUTRITIONIST.system_message}\n\n"
        "【本次议题】\n议题描述\n\n"
        "【本轮目标】\n本轮目标"
    )
    assert build_system_message(NUTRITIONIST, "议题描述", "本轮目标") == expected
    assert build_system_message(
        NUTRITIONIST, "议题描述", "本轮目标", reader_note=""
    ) == expected


def test_build_system_message_appends_reader_note_last():
    """档案挂在最后：人格 → 议题 → 目标 → 读者是谁。"""
    message = build_system_message(
        COACH, "议题描述", "本轮目标", reader_note="【读者档案】他 32 岁"
    )
    assert message.endswith("【读者档案】他 32 岁")
    assert message.index("【本轮目标】") < message.index("【读者档案】")


def test_debate_alternates_and_keeps_opening(backend):
    """开场白必须是营养师写的原文，之后严格教练、营养师交替。"""
    topic = TOPICS[0]
    transcript = society.debate_topic(topic, backend=backend, rounds=2)

    # 1 条开场 + 2 轮 × 2 人
    assert len(transcript.turns) == 5

    speakers = [t.speaker for t in transcript.turns]
    assert speakers == [
        NUTRITIONIST.name,
        COACH.name,
        NUTRITIONIST.name,
        COACH.name,
        NUTRITIONIST.name,
    ]

    # 开场白原样保留，没有被模型改写
    assert transcript.turns[0].content == topic.opening

    # 后续发言都来自模型后端
    assert all(t.content for t in transcript.turns)


def test_debate_rounds_are_configurable(backend):
    transcript = society.debate_topic(TOPICS[1], backend=backend, rounds=4)
    assert len(transcript.turns) == 1 + 4 * 2


def test_digest_produces_section(backend):
    topic = TOPICS[0]
    transcript = society.debate_topic(topic, backend=backend, rounds=1)
    section = digest.digest_section(topic, transcript.as_dialogue(), backend=backend)
    assert section.strip()


def test_assemble_puts_everything_together():
    handbook = digest.assemble(
        ["## 第一节\n内容甲"],
        "## 附录 A\n内容乙",
        header="# 标题",
    )
    assert "# 标题" in handbook
    assert "内容甲" in handbook
    assert "内容乙" in handbook
    assert "不构成医疗建议" in handbook


def test_mock_backend_never_claims_to_be_real(backend):
    """模拟后端必须在回复里自我标注，避免被误当成真实产出。"""
    transcript = society.debate_topic(TOPICS[0], backend=backend, rounds=1)
    generated = [t.content for t in transcript.turns[1:]]
    assert all("离线模拟回复" in text for text in generated)


def test_missing_key_gives_actionable_error(monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    with pytest.raises(MissingApiKey) as excinfo:
        build_backend(mock=False)
    assert ".env" in str(excinfo.value)


def test_mock_flag_bypasses_key_check(monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    assert isinstance(build_backend(mock=True), ScriptedBackend)
