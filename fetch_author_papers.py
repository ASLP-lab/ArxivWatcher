#!/usr/bin/env python3
"""按作者查询最近若干天提交到 arXiv 的论文，并返回 JSON。

示例：
    python fetch_author_papers.py "Geoffrey Hinton" --days 7
    python fetch_author_papers.py "Geoffrey Hinton" --days 30 -o papers.json
    python fetch_author_papers.py "Geoffrey Hinton" --days 7 --end-date 2026-07-01
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional
from xml.etree import ElementTree

import requests


ARXIV_API_URL = "https://export.arxiv.org/api/query"
USER_AGENT = "ArxivWatcher/2.0 (author-day lookup)"
ATOM_NS = "http://www.w3.org/2005/Atom"
ARXIV_NS = "http://arxiv.org/schemas/atom"
OPENSEARCH_NS = "http://a9.com/-/spec/opensearch/1.1/"
DEFAULT_BATCH_SIZE = 100
DEFAULT_REQUEST_DELAY = 3.0
DEFAULT_DAYS = 7


class ArxivAPIError(RuntimeError):
    """arXiv API 请求或响应无效。"""


def _parse_date(value: str | date | None) -> date:
    if value is None:
        return datetime.now(timezone.utc).date()
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("date 必须使用 YYYY-MM-DD 格式") from exc


def _text(element: ElementTree.Element, path: str) -> str:
    child = element.find(path)
    return "" if child is None or child.text is None else child.text.strip()


def _clean_text(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def _paper_id(entry_url: str) -> str:
    paper_id = re.sub(r"^https?://[^/]+/abs/", "", entry_url.rstrip("/"))
    return re.sub(r"v\d+$", "", paper_id)


def _parse_entry(entry: ElementTree.Element) -> dict[str, Any]:
    entry_url = _text(entry, f"{{{ATOM_NS}}}id")
    links: dict[str, str] = {}
    for link in entry.findall(f"{{{ATOM_NS}}}link"):
        href = link.get("href", "")
        rel = link.get("rel", "alternate")
        title = link.get("title", "")
        key = "pdf" if title == "pdf" else rel
        if href:
            links[key] = href

    categories = [
        item.get("term", "")
        for item in entry.findall(f"{{{ATOM_NS}}}category")
        if item.get("term")
    ]
    primary = entry.find(f"{{{ARXIV_NS}}}primary_category")

    return {
        "arxiv_id": _paper_id(entry_url),
        "entry_url": entry_url,
        "pdf_url": links.get("pdf", ""),
        "title": _clean_text(_text(entry, f"{{{ATOM_NS}}}title")),
        "summary": _clean_text(_text(entry, f"{{{ATOM_NS}}}summary")),
        "authors": [
            _text(author, f"{{{ATOM_NS}}}name")
            for author in entry.findall(f"{{{ATOM_NS}}}author")
        ],
        "published": _text(entry, f"{{{ATOM_NS}}}published"),
        "updated": _text(entry, f"{{{ATOM_NS}}}updated"),
        "primary_category": "" if primary is None else primary.get("term", ""),
        "categories": categories,
        "comment": _text(entry, f"{{{ARXIV_NS}}}comment"),
        "journal_ref": _text(entry, f"{{{ARXIV_NS}}}journal_ref"),
        "doi": _text(entry, f"{{{ARXIV_NS}}}doi"),
    }


def _parse_feed(xml_content: bytes) -> tuple[int, list[dict[str, Any]]]:
    try:
        root = ElementTree.fromstring(xml_content)
    except ElementTree.ParseError as exc:
        raise ArxivAPIError("arXiv API 返回了无法解析的 XML") from exc

    total_text = _text(root, f"{{{OPENSEARCH_NS}}}totalResults")
    try:
        total = int(total_text or 0)
    except ValueError as exc:
        raise ArxivAPIError("arXiv API 返回了无效的结果数量") from exc

    entries = root.findall(f"{{{ATOM_NS}}}entry")
    return total, [_parse_entry(entry) for entry in entries]


def fetch_author_papers(
    author: str,
    days: int = DEFAULT_DAYS,
    *,
    end_date: str | date | None = None,
    batch_size: int = DEFAULT_BATCH_SIZE,
    request_delay: float = DEFAULT_REQUEST_DELAY,
    timeout: float = 30.0,
    session: Optional[requests.Session] = None,
) -> dict[str, Any]:
    """查询作者在最近 ``days`` 个 UTC 自然日提交的所有论文。

    查询区间包含起止日期；``end_date`` 省略时使用当前 UTC 日期。arXiv 的
    作者搜索按姓名进行，同名作者可能出现在结果中；对身份要求严格时应再
    核对 ORCID 或论文主页。
    """
    author = _clean_text(author)
    if not author:
        raise ValueError("author 不能为空")
    if isinstance(days, bool) or not isinstance(days, int) or days < 1:
        raise ValueError("days 必须是大于等于 1 的整数")
    if not 1 <= batch_size <= 1000:
        raise ValueError("batch_size 必须在 1 到 1000 之间")
    if request_delay < 0:
        raise ValueError("request_delay 不能小于 0")

    query_end = _parse_date(end_date)
    query_start = query_end - timedelta(days=days - 1)
    start_day = query_start.strftime("%Y%m%d")
    end_day = query_end.strftime("%Y%m%d")
    escaped_author = author.replace("\\", "\\\\").replace('"', '\\"')
    search_query = (
        f'au:"{escaped_author}" AND '
        f"submittedDate:[{start_day}0000 TO {end_day}2359]"
    )

    client = session or requests.Session()
    client.headers.setdefault("User-Agent", USER_AGENT)

    papers: list[dict[str, Any]] = []
    total_results: Optional[int] = None
    start = 0

    while total_results is None or start < total_results:
        params = {
            "search_query": search_query,
            "start": start,
            "max_results": batch_size,
            "sortBy": "submittedDate",
            "sortOrder": "ascending",
        }
        try:
            response = client.get(ARXIV_API_URL, params=params, timeout=timeout)
            response.raise_for_status()
        except requests.RequestException as exc:
            raise ArxivAPIError(f"请求 arXiv API 失败: {exc}") from exc

        page_total, page_papers = _parse_feed(response.content)
        total_results = page_total
        papers.extend(page_papers)
        start += len(page_papers)

        if not page_papers or start >= total_results:
            break
        if request_delay:
            time.sleep(request_delay)

    return {
        "author": author,
        "days": days,
        "date_from": query_start.isoformat(),
        "date_to": query_end.isoformat(),
        "timezone": "UTC",
        "total_results": len(papers),
        "papers": papers,
    }


def fetch_author_papers_json(
    author: str,
    days: int = DEFAULT_DAYS,
    **kwargs: Any,
) -> str:
    """查询论文并返回 JSON 字符串。"""
    result = fetch_author_papers(author, days, **kwargs)
    return json.dumps(result, ensure_ascii=False, indent=2)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="查询某位作者最近若干个 UTC 自然日提交到 arXiv 的所有论文"
    )
    parser.add_argument("author", help='作者姓名，例如 "Geoffrey Hinton"')
    parser.add_argument(
        "--days",
        type=int,
        default=DEFAULT_DAYS,
        help=f"查询最近多少天（包含今天；默认 {DEFAULT_DAYS}）",
    )
    parser.add_argument(
        "--end-date",
        help="查询截止日期，格式 YYYY-MM-DD；默认使用当前 UTC 日期",
    )
    parser.add_argument("-o", "--output", type=Path, help="保存 JSON 的文件路径")
    parser.add_argument("--compact", action="store_true", help="输出紧凑 JSON")
    return parser


def main() -> int:
    args = _build_parser().parse_args()
    try:
        result = fetch_author_papers(
            args.author,
            args.days,
            end_date=args.end_date,
        )
    except (ValueError, ArxivAPIError) as exc:
        error_json = json.dumps(
            {"ok": False, "error": str(exc)}, ensure_ascii=False, indent=2
        )
        print(error_json, file=sys.stderr)
        return 1

    indent = None if args.compact else 2
    output = json.dumps(result, ensure_ascii=False, indent=indent) + "\n"
    if args.output:
        args.output.write_text(output, encoding="utf-8")
    else:
        sys.stdout.write(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
