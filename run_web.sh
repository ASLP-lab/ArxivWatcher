#!/usr/bin/env bash
# 启动 Web 服务（由 WEB_SERVER / WEB_SCHEDULER 控制模式）
set -euo pipefail
cd "$(dirname "$0")"

PYTHON="python"
GUNICORN="gunicorn"
if [ -x ".venv/bin/python" ]; then
  PYTHON=".venv/bin/python"
  GUNICORN=".venv/bin/gunicorn"
fi

SERVER="${WEB_SERVER:-gunicorn}"

# ── 模式 A：waitress 单进程（改 gunicorn 之前的方式，适合开发/小流量）──
if [ "$SERVER" = "waitress" ]; then
  exec "$PYTHON" web.py
fi

# ── 模式 B/C：gunicorn 多进程 ──
SCHEDULER_MODE="${WEB_SCHEDULER:-external}"
if [ "$SCHEDULER_MODE" = "external" ]; then
  # 默认：调度器独立进程，抓取时不拖垮 HTTP worker
  "$PYTHON" web.py --scheduler &
  SCHEDULER_PID=$!
  trap 'kill "$SCHEDULER_PID" 2>/dev/null || true' EXIT INT TERM
fi
# WEB_SCHEDULER=in_process 时不另起 scheduler，由某个 gunicorn worker 内嵌调度（旧 gunicorn 行为）

exec "$GUNICORN" -c gunicorn.conf.py web:app
