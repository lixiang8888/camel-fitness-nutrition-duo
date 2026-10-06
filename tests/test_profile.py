"""读者档案的离线测试：预设表、校验、派生数值、两段渲染。

不联网、不需要 API key。
"""

from __future__ import annotations

import pytest

from fitness_duo import pipeline, profile
from fitness_duo.profile import CROWDS, GOALS, InvalidProfile, Profile

# ---------------------------------------------------------------------------
# 预设表
# ---------------------------------------------------------------------------

def test_preset_keys_are_unique():
    for table in (GOALS, CROWDS):
        keys = [p.key for p in table]
        assert len(keys) == len(set(keys)), "预设 key 不能重复"


def test_every_preset_is_filled_in():
    for preset in (*GOALS, *CROWDS):
        assert preset.label.strip(), f"{preset.key} 缺少 label"
        if preset.key != "custom":
            # 非 custom 的预设必须带 brief：它就是进 system message 的那句话，
            # 空着等于两位教练只看到一个光秃秃的目标名
            assert preset.brief.strip(), f"{preset.key} 缺少 brief"


def test_custom_preset_is_last_in_both_tables():
    """「其他（自己写）」必须排在最后——网页按顺序渲染 chips，它应该垫底。"""
    assert GOALS[-1].key == "custom"
    assert CROWDS[-1].key == "custom"


def test_goal_presets_cover_the_goals_users_asked_for():
    """用户点名要的两个：减重和大量增肌。"""
    assert "fat_loss" in profile.GOALS_BY_KEY
    assert "muscle_gain" in profile.GOALS_BY_KEY


def test_bonus_topics_fit_pipeline_limits():
    """专属议题是以「预填的自定义议题」的身份走网页的，必须过 pipeline 的校验。"""
    for goal in GOALS:
        if goal.bonus is None:
            continue
        bonus = goal.bonus
        assert bonus.title.strip(), f"{goal.key} 的专属议题缺标题"
        assert bonus.brief.strip(), f"{goal.key} 的专属议题缺讨论内容"
        assert bonus.goal.strip(), f"{goal.key} 的专属议题缺收敛目标"
        assert bonus.opening.strip(), f"{goal.key} 的专属议题缺开场白"
        assert len(bonus.title) <= pipeline.MAX_TITLE
        assert len(bonus.brief) <= pipeline.MAX_BRIEF
        assert len(bonus.goal) <= pipeline.MAX_GOAL
        assert len(bonus.opening) <= pipeline.MAX_OPENING


def test_every_goal_has_a_bonus_topic_except_custom():
    """每个现实目标都该有专属议题——这正是「不同目标讨论的东西不同」的落点。"""
    for goal in GOALS:
        if goal.key == "custom":
            assert goal.bonus is None
        else:
            assert goal.bonus is not None, f"{goal.key} 没有专属议题"


# ---------------------------------------------------------------------------
# 校验
# ---------------------------------------------------------------------------

def test_parse_empty_mapping_is_empty_profile():
    prof = profile.parse_profile({})
    assert isinstance(prof, Profile)
    assert prof.is_empty()
    assert prof.summary() == ""
    assert prof.for_agents() == ""
    assert prof.facts_markdown() == ""


def test_parse_rejects_non_mapping():
    with pytest.raises(InvalidProfile):
        profile.parse_profile(["不是字典"])


def test_parse_trims_whitespace():
    prof = profile.parse_profile({"notes": "  周末有饭局  "})
    assert prof.notes == "周末有饭局"


def test_parse_accepts_numeric_strings():
    """网页表单发过来的一律是字符串。"""
    prof = profile.parse_profile({"age": "32", "height_cm": "175", "weight_kg": "82.5"})
    assert (prof.age, prof.height_cm, prof.weight_kg) == (32, 175.0, 82.5)


def test_parse_rejects_bool_as_number():
    """Python 里 bool 是 int 的子类，不显式挡掉的话 true 会被当成 1 岁。"""
    with pytest.raises(InvalidProfile) as exc:
        profile.parse_profile({"age": True})
    assert "数字" in str(exc.value)


@pytest.mark.parametrize("field, value, label", [
    ("age", 13, "年龄"),
    ("age", 101, "年龄"),
    ("height_cm", 119, "身高"),
    ("height_cm", 231, "身高"),
    ("weight_kg", 29, "体重"),
    ("weight_kg", 251, "体重"),
])
def test_parse_rejects_out_of_range(field, value, label):
    with pytest.raises(InvalidProfile) as exc:
        profile.parse_profile({field: value})
    assert label in str(exc.value)
    assert "合理范围" in str(exc.value)


@pytest.mark.parametrize("field, value", [
    ("age", 14), ("age", 100),
    ("height_cm", 120), ("height_cm", 230),
    ("weight_kg", 30), ("weight_kg", 250),
])
def test_parse_accepts_exactly_at_the_boundary(field, value):
    """边界值要放行，不能差一位。"""
    assert getattr(profile.parse_profile({field: value}), field) is not None


@pytest.mark.parametrize("value", ["三十岁", [32], {"a": 1}, float("nan"), float("inf")])
def test_parse_rejects_non_numeric(value):
    with pytest.raises(InvalidProfile) as exc:
        profile.parse_profile({"age": value})
    assert "数字" in str(exc.value)


def test_parse_rejects_unknown_goal_with_choices():
    with pytest.raises(InvalidProfile) as exc:
        profile.parse_profile({"goal_key": "减脂"})
    message = str(exc.value)
    assert "未知" in message
    assert "fat_loss" in message, "报错要列出可选值，用户才知道该填什么"


def test_parse_rejects_custom_goal_without_text():
    with pytest.raises(InvalidProfile) as exc:
        profile.parse_profile({"goal_key": "custom"})
    assert "其他" in str(exc.value)


def test_parse_rejects_custom_crowd_without_text():
    with pytest.raises(InvalidProfile) as exc:
        profile.parse_profile({"crowd_key": "custom"})
    assert "其他" in str(exc.value)


def test_parse_accepts_custom_goal_with_text():
    prof = profile.parse_profile({"goal_key": "custom", "goal_text": "备战马拉松"})
    assert prof.goal_label() == "备战马拉松"


@pytest.mark.parametrize("field, limit", [
    ("goal_text", profile.MAX_GOAL_TEXT),
    ("training", profile.MAX_TRAINING),
    ("constraints", profile.MAX_CONSTRAINTS),
    ("medical", profile.MAX_MEDICAL),
    ("notes", profile.MAX_NOTES),
])
def test_parse_rejects_overlong_text(field, limit):
    with pytest.raises(InvalidProfile) as exc:
        profile.parse_profile({field: "字" * (limit + 1)})
    assert str(limit) in str(exc.value)


def test_parse_rejects_non_text():
    with pytest.raises(InvalidProfile):
        profile.parse_profile({"notes": ["不是字符串"]})


def test_parse_rejects_bad_sex():
    with pytest.raises(InvalidProfile) as exc:
        profile.parse_profile({"sex": "男性"})
    assert "性别" in str(exc.value)


@pytest.mark.parametrize("sex", profile.SEX_VALUES)
def test_parse_accepts_known_sex_values(sex):
    assert profile.parse_profile({"sex": sex}).sex == sex


def test_is_empty_false_when_any_field_is_set():
    assert not profile.parse_profile({"notes": "想增重"}).is_empty()
    assert not profile.parse_profile({"age": 30}).is_empty()
    assert not profile.parse_profile({"goal_key": "fat_loss"}).is_empty()


# ---------------------------------------------------------------------------
# 派生数值
# ---------------------------------------------------------------------------

def test_bmi_and_chinese_categories():
    """身高 200cm 时 BMI = 体重 / 4，边界好算。"""
    cases = [
        (56, "偏瘦"),    # BMI 14
        (74, "正常"),    # BMI 18.5 —— 下边界算正常
        (80, "正常"),    # BMI 20
        (96, "超重"),    # BMI 24 —— 下边界算超重
        (110, "超重"),   # BMI 27.5
        (112, "肥胖"),   # BMI 28 —— 下边界算肥胖
    ]
    for weight, expected in cases:
        prof = Profile(height_cm=200, weight_kg=float(weight))
        assert prof.bmi_label == expected, f"{weight}kg/200cm 应该是 {expected}，得到 {prof.bmi_label}"


def test_bmi_needs_both_height_and_weight():
    assert Profile(weight_kg=80).bmi is None
    assert Profile(height_cm=175).bmi is None
    assert Profile(height_cm=175, weight_kg=80).bmi == 26.1


def test_bmr_mifflin_st_jeor():
    male = Profile(sex="男", age=32, height_cm=175, weight_kg=82)
    female = Profile(sex="女", age=32, height_cm=175, weight_kg=82)
    # 男：10*82 + 6.25*175 - 5*32 + 5
    assert male.bmr() == 1759
    # 女：同样条件下减掉 161+5 的性别差
    assert female.bmr() == 1593
    assert male.bmr() - female.bmr() == 166


def test_bmr_returns_none_without_enough_data():
    assert Profile(age=32, height_cm=175, weight_kg=82).bmr() is None, "没填性别就算不了"
    assert Profile(sex="不详", age=32, height_cm=175, weight_kg=82).bmr() is None
    assert Profile(sex="男", height_cm=175, weight_kg=82).bmr() is None, "没填年龄也算不了"


def test_protein_range_pins_the_formula():
    """这个数字必须和 personas.py 里营养师的 1.6–2.2 g/kg 完全一致。"""
    assert Profile(weight_kg=82).protein_range() == (131, 180)
    assert Profile(weight_kg=60).protein_range() == (96, 132)
    assert Profile().protein_range() is None


def test_weekly_rate_depends_on_goal():
    losing = Profile(goal_key="fat_loss", weight_kg=82)
    gaining = Profile(goal_key="muscle_gain", weight_kg=82)
    assert losing.weekly_rate() == (0.41, 0.82)
    assert gaining.weekly_rate() == (0.2, 0.41)
    # 增肌涨得比减脂慢——两条线不是一回事
    assert gaining.weekly_rate()[1] < losing.weekly_rate()[1]


def test_weekly_rate_is_none_without_goal_or_weight():
    assert Profile(weight_kg=82).weekly_rate() is None
    assert Profile(goal_key="fat_loss").weekly_rate() is None
    assert Profile(goal_key="maintain", weight_kg=82).weekly_rate() is None


# ---------------------------------------------------------------------------
# 渲染
# ---------------------------------------------------------------------------

@pytest.fixture
def full() -> Profile:
    return profile.parse_profile({
        "goal_key": "fat_loss",
        "crowd_key": "office",
        "sex": "男",
        "age": 32,
        "height_cm": 175,
        "weight_kg": 82,
        "training": "每周 3 次力量训练，练了半年",
        "constraints": "乳糖不耐；不吃牛肉",
        "notes": "想在三个月内看到明显变化，但周末有饭局",
    })


def test_summary_is_one_line(full):
    summary = full.summary()
    assert summary == "减脂减重 · 上班族 · 外食多 · 男 32 岁 · 175cm/82kg · BMI 26.8（超重）"


def test_summary_omits_what_was_not_filled():
    assert Profile(goal_key="muscle_gain").summary() == "增肌（含大量增肌）"
    assert Profile(sex="女").summary() == "女"


def test_for_agents_carries_goal_brief_and_derived_numbers(full):
    text = full.for_agents()
    assert "读者档案" in text
    # 目标带立场说明，两位教练才有具体的分歧轴
    assert "尽量保住肌肉" in text
    # 处境约束要进 system message——不然「理论上最优」没法被现实反驳
    assert "外卖" in text
    # 派生数值
    assert "131–180 g/天" in text
    assert "0.41–0.82 kg" in text
    assert "1759 千卡/天" in text
    # 落地要求
    assert "不要给通用模板" in text
    assert "不要替他假设" in text


def test_for_agents_mentions_medical_only_when_filled(full):
    assert "医生把关" in full.for_agents()  # 这是「怎么用」里的固定条款
    without = profile.parse_profile({"notes": "想增重"})
    # 「怎么用」里那条固定条款一定会提到「医疗情况」四个字，所以只断言事实行没出现
    assert "- 医疗情况：" not in without.for_agents()
    assert "- 医疗情况：" in Profile(medical="高血压").for_agents()


def test_facts_markdown_is_a_list_not_a_table(full):
    """launcher 的 mdToHtml 不认识表格，用表格会渲染成一行行字面竖线。"""
    text = full.facts_markdown()
    assert "|" not in text, "档案块不能用 markdown 表格"
    assert "- 目标：减脂减重" in text
    assert "- 身高 / 体重：175 cm / 82 kg（BMI 26.8，超重）" in text


def test_facts_markdown_subordinates_derived_numbers_to_the_body(full):
    """派生数值是代码算的、不在对话里，必须说清它只是起点。"""
    text = full.facts_markdown()
    assert "以正文为准" in text
    assert "131–180 g/天" in text


def test_facts_markdown_has_no_goal_brief(full):
    """手册开头只陈列事实；立场说明是给两位教练看的，不该混进成品。"""
    assert "尽量保住肌肉" not in full.facts_markdown()


def test_medical_note_only_when_medical_filled():
    assert Profile(medical="").medical_note() == ""
    note = Profile(medical="高血压，正在服药").medical_note()
    assert "咨询医生" in note
    assert note.count("\n") == 0, "这条提示要能作为单行 blockquote 插进手册"


@pytest.mark.parametrize("text", ["无", "无。", "没有", "暂无", " 无 ", "None", "n/a", "一切正常"])
def test_writing_none_in_medical_does_not_trigger_the_note(text):
    """人家明确说了没事，还回一句「你填写了医疗相关情况」，
    会让人怀疑这份手册根本没读他填的东西。"""
    assert Profile(medical=text).medical_note() == ""


def test_medical_text_is_still_kept_in_the_facts():
    """不提醒归不提醒，事实行还是要照实写出来——两位教练得知道他填了什么。"""
    prof = Profile(medical="无")
    assert "- 医疗情况：无" in prof.facts_markdown()


def test_free_text_without_a_preset_key_is_kept():
    """手写 JSON 时可能只写了文字没给 key，那段文字不能因此消失。"""
    prof = profile.parse_profile({"goal_text": "备战半马，顺便掉点体重"})
    assert not prof.is_empty()
    assert "- 目标：备战半马，顺便掉点体重" in prof.facts_markdown()
    assert "备战半马" in prof.for_agents()
    assert prof.summary() == "备战半马，顺便掉点体重"


def test_facts_markdown_works_with_partial_data():
    """只填了一项也要能出块——不能因为缺身高体重就整块消失。"""
    text = Profile(notes="想增重，但吃不下").facts_markdown()
    assert "- 其他诉求：想增重，但吃不下" in text
    assert "按档案换算出的起点" not in text, "算不出数值时不该留一个空标题"
