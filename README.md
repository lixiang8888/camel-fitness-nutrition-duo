# camel-fitness-nutrition-duo

用 **CAMEL** 框架搭的双智能体小练习：请两位立场相反的 AI 教练围绕「健身期间怎么吃」互相辩论，
再把辩论整理成一份普通人能直接照着做的饮食手册。

这个项目的重点不是「让 AI 写篇文章」，而是让两个**真的会吵起来**的人格互相挑刺——
最后交出来的手册里，连「他们没谈拢的地方」也一并交付给你。

---

## 一、两位智能体是谁

人格差异是整个项目的戏眼。如果两个人格只是互相附和，对话会退化成复读机，成果也就没有信息量。

| | **林数** | **陈实** |
|---|---|---|
| 身份 | 循证运动营养师 | 实战饮食教练（十年，学员多为上班族） |
| 立场 | 什么是对的 | 他到底做得到吗 |
| 说话方式 | 给数字：g/kg、百分比、区间、证据强度 | 举场景：加班、外卖、食堂、火锅局、便利店 |
| 信奉 | 热量平衡、蛋白质、能量可用性（RED-S） | 可持续性 > 完美，反对把吃饭变成考试 |
| 会怼什么 | 「排毒」「碱性体质」「局部减脂」 | 任何「理论上可行但你三天就放弃」的方案 |
| 自带的盲区 | 方案常是理论最优，不管执行成本 | 容易为了「能做到」而放松标准 |
| 在 CAMEL 里 | `assistant_agent` | `user_agent` |

两人各自被明确要求**不要掩饰自己的盲区**，也**不许附和对面的漏洞**——
这是让对话产生真实张力的关键。

---

## 二、CAMEL 架构

用的是 CAMEL 论文里的 `RolePlaying` 结构：一个负责产出内容的 assistant agent，
配一个负责提要求和挑刺的 user agent，各自带着自己的 system message 互相对话。

```
                    ┌──────────────────────────────────────┐
                    │   RolePlaying (camel.societies)      │
                    └──────────────────────────────────────┘
                                    │
             init_chat(opening)     │      step() × rounds
                    ┌───────────────┴───────────────┐
                    ▼                               ▼
        ┌────────────────────┐          ┌────────────────────┐
        │  assistant_agent   │          │    user_agent      │
        │  林数 · 循证营养师  │  ──────▶ │  陈实 · 实战教练    │
        │ system_message =   │  ◀────── │ system_message =   │
        │  人格 + 本次议题    │          │  人格 + 本次议题    │
        └────────────────────┘          └────────────────────┘
                    │                               │
                    └───────────────┬───────────────┘
                                    ▼
                      TopicTranscript（逐条发言记录）
                                    │
                                    ▼
                      digest（整理环节，非智能体）
                                    │
                                    ▼
                     transcript-*.md  +  handbook-*.md
```

**流程**：6 个议题 → 每个议题跑一次 `RolePlaying`（默认 3 轮）→ 对话实录落盘 →
整理成手册章节 → 拼装成最终手册。

议题表见 `src/fitness_duo/topics.py`，每个议题都对应手册的一节。
选题标准：**如果两人对这个议题天然意见一致，就不该收进来。**

> **关于整理环节**：`digest.py` 不是第三位智能体。它没有独立人格、不参与讨论，
> 只做格式压缩，并被硬性要求「不得添加对话里没有的观点」。
> 手册里的每一个观点都来自两位教练的发言。

---

## 三、目录结构

```
camel-fitness-nutrition-duo/
├── README.md
├── pyproject.toml              # 依赖与入口；含 mcp<2 的强制约束（见第六节）
├── .env.example                # 复制成 .env 后填 key
├── src/fitness_duo/
│   ├── personas.py             # ★ 两个人格设定
│   ├── topics.py               # ★ 议题表（= 手册大纲）
│   ├── society.py              # ★ CAMEL RolePlaying 编排
│   ├── digest.py               # 对话 → 手册
│   ├── backends.py             # 离线模拟后端（BaseModelBackend 子类）
│   ├── config.py               # 模型工厂
│   └── cli.py                  # 命令行入口
├── tests/
└── outputs/                    # 运行产物
```

---

## 四、快速开始

需要 Python 3.10–3.14（本机 3.14.4 已实测通过）和 [uv](https://docs.astral.sh/uv/)。

```bash
# 1. 装依赖
uv sync

# 2. 配置 key
cp .env.example .env
#    然后把 DEEPSEEK_API_KEY 填进 .env

# 3. 先不花钱试跑一遍（离线模拟，验证链路是否通）
uv run fitness-duo run --mock

# 4. 正式跑，产出真手册
uv run fitness-duo run

# 其他用法
uv run fitness-duo topics                      # 看议题表
uv run fitness-duo run --rounds 5              # 每个议题多吵几轮
uv run fitness-duo run --only baseline,reality # 只跑指定议题
```

跑完在 `outputs/` 下会得到两个文件：

| 文件 | 内容 |
|---|---|
| `transcript-<时间戳>.md` | 两位教练的完整对话实录（想看清他们怎么吵的，看这个） |
| `handbook-<时间戳>.md` | **最终成果**，整理后的《健身饮食实战手册》 |

模拟模式产出的文件带 `.mock` 后缀，且开头有醒目警告——**那份手册是占位内容，没有参考价值**，
它的用途只是让你确认代码链路是通的。

### 换成别的模型

走的是 OpenAI 兼容协议，换 Kimi / 通义 / 本地 Ollama 只需改 `.env` 里两行，代码不用动：

```ini
DEEPSEEK_BASE_URL=https://api.moonshot.cn/v1
DEEPSEEK_MODEL=moonshot-v1-8k
```

---

## 五、手册里有什么

六个议题对应六节，最后一节是附录：

1. **先定基准：热量与蛋白质** — 要不要称重算卡路里
2. **训练日和休息日，吃法要不要变** — 碳水放在什么时间吃
3. **增肌与减脂：热量怎么设，肌肉怎么保** — 速率控制与代谢适应
4. **真实场景生存指南** — 外卖、食堂、加班、便利店、火锅局
5. **补剂红黑榜与常见误区** — 哪些值得买，哪些是智商税
6. **附录 A/B/C** — 双教练分歧备忘、红线信号、每日自检清单

其中**附录 A 是这个项目最有价值的部分**：单人写方案时，
「这里我其实没想清楚」会被无意识地抹平；双人格对话则会把它暴露出来。
手册如实保留了两人始终没谈拢的点，并写明各自立场，让读者自己选。

---

## 六、踩坑记录

这三个坑都是本项目实际踩到并修掉的，照抄网上教程大概率会中招：

**1. `mcp` 版本过新会让 `import camel` 直接崩**

camel-ai 0.2.90 只声明了 `mcp>=1.3.0`，没有上界，于是装到 mcp 2.x。
而 mcp 2.x 移除了 `mcp.server.FastMCP`，camel 内部还在 `from mcp.server import FastMCP`：

```
ImportError: cannot import name 'FastMCP' from 'mcp.server'
```

`pyproject.toml` 里已锁 `mcp>=1.3.0,<2`，别删。

**2. `ChatAgent` 已经没有 `role_name` 参数了**

```
ChatAgent(system_message, model, memory, ...)   # 0.2.90
ChatAgent(role_name, system_message, model...)  # 旧教程里的写法
```

**3. `RolePlaying` 会覆盖你写的人格**

默认情况下 `RolePlaying` 会根据 `assistant_role_name` 自己生成 system message，
你辛苦写的人格设定会被丢掉。必须把构建好的 agent 显式传进去：

```python
RolePlaying(
    assistant_role_name="林数",
    user_role_name="陈实",
    assistant_agent=assistant_agent,   # ← 传了才用你的 system_message
    user_agent=user_agent,
    with_task_specify=False,           # ← 否则内部还会再建 agent
    with_critic_in_the_loop=False,
)
```

另外 `with_task_specify` 默认为 `True`，会在内部再建一个 agent 改写任务，
与「只有两位智能体」的设定不符，所以关掉。

---

## 七、免责声明

本项目是 CAMEL 框架的学习练习，产出的手册为一般性健身饮食参考，**不构成医疗建议**。
如有基础疾病、孕期或进食障碍史，请先咨询医生或注册营养师。
