"""读者档案：把「这个人是谁、想要什么」变成两段确定性文本。

这个模块是「定制手册」的单一出处。它做三件事：

1. **预设表** —— 目标（减脂、增肌、力量表现…）与处境（上班族外食、学生党食堂…）。
   每个预设都带一句 `brief`，会跟着档案一起进 system message，让两位教练有具体的
   立场可吵，而不是对着一个空泛的「我想减脂」说通用建议。
2. **校验** —— `parse_profile()` 把网页表单 / CLI / JSON 来的裸数据变成一个 `Profile`，
   越界就报错（**不做静默 clamp**：1750 手滑打成 175，钳到 230 比报错危险得多）。
3. **两段渲染** —— `for_agents()` 和 `facts_markdown()`。**两个受众、两套措辞，绝不混用**：
   - `for_agents()` 进两位教练的 system message，可以（也应当）引导他们产出针对这个人的新内容；
   - `facts_markdown()` 进手册开头，只是**事实陈列**，显式从属于正文。

关于派生数值（BMI / 蛋白质克数 / 每周安全变化速率）的边界
----------------------------------------------------------
手册正文的红线是「只整理对话里出现过的东西」（见 digest.py）。
而这几行数字是代码算出来的、不在对话里，所以措辞上必须把层级说破：
它们是**换算起点**，不是手册结论，**以正文为准**。同一份数字也会喂给两位教练，
所以他们多半会在对话里正面讨论到它——页眉写区间、正文给结论，两者不打架。

`digest.py` **不 import 这个模块**：它只收渲染好的字符串。
低层保持哑的、可单测的，只有 pipeline 和两个前端知道 `Profile` 存在。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Mapping, Optional


class InvalidProfile(ValueError):
    """网页 / CLI 提交的读者档案不合法。报文要能直接摊给用户看。"""


# ---------------------------------------------------------------------------
# 边界与常量
# ---------------------------------------------------------------------------

#: 文本长度上限。挡的不是攻击（网页在本机，只影响自己这一次跑），
#: 是「顺手粘一整份体检报告进去」——档案每一轮都要进 system message 发给两个 agent，
#: 撑大了成本是成倍翻的，还会挤掉议题本身。思路与 pipeline.MAX_* 一致。
MAX_GOAL_TEXT = 60
MAX_TRAINING = 200
MAX_CONSTRAINTS = 200
MAX_MEDICAL = 200
MAX_NOTES = 500

SEX_VALUES = ("男", "女", "不详")

AGE_RANGE = (14.0, 100.0)
HEIGHT_RANGE = (120.0, 230.0)
WEIGHT_RANGE = (30.0, 250.0)

#: 蛋白质按体重算的区间。与 personas.py 里营养师的立场（1.6–2.2 g/kg）保持一致——
#: 这里不能另立一套，否则页眉和正文第一句就打架。
PROTEIN_PER_KG = (1.6, 2.2)

#: 每周体重安全变化速率（占体重的百分比）。减脂掉快了掉的是肌肉，
#: 增肌涨快了多出来的是脂肪，两条线不是一回事。
RATE_FAT_LOSS = (0.5, 1.0)
RATE_MUSCLE_GAIN = (0.25, 0.5)

_RATE_BY_GOAL: dict[str, tuple[float, float]] = {
    "fat_loss": RATE_FAT_LOSS,
    "recomp": RATE_FAT_LOSS,
    "muscle_gain": RATE_MUSCLE_GAIN,
    "strength": RATE_MUSCLE_GAIN,
}


# ---------------------------------------------------------------------------
# 预设表
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Bonus:
    """目标自带的专属议题。

    它**不是**后端概念——网页把它当成一条预填好的自定义议题（见 launcher.py），
    点了才进议题表、才花钱。所以它必须满足 pipeline 对自定义议题的长度上限。
    """

    title: str
    brief: str
    goal: str
    opening: str


@dataclass(frozen=True)
class Goal:
    key: str
    label: str
    brief: str  # 跟着档案进 system message，说明这个目标的立场
    bonus: Optional[Bonus] = None


@dataclass(frozen=True)
class Crowd:
    key: str
    label: str
    brief: str  # 处境约束：决定了「理论上最优」能不能落地


GOALS: tuple[Goal, ...] = (
    Goal(
        key="fat_loss",
        label="减脂减重",
        brief="以降体脂、降体重为主，尽量保住肌肉和训练表现，不接受极端节食。",
        bonus=Bonus(
            title="减脂平台期与饮食休息怎么安排",
            brief=(
                "减重停滞两三周之后该怎么办？要不要安排「饮食休息」或者高碳日？"
                "怎么区分是真平台期，还是只是水分波动造成的假停滞？"
            ),
            goal=(
                "给出一套判断「真平台期还是正常波动」的标准，"
                "以及确认停滞之后按什么顺序调整——先动什么、后动什么、什么不要动。"
            ),
            opening=(
                "【结论先行】先别急着再砍热量。平台期多数不是「代谢坏了」，"
                "而是摄入被低估、日常活动量悄悄下降，或者水潴留把脂肪的下降盖住了。\n\n"
                "我的做法是先做两周的摄入复核加体重趋势线（看周均值，不看单日），"
                "确认真的停了再动手——而且第一步是加活动量，不是减饭。"
                "热量已经很低的人，反而应该先安排一段饮食休息把摄入提回来。"
            ),
        ),
    ),
    Goal(
        key="muscle_gain",
        label="增肌（含大量增肌）",
        brief="以增加肌肉量和围度为主，接受体重上升，希望多出来的体重尽量长成肌肉而不是脂肪。",
        bonus=Bonus(
            title="增肌期吃不下怎么办",
            brief=(
                "目标热量比平时高好几百大卡、还要吃够蛋白质，很多人根本吃不下。"
                "怎么在不把胃撑爆的前提下把热量吃进去？吃不够的时候先保哪一项？"
            ),
            goal=(
                "给出可执行的高热量密度吃法，以及吃不够时的优先级排序——"
                "热量、蛋白质、训练量三者谁先让步。"
            ),
            opening=(
                "【结论先行】吃不下是个真问题，解决办法是提高能量密度，不是硬塞体积。"
                "同样是 500 大卡，一碗米饭能把人撑死，两勺油加一把坚果轻松就进去了。\n\n"
                "我的优先级是：先把蛋白质保够，热量缺口用液体和脂肪去补，训练量最后调。"
                "但我要先说清楚——判断吃没吃够，看的是每周体重和围度的趋势，不是当天的饱腹感。"
            ),
        ),
    ),
    Goal(
        key="recomp",
        label="减脂增肌同步（体态重组）",
        brief="体重基本不动，目标是同时减掉脂肪、长一点肌肉，靠改变体成分来改善体型。",
        bonus=Bonus(
            title="体重不动，怎么判断方案到底有没有用",
            brief=(
                "体态重组期间体重秤几乎不动，怎么判断方案到底有没有效果？"
                "除了体重还该看哪些指标，多久看一次才不会自己吓自己？"
            ),
            goal=(
                "给出一套体重之外的判据和判定周期，"
                "让读者知道什么时候该继续、什么时候该改方案。"
            ),
            opening=(
                "【结论先行】体重不动不等于没效果——重组期看体重是最差的指标，"
                "因为脂肪在掉、肌肉在长，两者在秤上正好抵消。\n\n"
                "我建议固定条件测围度（同一时间、同一位置）、拍同角度的照片、"
                "记录同样重量下的训练表现，两周看一次趋势，不看单日数字。"
                "真要盯数值就盯腰围和力量，这两个动了就是有效。"
            ),
        ),
    ),
    Goal(
        key="maintain",
        label="保持现状 / 一般健康",
        brief="体重和体型维持现状，重点是吃得健康一点、训练状态稳一点，不追求明显变化。",
        bonus=Bonus(
            title="偶尔吃多了一顿，要不要补回来",
            brief=(
                "维持期偶尔吃多了一顿，第二天要不要刻意少吃或者加练补回来？"
                "「补偿」到底是负责任，还是会把吃饭变成惩罚？"
            ),
            goal=(
                "给出「偶尔吃多」之后的处理原则，"
                "明确哪些做法是必要的、哪些只会把吃饭变成惩罚。"
            ),
            opening=(
                "【结论先行】一顿饭吃不出体型变化，但也不是什么都不用做。"
                "按热量算，多吃的那部分平摊到接下来两三天，每餐少一口主食就抵掉了，"
                "这在数据上是成立的。\n\n"
                "我反对的是第二天饿一整天、或者加练两小时那种极端补偿——"
                "那是惩罚，不是方案，而且很容易引发下一轮失控。"
            ),
        ),
    ),
    Goal(
        key="strength",
        label="提升力量表现",
        brief="目标是三大项成绩和绝对力量提升，训练以大重量低次数为主，吃法服务于训练表现和恢复。",
        bonus=Bonus(
            title="冲大重量和比赛日前后怎么吃",
            brief=(
                "比赛日、或者要冲个人最好成绩的那天，前一天、当天、赛后分别怎么吃？"
                "体重级别的项目要不要控体重？"
            ),
            goal=(
                "给出冲成绩当天和前后各一天的吃法，"
                "以及体重级别项目控重的红线。"
            ),
            opening=(
                "【结论先行】冲大重量的日子不做任何新尝试，只吃你平时吃惯的东西。\n\n"
                "前一天把碳水提上去、纤维降下来，避免肠胃负担；"
                "当天提前三小时吃完主餐，中间用易消化的碳水补；"
                "赛后一小时内补上蛋白质和碳水。\n\n"
                "至于控体重，脱水减重是另一回事——那条线我的建议是："
                "没有专业人士盯着就不要碰。"
            ),
        ),
    ),
    Goal(
        key="endurance",
        label="提升耐力表现",
        brief="目标是跑步、骑行、球类等耐力项目的表现，训练量偏大，吃法服务于训练量和恢复。",
        bonus=Bonus(
            title="长距离训练和比赛的补给怎么安排",
            brief=(
                "长距离训练和比赛当天，碳水该怎么补？"
                "训练中间要不要吃能量胶，隔多久吃一次，怎么避免肠胃出事？"
            ),
            goal=(
                "给出长距离训练和比赛日的碳水补给方案（每小时多少克、什么时候开始），"
                "以及肠胃耐受方面的注意事项。"
            ),
            opening=(
                "【结论先行】耐力项目的核心变量是碳水，不是蛋白质。\n\n"
                "90 分钟以上的训练，每小时 30–60 克碳水是起步，"
                "强度高、时间长的可以往上走，同时要注意钠的补充。\n\n"
                "最重要的一条纪律：所有补给都要在训练里先试一遍，"
                "别到比赛当天第一次吃。能量胶不是必需品，训练里试过没问题再用。"
            ),
        ),
    ),
    Goal(
        key="senior",
        label="中老年健康（防肌少症、控三高）",
        brief=(
            "中老年人群，重点是保住肌肉、维持骨密度和代谢健康，"
            "可能同时有血压、血糖、血脂方面的问题。"
        ),
        bonus=Bonus(
            title="中老年的蛋白质和力量训练怎么配套",
            brief=(
                "中老年人怎么在吃够蛋白质的同时安排力量训练，才能真正把肌肉留住？"
                "每餐怎么分配和一天吃多少，哪个更重要？"
            ),
            goal=(
                "给出中老年可执行的蛋白质分配方案和配套训练建议，"
                "并明确哪些情况必须先问医生。"
            ),
            opening=(
                "【结论先行】年龄上来之后，肌肉对蛋白质的合成反应会变钝，"
                "所以量和分配都要比年轻人讲究。\n\n"
                "总量要比现在普遍吃得高，而且要拆到每餐去——"
                "一顿塞一大块肉，不如分三顿每顿都够量。\n\n"
                "配套上必须说清楚：没有力量训练的刺激，蛋白质吃再多也留不住。"
                "正在用药或者有慢性病的，方案要医生点头再执行。"
            ),
        ),
    ),
    Goal(key="custom", label="其他（自己写）", brief=""),
)

CROWDS: tuple[Crowd, ...] = (
    Crowd(
        key="office",
        label="上班族 · 外食多",
        brief="三餐基本靠外卖和公司周边解决，加班常态化，几乎没有做饭时间，可能还有应酬。",
    ),
    Crowd(
        key="student",
        label="学生党 · 食堂宿舍",
        brief="预算有限，主要吃食堂和便利店，宿舍没有厨房和冰箱。",
    ),
    Crowd(
        key="home",
        label="居家训练",
        brief="在家训练，器械有限（哑铃、弹力带、自重），暂时不去健身房。",
    ),
    Crowd(
        key="gym",
        label="健身房常客",
        brief="有健身房会籍，器械齐全，训练已经比较规律。",
    ),
    Crowd(
        key="travel",
        label="经常出差应酬",
        brief="一个月有相当一部分时间在外地，酒店餐、客户饭局、机场餐是常态。",
    ),
    Crowd(
        key="shift",
        label="倒班 · 作息不规律",
        brief="有夜班或者作息经常变，睡眠时间被切碎，吃饭时间跟着乱。",
    ),
    Crowd(key="custom", label="其他（自己写）", brief=""),
)

GOALS_BY_KEY: dict[str, Goal] = {g.key: g for g in GOALS}
CROWDS_BY_KEY: dict[str, Crowd] = {c.key: c for c in CROWDS}

#: 选了「其他」却没写文字时的报错文案。两个预设表共用一套说法。
_CUSTOM_LABELS = {"goal_key": "目标", "crowd_key": "处境"}

#: 「医疗情况」里表达「没有」的常见写法（已统一成小写、去掉句尾标点）。
#: 命中这些就当作没填，不再附就医提醒。
_NO_MEDICAL = frozenset({
    "无", "没有", "暂无", "无异常", "一切正常", "正常", "无其他", "都没有",
    "none", "no", "n/a", "na", "nil",
})


# ---------------------------------------------------------------------------
# 校验
# ---------------------------------------------------------------------------

def _n(value: float) -> str:
    """82.0 -> '82'，82.5 -> '82.5'。

    不写成 82.0 是因为那是假精确：用户填的是 82，手册不该假装它精确到小数点后一位。
    """
    return f"{value:g}"


def _text(raw: Mapping, name: str, label: str, limit: int) -> str:
    value = raw.get(name)
    if value is None or value == "":
        return ""
    if not isinstance(value, str):
        raise InvalidProfile(f"{label}必须是文本。")
    value = value.strip()
    if len(value) > limit:
        raise InvalidProfile(f"{label}太长了：上限 {limit} 字，你给了 {len(value)} 字。")
    return value


def _num(
    raw: Mapping, name: str, label: str, bounds: tuple[float, float], *, integer: bool = False
):
    value = raw.get(name)
    if value is None or value == "":
        return None
    # 坑：Python 里 bool 是 int 的子类，isinstance(True, int) 为真。
    # 不显式挡掉的话，JSON 里的 true 会被静默当成 1（年龄 1 岁）。
    if isinstance(value, bool):
        raise InvalidProfile(f"{label}必须是数字。")
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise InvalidProfile(f"{label}必须是数字。") from None
    if not math.isfinite(number):  # NaN / inf 都能通过 float()，但拿去算 BMI 就是垃圾
        raise InvalidProfile(f"{label}必须是数字。")
    low, high = bounds
    if not (low <= number <= high):
        # 不 clamp：1750 手滑打成 175 时，钳到 230 会悄悄产出一份基于错数据的方案
        raise InvalidProfile(f"{label}超出合理范围：应在 {_n(low)}–{_n(high)} 之间。")
    return int(number) if integer else round(number, 1)


# ---------------------------------------------------------------------------
# 档案
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Profile:
    """读者档案。所有字段都可选——全空就是「没有档案」，行为与从前逐字一致。"""

    goal_key: str = ""
    goal_text: str = ""
    crowd_key: str = ""
    crowd_text: str = ""
    sex: str = ""  # 男 / 女 / 不详 / 空
    age: Optional[int] = None
    height_cm: Optional[float] = None
    weight_kg: Optional[float] = None
    training: str = ""  # 每周几次、练什么、练了多久
    constraints: str = ""  # 素食 / 乳糖不耐 / 过敏 / 不吃牛羊
    medical: str = ""  # 伤病 / 基础病 / 用药 / 孕期
    notes: str = ""  # 其他小需求

    # ---- 构造 ----

    @classmethod
    def from_mapping(cls, raw: object) -> "Profile":
        if not isinstance(raw, Mapping):
            raise InvalidProfile("读者档案的格式不对。")

        goal_key = _text(raw, "goal_key", "目标", 40)
        crowd_key = _text(raw, "crowd_key", "处境", 40)
        for key, table, field in (
            (goal_key, GOALS_BY_KEY, "goal_key"),
            (crowd_key, CROWDS_BY_KEY, "crowd_key"),
        ):
            if key and key not in table:
                valid = "、".join(table)
                raise InvalidProfile(
                    f"未知的{_CUSTOM_LABELS[field]} {key!r}，可选：{valid}"
                )

        goal_text = _text(raw, "goal_text", "自定义目标", MAX_GOAL_TEXT)
        crowd_text = _text(raw, "crowd_text", "自定义处境", MAX_GOAL_TEXT)
        if goal_key == "custom" and not goal_text:
            raise InvalidProfile("目标选了「其他」，请用一句话说明你想要什么。")
        if crowd_key == "custom" and not crowd_text:
            raise InvalidProfile("处境选了「其他」，请用一句话说明你的情况。")

        sex = _text(raw, "sex", "性别", 8)
        if sex and sex not in SEX_VALUES:
            raise InvalidProfile(f"性别只能是 {'、'.join(SEX_VALUES)}。")

        return cls(
            goal_key=goal_key,
            goal_text=goal_text,
            crowd_key=crowd_key,
            crowd_text=crowd_text,
            sex=sex,
            age=_num(raw, "age", "年龄", AGE_RANGE, integer=True),
            height_cm=_num(raw, "height_cm", "身高", HEIGHT_RANGE),
            weight_kg=_num(raw, "weight_kg", "体重", WEIGHT_RANGE),
            training=_text(raw, "training", "训练情况", MAX_TRAINING),
            constraints=_text(raw, "constraints", "限制与偏好", MAX_CONSTRAINTS),
            medical=_text(raw, "medical", "医疗情况", MAX_MEDICAL),
            notes=_text(raw, "notes", "其他诉求", MAX_NOTES),
        )

    # ---- 基本状态 ----

    def is_empty(self) -> bool:
        """全空 = 没有档案。

        pipeline 会用它把空档案归一成 None，好让「网页永远带一个 profile 键」
        和 CLI 那条路径走进同一个分支。
        """
        return not any((
            self.goal_key, self.goal_text, self.crowd_key, self.crowd_text,
            self.sex, self.age, self.height_cm, self.weight_kg,
            self.training, self.constraints, self.medical, self.notes,
        ))

    def goal_label(self) -> str:
        if self.goal_key == "custom":
            return self.goal_text
        goal = GOALS_BY_KEY.get(self.goal_key)
        # 没选预设但写了文字（手写 JSON）时退回文字，别让摘要里那一项凭空消失
        return goal.label if goal else self.goal_text

    def crowd_label(self) -> str:
        if self.crowd_key == "custom":
            return self.crowd_text
        crowd = CROWDS_BY_KEY.get(self.crowd_key)
        return crowd.label if crowd else self.crowd_text

    # ---- 派生数值（数据不够就返回 None，绝不猜） ----

    @property
    def bmi(self) -> Optional[float]:
        if not self.height_cm or not self.weight_kg:
            return None
        height_m = self.height_cm / 100
        return round(self.weight_kg / (height_m * height_m), 1)

    @property
    def bmi_label(self) -> str:
        value = self.bmi
        if value is None:
            return ""
        if value < 18.5:
            return "偏瘦"
        if value < 24:
            return "正常"
        if value < 28:
            return "超重"
        return "肥胖"

    def bmr(self) -> Optional[int]:
        """Mifflin-St Jeor 基础代谢估算。性别没填就算不了，返回 None。"""
        if self.sex not in ("男", "女"):
            return None
        if self.age is None or self.height_cm is None or self.weight_kg is None:
            return None
        base = 10 * self.weight_kg + 6.25 * self.height_cm - 5 * self.age
        return round(base + (5 if self.sex == "男" else -161))

    def protein_range(self) -> Optional[tuple[int, int]]:
        if not self.weight_kg:
            return None
        return (
            round(self.weight_kg * PROTEIN_PER_KG[0]),
            round(self.weight_kg * PROTEIN_PER_KG[1]),
        )

    def rate_pct(self) -> Optional[tuple[float, float]]:
        """每周体重安全变化速率，占体重的百分比。目标不涉及体重变化时返回 None。"""
        return _RATE_BY_GOAL.get(self.goal_key)

    def weekly_rate(self) -> Optional[tuple[float, float]]:
        """换算成公斤。"""
        pct = self.rate_pct()
        if pct is None or not self.weight_kg:
            return None
        return (
            round(self.weight_kg * pct[0] / 100, 2),
            round(self.weight_kg * pct[1] / 100, 2),
        )

    # ---- 渲染：给人看的一行摘要 ----

    def summary(self) -> str:
        """CLI 和网页共用的一行摘要。没填的部分直接不出现。"""
        parts: list[str] = []
        if self.goal_label():
            parts.append(self.goal_label())
        if self.crowd_label():
            parts.append(self.crowd_label())
        who = " ".join(x for x in (self.sex, f"{self.age} 岁" if self.age else "") if x)
        if who:
            parts.append(who)
        size = "/".join(
            x for x in (
                f"{_n(self.height_cm)}cm" if self.height_cm else "",
                f"{_n(self.weight_kg)}kg" if self.weight_kg else "",
            ) if x
        )
        if size:
            parts.append(size)
        if self.bmi is not None:
            parts.append(f"BMI {_n(self.bmi)}（{self.bmi_label}）")
        return " · ".join(parts)

    # ---- 渲染：事实条目（两段文本共用） ----

    def _fact_lines(self, *, with_brief: bool) -> list[str]:
        lines: list[str] = []

        def preset_line(label: str, key: str, text: str, table: dict) -> None:
            if key == "custom":
                lines.append(f"{label}：{text}")
                return
            preset = table.get(key)
            if preset is None:
                # 只写了自定义文字、没选预设（手写 JSON 会走到这里）：
                # 把文字用上，别因为没配 key 就把它悄悄丢掉
                if text:
                    lines.append(f"{label}：{text}")
                return
            if with_brief and preset.brief:
                lines.append(f"{label}：{preset.label} —— {preset.brief}")
            else:
                lines.append(f"{label}：{preset.label}")

        preset_line("目标", self.goal_key, self.goal_text, GOALS_BY_KEY)
        preset_line("处境", self.crowd_key, self.crowd_text, CROWDS_BY_KEY)

        who = " / ".join(x for x in (self.sex, f"{self.age} 岁" if self.age else "") if x)
        if who:
            lines.append(f"性别 / 年龄：{who}")

        size = " / ".join(
            x for x in (
                f"{_n(self.height_cm)} cm" if self.height_cm else "",
                f"{_n(self.weight_kg)} kg" if self.weight_kg else "",
            ) if x
        )
        if size:
            if self.bmi is not None:
                size += f"（BMI {_n(self.bmi)}，{self.bmi_label}）"
            lines.append(f"身高 / 体重：{size}")

        for label, value in (
            ("训练情况", self.training),
            ("限制与偏好", self.constraints),
            ("医疗情况", self.medical),
            ("其他诉求", self.notes),
        ):
            if value:
                lines.append(f"{label}：{value}")
        return lines

    def _derived_lines(self) -> list[str]:
        lines: list[str] = []
        if (protein := self.protein_range()) is not None:
            lines.append(f"蛋白质参考 {protein[0]}–{protein[1]} g/天（{_n(PROTEIN_PER_KG[0])}–{_n(PROTEIN_PER_KG[1])} g/kg 体重）")
        if (rate := self.weekly_rate()) is not None:
            pct = self.rate_pct() or (0.0, 0.0)
            lines.append(
                f"每周安全变化 {_n(rate[0])}–{_n(rate[1])} kg"
                f"（体重的 {_n(pct[0])}–{_n(pct[1])}%）"
            )
        if (bmr := self.bmr()) is not None:
            lines.append(f"基础代谢约 {bmr} 千卡/天（Mifflin-St Jeor 估算）")
        return lines

    # ---- 渲染：给两位智能体的（可以、也应当定制） ----

    def for_agents(self) -> str:
        """进两位教练的 system message。

        这里**允许**引导他们产出针对这个人的新内容——那正是双人格对话的价值所在。
        所以措辞是施压式的：要求所有建议落到具体数字和处境上，要求正面处理做不到的部分。
        """
        lines = self._fact_lines(with_brief=True)
        if not lines:
            return ""

        parts = [
            "【读者档案 · 这份手册只服务这一个人】",
            "\n".join(f"- {line}" for line in lines),
        ]
        derived = self._derived_lines()
        if derived:
            parts.append(
                "【按档案换算出的起点】（估算，代码算的，不是结论；该反对就反对并说明理由）\n"
                + "\n".join(f"- {line}" for line in derived)
            )
        parts.append(
            "【怎么用这份档案】\n"
            "1. 所有建议都要落到这位读者的具体数字和处境上，不要给通用模板。\n"
            "2. 读者不能吃的、做不到的、身体不允许的，要正面处理并给替代方案，不要绕开。\n"
            "3. 档案里没写的信息不要替他假设；确实需要时明说「这一点要先确认」。\n"
            "4. 如果「医疗情况」一栏不是「无」，涉及它的部分要讲清楚「这需要医生把关」。"
        )
        return "\n\n".join(parts)

    # ---- 渲染：进手册的（只是事实陈列，从属于正文） ----

    def facts_markdown(self) -> str:
        """进手册开头。**只用列表，不用表格。**

        因为 launcher.py 的 mdToHtml 只有标题 / hr / 复选框 / 无序 / 有序 / 引用 / 段落
        七个分支，**没有表格**——markdown 表格会原样渲染成一行行字面竖线。
        """
        lines = self._fact_lines(with_brief=False)
        if not lines:
            return ""

        out = [
            "**本手册对应的档案**（生成时填写，代码据此换算，不是模型写的）",
            "",
            *(f"- {line}" for line in lines),
        ]
        derived = self._derived_lines()
        if derived:
            out += [
                "",
                "**按档案换算出的起点**（估算，仅供两位教练讨论时参考，**以正文为准**）：",
                *(f"- {line}" for line in derived),
            ]
        return "\n".join(out)

    def medical_note(self) -> str:
        """填了医疗情况时附在手册开头的确定性提示。不是模型产物，也不是医嘱。

        写「无」和什么都不写是一个意思——人家明确说了没事，还回一句
        「你填写了医疗相关情况」，会让人怀疑这份手册根本没读他填的东西。
        """
        text = self.medical.strip().lower().rstrip("。. ")
        if not text or text in _NO_MEDICAL:
            return ""
        return (
            "> ⚠️ 你填写了医疗相关情况。以下内容都是一般性参考，"
            "不能替代医生的意见，执行前请先咨询医生或注册营养师。"
        )


def parse_profile(raw: object) -> Profile:
    """把网页表单 / CLI / JSON 来的裸数据变成一个 Profile。"""
    return Profile.from_mapping(raw)
