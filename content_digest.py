"""内容哈希：论文列表、精读解读等可长期 CDN 缓存的 API 响应。"""

from __future__ import annotations

import hashlib
import json
import logging
from typing import Optional

import markdown as md_lib

log = logging.getLogger("content_digest")

HASH_LEN = 10
_MD_EXTENSIONS = ["tables", "fenced_code", "nl2br", "sane_lists"]


def digest_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:HASH_LEN]


def digest_json(obj: object) -> str:
    canonical = json.dumps(obj, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return digest_bytes(canonical.encode("utf-8"))


def build_analysis_html(paper: dict) -> str:
    html = paper.get("analysis_html") or ""
    if not html:
        raw = paper.get("analysis") or ""
        if raw:
            html = md_lib.markdown(raw, extensions=_MD_EXTENSIONS)
    return html


def paper_analysis_digest(paper: dict) -> Optional[str]:
    html = build_analysis_html(paper)
    if not html:
        return None
    return digest_bytes(html.encode("utf-8"))


def _process_paper_for_list(p: dict) -> dict:
    """处理单篇论文，返回不含正文但含 has_analysis / analysis_digest 的副本。"""
    pp = dict(p)
    has = bool(pp.get("analysis_html") or pp.get("analysis"))
    pp["has_analysis"] = has
    if has:
        ad = paper_analysis_digest(p)
        if ad:
            pp["analysis_digest"] = ad
    pp.pop("analysis_html", None)
    pp.pop("analysis", None)
    return pp


def build_papers_list_payload(index: dict) -> dict:
    """构造论文列表 API 响应（不含精读正文，含 analysis_digest）。"""
    data = dict(index)
    papers_out = [_process_paper_for_list(p) for p in data.get("papers", [])]
    data["papers"] = papers_out
    featured_raw = data.get("featured_papers") or []
    if featured_raw:
        data["featured_papers"] = [
            _process_paper_for_list(p) for p in featured_raw
        ]
    # 额外论文（加餐）
    extra_raw = data.get("extra_papers") or []
    if extra_raw:
        data["extra_papers"] = [_process_paper_for_list(p) for p in extra_raw]
    return data


def papers_list_digest(index: dict) -> str:
    return digest_json(build_papers_list_payload(index))


def build_single_paper_payload(paper: dict) -> dict:
    """单篇论文 API 响应（不含精读正文，含 analysis_digest）。"""
    pp = dict(paper)
    has = bool(pp.get("analysis_html") or pp.get("analysis"))
    pp["has_analysis"] = has
    if has:
        ad = paper_analysis_digest(paper)
        if ad:
            pp["analysis_digest"] = ad
    pp.pop("analysis_html", None)
    pp.pop("analysis", None)
    return {"ok": True, "paper": pp}


def single_paper_digest(paper: dict) -> str:
    return digest_json(build_single_paper_payload(paper))


def is_hashed_api_path(path: str) -> bool:
    return path.startswith("/api/h/")
