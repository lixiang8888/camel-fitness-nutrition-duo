"""把对话整理成手册。

注意：这一步不是第三位智能体。它没有独立人格，也不参与讨论，
只是一个格式整理环节——把两位教练已经说过的话，压缩成读者能直接用的条文。
观点全部来自对话本身，整理环节被明确要求不得添加新观点。

读者档案（`reader_facts`）在这里的角色是**取舍镜片，不是内容来源**：
它只用来判断对话里哪几条与该读者无关（忌口、伤病、作息对不上），
以及把贴合他目标的那条排在前面。措辞刻意不写任何祈使句——
一旦出现「请为这位读者给出…」，模型就会开始造对话里没有的东西。

本模块**不认识 `Profile` 这个类型**，只收渲染好的字符串，好单测。
"""

from __future__ import annotations

from camel.agents import ChatAgent

from .topics import Topic

EDITOR_SYSTEM = """你是《健身饮食实战手册》的文字编辑。

你唯一的工作是把两位教练的对话整理成读者可直接照做的条文。

硬性要求：
1. 只整理对话里已经出现过的内容。严禁添加任何新观点、新数据。
2. 用「怎么做」的语气写，不要写成对话实录。
3. 凡是出现数字的地方，必须保留原数字和单位。
4. 两位教练有分歧的地方，必须如实并列写出双方立场，不要擅自调和。
5. 直接输出 Markdown 正文，不要写「好的」「以下是」这类开场白。
6. 如果对话里某一点说得不具体、无法执行，就不要写进手册。"""

#: 只在带读者档案时追加到 EDITOR_SYSTEM 后面。
#: 分成两段常量而不是让模板永远带一个可能为空的占位符——不带档案时
#: system message 和 prompt 都必须与从前**逐字节相同**。
EDITOR_SYSTEM_PROFILE_RULE = """
7. 提示里可能附带「读者档案」。它只用于判断取舍，绝不是内容来源：
   不得据此新增对话里没有出现的建议、数字或方案。"""

SECTION_TEMPLATE = """议题：{title}

议题背景：
{brief}

本轮要收敛的结论：
{goal}

以下是两位教练围绕这个议题的完整对话：

{dialogue}

---

请把它整理成手册的一节。

输出格式（严格遵守）：

## {title}

### 要点
（3–5 条，每条一句话，直接给可执行的做法，带数字）

### 具体怎么做
（分条展开，每条都要具体到读者今天就能照做）

### 两人分歧
（如实写出两位教练没有谈拢的地方，各自立场和理由。
 如果确实完全一致，写「本议题双方无实质分歧」。）

### 注意
（这一节里最容易做错、或者不适合哪些人的地方。没有就写「无」。）

不要使用一级标题，不要重复输出议题名称之外的标题。"""

CLOSING_TEMPLATE = """以下是两位教练在收尾讨论中的完整对话，以及他们此前各议题的结论摘要：

{dialogue}

---

请整理成手册的附录，输出格式（严格遵守）：

## 附录 A · 双教练分歧备忘
（把整个项目中两人始终没有谈拢的点列出来。每条写：分歧点 / 营养师立场 / 教练立场 / 读者该怎么选。
 这是本手册最有价值的部分——如实呈现，不要和稀泥。）

## 附录 B · 红线信号
（出现哪些身体或心理信号时，必须停止当前的饮食方案并去看医生，而不是再调整饮食。
 只写对话里提到的。）

## 附录 C · 每日自检清单
（一份可以每天勾选的清单，用 Markdown 复选框 `- [ ]` 格式，控制在 8 条以内。
 每条必须是能明确判断「做到/没做到」的，不要写「保持好心情」这种无法判定的条目。）"""


def _editor_system(with_profile: bool) -> str:
    """带档案时才追加那条规则，否则 system message 与从前逐字相同。"""
    if with_profile:
        return EDITOR_SYSTEM + EDITOR_SYSTEM_PROFILE_RULE
    return EDITOR_SYSTEM


def _reader_section(facts: str, *, closing: bool = False) -> str:
    """附在 prompt **末尾**的读者档案块。

    两个讲究：

    1. 措辞里**没有一个祈使句**。一旦写成「请为这位读者给出…」，模型就开始造了。
       这里只授权两件事：把相关的优先保留、把明显不适用的略去或写进「注意」。
    2. 追加在末尾而不是插在开头，除了让空档案时的 prompt 逐字节不变，
       还顺带稳住了离线模拟后端——ScriptedBackend 摘的是消息的**前 60 字**，
       动尾部不影响它。
    """
    tail = (
        "\n\n附录 C 的自检清单要贴着这位读者的训练安排、吃饭方式和忌口来写，"
        "每一条仍然必须是能明确判断「做到 / 没做到」的。"
        if closing
        else ""
    )
    return (
        "\n\n【读者档案 —— 只用于取舍，不是内容来源】\n"
        f"{facts.strip()}\n\n"
        "这份档案只描述读者是谁。它唯一的用途是：在上面的对话里挑出与他相关的内容优先保留，\n"
        "明显不适用于他的可以略去，或写进「注意」。严禁依据档案新增任何对话里没有出现过的\n"
        "建议、数字或方案——你写下的每一句都必须能在上面的对话里找到出处。"
        f"{tail}"
    )


def _generate(system: str, prompt: str, backend) -> str:
    agent = ChatAgent(system_message=system, model=backend)
    response = agent.step(prompt)
    msgs = getattr(response, "msgs", None) or []
    return str(msgs[0].content).strip() if msgs else ""


def digest_section(topic: Topic, dialogue: str, *, backend, reader_facts: str = "") -> str:
    prompt = SECTION_TEMPLATE.format(
        title=topic.section_title,
        brief=topic.brief,
        goal=topic.goal,
        dialogue=dialogue,
    )
    has_profile = bool(reader_facts.strip())
    if has_profile:
        prompt += _reader_section(reader_facts)
    return _generate(_editor_system(has_profile), prompt, backend)


def digest_closing(dialogue: str, *, backend, reader_facts: str = "") -> str:
    prompt = CLOSING_TEMPLATE.format(dialogue=dialogue)
    has_profile = bool(reader_facts.strip())
    if has_profile:
        prompt += _reader_section(reader_facts, closing=True)
    return _generate(_editor_system(has_profile), prompt, backend)


HEADER = """# 健身饮食实战手册

> 本手册由两个立场相反的智能体对谈产出：
> **{a}**（负责「什么是对的」）
> ×
> **{b}**（负责「他到底做得到吗」）。
>
> 所有结论都来自两人的分歧与妥协。凡是他们没谈拢的地方，
> 附录 A 里原样保留，没有替你抹平。

**怎么用**：先读第一节定基准，再挑和你目标相符的部分执行。
不要试图一次全部做到。
"""

FOOTER = """
---

*本手册由 camel-fitness-nutrition-duo 生成，内容为一般性健身饮食参考，不构成医疗建议。
如有基础疾病、孕期或进食障碍史，请先咨询医生或注册营养师。*
"""


def assemble(sections: list[str], closing: str, *, header: str = HEADER) -> str:
    body = "\n\n---\n\n".join(s.strip() for s in sections if s.strip())
    parts = [header.strip(), body]
    if closing.strip():
        parts.append(closing.strip())
    parts.append(FOOTER.strip())
    return "\n\n".join(parts) + "\n"
