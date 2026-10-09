"""arXiv abs 页版本解析与 SQLite/JSON 永久缓存。"""

from __future__ import annotations

import re
import threading
from datetime import datetime, timezone
from typing import Dict, List

from bs4 import BeautifulSoup

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
PARSER_VERSION = 3

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


def parse_comments_from_html(html: str) -> str:
    """解析 abs 页 Comments 字段。"""
    soup = BeautifulSoup(html, "html.parser")
    cell = soup.find("td", class_=lambda value: value and "comments" in value.split())
    return cell.get_text(" ", strip=True) if cell else ""


def _normalized_comments(value: str) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip().casefold()


def fetch_version_info_from_arxiv(base_id: str, current_comments: str = "") -> dict:
    """获取当前版本，并在 V2+ 时比较上一版本的 Comments。"""
    headers = {"User-Agent": USER_AGENT, "Accept": "text/html"}
    current_resp = requests.get(
        f"https://arxiv.org/abs/{base_id}", timeout=ARXIV_ABS_TIMEOUT, headers=headers
    )
    current_resp.raise_for_status()
    version = parse_version_from_html(current_resp.text)
    current = current_comments or parse_comments_from_html(current_resp.text)
    unchanged = False
    previous = ""
    if version >= 2:
        previous_resp = requests.get(
            f"https://arxiv.org/abs/{base_id}v{version - 1}",
            timeout=ARXIV_ABS_TIMEOUT,
            headers=headers,
        )
        previous_resp.raise_for_status()
        previous = parse_comments_from_html(previous_resp.text)
        unchanged = _normalized_comments(current) == _normalized_comments(previous)
    return {
        "version": version,
        "comments_unchanged": unchanged,
        "current_comments": current,
        "previous_comments": previous,
    }


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
        """兼容原有只取版本号的调用；详情接口会按新解析版本再补 comments。"""
        key = cache_key(date, paper_id)
        base_id = normalize_base_id(paper_id)
        if not is_arxiv_base_id(base_id):
            return 1, True
        with _lock_for_key(key):
            cached = self.store.get(key)
            if cached is not None and int(cached.get("parser_version", 0)) >= 2:
                return int(cached.get("version", 1)), True
            version = fetch_version_from_arxiv(base_id)
            self.store.put(key, {
                "version": version,
                "paper_id": paper_id,
                "base_id": base_id,
                "parser_version": 2,
                "fetched_at": datetime.now(timezone.utc).isoformat(),
            })
            return version, False

    def get_version_info(
        self, date: str, paper_id: str, current_comments: str = ""
    ) -> tuple[dict, bool]:
        key = cache_key(date, paper_id)
        base_id = normalize_base_id(paper_id)
        if not is_arxiv_base_id(base_id):
            return {"version": 1, "comments_unchanged": False}, True

        with _lock_for_key(key):
            cached = self.store.get(key)
            if cached is not None and int(cached.get("parser_version", 0)) >= PARSER_VERSION:
                return dict(cached), True

            info = fetch_version_info_from_arxiv(base_id, current_comments)
            info.update({
                "paper_id": paper_id,
                "base_id": base_id,
                "parser_version": PARSER_VERSION,
                "fetched_at": datetime.now(timezone.utc).isoformat(),
            })
            self.store.put(key, info)
            return info, False
