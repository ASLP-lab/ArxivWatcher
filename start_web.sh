#!/usr/bin/env bash
# 启动 arXiv 每日精读 Web 服务（工作日北京时间定时调度）
set -euo pipefail
cd "$(dirname "$0")"

if [ -f .venv/bin/activate ]; then
  # shellcheck source=/dev/null
  source .venv/bin/activate
fi

export TZ="${TZ:-Asia/Shanghai}"

export WEB_HOST="${WEB_HOST:-127.0.0.1}"
export WEB_PORT="${WEB_PORT:-8091}"
export WEB_WORKERS="${WEB_WORKERS:-48}"
export WEB_SERVER="${WEB_SERVER:-gunicorn}"
export WEB_PUBLIC_URL="${WEB_PUBLIC_URL:-http://${WEB_HOST}:${WEB_PORT}}"

# 飞书 webhook（可选，会传给定时任务里的 run.sh）
# export FEISHU_WEBHOOK_URL="https://open.feishu.cn/open-apis/bot/v2/hook/xxxxxxxx"

export DAILY_HOUR="${DAILY_HOUR:-10}"
export DAILY_MINUTE="${DAILY_MINUTE:-0}"
export ARXIV_CHECK_CATEGORIES="${ARXIV_CHECK_CATEGORIES:-eess.AS cs.SD}"
export RUN_SCRIPT="${RUN_SCRIPT:-$(pwd)/run.sh}"

echo "Web:  http://${WEB_HOST}:${WEB_PORT}"
echo "定时: 工作日 北京时间 ${DAILY_HOUR}:$(printf '%02d' "${DAILY_MINUTE}") → ${RUN_SCRIPT}"
echo "检测: ${ARXIV_CHECK_CATEGORIES}"

exec bash run_web.sh
