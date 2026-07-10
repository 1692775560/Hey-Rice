#!/usr/bin/env bash
# 小瓜 · 一键启动(Mac 端 Web + 意图 + DeepSeek + edge-tts→机器人语音)
#
#   ./start_heyrice.sh
#
# 前置:
#   - .env 已填好 MEALMATE_API_KEY / DOUBAO_* / HEYRICE_ROBOT_TTS_URL
#   - .venv 已装依赖(edge-tts / sherpa-onnx / volcengine-audio / websockets)
#   - 机器人侧播放服务已启动(见 robot 上的 run_player.sh)
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -f .env ]; then
  echo "错误: 缺少 .env(从 .env.example 复制并填好密钥)。" >&2
  exit 1
fi
# 载入 .env 全部变量
set -a; . ./.env; set +a

# 优先用 venv 里的 python(带 edge-tts / 语音依赖),否则回退 python3
PY="./.venv/bin/python"
[ -x "$PY" ] || PY="python3"

echo "启动小瓜 Web 服务: http://${MEALMATE_HOST:-127.0.0.1}:${MEALMATE_PORT:-8000}"
echo "  机器人语音: ${HEYRICE_ROBOT_TTS_URL:-未配置}"
exec "$PY" server.py
