"""原料成分表：两位教练发言里出现的食物营养数字，出处只有这一个。

## 为什么要有这个模块

在这个模块之前，手册里的食物建议长这样：「便利店两个茶叶蛋加一盒无糖酸奶，
蛋白质约 25 g」。这句话里没有一个数字是查得到的——全是模型凭记忆估的。
问题不在于它估得离谱，而在于**估错了没人发现**：读者照着吃，也不会有人核对。

所以把「食物 → 每 100 克营养成分」这张表交给两位教练，并立一条纪律：
**凡是发言里要出现具体食物的营养数字，先查表**；查不到就说查不到，不许编。
教练还多一条权力——可以拿这张表抽查营养师报的数字。

## 数据从哪来

`data/food_composition.csv`，146 条，绝大部分取自《中国食物成分表标准版(第6版)》
「能量和食物一般营养成分」部分，每行带 `src_code` 可回原书核对。
只挑了健身饮食议题真会用到的食物（蛋白来源、主食、常见蔬果、坚果油脂），
**不是原书全量**——原书 1677 条，全塞进 prompt 会挤掉议题本身。

少数几条来自 USDA FoodData Central（`source` 列会写明）。那是因为这份 OCR 版
缺了调味品和烘焙类，而其中「单一原料、成分跨地区稳定」的（生鲜水果、纯芝麻酱）
可以从 USDA 补——它是公有领域，每行带 fdcId。**配方类产品一律不补**：
美式花生酱加糖加油、美式面包和中式面包差得远，套过来就是「精确的错」。

刻意不收录的东西：蛋白粉、能量胶等补剂。原书没有，而补剂恰恰是最容易被
模型编出「每勺 30 g 蛋白」这类漂亮数字的地方，宁可让它查不到。

## 与 digest 的关系

`digest.py` 的红线是「只整理对话里出现过的内容」。工具返回的数字会出现在教练的
发言里，所以能顺利进手册；**没被说出口的查表结果不会**。这正是想要的——
表是发言的依据，不是手册的第二个内容源。

`digest.py` 同样不 import 这个模块：它只收渲染好的字符串。
"""

from __future__ import annotations

import csv
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

DATA_PATH = Path(__file__).resolve().parent / "data" / "food_composition.csv"

#: 查不到时最多列几个相近名称，让模型换个说法再查（而不是就此编一个数字）。
_SUGGEST_LIMIT = 8

#: 低于这个蛋白含量就不用算「凑 20 g 蛋白要吃多少克」了——
#: 黄瓜要 2.5 公斤，这个数字只会分散注意力。
_PROTEIN_DENSE_MIN = 5.0

#: 归一化时要抹掉的字符。中文食物名里的括号、顿号、空格在用户嘴里不会出现：
#: 用户说「鸡胸肉」，表里写「鸡胸脯肉」；说「豆腐北」，表里写「豆腐(北豆腐)」。
_NOISE = re.compile(r"[\s()（）\[\]［］·,，、;；:：]+")


@dataclass(frozen=True)
class Food:
    """表里的一行。所有数值都是**每 100 克可食部**。"""

    name: str
    alias: tuple[str, ...]
    category: str
    edible_pct: float
    energy_kcal: float
    protein_g: float
    fat_g: float
    carb_g: float
    fiber_g: float
    sodium_mg: float
    source: str
    src_code: str

    def render(self) -> str:
        """给模型看的文本。数字后面一定跟单位和「每 100 克」的基准。"""
        lines = [f"{self.name} · {self.category} · 每 100 克可食部"]
        lines.append(
            f"能量 {_num(self.energy_kcal)} kcal | 蛋白质 {_num(self.protein_g)} g | "
            f"脂肪 {_num(self.fat_g)} g | 碳水 {_num(self.carb_g)} g | "
            f"膳食纤维 {_num(self.fiber_g)} g | 钠 {_num(self.sodium_mg)} mg"
        )
        if self.energy_kcal > 0 and self.protein_g > 0:
            # 蛋白密度由代码算，不由模型算——它算除法经常出错，而且错了看不出来。
            lines.append(f"蛋白密度 {self.protein_g / self.energy_kcal * 100:.1f} g/100kcal")
        if self.protein_g >= _PROTEIN_DENSE_MIN:
            grams = 20 / self.protein_g * 100
            lines.append(
                f"凑够 20 g 蛋白需 {_num(grams)} g（{_num(grams * self.energy_kcal / 100)} kcal）"
            )
        # 写「来源」，不写「原书」——表里有两处出处，USDA 那几行不是书。
        lines.append(f"来源：{self.source}（编号 {self.src_code}）")
        return "\n".join(lines)


def _num(value: float) -> str:
    """118.0 -> '118'，24.6 -> '24.6'。手册里出现 118.0 会显得很假。"""
    return f"{value:.0f}" if abs(value - round(value)) < 0.05 else f"{value:.1f}"


def _norm(text: str) -> str:
    return _NOISE.sub("", str(text)).lower()


def _row_to_food(row: dict[str, str]) -> Food:
    def f(key: str) -> float:
        try:
            return float(row.get(key) or 0)
        except ValueError:
            return 0.0

    alias = tuple(a for a in (row.get("alias") or "").split("|") if a)
    return Food(
        name=row["name"],
        alias=alias,
        category=row.get("category", ""),
        edible_pct=f("edible_pct"),
        energy_kcal=f("energy_kcal"),
        protein_g=f("protein_g"),
        fat_g=f("fat_g"),
        carb_g=f("carb_g"),
        fiber_g=f("fiber_g"),
        sodium_mg=f("sodium_mg"),
        source=row.get("source", ""),
        src_code=row.get("src_code", ""),
    )


@lru_cache(maxsize=1)
def load_table() -> tuple[Food, ...]:
    """读一次，缓存住。表是只读的常量，没有失效问题。"""
    with DATA_PATH.open(encoding="utf-8-sig", newline="") as handle:
        return tuple(_row_to_food(row) for row in csv.DictReader(handle))


def _candidates(food: Food) -> list[str]:
    return [_norm(food.name), *(_norm(a) for a in food.alias)]


def lookup(name: str) -> Food | None:
    """按名称找食物。全等 → 表名包含查询。

    **只允许「表名包含查询」这一个方向，不允许反过来。** 反过来会出一类
    很隐蔽的错误：查询「蛋白粉」时，「鸡蛋白」的别名「蛋白」是它的子串，
    于是返回蛋清的数据，模型还以为查到了，把 11.6 g 当成蛋白粉的蛋白质
    写进手册。宁可查不到——查不到模型会换名称重试，查错了没有任何补救机会。

    放宽的方向只有「模型写得比表短」：「鸡胸」命中「鸡胸肉」、「米饭」命中
    「米饭(熟)」。真写长了（「去皮鸡腿肉」）就落空，靠 not-found 提示里的
    相近名称把它引回来。
    """
    query = _norm(name)
    if not query:
        return None

    table = load_table()
    for food in table:  # 1. 全等（名称或别名）
        if query in _candidates(food):
            return food
    for food in table:  # 2. 表名包含查询。按表内顺序，先写的先命中——
        #                 「豆腐」要落在代表值上，所以代表值在表里排在前面
        if any(query in c for c in _candidates(food)):
            return food
    return None


def similar(name: str, limit: int = _SUGGEST_LIMIT) -> list[str]:
    """查不到时，按「共同汉字数」排几个相近的名称，供模型换个说法。"""
    query = set(_norm(name))
    if not query:
        return []
    scored = []
    for food in load_table():
        hit = len(query & set(_norm(food.name)))
        if hit:
            scored.append((hit, food.name))
    scored.sort(key=lambda pair: (-pair[0], pair[1]))
    return [name for _, name in scored[:limit]]


def search(keyword: str) -> list[Food]:
    """按关键词列出食物（不返回营养数据，只让人知道有什么可查）。"""
    if not keyword.strip():
        return list(load_table())
    query = _norm(keyword)
    return [f for f in load_table() if any(query in c for c in _candidates(f))]


# ---------------------------------------------------------------------------
# 给 CAMEL 的工具。docstring 会被 FunctionTool 拿去当工具描述，
# 所以它是写给模型看的指令，不是写给同事看的文档。
# ---------------------------------------------------------------------------

_NOT_FOUND_HINT = (
    "成分表里没有这个食物。可以换一个更常见的名称再查一次；"
    "如果仍然查不到，就在发言里直说「成分表里没有」，"
    "**不要凭记忆给数字**。"
)

#: 成分表故意不收录的东西。补剂是模型最容易编数字的地方——
#: 「一勺蛋白粉 25 g 蛋白」听着精确，其实各家配方差一倍。
#: 单独拦一道，是为了给出一句明确的拒绝，而不是让它在相近名称里
#: 挑一个凑合（「蛋白粉」和「鸡蛋白」就是这么撞上的）。
_SUPPLEMENTS = (
    "蛋白粉", "乳清", "酪蛋白", "增肌粉", "肌酸", "氮泵", "左旋肉碱",
    "支链氨基酸", "bcaa", "能量胶", "能量棒", "代餐", "燃脂", "补剂",
)

_SUPPLEMENT_REPLY = (
    "「{q}」属于补剂，成分表不收录。补剂的成分各家配方差异很大，"
    "没有统一的每 100 克数值，**不要拿别的食物的数据代替它**。"
    "需要谈补剂时，讲剂量区间和证据强度，不要给具体的营养成分表数字。"
)


def lookup_food(name: str) -> str:
    """查询某种食物每 100 克的营养成分（能量、蛋白质、脂肪、碳水、膳食纤维、钠）。

    发言里只要出现具体食物的营养数字，就必须先调用这个工具取数，不要凭记忆。
    返回的数字来自《中国食物成分表标准版(第6版)》，可以直接引用。
    查不到时返回相近的食物名，换个说法再查一次。

    Args:
        name: 中文食物名，例如「鸡胸肉」「米饭(熟)」「豆腐(北豆腐)」「鸡蛋」。
    """
    query = _norm(name)
    if any(word in query for word in _SUPPLEMENTS):
        return _SUPPLEMENT_REPLY.format(q=name)

    food = lookup(name)
    if food is None:
        hints = similar(name)
        tail = f"表里有这些相近的：{'、'.join(hints)}。" if hints else ""
        return f"「{name}」查不到。{tail}\n{_NOT_FOUND_HINT}"
    return food.render()


def search_food(keyword: str) -> str:
    """列出成分表里名称包含关键词的食物，用来确认「有什么可查」。

    这个工具**不返回营养数字**，只返回名称。想拿数字还得用 lookup_food。
    当你不确定某种食物在不在表里、或者想找同类食物时用它。

    Args:
        keyword: 关键词，例如「鸡」「豆腐」「牛」。传空字符串会列出全部。
    """
    hits = search(keyword)
    if not hits:
        return f"没有名称包含「{keyword}」的食物。"
    names = "、".join(f.name for f in hits)
    return f"共 {len(hits)} 条：{names}"


def build_food_tools() -> list:
    """给一位智能体配的工具集。

    每次调用都新建：两位智能体各拿一份，避免共用实例带来的意外耦合。
    """
    from camel.toolkits import FunctionTool

    return [FunctionTool(lookup_food), FunctionTool(search_food)]
