"""原料成分表的测试。

这张表和两位教练的发言是**授权关系**：模型被明令禁止凭记忆报营养数字，
只能引用工具返回的内容。所以表本身出错，或者查询匹配错了对象，
后果比一般的数据 bug 严重——它会以「成分表」的名义被写进手册。
这里的测试基本都在盯这两件事。
"""

from __future__ import annotations

import pytest

from fitness_duo.fooddata import (
    build_food_tools,
    load_table,
    lookup,
    lookup_food,
    search,
    search_food,
    similar,
)

# ---------------------------------------------------------------------------
# 表本身
# ---------------------------------------------------------------------------


#: 表里的合法出处。每一条都必须能回原出处核对，不能有「不知道哪来的」。
KNOWN_SOURCES = (
    "中国食物成分表标准版(第6版)",
    "USDA FoodData Central (SR Legacy)",
)


def test_table_loads_and_every_row_is_complete():
    table = load_table()
    assert len(table) >= 100, "表被截断了？"
    for food in table:
        assert food.name, "有行没名字"
        assert food.category, f"{food.name} 没分类"
        assert food.source in KNOWN_SOURCES, f"{food.name} 出处不明：{food.source!r}"
        assert food.src_code, f"{food.name} 没留编号，回溯不了"


def test_usda_rows_carry_an_fdc_id():
    """USDA 那几行要能用 fdcId 在 fdc.nal.usda.gov 上直接查到。"""
    usda = [f for f in load_table() if f.source.startswith("USDA")]
    assert usda, "USDA 补充行不见了？"
    for food in usda:
        assert food.src_code.startswith("fdcId "), f"{food.name} 的编号不是 fdcId"


def test_renders_say_where_the_number_came_from():
    """两条出处的措辞都要对——USDA 的行不能写成「原书编码」。"""
    assert "中国食物成分表" in lookup_food("鸡胸肉")
    blueberry = lookup_food("蓝莓")
    assert "USDA" in blueberry
    assert "原书" not in blueberry


def test_names_are_unique():
    """重名会让查找结果取决于行序，是不可复现的来源。"""
    names = [f.name for f in load_table()]
    assert len(names) == len(set(names))


def test_no_field_is_negative():
    for food in load_table():
        for field in (
            "energy_kcal", "protein_g", "fat_g", "carb_g", "fiber_g", "sodium_mg",
        ):
            assert getattr(food, field) >= 0, f"{food.name}.{field} 是负数"


def test_energy_is_consistent_with_macronutrients():
    """用四大营养素反推热量，对不上的就是 OCR 出错。

    已知的出错类型是「能量 kcal 和 kJ 两列标反」——那会让热量膨胀 4.18 倍，
    远远超出这个区间的上沿，能被稳定抓住。区间本身放得很宽（0.6–1.4），
    因为膳食纤维、有机酸、糖醇都会让实测热量低于 Atwater 估算，
    叶菜尤其明显（油麦菜 12 kcal vs 估算 16.4）。放窄了会天天误报。
    """
    for food in load_table():
        low = food.protein_g * 4 + food.fat_g * 9 + max(food.carb_g - food.fiber_g, 0) * 4
        high = food.protein_g * 4 + food.fat_g * 9 + food.carb_g * 4
        if high == 0:
            continue  # 纯水之类，没有营养素可推
        ratio = food.energy_kcal / high
        assert 0.6 <= ratio <= 1.4, (
            f"{food.name} 能量 {food.energy_kcal} kcal 与四大营养素推算的 "
            f"{low:.1f}–{high:.1f} 差得太远，疑似 kcal/kJ 标反"
        )


# ---------------------------------------------------------------------------
# 查找
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "query,expected",
    [
        ("鸡胸肉", "鸡胸肉"),
        ("鸡蛋", "鸡蛋(全蛋)"),
        ("米饭(熟)", "米饭(熟)"),
        ("豆腐(北豆腐)", "豆腐(北豆腐)"),
    ],
)
def test_exact_names_resolve(query, expected):
    found = lookup(query)
    assert found is not None and found.name == expected


@pytest.mark.parametrize(
    "query,expected",
    [
        ("鸡胸", "鸡胸肉"),      # 模型常写简称
        ("米饭", "米饭(熟)"),
        ("北豆腐", "豆腐(北豆腐)"),
        ("燕麦", "燕麦片"),
        ("牛奶", "纯牛奶(全脂)"),
    ],
)
def test_shorter_query_matches_a_longer_table_name(query, expected):
    found = lookup(query)
    assert found is not None and found.name == expected


def test_tables_representative_value_wins_for_a_generic_name():
    """「豆腐」该落到代表值，不该落到北豆腐——两者蛋白差 40%。"""
    found = lookup("豆腐")
    assert found is not None and found.name == "豆腐(代表值)"


def test_lookup_refuses_to_feed_a_longer_name_off_a_short_alias():
    """回归：曾经把「蛋白粉」匹配到「鸡蛋白」，返回 11.6 g 的蛋清数据。

    这是最坏的一类错误——查错了比查不到危险，因为模型会以为查到了。
    「鸡蛋白」的别名「蛋白」是「蛋白粉」的子串，所以匹配**不能**允许
    「表里的名字是查询的子串」这个方向。
    """
    assert lookup("蛋白粉") is None
    assert lookup("乳清蛋白粉") is None


def test_supplements_get_an_explicit_refusal_not_a_lookalike():
    text = lookup_food("蛋白粉")
    assert "补剂" in text
    assert "kcal" not in text, "拒绝补剂时不该带任何营养数字"
    assert "鸡蛋白" not in text, "不该把蛋清当作蛋白粉的替身"


@pytest.mark.parametrize("name", ["蛋白粉", "乳清", "肌酸", "BCAA", "左旋肉碱"])
def test_every_supplement_term_is_refused(name):
    assert "补剂" in lookup_food(name)


def test_unknown_food_reports_no_numbers():
    text = lookup_food("鸭脖子")
    assert "查不到" in text
    assert "kcal" not in text
    assert "不要凭记忆" in text


def test_unknown_food_suggests_something_close():
    """落空时要给出相近名称，否则模型只会换个说法继续瞎猜。"""
    assert similar("鸡胸脯") , "相近名称不该为空"
    assert "鸡胸肉" in lookup_food("鸡胸脯")


# ---------------------------------------------------------------------------
# 渲染
# ---------------------------------------------------------------------------


def test_render_states_basis_and_source():
    """数字光有值不够——基准（每 100 克）和出处都必须写出来。"""
    text = lookup_food("鸡胸肉")
    assert "每 100 克可食部" in text
    assert "中国食物成分表" in text
    assert "24.6" in text and "118" in text


def test_render_computes_protein_density_in_code():
    """蛋白密度是算出来的，不是模型算的——顺手验一下算术。"""
    text = lookup_food("鸡胸肉")
    assert "20.8 g/100kcal" in text  # 24.6 / 118 * 100


def test_render_skips_the_portion_math_for_low_protein_foods():
    """黄瓜要 2.5 公斤才凑够 20 g 蛋白，这个数字只会分散注意力。"""
    text = lookup_food("黄瓜")
    assert "凑够 20 g 蛋白" not in text


def test_zero_protein_food_does_not_divide_by_zero():
    text = lookup_food("橄榄油")
    assert "蛋白密度" not in text
    assert "899" in text


# ---------------------------------------------------------------------------
# 工具封装
# ---------------------------------------------------------------------------


def test_search_lists_names_without_numbers():
    text = search_food("牛")
    assert "牛肉(瘦)" in text
    assert "kcal" not in text, "search 只报名称，数字要走 lookup"


def test_search_covers_whole_table_when_empty():
    assert len(search("")) == len(load_table())


def test_tools_are_built_with_openai_schemas():
    tools = build_food_tools()
    assert len(tools) == 2
    names = {tool.get_function_name() for tool in tools}
    assert names == {"lookup_food", "search_food"}
    for tool in tools:
        schema = tool.get_openai_tool_schema()
        assert schema["type"] == "function"
        assert schema["function"]["description"], "没有描述，模型不知道该什么时候调"


def test_each_agent_gets_its_own_tool_objects():
    """两位智能体不共用实例，免得将来在 CAMEL 内部互相串状态。"""
    first, second = build_food_tools(), build_food_tools()
    assert first[0] is not second[0]
