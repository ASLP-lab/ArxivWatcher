"""一次性脚本：从已有的 report_YYYY-MM-DD.html 反解析出 JSON 元数据。

历史报告早期没有写过 JSON 元数据（send.py 现在会自动写），用这个脚本一次性
把根目录 / reports/ 下的旧报告全部反解析为 data/papers/YYYY-MM-DD.json。

用法:
    python scripts/build_index.py
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from datetime import datetime
from typing import Optional

from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data" / "papers"
REPORTS_DIR = ROOT / "reports"

REPORT_FILE_RE = re.compile(r"^report_(\d{4}-\d{2}-\d{2})(?:[ _].*)?\.html$")


def find_report_files() -> dict[str, Path]:
    """收集所有 report_YYYY-MM-DD*.html 文件，按日期 → 最新文件返回。"""
    candidates: dict[str, list[Path]] = {}
    for path in list(ROOT.glob("report_*.html")) + list(REPORTS_DIR.glob("report_*.html")):
        m = REPORT_FILE_RE.match(path.name)
        if not m:
            continue
        candidates.setdefault(m.group(1), []).append(path)

    result: dict[str, Path] = {}
    for date_str, paths in candidates.items():
        # 取修改时间最新的文件
        paths.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        result[date_str] = paths[0]
    return result


def _meta_value(meta_div, label_keywords: list[str]) -> str:
    """从 meta-item 里依据 svg path / 顺序定位字段。这里通过文字内容拿。"""
    txt = meta_div.get_text(" ", strip=True)
    for kw in label_keywords:
        if kw in txt:
            return txt
    return ""


def _split_authors(text: str) -> tuple[list[str], int]:
    """解析 'a, b, c, d, e 等 (N 人)' 这样的字符串。返回 (作者列表, 实际总人数)。"""
    text = text.strip()
    total = 0
    m = re.search(r"等\s*\(\s*(\d+)\s*人\s*\)", text)
    if m:
        total = int(m.group(1))
        text = text[: m.start()].rstrip(" ,，等")
    parts = [p.strip() for p in re.split(r"[,，]", text) if p.strip()]
    if total == 0:
        total = len(parts)
    return parts, total


def _extract_categories(subjects_text: str) -> tuple[str, list[str]]:
    """从 'Audio and Speech Processing (eess.AS); ...' 提取分类代码列表 + 主分类。"""
    cats = re.findall(r"\(([a-zA-Z]+\.[a-zA-Z]+)\)", subjects_text)
    primary = cats[0] if cats else ""
    return primary, cats


def parse_report(html_path: Path, date_str: str) -> Optional[dict]:
    try:
        html = html_path.read_text(encoding="utf-8", errors="ignore")
    except Exception as e:
        print(f"[warn] 读取失败: {html_path}: {e}", file=sys.stderr)
        return None

    soup = BeautifulSoup(html, "html.parser")

    # 顶部分类
    subtitle = soup.select_one(".report-header .subtitle")
    if subtitle:
        cat_labels = [c.strip() for c in subtitle.get_text(strip=True).replace("📡", "").split("/") if c.strip()]
    else:
        cat_labels = []

    model_info = soup.select_one(".report-header .model-info")
    llm_model = ""
    skip_llm = False
    if model_info:
        t = model_info.get_text(strip=True)
        if "未使用" in t:
            skip_llm = True
        else:
            llm_model = t.replace("LLM:", "").strip()

    articles = soup.select("article.paper")
    papers = []
    for art in articles:
        # 标题 + abs_url
        title_a = art.select_one(".paper-title a")
        if not title_a:
            continue
        title = title_a.get_text(strip=True)
        abs_url = title_a.get("href", "")
        m = re.search(r"/abs/([^/?#]+)$", abs_url)
        paper_id = m.group(1) if m else ""

        # 源分类标签
        source_categories = [t.get_text(strip=True) for t in art.select(".cat-tag")]

        # 跨领域 badge
        is_cross_list = bool(art.select_one(".badge-cross"))

        # 各 meta 字段
        authors_list: list[str] = []
        subjects = ""
        comments = ""
        pdf_url = ""
        for meta in art.select(".paper-meta .meta-item"):
            text = meta.get_text(" ", strip=True)

            # Links
            links = meta.select("a")
            if "PDF" in text and "Abstract" in text and links:
                for a in links:
                    if "PDF" in a.get_text():
                        pdf_url = a.get("href", "")
                continue

            # 作者：通常包含逗号
            if any(c in text for c in [",", "，"]) and "Subjects:" not in text and "Comments:" not in text:
                if not subjects and ("Processing" in text or "Learning" in text or "Vision" in text or "Language" in text or "(" in text and ")" in text and re.search(r"\([a-z]+\.[A-Z]+\)", text)):
                    # 容易和分类混淆 — 用 svg path 区分
                    pass

            label_span = meta.select_one(".meta-label")
            if label_span and "Comments" in label_span.get_text():
                comments = text.replace("Comments:", "").strip()
                continue

            # 用 svg 第一段 path 区分 (用户/书本/对话气泡)
            svg_path = meta.select_one("svg path")
            d_attr = svg_path.get("d", "") if svg_path else ""
            if d_attr.startswith("M20 21v-2"):
                # 作者图标
                authors_list, _total = _split_authors(text)
            elif d_attr.startswith("M4 19.5"):
                # 学科图标
                subjects = text

        primary_category, subject_codes = _extract_categories(subjects)
        # 若 source_categories 解析失败 (老报告可能没有 cat-tag)，从 subjects 推断
        if not source_categories and subject_codes:
            source_categories = [subject_codes[0]]

        # 摘要
        abstract_div = art.select_one(".abstract-content")
        abstract = abstract_div.get_text(" ", strip=True) if abstract_div else ""

        # 深度解读 (markdown 转出的 HTML，这里只保留文字)
        analysis_div = art.select_one(".analysis")
        analysis_text = analysis_div.get_text("\n", strip=True) if analysis_div else ""

        papers.append({
            "paper_id": paper_id,
            "title": title,
            "authors": authors_list,
            "comments": comments,
            "subjects": subjects,
            "abstract": abstract,
            "pdf_url": pdf_url or (f"https://arxiv.org/pdf/{paper_id}" if paper_id else ""),
            "abs_url": abs_url,
            "source_categories": source_categories,
            "is_cross_list": is_cross_list,
            "primary_category": primary_category,
            "analysis": analysis_text,
            "error": None,
        })

    payload = {
        "date": date_str,
        "generated_at": datetime.fromtimestamp(html_path.stat().st_mtime).isoformat(),
        "categories": cat_labels,
        "llm_model": llm_model,
        "skip_llm_analysis": skip_llm,
        "total": len(papers),
        "cross_count": sum(1 for p in papers if p["is_cross_list"]),
        "papers": papers,
        "source_html": str(html_path.relative_to(ROOT)),
    }
    return payload


def main():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    found = find_report_files()
    if not found:
        print("未发现任何 report_*.html")
        return

    force = "--force" in sys.argv
    for date_str in sorted(found.keys()):
        out_path = DATA_DIR / f"{date_str}.json"
        html_path = found[date_str]
        if out_path.exists() and not force:
            print(f"[skip] {date_str} (已存在 {out_path.name}, 用 --force 覆盖)")
            continue
        payload = parse_report(html_path, date_str)
        if not payload:
            continue
        out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[ok]   {date_str} ← {html_path.name} ({payload['total']} 篇)")


if __name__ == "__main__":
    main()
