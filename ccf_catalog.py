"""CCF 推荐目录：从 xjsc01/ccf-catalog 动态拉取并匹配 arXiv Comments。

数据源：https://xjsc01.github.io/ccf-catalog/
"""

from __future__ import annotations

import logging
import re
import threading
import time
from typing import Optional

import requests

from content_digest import digest_bytes

log = logging.getLogger("ccf_catalog")

CCF_SOURCE_URL = "https://raw.githubusercontent.com/xjsc01/ccf-catalog/master/index.html"
CCF_PAGE_URL = "https://xjsc01.github.io/ccf-catalog/"
DEFAULT_REFRESH_INTERVAL = 3600
FETCH_TIMEOUT = 30

_ENTRY_RE = re.compile(
    r'\{s:"((?:[^"\\]|\\.)*)",f:"((?:[^"\\]|\\.)*)",p:"((?:[^"\\]|\\.)*)",r:"([ABC])",t:"(conf|jour)",a:"(\w+)"\}'
)

AREA_LABELS = {
    "arch": "体系结构",
    "net": "计算机网络",
    "sec": "网络安全",
    "se": "软件工程",
    "db": "数据库",
    "theory": "理论",
    "cg": "图形多媒体",
    "ai": "人工智能",
    "hci": "人机交互",
    "cross": "交叉综合",
}

TYPE_LABELS = {"conf": "会议", "jour": "期刊"}

_lock = threading.Lock()
_entries: list[dict] = []
_digest: str = ""
_last_refresh: float = 0.0
_thread: Optional[threading.Thread] = None
_started = False


def _parse_catalog_html(html: str) -> list[dict]:
    entries: list[dict] = []
    for m in _ENTRY_RE.finditer(html):
        area = m.group(6)
        entries.append({
            "s": m.group(1),
            "f": m.group(2),
            "p": m.group(3),
            "r": m.group(4),
            "t": m.group(5),
            "a": area,
            "area_label": AREA_LABELS.get(area, area),
            "type_label": TYPE_LABELS.get(m.group(5), m.group(5)),
        })
    return entries


def _fetch_html() -> str:
    for url in (CCF_SOURCE_URL, CCF_PAGE_URL):
        try:
            resp = requests.get(
                url,
                timeout=FETCH_TIMEOUT,
                headers={"User-Agent": "ArxivWatcher/1.0"},
            )
            resp.raise_for_status()
            if "const DATA" in resp.text:
                return resp.text
        except Exception as e:  # noqa: BLE001
            log.warning("CCF 目录拉取失败 %s: %s", url, e)
    raise RuntimeError("无法获取 CCF 目录页面")


def refresh() -> bool:
    """拉取并解析 CCF 目录。成功返回 True。"""
    try:
        html = _fetch_html()
        entries = _parse_catalog_html(html)
        if not entries:
            log.warning("CCF 目录解析结果为空")
            return False
        digest = digest_bytes(html.encode("utf-8"))
    except Exception as e:  # noqa: BLE001
        log.warning("CCF 目录刷新失败: %s", e)
        return False

    global _entries, _digest, _last_refresh
    with _lock:
        _entries = entries
        _digest = digest
        _last_refresh = time.time()
    log.info("CCF 目录已更新，共 %d 条，digest=%s", len(entries), digest)
    return True


def ensure_fresh(max_age: float = DEFAULT_REFRESH_INTERVAL) -> None:
    if not _entries or (time.time() - _last_refresh) > max_age:
        refresh()


def get_entries() -> list[dict]:
    with _lock:
        return list(_entries)


def get_digest() -> str:
    with _lock:
        return _digest


def _is_word_char(ch: str) -> bool:
    return ch.isalnum() or ch in "_-"


def _exact_token_in_text(text: str, term: str) -> bool:
    """term 在 text 中以完整 token / 连续子串出现（大小写不敏感）。"""
    if not term or not text:
        return False
    tl, term_l = text.lower(), term.lower()
    if tl == term_l:
        return True
    start = 0
    n = len(term)
    while True:
        idx = tl.find(term_l, start)
        if idx == -1:
            return False
        before = text[idx - 1] if idx > 0 else ""
        after = text[idx + n] if idx + n < len(text) else ""
        if not (before and _is_word_char(before)) and not (after and _is_word_char(after)):
            return True
        start = idx + 1


def match_tags(comment: str) -> list[dict]:
    """用 Comments 文本匹配 CCF 简称 / 全称，返回命中条目（去重）。"""
    text = (comment or "").strip()
    if not text:
        return []

    ensure_fresh()
    with _lock:
        catalog = list(_entries)
    if not catalog:
        return []

    text_lower = text.lower()
    seen: set[str] = set()
    hits: list[dict] = []

    for entry in catalog:
        abbr = entry["s"]
        full = entry["f"]
        abbr_l, full_l = abbr.lower(), full.lower()
        matched = (
            text_lower == abbr_l
            or text_lower == full_l
            or _exact_token_in_text(text, abbr)
            or (len(full) >= 8 and _exact_token_in_text(text, full))
        )
        if not matched:
            continue
        key = f"{abbr}|{entry['r']}|{entry['t']}"
        if key in seen:
            continue
        seen.add(key)
        hits.append({
            "abbr": abbr,
            "full": full,
            "rank": entry["r"],
            "type": entry["t"],
            "type_label": entry["type_label"],
            "area_label": entry["area_label"],
            "publisher": entry["p"],
        })

    rank_order = {"A": 0, "B": 1, "C": 2}
    hits.sort(key=lambda x: (rank_order.get(x["rank"], 9), x["abbr"]))
    return hits


def _loop(interval: int) -> None:
    while True:
        refresh()
        time.sleep(interval)


def start_background_refresh(interval: int = DEFAULT_REFRESH_INTERVAL) -> None:
    global _thread, _started
    with _lock:
        if _started:
            return
        _started = True
    if not _entries:
        refresh()
    _thread = threading.Thread(
        target=_loop, args=(interval,), daemon=True, name="ccf-catalog-refresh"
    )
    _thread.start()
    log.info("CCF 目录后台刷新已启动（间隔 %d 秒）", interval)
