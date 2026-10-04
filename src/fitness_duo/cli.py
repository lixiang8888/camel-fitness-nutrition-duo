"""命令行入口。"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

from .config import OUTPUT_DIR, MissingApiKey, build_backend
from .personas import CHEN_SHI, LIN_SHU
from .topics import CLOSING_TOPIC, TOPICS, Topic, get_topic


def _cmd_topics(_args: argparse.Namespace) -> int:
    print("本项目的议题表（每个议题产出手册的一节）：\n")
    for i, topic in enumerate((*TOPICS, CLOSING_TOPIC), 1):
        print(f"{i}. [{topic.key}] {topic.section_title}")
        print(f"   {topic.brief}\n")
    return 0


def _cmd_run(args: argparse.Namespace) -> int:
    from . import digest, society

    try:
        backend = build_backend(mock=args.mock)
    except MissingApiKey as exc:
        print(f"\n[配置缺失] {exc}\n", file=sys.stderr)
        return 2

    if args.mock:
        print("!! 模拟模式：不会调用真实模型，产出的是占位内容，没有参考价值。\n")

    topics: list[Topic] = [get_topic(k) for k in args.only.split(",")] if args.only else list(TOPICS)
    topics.append(CLOSING_TOPIC)

    print(f"人格：{LIN_SHU.name}（{LIN_SHU.title}） × {CHEN_SHI.name}（{CHEN_SHI.title}）")
    print(f"议题数：{len(topics)}，每个议题 {args.rounds} 轮\n")

    transcripts = []
    sections: list[str] = []

    for index, topic in enumerate(topics, 1):
        is_closing = topic.key == CLOSING_TOPIC.key
        print(f"[{index}/{len(topics)}] {topic.section_title}")

        transcript = society.debate_topic(
            topic,
            backend=backend,
            rounds=args.rounds,
            on_turn=lambda t: print(f"    · {t.speaker} 发言 {len(t.content)} 字"),
        )
        transcripts.append(transcript)
        print(f"    对话完成，共 {len(transcript.turns)} 条发言")

        if is_closing:
            # 收尾议题要把前面所有议题的结论一起喂进去，否则写不出「分歧备忘」
            context = "\n\n".join(t.as_dialogue() for t in transcripts)
            closing = digest.digest_closing(context, backend=backend)
            print("    已整理出附录")
        else:
            sections.append(digest.digest_section(topic, transcript.as_dialogue(), backend=backend))
            print("    已整理成手册章节")

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    out_dir: Path = args.out or OUTPUT_DIR
    out_dir.mkdir(parents=True, exist_ok=True)

    suffix = ".mock" if args.mock else ""
    transcript_path = out_dir / f"transcript-{stamp}{suffix}.md"
    handbook_path = out_dir / f"handbook-{stamp}{suffix}.md"

    header = digest.HEADER.format(
        a=LIN_SHU.name,
        a_title=LIN_SHU.title,
        b=CHEN_SHI.name,
        b_title=CHEN_SHI.title,
    )
    if args.mock:
        header = "> ⚠️ **这是模拟模式生成的占位手册，不含任何真实营养学内容。**\n\n" + header

    handbook = digest.assemble(sections, closing, header=header)

    transcript_path.write_text(
        "\n\n".join(t.to_markdown() for t in transcripts), encoding="utf-8"
    )
    handbook_path.write_text(handbook, encoding="utf-8")

    print(f"\n对话实录：{transcript_path}")
    print(f"成果手册：{handbook_path}")
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
    run.set_defaults(func=_cmd_run)

    topics = sub.add_parser("topics", help="列出所有议题")
    topics.set_defaults(func=_cmd_topics)

    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return 1
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
