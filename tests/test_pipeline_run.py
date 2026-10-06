"""pipeline 层（编排）的离线测试。

不联网、不需要 API key——全部走 ScriptedBackend。

其中一个重点是**黄金回归**：cli.py 从「自己写编排」改成「把 pipeline 的 hook
接到 print 上」之后，终端输出必须逐字不变。ScriptedBackend 是确定性的
（模板 + 调用计数），所以这段输出完全可复现，可以钉死在测试里。
"""

from __future__ import annotations

import re

import pytest

from fitness_duo import digest, pipeline, profile, society
from fitness_duo.backends import ScriptedBackend
from fitness_duo.personas import COACH, NUTRITIONIST
from fitness_duo.topics import CLOSING_TOPIC, TOPICS


@pytest.fixture
def backend() -> ScriptedBackend:
    return ScriptedBackend()


# ---------------------------------------------------------------------------
# 议题选择
# ---------------------------------------------------------------------------

def test_select_topics_default_includes_closing():
    keys = [t.key for t in pipeline.select_topics("")]
    assert keys == [t.key for t in TOPICS] + [CLOSING_TOPIC.key]


def test_select_topics_only_appends_closing():
    keys = [t.key for t in pipeline.select_topics("baseline,reality")]
    assert keys == ["baseline", "reality", CLOSING_TOPIC.key]


def test_select_topics_preserves_closing_quirk():
    """CLI 的既有行为：--only closing 会得到两份收尾议题。

    收尾议题是无条件 append 的。这个坑**刻意保留**——修了就不再是「CLI 输出
    逐字不变」。网页走 resolve_topics，那条路会去重。
    """
    keys = [t.key for t in pipeline.select_topics("closing")]
    assert keys == ["closing", "closing"]


def test_resolve_topics_dedupes_closing():
    keys = [t.key for t in pipeline.resolve_topics(["baseline", "closing"])]
    assert keys == ["baseline", "closing"]


def test_resolve_topics_appends_closing_when_missing():
    keys = [t.key for t in pipeline.resolve_topics(["baseline"])]
    assert keys == ["baseline", "closing"]


def test_resolve_topics_none_keys_means_all_builtins():
    assert [t.key for t in pipeline.resolve_topics()] == [t.key for t in TOPICS] + ["closing"]


def test_resolve_topics_empty_keys_means_no_builtins():
    """`[]` 和 `None` 不是一回事：前者是「一个内置的都不要」，后者是「没指定」。

    网页上把内置议题全取消勾选、只留自定义议题时会走到这里；如果按 falsy 处理，
    会悄悄把五个内置议题全跑一遍。
    """
    assert [t.key for t in pipeline.resolve_topics([])] == ["closing"]


# ---------------------------------------------------------------------------
# 自定义议题（网页表单来的）
# ---------------------------------------------------------------------------

def test_make_custom_topic_fills_defaults():
    topic = pipeline.make_custom_topic(
        {"title": "六、外食党的早餐", "brief": "只能买着吃的人怎么凑够蛋白质？"}, index=3
    )
    assert topic.key == "custom-3"
    assert topic.section_title == "六、外食党的早餐"
    assert topic.goal == pipeline.DEFAULT_GOAL
    # 开场白留空时必须合成一段，否则对话第一步就没有种子文本
    assert "六、外食党的早餐" in topic.opening
    assert "只能买着吃的人怎么凑够蛋白质？" in topic.opening


def test_make_custom_topic_keeps_supplied_goal_and_opening():
    topic = pipeline.make_custom_topic({
        "title": "标题", "brief": "议题", "goal": "我的目标", "opening": "我的开场",
    })
    assert topic.goal == "我的目标"
    assert topic.opening == "我的开场"


def test_make_custom_topic_trims_whitespace():
    topic = pipeline.make_custom_topic({"title": "  标题  ", "brief": "  议题  "})
    assert (topic.section_title, topic.brief) == ("标题", "议题")


@pytest.mark.parametrize("raw, keyword", [
    ({}, "标题"),
    ({"title": "只有标题"}, "要讨论什么"),
    ({"brief": "只有描述"}, "标题"),
    ({"title": "   ", "brief": "  "}, "标题"),
])
def test_make_custom_topic_rejects_incomplete(raw, keyword):
    with pytest.raises(pipeline.InvalidTopic) as exc:
        pipeline.make_custom_topic(raw)
    assert keyword in str(exc.value)


@pytest.mark.parametrize("field, limit", [
    ("title", pipeline.MAX_TITLE),
    ("brief", pipeline.MAX_BRIEF),
    ("goal", pipeline.MAX_GOAL),
    ("opening", pipeline.MAX_OPENING),
])
def test_make_custom_topic_rejects_overlong(field, limit):
    raw = {"title": "标题", "brief": "议题", field: "字" * (limit + 1)}
    with pytest.raises(pipeline.InvalidTopic) as exc:
        pipeline.make_custom_topic(raw)
    assert str(limit) in str(exc.value)


def test_make_custom_topic_rejects_non_text():
    with pytest.raises(pipeline.InvalidTopic):
        pipeline.make_custom_topic({"title": ["不是字符串"], "brief": "议题"})


def test_make_custom_topic_accepts_exactly_at_limit():
    """边界：正好等于上限要放行，不能差一位。"""
    topic = pipeline.make_custom_topic({"title": "字" * pipeline.MAX_TITLE, "brief": "议题"})
    assert len(topic.section_title) == pipeline.MAX_TITLE


def test_resolve_topics_puts_custom_after_builtin_before_closing():
    keys = [
        t.key
        for t in pipeline.resolve_topics(["baseline"], [{"title": "甲", "brief": "甲议题"},
                                                       {"title": "乙", "brief": "乙议题"}])
    ]
    assert keys == ["baseline", "custom-1", "custom-2", "closing"]


def test_resolve_topics_custom_only():
    keys = [t.key for t in pipeline.resolve_topics([], [{"title": "甲", "brief": "甲议题"}])]
    assert keys == ["custom-1", "closing"]


def test_resolve_topics_keeps_closing_last_even_if_caller_includes_it():
    """回归：网页把固定勾选的收尾议题一起发过来时，它曾经被自定义议题挤到中间。

    收尾议题要靠前面所有议题的对话才能写出分歧备忘，位置错了内容是错的，
    所以这个不变量由后端强制，不指望调用方守规矩。
    """
    keys = [t.key for t in pipeline.resolve_topics(
        ["baseline", "closing"], [{"title": "甲", "brief": "甲议题"}]
    )]
    assert keys == ["baseline", "custom-1", "closing"]


def test_resolve_topics_collapses_duplicate_closing():
    keys = [t.key for t in pipeline.resolve_topics(["closing", "baseline", "closing"])]
    assert keys == ["baseline", "closing"]


def test_run_pipeline_with_custom_topic(backend, tmp_path):
    """自定义议题要真的进到流程里：实录里得出现它自己的标题。"""
    topics = pipeline.resolve_topics([], [{"title": "六、外食党的早餐", "brief": "买着吃怎么凑蛋白质"}])
    result = pipeline.run_pipeline(
        backend=backend, rounds=1, topics=topics, out_dir=tmp_path, mock=True
    )

    transcript = result.transcript_path.read_text(encoding="utf-8")
    assert "## 议题：六、外食党的早餐" in transcript
    assert "买着吃怎么凑蛋白质" in transcript
    assert len(result.transcripts) == 2  # 自定义议题 + 收尾议题


# ---------------------------------------------------------------------------
# run_pipeline 本身
# ---------------------------------------------------------------------------

def test_run_pipeline_writes_files(backend, tmp_path):
    result = pipeline.run_pipeline(
        backend=backend,
        rounds=1,
        topics=pipeline.resolve_topics(["baseline"]),
        out_dir=tmp_path,
        mock=True,
    )

    assert result.transcript_path.exists()
    assert result.handbook_path.exists()
    assert result.transcript_path.name.startswith("transcript-")
    assert result.transcript_path.name.endswith(".mock.md")
    assert "不构成医疗建议" in result.handbook
    assert "模拟模式生成的占位手册" in result.handbook


def test_run_pipeline_without_mock_has_no_suffix(backend, tmp_path):
    result = pipeline.run_pipeline(
        backend=backend,
        rounds=1,
        topics=pipeline.resolve_topics(["baseline"]),
        out_dir=tmp_path,
        mock=False,
    )
    assert not result.transcript_path.name.endswith(".mock.md")
    assert "模拟模式生成的占位手册" not in result.handbook


def test_run_pipeline_emits_hooks_in_order(backend, tmp_path):
    seen: list[str] = []
    hooks = pipeline.PipelineHooks(
        on_run_start=lambda total, rounds: seen.append(f"run_start:{total}:{rounds}"),
        on_topic_start=lambda i, total, topic: seen.append(f"topic_start:{i}/{total}:{topic.key}"),
        on_turn=lambda topic, turn: seen.append(f"turn:{topic.key}:{turn.speaker}"),
        on_topic_done=lambda topic, tr: seen.append(f"topic_done:{topic.key}:{len(tr.turns)}"),
        on_section_start=lambda topic, closing: seen.append(f"section_start:{topic.key}:{closing}"),
        on_section_done=lambda topic, closing, md: seen.append(f"section_done:{topic.key}:{closing}"),
        on_run_done=lambda result: seen.append("run_done"),
    )

    pipeline.run_pipeline(
        backend=backend,
        rounds=1,
        topics=pipeline.resolve_topics(["baseline"]),
        out_dir=tmp_path,
        mock=True,
        hooks=hooks,
    )

    # 一个议题：1 条开场 + 1 轮 × 2 人 = 3 条发言
    assert seen == [
        "run_start:2:1",
        "topic_start:1/2:baseline",
        "turn:baseline:营养师",
        "turn:baseline:教练",
        "turn:baseline:营养师",
        "topic_done:baseline:3",
        "section_start:baseline:False",
        "section_done:baseline:False",
        "topic_start:2/2:closing",
        "turn:closing:营养师",
        "turn:closing:教练",
        "turn:closing:营养师",
        "topic_done:closing:3",
        "section_start:closing:True",
        "section_done:closing:True",
        "run_done",
    ]


def test_run_pipeline_stop_writes_nothing(backend, tmp_path):
    """中途喊停 = 这一轮作废，一个文件都不写（半成品手册比没有更危险）。"""
    with pytest.raises(pipeline.RunStopped):
        pipeline.run_pipeline(
            backend=backend,
            rounds=1,
            topics=pipeline.resolve_topics(["baseline"]),
            out_dir=tmp_path,
            mock=True,
            should_stop=lambda: True,
        )
    assert list(tmp_path.iterdir()) == []


def test_run_pipeline_stop_after_first_topic_writes_nothing(backend, tmp_path):
    """跑完一个议题再喊停，同样不落盘——落盘只发生在整个循环之后。"""
    calls = {"n": 0}

    def stop() -> bool:
        calls["n"] += 1
        # 第 1 次问（议题开始前）放行，之后一律停
        return calls["n"] > 1

    with pytest.raises(pipeline.RunStopped):
        pipeline.run_pipeline(
            backend=backend,
            rounds=1,
            topics=pipeline.resolve_topics(["baseline", "reality"]),
            out_dir=tmp_path,
            mock=True,
            should_stop=stop,
        )
    assert list(tmp_path.iterdir()) == []


def test_run_pipeline_closing_sees_all_previous_dialogue(backend, tmp_path, monkeypatch):
    """收尾议题要把前面所有议题的对话一起喂进去，否则写不出分歧备忘。"""
    captured: dict[str, str] = {}

    def fake_closing(dialogue: str, *, backend, reader_facts: str = ""):
        captured["context"] = dialogue
        return "## 附录 A\n（假）"

    monkeypatch.setattr(digest, "digest_closing", fake_closing)

    pipeline.run_pipeline(
        backend=backend,
        rounds=1,
        topics=pipeline.resolve_topics(["baseline", "reality"]),
        out_dir=tmp_path,
        mock=True,
    )

    # context 是纯对话正文（「说话人：内容」），不含章节标题，
    # 所以用各议题开场白里的特征片段当指纹。
    context = captured["context"]
    assert "我的做法是：花两周时间称重记录" in context  # baseline 的开场白
    assert "外卖优先选能看清食材构成的" in context  # reality 的开场白
    assert "哪些结论是我们都真的认同的" in context  # 收尾议题自己也参与了


# ---------------------------------------------------------------------------
# 读者档案（定制手册）
# ---------------------------------------------------------------------------

@pytest.fixture
def prof() -> profile.Profile:
    return profile.parse_profile({
        "goal_key": "fat_loss",
        "crowd_key": "office",
        "sex": "男",
        "age": 32,
        "height_cm": 175,
        "weight_kg": 82,
    })


def test_no_profile_and_empty_profile_are_the_same_run(backend, tmp_path):
    """「没填档案」和「填了一张空表」必须走出同一份手册。

    网页永远会带一个 profile 键（哪怕是空对象），CLI 不带。
    这两条路径不归一的话，同一个输入会因为入口不同而产出不同的东西。
    """
    # 两次跑各用一个全新的 ScriptedBackend：它是**有状态**的（回复里带调用计数），
    # 共用一个的话第二次的文本天然不同，比出来的差异与档案无关。
    def run(out, **kwargs):
        return pipeline.run_pipeline(
            backend=ScriptedBackend(), rounds=1,
            topics=pipeline.resolve_topics(["baseline"]),
            out_dir=out, mock=True, **kwargs,
        )

    without = run(tmp_path)
    empty = run(tmp_path / "b", profile=profile.Profile())
    assert without.handbook == empty.handbook
    assert "本手册对应的档案" not in without.handbook
    assert "本手册对应的档案" not in empty.handbook


def test_profile_lands_in_the_handbook_header(backend, tmp_path, prof):
    result = pipeline.run_pipeline(
        backend=backend, rounds=1, topics=pipeline.resolve_topics(["baseline"]),
        out_dir=tmp_path, mock=True, profile=prof,
    )
    handbook = result.handbook
    assert "本手册对应的档案" in handbook
    assert "- 目标：减脂减重" in handbook
    assert "BMI 26.8，超重" in handbook
    assert "131–180 g/天" in handbook
    # 派生数值是代码算的、不在对话里，必须说清它只是起点
    assert "以正文为准" in handbook
    # 手册开头不能出现 markdown 表格——launcher 的渲染器不认识
    assert "| 项目 |" not in handbook


def test_profile_block_sits_after_the_standard_header(backend, tmp_path, prof):
    """档案要挂在页眉之后、正文之前，别插到章节中间去。"""
    result = pipeline.run_pipeline(
        backend=backend, rounds=1, topics=pipeline.resolve_topics(["baseline"]),
        out_dir=tmp_path, mock=True, profile=prof,
    )
    assert result.handbook.index("# 健身饮食实战手册") < result.handbook.index("本手册对应的档案")
    # 正文第一节在档案块之后（这里拿实际整理出来的那节正文比对，
    # 不写死章节标题——模拟模式下整理环节产出的不是真实标题）
    assert result.handbook.index("本手册对应的档案") < result.handbook.index(
        result.sections[0].strip()
    )


def test_medical_profile_adds_a_deterministic_note(backend, tmp_path):
    prof = profile.parse_profile({"medical": "高血压，正在服药"})
    result = pipeline.run_pipeline(
        backend=backend, rounds=1, topics=pipeline.resolve_topics(["baseline"]),
        out_dir=tmp_path, mock=True, profile=prof,
    )
    assert "执行前请先咨询医生" in result.handbook


def test_no_medical_note_when_nothing_was_filled(backend, tmp_path, prof):
    result = pipeline.run_pipeline(
        backend=backend, rounds=1, topics=pipeline.resolve_topics(["baseline"]),
        out_dir=tmp_path, mock=True, profile=prof,
    )
    assert "执行前请先咨询医生" not in result.handbook


def test_goal_key_lands_in_the_filename(backend, tmp_path, prof):
    """同一个 outputs/ 下会躺着好几个人的手册，只靠时间戳分不出哪份是给谁的。"""
    result = pipeline.run_pipeline(
        backend=backend, rounds=1, topics=pipeline.resolve_topics(["baseline"]),
        out_dir=tmp_path, mock=True, profile=prof,
    )
    assert "-fat_loss" in result.handbook_path.name
    assert "-fat_loss" in result.transcript_path.name
    assert result.handbook_path.name.endswith(".mock.md")


def test_filename_has_no_slug_without_a_goal(backend, tmp_path):
    """没选目标就保持既有命名不变。"""
    result = pipeline.run_pipeline(
        backend=backend, rounds=1, topics=pipeline.resolve_topics(["baseline"]),
        out_dir=tmp_path, mock=True, profile=profile.parse_profile({"age": 30}),
    )
    stamp = result.handbook_path.name.removeprefix("handbook-").removesuffix(".mock.md")
    assert re.fullmatch(r"\d{8}-\d{6}", stamp), f"文件名里多出了别的段：{stamp}"


def test_reader_note_reaches_both_coaches(backend, tmp_path, monkeypatch, prof):
    """档案要进两位教练的 system message——不然数字没有对象。"""
    seen: list[tuple[str, str]] = []
    real = society.build_system_message

    def spy(persona, topic_brief, goal, *, reader_note=""):
        seen.append((persona.name, reader_note))
        return real(persona, topic_brief, goal, reader_note=reader_note)

    monkeypatch.setattr(society, "build_system_message", spy)

    pipeline.run_pipeline(
        backend=backend, rounds=1, topics=pipeline.resolve_topics(["baseline"]),
        out_dir=tmp_path, mock=True, profile=prof,
    )

    # baseline + closing 两个议题，每个议题建两个 agent
    assert len(seen) == 4
    assert {name for name, _ in seen} == {NUTRITIONIST.name, COACH.name}
    for _, note in seen:
        assert "读者档案" in note
        assert "131–180 g/天" in note
        assert "尽量保住肌肉" in note, "目标自带的立场说明也要带上"


def test_reader_facts_reach_the_editor(backend, tmp_path, monkeypatch, prof):
    """整理环节也要拿到档案——它负责把不适合这位读者的建议挑出去。"""
    seen: dict[str, str] = {}

    def fake_section(topic, dialogue, *, backend, reader_facts=""):
        seen[topic.key] = reader_facts
        return "## 节"

    def fake_closing(dialogue, *, backend, reader_facts=""):
        seen["closing"] = reader_facts
        return "## 附录"

    monkeypatch.setattr(digest, "digest_section", fake_section)
    monkeypatch.setattr(digest, "digest_closing", fake_closing)

    pipeline.run_pipeline(
        backend=backend, rounds=1, topics=pipeline.resolve_topics(["baseline"]),
        out_dir=tmp_path, mock=True, profile=prof,
    )

    assert set(seen) == {"baseline", "closing"}
    assert all("减脂减重" in facts for facts in seen.values())
    # 给整理环节的是事实，不带目标立场说明（那份是给两位教练吵架用的）
    assert all("尽量保住肌肉" not in facts for facts in seen.values())


def test_editor_gets_empty_facts_without_a_profile(backend, tmp_path, monkeypatch):
    seen: dict[str, str] = {}

    def fake_section(topic, dialogue, *, backend, reader_facts=""):
        seen["facts"] = reader_facts
        return "## 节"

    monkeypatch.setattr(digest, "digest_section", fake_section)
    monkeypatch.setattr(digest, "digest_closing", lambda d, *, backend, reader_facts="": "## 附录")

    pipeline.run_pipeline(
        backend=backend, rounds=1, topics=pipeline.resolve_topics(["baseline"]),
        out_dir=tmp_path, mock=True,
    )
    assert seen["facts"] == ""


def test_stopping_still_writes_nothing_with_a_profile(backend, tmp_path, prof):
    with pytest.raises(pipeline.RunStopped):
        pipeline.run_pipeline(
            backend=backend, rounds=1, topics=pipeline.resolve_topics(["baseline"]),
            out_dir=tmp_path, mock=True, profile=prof, should_stop=lambda: True,
        )
    assert list(tmp_path.iterdir()) == []


# ---------------------------------------------------------------------------
# CLI 黄金回归：重构后终端输出必须逐字不变
# ---------------------------------------------------------------------------

#: camel 自己往 stdout 打的日志行，不属于本项目的输出，比对前掐掉。
_LOG_LINE = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3} - ")

#: `--mock --only baseline` 应有的完整输出。
#: 发言字数随 ScriptedBackend 的模板走，比定时归一化成 N（否则改个 mock 文案就炸）。
#: 顺序是：营养师开场 → 每轮「教练先说，营养师后答」，1 + 3×2 = 7 条。
_GOLDEN = [
    "!! 模拟模式：不会调用真实模型，产出的是占位内容，没有参考价值。",
    "",
    "人格：营养师（循证运动营养师） × 教练（实战饮食教练）",
    "议题数：2，每个议题 3 轮",
    "",
    "[1/2] 一、先定基准：热量与蛋白质",
    *["    · 营养师 发言 N 字", "    · 教练 发言 N 字"] * 3,
    "    · 营养师 发言 N 字",
    "    对话完成，共 7 条发言",
    "    已整理成手册章节",
    "[2/2] 附录：双教练分歧备忘 · 红线信号 · 每日自检清单",
    *["    · 营养师 发言 N 字", "    · 教练 发言 N 字"] * 3,
    "    · 营养师 发言 N 字",
    "    对话完成，共 7 条发言",
    "    已整理出附录",
    "",
    "对话实录：<OUT>/transcript-<STAMP>.mock.md",
    "成果手册：<OUT>/handbook-<STAMP>.mock.md",
]


def _normalize(raw: str, out_dir) -> list[str]:
    lines = []
    for line in raw.splitlines():
        if _LOG_LINE.match(line):
            continue
        line = re.sub(r"发言 \d+ 字", "发言 N 字", line)
        line = re.sub(r"\d{8}-\d{6}", "<STAMP>", line)
        line = line.replace(str(out_dir), "<OUT>")
        lines.append(line)
    return lines


def test_cli_stdout_is_byte_identical(capsys, tmp_path):
    from fitness_duo import cli

    code = cli.main(["run", "--mock", "--only", "baseline", "--out", str(tmp_path)])
    captured = capsys.readouterr()

    assert code == 0
    assert captured.err == ""
    assert _normalize(captured.out, tmp_path) == _GOLDEN


def test_cli_missing_key_returns_2(capsys, monkeypatch):
    from fitness_duo import cli

    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    code = cli.main(["run"])
    captured = capsys.readouterr()

    assert code == 2
    assert "配置缺失" in captured.err
    assert ".env" in captured.err


def test_cli_no_subcommand_prints_help(capsys):
    from fitness_duo import cli

    assert cli.main([]) == 1
    assert "usage: fitness-duo" in capsys.readouterr().out
