#!/usr/bin/env bash
# macOS：将 launchd plist 安装到 ~/Library/LaunchAgents
set -euo pipefail

PLIST_NAME="com.example.arxiv-watcher.plist"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
PLIST_SRC="${SCRIPT_DIR}/${PLIST_NAME}"
PLIST_DST="${HOME}/Library/LaunchAgents/${PLIST_NAME}"

if [[ ! -f "${PLIST_SRC}" ]]; then
  echo "missing ${PLIST_SRC}" >&2
  exit 1
fi

echo "请先编辑 ${PLIST_SRC}，把 /path/to/arxiv-watcher 改成实际路径："
echo "  ${REPO_ROOT}"
read -r -p "已编辑完成？按 Enter 继续…"

mkdir -p "${HOME}/Library/LaunchAgents" "${REPO_ROOT}/logs"
cp "${PLIST_SRC}" "${PLIST_DST}"

launchctl unload "${PLIST_DST}" 2>/dev/null || true
launchctl load "${PLIST_DST}"

echo "已加载 ${PLIST_DST}"
echo "查看状态: launchctl list | grep arxiv-watcher"
