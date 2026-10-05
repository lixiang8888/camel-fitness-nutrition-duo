"""命令行入口。

这里只负责「把 pipeline 的回调接到 print 上」和解析参数；编排本身在 pipeline.py。
同样的编排，launcher.py 把它接到网页的 SSE 广播上——两个前端共用一套流程。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import pipeline
from .config import MissingApiKey, build_backend
from .personas import COACH, NUTRITIONIST
from .topics import CLOSING_TOPIC, TOPICS


def _cmd_topics(_args: argparse.Namespace) -> int:
    print("本项目的议题表（每个议题产出手册的一节）：\n")
    for i, topic in enumerate((*TOPICS, CLOSING_TOPIC), 1):
        print(f"{i}. [{topic.key}] {topic.section_title}")
        print(f"   {topic.brief}\n")
    return 0


def _cmd_run(args: argparse.Namespace) -> int:
    try:
        backend = build_backend(mock=args.mock)
    except MissingApiKey as exc:
        print(f"\n[配置缺失] {exc}\n", file=sys.stderr)
        return 2

    if args.mock:
        print("!! 模拟模式：不会调用真实模型，产出的是占位内容，没有参考价值。\n")

    # 每个 hook 对应原来 _cmd_run 里的每一句 print，顺序与内容逐字不变。
    def run_start(total: int, rounds: int) -> None:
        print(f"人格：{NUTRITIONIST.name}（{NUTRITIONIST.title}） × {COACH.name}（{COACH.title}）")
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
