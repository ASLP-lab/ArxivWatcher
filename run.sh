#!/usr/bin/env bash
# 单次抓取：arXiv 列表 → LLM 解读 → 生成报告 / 索引 → 可选邮件与飞书推送
set -euo pipefail
cd "$(dirname "$0")"

if [ -f .venv/bin/activate ]; then
  # shellcheck source=/dev/null
  source .venv/bin/activate
fi

export TZ="${TZ:-Asia/Shanghai}"

# ── LLM（OpenAI 兼容接口）──
: "${LLM_BASE_URL:=https://api.openai.com/v1}"
: "${LLM_MODEL:=gpt-4o}"
# export LLM_API_KEY="sk-..."

# ── 邮件（可选）──
# export SMTP_HOST="smtp.gmail.com"
# export SMTP_PORT=587
# export SMTP_USER="you@example.com"
# export SMTP_PASS="your-app-password"
# export EMAIL_TO="recipient@example.com"

# ── 飞书机器人（可选）──
# export FEISHU_WEBHOOK_URL="https://open.feishu.cn/open-apis/bot/v2/hook/xxxxxxxx"

# Web / 插件下载页展示的公网地址（飞书消息、Zotero xpi 会用到）
export WEB_PUBLIC_URL="${WEB_PUBLIC_URL:-http://127.0.0.1:8091}"

# 仅测试飞书 webhook（不调 arXiv / LLM）
if [ "${TEST_FEISHU_ONLY:-}" = "1" ]; then
  python send.py --test-feishu --category "${ARXIV_CATEGORIES:-eess.AS cs.SD}"
  exit $?
fi

python send.py \
  --category ${ARXIV_CATEGORIES:-eess.AS cs.SD} \
  --no-email
  # --no-llm \
  # --no-feishu \
  # --max-papers 5
