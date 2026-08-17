"""重点作者名单：从 highlight_authors.txt 读取，供前端高亮匹配论文作者。"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import threading
from pathlib import Path

log = logging.getLogger("highlight_authors")

ROOT = Path(__file__).resolve().parent
CONFIG_DIR = Path(os.environ.get("ARXIVWATCHER_CONFIG_DIR") or (ROOT / "config"))
DEFAULT_PATH = (
    CONFIG_DIR / "highlight_authors.txt"
    if (CONFIG_DIR / "highlight_authors.txt").exists() or not (ROOT / "highlight_authors.txt").exists()
    else ROOT / "highlight_authors.txt"
)

_lock = threading.Lock()
_names: list[str] = []
_digest: str = ""
_mtime_ns: int = -1


def _normalize_name(name: str) -> str:
    return re.sub(r"\s+", " ", (name or "").strip())


def load_names(path: Path | None = None) -> list[str]:
    """读取名单文件，返回规范化后的姓名列表。"""
    path = path or DEFAULT_PATH
    if not path.is_file():
        return []
    names: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        name = _normalize_name(line)
        if name:
            names.append(name)
    return names


def _compute_digest(names: list[str]) -> str:
    payload = "\n".join(names).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()[:10]


def reload_if_needed(path: Path | None = None) -> None:
    """按文件 mtime 热加载名单。"""
    global _names, _digest, _mtime_ns
    path = path or DEFAULT_PATH
    try:
        mtime_ns = path.stat().st_mtime_ns if path.is_file() else 0
    except OSError:
        mtime_ns = 0
    with _lock:
        if mtime_ns == _mtime_ns and _names:
            return
        names = load_names(path)
        _names = names
        _digest = _compute_digest(names)
        _mtime_ns = mtime_ns
        log.info("highlight_authors: loaded %d name(s)", len(names))


def get_names() -> list[str]:
    reload_if_needed()
    with _lock:
        return list(_names)


def get_digest() -> str:
    reload_if_needed()
    with _lock:
        return _digest
