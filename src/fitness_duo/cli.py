"""命令行入口。

这里只负责「把 pipeline 的回调接到 print 上」和解析参数；编排本身在 pipeline.py。
同样的编排，launcher.py 把它接到网页的 SSE 广播上——两个前端共用一套流程。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Optional

from . import pipeline
from .config import MissingApiKey, build_backend
from .personas import COACH, NUTRITIONIST
from .profile import CROWDS, GOALS, InvalidProfile, Profile, parse_profile
from .topics import CLOSING_TOPIC, TOPICS


def _cmd_topics(_args: argparse.Namespace) -> int:
    print("本项目的议题表（每个议题产出手册的一节）：\n")
    for i, topic in enumerate((*TOPICS, CLOSING_TOPIC), 1):
        print(f"{i}. [{topic.key}] {topic.section_title}")
        print(f"   {topic.brief}\n")
    return 0


def _cmd_goals(_args: argparse.Namespace) -> int:
    print("可以填的目标（--goal）：\n")
    for goal in GOALS:
        print(f"  {goal.key}  {goal.label}")
        if goal.brief:
            print(f"      {goal.brief}")
        if goal.bonus:
            print(f"      专属议题：{goal.bonus.title}（网页上选中目标后可一键加入）")
    print("\n可以填的处境（--crowd）：\n")
    for crowd in CROWDS:
        print(f"  {crowd.key}  {crowd.label}")
        if crowd.brief:
            print(f"      {crowd.brief}")
    print(
        "\n以上全都可以不填——不填就是一份面向所有人的通用手册。\n"
        "填了的话，手册开头会多一块《你的档案》，两位教练也会围绕这个人的具体数字吵。\n\n"
        "示例：\n"
        "  uv run fitness-duo run --goal fat_loss --crowd office \\\n"
        '      --sex 男 --age 32 --height 175 --weight 82 --notes "周末有饭局"\n'
        "  uv run fitness-duo run --profile my.json   # 从 JSON 读，命令行上给的项覆盖它\n"
    )
    return 0


def _profile_from_args(args: argparse.Namespace) -> Optional[Profile]:
    """把命令行参数（外加可选的 JSON 档案文件）拼成一个 Profile。

    `--profile` 里的内容当底稿，命令行上显式写了的那几项覆盖它——
    这样「一份常用档案 + 临时改两个数」不用另存一个文件。
    """
    raw: dict = {}
    if args.profile:
        try:
            raw = json.loads(Path(args.profile).read_text(encoding="utf-8"))
        except OSError as exc:
            raise InvalidProfile(f"读不出档案文件 {args.profile}：{exc}") from None
        except json.JSONDecodeError as exc:
            raise InvalidProfile(f"档案文件不是合法的 JSON：{exc}") from None
        if not isinstance(raw, dict):
            raise InvalidProfile("档案文件的顶层必须是一个 JSON 对象。")

    overrides = {
        "goal_key": args.goal,
        "crowd_key": args.crowd,
        "notes": args.notes,
        "sex": args.sex,
        "age": args.age,
        "height_cm": args.height,
        "weight_kg": args.weight,
        "training": args.training,
        "constraints": args.constraints,
        "medical": args.medical,
    }
    raw = {**raw, **{k: v for k, v in overrides.items() if v not in (None, "")}}

    reader = parse_profile(raw)
    # 空档案归一成 None：和「一个参数都没填」走同一条路，手册才不会有两种写法
    return None if reader.is_empty() else reader


def _cmd_run(args: argparse.Namespace) -> int:
    try:
        reader = _profile_from_args(args)
    except InvalidProfile as exc:
        print(f"\n[档案有问题] {exc}\n", file=sys.stderr)
        return 2

    try:
        backend = build_backend(mock=args.mock)
    except MissingApiKey as exc:
        print(f"\n[配置缺失] {exc}\n", file=sys.stderr)
        return 2

    if args.mock:
        print("!! 模拟模式：不会调用真实模型，产出的是占位内容，没有参考价值。\n")
    else:
        # 真实模式很慢，先说清楚要等多久，免得跑到一半以为卡死了。
        # 只在真实模式这一支打印——模拟模式的输出是黄金回归钉死的。
        # 每个议题 = 每轮 2 次发言 + 1 次整理，开场白不花调用。
        calls = len(pipeline.select_topics(args.only)) * (args.rounds * 2 + 1)
        print(
            f"真实模式：本次约 {calls} 次模型调用，每次通常十几秒，"
            f"整轮大约 {max(1, round(calls * 16 / 60))} 分钟。跑的过程中会逐条打印进展。\n"
        )

    # 每个 hook 对应原来 _cmd_run 里的每一句 print，顺序与内容逐字不变。
    def run_start(total: int, rounds: int) -> None:
        print(f"人格：{NUTRITIONIST.name}（{NUTRITIONIST.title}） × {COACH.name}（{COACH.title}）")
        # 只在填了档案时才多这一行——没填档案的终端输出必须逐字不变
        if reader is not None:
            print(f"档案：{reader.summary()}")
        print(f"议题数：{total}，每个议题 {rounds} 轮\n")

    def run_done(result: pipeline.RunResult) -> None:
        print(f"\n对话实录：{result.transcript_path}")
        print(f"成果手册：{result.handbook_path}")

    hooks = pipeline.PipelineHooks(
        on_run_start=run_start,
        on_topic_start=lambda index, total, topic: print(
            f"[{index}/{total}] {topic.section_title}"
        ),
        on_turn=lambda topic, turn: print(f"    · {turn.speaker} 发言 {len(turn.content)} 字"),
        on_topic_done=lambda topic, transcript: print(
            f"    对话完成，共 {len(transcript.turns)} 条发言"
        ),
        on_section_done=lambda topic, is_closing, markdown: print(
            "    已整理出附录" if is_closing else "    已整理成手册章节"
        ),
        on_run_done=run_done,
    )

    pipeline.run_pipeline(
        backend=backend,
        rounds=args.rounds,
        topics=pipeline.select_topics(args.only),
        out_dir=args.out,
        mock=args.mock,
        hooks=hooks,
        profile=reader,
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="fitness-duo",
        description="用 CAMEL 双智能体角色扮演，产出面向健身人群的饮食实战手册",
    )
    sub = parser.add_subparsers(dest="command")

    run = sub.add_parser("run", help="跑完整流程：对谈 → 整理 → 出手册")
    run.add_argument("--rounds", type=int, default=3, help="每个议题的对谈轮数（默认 3）")
    run.add_argument("--mock", action="store_true", help="离线模拟，不调用真实模型")
    run.add_argument("--only", default="", help="只跑指定议题，逗号分隔，如 baseline,reality")
    run.add_argument("--out", type=Path, default=None, help="产物输出目录（默认 outputs/）")

    # 读者档案：决定这份手册是「面向所有人」还是「写给这一个人」。
    # 每一项都可留空，全留空 = 与从前完全一样。
    person = run.add_argument_group(
        "读者档案（都可留空；留空出通用手册，填了就是给这个人写的手册）"
    )
    person.add_argument("--goal", default="", help="目标预设，取值见 fitness-duo goals")
    person.add_argument("--crowd", default="", help="处境预设，取值见 fitness-duo goals")
    person.add_argument("--sex", default="", help="性别：男 / 女 / 不详（不填就算不了基础代谢）")
    person.add_argument("--age", type=int, default=None, help="年龄（14–100）")
    person.add_argument("--height", type=float, default=None, help="身高，厘米（120–230）")
    person.add_argument("--weight", type=float, default=None, help="体重，公斤（30–250）")
    person.add_argument("--training", default="", help="训练情况：每周几次、练什么、练了多久")
    person.add_argument(
        "--constraints", default="", help="饮食限制与忌口：素食、乳糖不耐、过敏、不吃牛羊…"
    )
    person.add_argument(
        "--medical", default="", help="伤病与健康状况；有慢性病或正在用药请写在这里"
    )
    person.add_argument("--notes", default="", help="其他诉求：说给两位教练听")
    person.add_argument(
        "--profile", type=Path, default=None,
        help="从 JSON 文件读档案当底稿；命令行上显式给的项覆盖它",
    )
    run.set_defaults(func=_cmd_run)

    topics = sub.add_parser("topics", help="列出所有议题")
    topics.set_defaults(func=_cmd_topics)

    goals = sub.add_parser("goals", help="列出可以填的目标与处境预设")
    goals.set_defaults(func=_cmd_goals)

    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return 1
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
