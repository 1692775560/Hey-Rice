#!/bin/bash
# Galbot G1 控制台停止脚本
# 用法: ./stop.sh [端口]  默认 7860

PORT="${1:-7860}"
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PID_FILE="$DIR/.server.pid"

echo "================================================"
echo "  🤖 Galbot G1 控制台停止"
echo "================================================"
echo "  端口: $PORT"
echo "  目录: $DIR"
echo "================================================"

if [ -f "$PID_FILE" ]; then
  PID="$(cat "$PID_FILE" 2>/dev/null)"
  if [ -n "$PID" ] && kill -0 "$PID" 2>/dev/null; then
    echo "🛑 通过 PID 停止: $PID"
    kill "$PID" 2>/dev/null || true
    sleep 1
    kill -9 "$PID" 2>/dev/null || true
    rm -f "$PID_FILE"
    echo "✅ 已停止"
    exit 0
  fi
fi

PIDS="$(lsof -ti tcp:"$PORT" 2>/dev/null)"
if [ -n "$PIDS" ]; then
  echo "🛑 通过端口停止: $PIDS"
  kill $PIDS 2>/dev/null || true
  sleep 1
  kill -9 $PIDS 2>/dev/null || true
  echo "✅ 已停止"
  exit 0
fi

echo "⚠ 没有找到运行中的控制台进程"
