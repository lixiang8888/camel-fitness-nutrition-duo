"""离线自测：不联网、不需要 API key，验证编排链路本身是对的。

跑法：uv run pytest
"""

from __future__ import annotations

import pytest

from fitness_duo import digest, society
from fitness_duo.backends import ScriptedBackend
from fitness_duo.config import MissingApiKey, build_backend
from fitness_duo.personas import CHEN_SHI, LIN_SHU
from fitness_duo.topics import CLOSING_TOPIC, TOPICS, get_topic


@pytest.fixture
def backend() -> ScriptedBackend:
    return ScriptedBackend()


def test_two_personas_are_actually_different():
    """人格设定是这个项目的前提，两者必须真的不同。"""
    assert LIN_SHU.name != CHEN_SHI.name
    assert LIN_SHU.system_message != CHEN_SHI.system_message


def test_topics_are_wellformed():
    keys = [t.key for t in (*TOPICS, CLOSING_TOPIC)]
    assert len(keys) == len(set(keys)), "议题 key 不能重复"
    for topic in (*TOPICS, CLOSING_TOPIC):
        assert topic.opening.strip(), f"{topic.key} 缺少开场白"
        assert topic.goal.strip(), f"{topic.key} 缺少收敛目标"


def test_get_topic_rejects_unknown_key():
    with pytest.raises(KeyError):
        get_topic("不存在的议题")


def test_debate_alternates_and_keeps_opening(backend):
    """开场白必须是林数写的原文，之后严格陈实、林数交替。"""
    topic = TOPICS[0]
    transcript = society.debate_topic(topic, backend=backend, rounds=2)

    # 1 条开场 + 2 轮 × 2 人
    assert len(transcript.turns) == 5

    speakers = [t.speaker for t in transcript.turns]
    assert speakers == [
        LIN_SHU.name,
        CHEN_SHI.name,
        LIN_SHU.name,
        CHEN_SHI.name,
        LIN_SHU.name,
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
