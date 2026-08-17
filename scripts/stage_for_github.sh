#!/usr/bin/env bash
# 将可公开发布的改动加入暂存区，排除内含密钥/内网信息的文件。
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

INTERNAL_PATHS=(
  start_web_internal.sh
  run_internal.sh
  llm_config.sh
  blacklist.txt
  highlight_authors.txt
  featured_authors.txt
  select_sites.txt
  config/llm_config.sh
  config/llm_config.local.sh
  config/blacklist.txt
  config/highlight_authors.txt
  config/featured_authors.txt
  config/select_sites.txt
  pubkey_admin
)

DATA_PATHS=(
  data/.secret_key
  data/app.db
  data/users.json
  data/comments.json
  data/interactions.json
  data/highlights.json
  data/favorites.json
  data/visits.json
  data/arxiv_versions.json
)

for p in "${INTERNAL_PATHS[@]}" "${DATA_PATHS[@]}"; do
  if git ls-files --error-unmatch "$p" >/dev/null 2>&1; then
    git rm --cached -f "$p"
  fi
done

git add \
  .gitignore \
  .dockerignore \
  Dockerfile \
  docker-compose.yml \
  pyproject.toml \
  requirements.txt \
  uv.lock \
  arxiv_version.py \
  web.py \
  storage.py \
  static/app.js \
  static/highlights.js \
  static/style.css \
  content_digest.py \
  static_assets.py \
  ccf_catalog.py \
  aslp_feed.py \
  auth.py \
  user_admin.py \
  storage_tool.py \
  templates/ \
  start_web.sh \
  run.sh \
  README.md \
  send.py \
  arxiv_daily_digest.py \
  scripts/stage_for_github.sh \
  config/blacklist.example.txt \
  config/highlight_authors.example.txt \
  config/featured_authors.example.txt \
  config/select_sites.example.txt \
  config/llm_config.example.sh

echo "=== Staged ==="
git diff --cached --name-only

echo ""
echo "=== Ignored internal (should not appear above) ==="
for p in "${INTERNAL_PATHS[@]}"; do
  git check-ignore -v "$p" || echo "WARN: $p is not ignored"
done
