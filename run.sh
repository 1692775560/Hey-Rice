#!/usr/bin/env bash
# 小瓜 · 喂饭陪伴 Agent 启动脚本。
#
# 用法:
#   ./run.sh                    启动 Web 服务(默认 http://127.0.0.1:8000)
#   ./run.sh web                同上,显式指定 Web 模式
#   ./run.sh cli                启动命令行交互模式(逐句输入)
#   ./run.sh cli "喂我吃饭吧"     命令行单句模式
#
# 密钥从环境变量读取,请先设置 MEALMATE_API_KEY(不要写进代码或提交仓库):
#   export MEALMATE_API_KEY=你的密钥
set -euo pipefail

# 切到脚本所在目录,保证相对路径(index.html 等)可用。
cd "$(dirname "$0")"

# 若存在 .env 则自动载入(密钥/模型/语音配置);.env 已被 gitignore。
if [ -f .env ]; then
  set -a; . ./.env; set +a
fi

# 选 Python 解释器:优先项目 venv(带语音/依赖),再 $PYTHON,再 python3,最后 python。
if [ -x ./.venv/bin/python ]; then
  PY="./.venv/bin/python"
else
  PY="${PYTHON:-python3}"
  if ! command -v "$PY" >/dev/null 2>&1; then
    PY="python"
  fi
fi
if [ "$PY" != "./.venv/bin/python" ] && ! command -v "$PY" >/dev/null 2>&1; then
  echo "错误: 未找到 Python 解释器,请先安装 Python 3.9+。" >&2
  exit 1
fi

# 缺密钥不直接退出:Web 页面仍可打开,发消息时后端会给清晰提示。
if [ -z "${MEALMATE_API_KEY:-}" ]; then
  echo "提示: 尚未设置 MEALMATE_API_KEY 环境变量。"
  echo "     Web 页面可打开,但发消息会提示配置密钥;命令行模式会直接报错。"
  echo "     设置方法:  export MEALMATE_API_KEY=你的密钥"
fi

mode="${1:-web}"
case "$mode" in
  web)
    exec "$PY" server.py
    ;;
  cli)
    shift || true
    exec "$PY" agent.py "$@"
    ;;
  *)
    echo "未知模式: $mode (可用: web | cli)" >&2
    exit 1
    ;;
esac
