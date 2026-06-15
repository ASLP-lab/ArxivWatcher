"""arXiv abs 页版本解析与 SQLite/JSON 永久缓存。"""

from __future__ import annotations

import re
import threading
from datetime import datetime, timezone
from typing import Dict, List

import requests

import storage

_ARXIV_ID_RE = re.compile(r"^\d{4}\.\d{4,5}$", re.I)
_BASE_ID_RE = re.compile(r"v\d+$", re.I)
_CITATION_VER_RE = re.compile(r'citation_arxiv_id"\s+content="[^"]*v(\d+)', re.I)
_CITATION_ID_RE = re.compile(r'citation_arxiv_id"\s+content="([^"]+)"', re.I)
_ABS_VER_RE = re.compile(r"arxiv\.org/abs/[0-9.]+v(\d+)", re.I)
_THIS_VERSION_RE = re.compile(r"this version, v(\d+)", re.I)
_SUBMISSION_TAG_RE = re.compile(r"\[v(\d+)\]", re.I)

# 解析逻辑升级时递增，触发旧缓存条目重新拉取 abs 页
PARSER_VERSION = 2

ARXIV_ABS_TIMEOUT = 20
USER_AGENT = "ArxivWatcher/1.0 (+https://arxiv.npu-aslp.org)"

_key_locks: Dict[str, threading.Lock] = {}
_key_locks_guard = threading.Lock()


def cache_key(date: str, paper_id: str) -> str:
    """与互动数据一致：日期 + arXiv 号。"""
    return f"{str(date).strip()}/{str(paper_id).strip()}"


def normalize_base_id(paper_id: str) -> str:
    return _BASE_ID_RE.sub("", str(paper_id or "").strip())


def is_arxiv_base_id(base_id: str) -> bool:
    return bool(_ARXIV_ID_RE.fullmatch(base_id))


def parse_version_from_html(html: str) -> int:
    """从 abs 页 HTML 解析当前版本；取页面中出现的最高版本号。"""
    versions: List[int] = []

    m = _CITATION_VER_RE.search(html)
    if m:
        versions.append(int(m.group(1)))

    for m in _THIS_VERSION_RE.finditer(html):
        versions.append(int(m.group(1)))

    for m in _SUBMISSION_TAG_RE.finditer(html):
        versions.append(int(m.group(1)))

    for m in _ABS_VER_RE.finditer(html):
        versions.append(int(m.group(1)))

    if versions:
        return max(versions)

    if _CITATION_ID_RE.search(html):
        return 1

    return 1


def fetch_version_from_arxiv(base_id: str) -> int:
    url = f"https://arxiv.org/abs/{base_id}"
    resp = requests.get(
        url,
        timeout=ARXIV_ABS_TIMEOUT,
        headers={"User-Agent": USER_AGENT, "Accept": "text/html"},
    )
    resp.raise_for_status()
    return parse_version_from_html(resp.text)


def _lock_for_key(key: str) -> threading.Lock:
    with _key_locks_guard:
        lock = _key_locks.get(key)
        if lock is None:
            lock = threading.Lock()
            _key_locks[key] = lock
        return lock


class ArxivVersionCache:
    """{date}/{paper_id} -> {version, fetched_at}，SQLite 表 arxiv_versions 永久缓存。"""

    def __init__(self, store: storage.Store):
        self.store = store

    def get_version(self, date: str, paper_id: str) -> tuple[int, bool]:
        key = cache_key(date, paper_id)
        base_id = normalize_base_id(paper_id)
        if not is_arxiv_base_id(base_id):
            return 1, True

        with _lock_for_key(key):
            cached = self.store.get(key)
            if cached is not None and int(cached.get("parser_version", 0)) >= PARSER_VERSION:
                return int(cached.get("version", 1)), True

            ver = fetch_version_from_arxiv(base_id)
            self.store.put(
                key,
                {
                    "version": ver,
                    "paper_id": paper_id,
                    "base_id": base_id,
                    "parser_version": PARSER_VERSION,
                    "fetched_at": datetime.now(timezone.utc).isoformat(),
                },
            )
            return ver, False
