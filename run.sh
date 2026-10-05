#!/usr/bin/env bash
# 快捷启动：跑 CAMEL 双智能体对谈，产出手册。
#
#   ./run.sh                 正式跑（会调用 DeepSeek，花钱）
#   ./run.sh --mock          离线模拟，不花钱，先验证链路通不通
#   ./run.sh topics          只列议题表
#   ./run.sh run --rounds 5  参数原样透传给 fitness-duo
#
# 不管在哪个目录下调用都没问题，脚本会自己切回项目根。
set -euo pipefail

cd "$(dirname "$(readlink -f "$0")")"

# --- 前置检查 ---------------------------------------------------------------

if ! command -v uv >/dev/null 2>&1; then
  cat >&2 <<'MSG'
✗ 没找到 uv，装一下：

    curl -LsSf https://astral.sh/uv/install.sh | sh

装完重开一个终端（或 source ~/.bashrc）再试。
MSG
  exit 1
fi

# --mock 走的是内置脚本后端，不需要 key；正式跑必须有。
if [[ "$*" != *"--mock"* ]] && ! grep -qE '^DEEPSEEK_API_KEY=.+' .env 2>/dev/null; then
  cat >&2 <<'MSG'
✗ 没有找到 DEEPSEEK_API_KEY。

  1) cp .env.example .env
  2) 把 DeepSeek 的 key（sk- 开头）填进 .env

想先不花钱跑通流程：./run.sh --mock
MSG
  exit 1
fi

# --- 跑 ---------------------------------------------------------------------

# 没给子命令时默认补 run。两种情况：完全不带参数，或者直接甩选项
# （--mock / --rounds / --only 都是 run 子命令的选项，直接透传到顶层
#  会被 argparse 当成 unrecognized arguments 拒掉）
if [[ $# -eq 0 || "$1" == -* ]]; then
  set -- run "$@"
fi

echo "▶ fitness-duo $*"
echo

uv run fitness-duo "$@"

# 跑完指一下产物在哪
if [[ "$1" == "run" ]]; then
  echo
  echo "✓ 产物已写入 outputs/ ："
  ls -1t outputs/handbook-*.md outputs/transcript-*.md 2>/dev/null | head -4 || true
fi
