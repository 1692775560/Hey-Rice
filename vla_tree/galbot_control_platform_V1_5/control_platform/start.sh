#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"
export PORT="${1:-7860}"
export LD_LIBRARY_PATH="/data/galbot/lib/python3.8.10:/data/galbot/lib:${LD_LIBRARY_PATH:-}"
export PYTHONPATH="/data/galbot/lib:/data/galbot/lib/python3/site-packages:/data/galbot/lib/python3.8.10:/data/galbot/lib/galbot_interface-tf2-devel/devel/aarch64-Linux-GNU-9.4.0/lib/python3/site-packages:${PYTHONPATH:-}"
PYTHON_BIN="python3"
if [ -x ".venv/bin/python" ]; then
  PYTHON_BIN=".venv/bin/python"
fi
if "$PYTHON_BIN" -c "import waitress" >/dev/null 2>&1; then
  exec "$PYTHON_BIN" -m waitress --host=0.0.0.0 --port="$PORT" --threads="${GALBOT_HTTP_THREADS:-8}" --channel-timeout=30 server:app
fi
exec "$PYTHON_BIN" server.py
