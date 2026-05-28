"""
ArxivWatcher — 多领域论文精读工具

功能特性:
  - 支持多个 arXiv 分类，自动合并去重（含跨领域 cross-list 论文）
  - 使用 OpenAI 兼容格式 (/v1/chat/completions)，支持自定义 API URL
    可接入: OpenAI, Anthropic (via proxy), DeepSeek, vLLM, Ollama, LiteLLM 等
  - 下载 PDF 并提取全文，调用 LLM 深度解读
  - 生成精美 HTML 报告，支持邮件发送

使用方法:
    # ── 基本配置 ──
    export LLM_API_KEY="sk-..."
    export LLM_BASE_URL="https://api.openai.com/v1"      # OpenAI
    export LLM_MODEL="gpt-4o"

    # ── 单领域 ──
    python arxiv_daily_digest.py --category eess.AS

    # ── 多领域（自动去重跨领域论文）──
    python arxiv_daily_digest.py --category eess.AS cs.SD cs.CL

    # ── 仅生成 HTML ──
    python arxiv_daily_digest.py --category eess.AS cs.SD --no-email

    # ── 限制数量 ──
    python arxiv_daily_digest.py --category eess.AS --max-papers 5

    # ── 邮件发送 ──
    export SMTP_HOST="smtp.gmail.com"
    export SMTP_PORT=587
    export SMTP_USER="your@gmail.com"
    export SMTP_PASS="your-app-password"
    export EMAIL_TO="recipient@example.com"
    python arxiv_daily_digest.py --category eess.AS cs.SD
"""

import os
import re
import sys
import time
import json
import logging
import argparse
import smtplib
from pathlib import Path
from datetime import datetime, timezone
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from dataclasses import dataclass
from typing import Optional

import requests
from bs4 import BeautifulSoup
from pypdf import PdfReader
import markdown
# ─────────────────────────────────────────────
# 配置 & 常量
# ─────────────────────────────────────────────

ARXIV_NEW_URL = "https://arxiv.org/list/{category}/new"
ARXIV_PDF_URL = "https://arxiv.org/pdf/{paper_id}"
ARXIV_ABS_URL = "https://arxiv.org/abs/{paper_id}"

# LLM 默认配置（均可通过环境变量或命令行覆盖）
DEFAULT_LLM_BASE_URL = "https://api.openai.com/v1"
DEFAULT_LLM_MODEL = "gpt-4o"
DEFAULT_MAX_TOKENS = 4096

REQUEST_DELAY = 2        # arXiv 礼貌爬取间隔（秒）
PDF_DOWNLOAD_TIMEOUT = 60
MAX_TEXT_LENGTH = 80000  # 发送给 LLM 的最大字符数

# arXiv 分类名称映射（常用领域）
CATEGORY_NAMES = {
    "eess.AS": "Audio and Speech Processing",
    "eess.SP": "Signal Processing",
    "eess.IV": "Image and Video Processing",
    "cs.SD":   "Sound",
    "cs.CL":   "Computation and Language",
    "cs.CV":   "Computer Vision",
    "cs.AI":   "Artificial Intelligence",
    "cs.LG":   "Machine Learning",
    "cs.IR":   "Information Retrieval",
    "cs.MM":   "Multimedia",
    "cs.RO":   "Robotics",
    "cs.HC":   "Human-Computer Interaction",
    "stat.ML": "Machine Learning (stat)",
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)


# ─────────────────────────────────────────────
# 数据模型
# ─────────────────────────────────────────────

@dataclass
class Paper:
    paper_id: str
    title: str
    authors: list[str]
    subjects: str
    abstract: str
    pdf_url: str
    abs_url: str
    source_categories: list[str]   # 来自哪些分类页面
    is_cross_list: bool = False    # 是否为跨领域论文
    primary_category: str = ""     # 主分类
    full_text: str = ""
    analysis: str = ""
    error: Optional[str] = None


@dataclass
class LLMConfig:
    """OpenAI 兼容 API 配置。"""
    base_url: str = DEFAULT_LLM_BASE_URL
    api_key: str = ""
    model: str = DEFAULT_LLM_MODEL
    max_tokens: int = DEFAULT_MAX_TOKENS
    temperature: float = 0.3
    enable_thinking: bool = False

    @property
    def chat_completions_url(self) -> str:
        """构造 /chat/completions 端点 URL。"""
        base = self.base_url.rstrip("/")
        # 如果用户已经给了完整的 /chat/completions 路径，直接用
        if base.endswith("/chat/completions"):
            return base
        # 如果结尾是 /v1，拼上 /chat/completions
        if base.endswith("/v1"):
            return f"{base}/chat/completions"
        # 否则尝试拼完整路径
        return f"{base}/v1/chat/completions"


# ─────────────────────────────────────────────
# 步骤 1 & 2: 爬取 arXiv 论文列表（支持多分类）
# ─────────────────────────────────────────────

def fetch_paper_list(category: str) -> list[Paper]:
    """从 arXiv /list/{category}/new 页面获取每日新论文列表。"""
    url = ARXIV_NEW_URL.format(category=category)
    log.info(f"正在获取论文列表: {url}")

    headers = {
        "User-Agent": "arXiv-Daily-Digest/2.0 (Academic Research Tool)"
    }
    resp = requests.get(url, headers=headers, timeout=30)
    resp.raise_for_status()

    soup = BeautifulSoup(resp.text, "html.parser")
    papers = []

    dl_items = soup.find("div", id="dlpage")
    if not dl_items:
        log.warning(f"[{category}] 未找到论文列表容器")
        return papers

    dt_list = dl_items.find_all("dt")
    dd_list = dl_items.find_all("dd")

    # 解析分区: New submissions / Cross-lists / Replacements
    h3_tags = dl_items.find_all("h3")
    sections = _parse_sections(h3_tags, len(dt_list))

    new_end = sections.get("cross_start", sections.get("replace_start", len(dt_list)))
    cross_end = sections.get("replace_start", len(dt_list))

    log.info(f"[{category}] 新提交: 0-{new_end}, 跨领域: {sections.get('cross_start', 'N/A')}-{cross_end}")

    for i, (dt, dd) in enumerate(zip(dt_list, dd_list)):
        # 跳过 replacements
        if i >= cross_end:
            break

        try:
            paper = _parse_paper_entry(dt, dd, category)
            if paper:
                if sections.get("cross_start") is not None and i >= sections["cross_start"]:
                    paper.is_cross_list = True
                papers.append(paper)
        except Exception as e:
            log.warning(f"[{category}] 解析第 {i+1} 篇论文时出错: {e}")
            continue

    log.info(f"[{category}] 解析完成: {len(papers)} 篇（含跨领域）")
    return papers


def _parse_sections(h3_tags, total_count: int) -> dict:
    """解析 arXiv new listing 页面的分区边界。"""
    sections = {}

    for h3 in h3_tags:
        text = h3.get_text(strip=True).lower()

        if "cross-list" in text or "cross list" in text:
            sections["_cross_h3"] = h3

        elif "replacement" in text:
            sections["_replace_h3"] = h3

        elif "new submissions" in text:
            match = re.search(r"of\s+(\d+)\s+entries", text)
            if match:
                sections["new_count"] = int(match.group(1))

    # 用 new_count 推算 cross_start
    if "new_count" in sections:
        sections["cross_start"] = sections["new_count"]

    if "_cross_h3" in sections and "cross_start" not in sections:
        sections["cross_start"] = _count_dt_before(sections["_cross_h3"])

    if "_replace_h3" in sections:
        replace_start = _count_dt_before(sections["_replace_h3"])
        if replace_start > 0:
            sections["replace_start"] = replace_start
        elif "cross_start" in sections and "_cross_h3" in sections:
            cross_text = sections["_cross_h3"].get_text(strip=True).lower()
            cross_match = re.search(r"of\s+(\d+)\s+entries", cross_text)
            if cross_match:
                sections["replace_start"] = sections["cross_start"] + int(cross_match.group(1))

    # 清理临时键
    sections.pop("_cross_h3", None)
    sections.pop("_replace_h3", None)
    sections.pop("new_count", None)

    return sections


def _count_dt_before(h3_tag) -> int:
    """计算某个 h3 标签之前有多少 <dt> 元素。"""
    count = 0
    for sibling in h3_tag.previous_siblings:
        if getattr(sibling, "name", None) == "dt":
            count += 1
    return count


def _parse_paper_entry(dt, dd, source_category: str) -> Optional[Paper]:
    """解析单个论文条目。"""
    link_tag = dt.find("a", title="Abstract")
    if not link_tag:
        link_tag = dt.find("a", href=re.compile(r"/abs/"))
    if not link_tag:
        return None

    href = link_tag.get("href", "")
    paper_id = href.replace("/abs/", "").strip()
    if not paper_id:
        return None

    meta = dd.find("div", class_="meta")
    if not meta:
        return None

    # 标题
    title_div = meta.find("div", class_="list-title")
    title = title_div.get_text(strip=True) if title_div else "Unknown Title"
    title = re.sub(r"^Title:\s*", "", title)

    # 作者
    authors_div = meta.find("div", class_="list-authors")
    authors = []
    if authors_div:
        for a_tag in authors_div.find_all("a"):
            authors.append(a_tag.get_text(strip=True))
    if not authors:
        authors = ["Unknown"]

    # 学科
    subjects_div = meta.find("div", class_="list-subjects")
    subjects = subjects_div.get_text(strip=True) if subjects_div else ""
    subjects = re.sub(r"^Subjects:\s*", "", subjects)

    # 主分类
    primary_category = ""
    if subjects_div:
        primary_span = subjects_div.find("span", class_="primary-subject")
        if primary_span:
            cat_match = re.search(r"\(([^)]+)\)", primary_span.get_text(strip=True))
            if cat_match:
                primary_category = cat_match.group(1)

    # 摘要
    abstract_tag = meta.find("p", class_="mathjax")
    abstract = ""
    if abstract_tag:
        abstract = abstract_tag.get_text(separator=" ", strip=True)

    return Paper(
        paper_id=paper_id,
        title=title,
        authors=authors,
        subjects=subjects,
        abstract=abstract,
        pdf_url=ARXIV_PDF_URL.format(paper_id=paper_id),
        abs_url=ARXIV_ABS_URL.format(paper_id=paper_id),
        source_categories=[source_category],
        primary_category=primary_category,
    )


def fetch_all_categories(categories: list[str]) -> list[Paper]:
    """从多个分类获取论文并合并去重。"""
    paper_map: dict[str, Paper] = {}

    for cat in categories:
        papers = fetch_paper_list(cat)
        time.sleep(REQUEST_DELAY)

        for p in papers:
            if p.paper_id in paper_map:
                existing = paper_map[p.paper_id]
                if cat not in existing.source_categories:
                    existing.source_categories.append(cat)
                if p.is_cross_list:
                    existing.is_cross_list = True
            else:
                paper_map[p.paper_id] = p

    merged = list(paper_map.values())
    cross_count = sum(1 for p in merged if p.is_cross_list or len(p.source_categories) > 1)
    log.info(f"合并去重完成: {len(merged)} 篇唯一论文（{cross_count} 篇跨领域）")
    return merged


# ─────────────────────────────────────────────
# 步骤 3: 下载 PDF
# ─────────────────────────────────────────────

def download_pdf(paper: Paper, output_dir: Path) -> Optional[Path]:
    """下载论文 PDF。"""
    safe_name = re.sub(r"[^\w\-.]", "_", paper.paper_id) + ".pdf"
    pdf_path = output_dir / safe_name

    if pdf_path.exists():
        log.info(f"  PDF 已存在: {pdf_path.name}")
        return pdf_path

    log.info(f"  正在下载 PDF: {paper.pdf_url}")
    headers = {"User-Agent": "arXiv-Daily-Digest/2.0 (Academic Research Tool)"}
    try:
        resp = requests.get(paper.pdf_url, headers=headers, timeout=PDF_DOWNLOAD_TIMEOUT, stream=True)
        resp.raise_for_status()
        with open(pdf_path, "wb") as f:
            for chunk in resp.iter_content(chunk_size=8192):
                f.write(chunk)
        log.info(f"  下载完成: {pdf_path.name} ({pdf_path.stat().st_size / 1024:.0f} KB)")
        return pdf_path
    except Exception as e:
        log.error(f"  下载失败: {e}")
        return None


# ─────────────────────────────────────────────
# 步骤 4: PDF 文本提取
# ─────────────────────────────────────────────

def extract_text_from_pdf(pdf_path: Path) -> str:
    """从 PDF 提取文本。"""
    log.info(f"  正在提取文本: {pdf_path.name}")
    try:
        reader = PdfReader(str(pdf_path))
        text_parts = []
        for page in reader.pages:
            page_text = page.extract_text()
            if page_text:
                text_parts.append(page_text)
        full_text = "\n\n".join(text_parts)
        full_text = re.sub(r"\n{3,}", "\n\n", full_text)
        full_text = re.sub(r"[ \t]{2,}", " ", full_text)
        log.info(f"  提取完成: {len(full_text)} 字符, {len(reader.pages)} 页")
        return full_text
    except Exception as e:
        log.error(f"  文本提取失败: {e}")
        return ""


# ─────────────────────────────────────────────
# 步骤 5: LLM 解读 (OpenAI 兼容格式)
# ─────────────────────────────────────────────

ANALYSIS_SYSTEM_PROMPT = """你是一位资深的学术论文解读专家。请仔细阅读用户提供的论文全文，并按照以下框架输出结构化的中文解读报告。

## 输出格式要求（请严格遵守）

### 1. 一句话总结
用一句通俗易懂的话概括这篇论文做了什么、解决了什么问题。

### 2. 研究背景与动机
- 这篇论文要解决的核心问题是什么？
- 该问题为什么重要？
- 现有方法存在哪些不足？

### 3. 核心方法
- 论文提出的方法/模型/框架是什么？
- 关键创新点有哪些？（列出 2-4 个）
- 用直觉性的语言解释方法的核心思路，避免堆砌公式。

### 4. 实验与结果
- 使用了哪些数据集/基准？
- 对比了哪些基线方法？
- 主要实验结果如何？（突出最关键的数字）
- 消融实验揭示了什么？

### 5. 优势与局限
- 本文方法的主要优势（2-3 点）
- 局限性（2-3 点）

### 6. 关键结论与启发
- 论文最重要的 takeaway 是什么？
- 对后续研究有什么启发或可能的延伸方向？

## 写作风格
- 语言简洁清晰，用自己的话重新组织，不要照搬原文
- 对复杂概念提供类比或直觉解释
- 保持客观，区分"论文声称的"和"实际展示的"
- 如果全文提取不完整导致某些部分缺失，请如实说明"""


def analyze_paper_with_llm(paper: Paper, llm_config: LLMConfig) -> str:
    """使用 OpenAI 兼容 API (/v1/chat/completions) 调用 LLM。

    兼容: OpenAI, DeepSeek, vLLM, Ollama, LiteLLM, Azure OpenAI 等。
    """
    log.info(f"  正在调用 LLM ({llm_config.model}): {paper.title[:50]}...")

    text_to_send = paper.full_text
    if len(text_to_send) > MAX_TEXT_LENGTH:
        log.info(f"  文本过长 ({len(text_to_send)} 字符)，截断至 {MAX_TEXT_LENGTH}")
        text_to_send = text_to_send[:MAX_TEXT_LENGTH] + "\n\n[... 文本已截断 ...]"

    if not text_to_send.strip():
        log.warning("  全文为空，使用摘要代替")
        text_to_send = (
            f"标题: {paper.title}\n"
            f"作者: {', '.join(paper.authors)}\n"
            f"分类: {paper.subjects}\n"
            f"摘要:\n{paper.abstract}"
        )

    user_message = f"请解读以下论文：\n\n<paper>\n{text_to_send}\n</paper>"

    # ── OpenAI 兼容请求 ──
    headers = {"Content-Type": "application/json"}
    if llm_config.api_key:
        headers["Authorization"] = f"Bearer {llm_config.api_key}"

    payload = {
        "model": llm_config.model,
        "max_tokens": llm_config.max_tokens,
        "temperature": llm_config.temperature,
        "messages": [
            {"role": "system", "content": ANALYSIS_SYSTEM_PROMPT},
            {"role": "user", "content": user_message},
        ],
    }

    api_url = llm_config.chat_completions_url
    log.info(f"  API 端点: {api_url}")

    try:
        resp = requests.post(api_url, headers=headers, json=payload, timeout=180)
        resp.raise_for_status()
        data = resp.json()

        # 标准 OpenAI 格式: data["choices"][0]["message"]["content"]
        choices = data.get("choices", [])
        if choices:
            analysis = choices[0].get("message", {}).get("content", "")
        else:
            analysis = ""

        if not analysis:
            log.warning(f"  LLM 返回为空，响应: {json.dumps(data, ensure_ascii=False)[:300]}")
            return "[LLM 返回为空]"

        log.info(f"  LLM 解读完成: {len(analysis)} 字符")

        usage = data.get("usage", {})
        if usage:
            log.info(
                f"  Token: prompt={usage.get('prompt_tokens', '?')}, "
                f"completion={usage.get('completion_tokens', '?')}, "
                f"total={usage.get('total_tokens', '?')}"
            )
        return analysis

    except requests.exceptions.HTTPError as e:
        status = e.response.status_code
        body = e.response.text[:300]
        log.error(f"  API 错误 (HTTP {status}): {body}")
        return f"[LLM 解读失败: HTTP {status}]"
    except requests.exceptions.ConnectionError as e:
        log.error(f"  无法连接: {api_url} — {e}")
        return f"[LLM 连接失败: {api_url}]"
    except Exception as e:
        log.error(f"  API 异常: {e}")
        return f"[LLM 解读失败: {e}]"


# ─────────────────────────────────────────────
# 步骤 6: 生成 HTML 报告
# ─────────────────────────────────────────────

def generate_html_report(papers: list[Paper], categories: list[str], llm_config: LLMConfig) -> str:
    """汇总为 HTML 报告。"""
    today = datetime.now(timezone.utc).strftime("%Y年%m月%d日")
    successful = [p for p in papers if p.analysis and not p.analysis.startswith("[LLM")]
    failed = [p for p in papers if not p.analysis or p.analysis.startswith("[LLM")]
    cross_count = sum(1 for p in papers if p.is_cross_list or len(p.source_categories) > 1)

    cat_labels = " / ".join(categories)
    cat_names = ", ".join(CATEGORY_NAMES.get(c, c) for c in categories)

    papers_html = ""
    for i, paper in enumerate(papers, 1):
        authors_str = ", ".join(paper.authors[:5])
        if len(paper.authors) > 5:
            authors_str += f" 等 ({len(paper.authors)} 人)"

        analysis_html = _markdown_to_html(paper.analysis) if paper.analysis else "<p>解读暂不可用</p>"

        badges = ""
        if paper.error or paper.analysis.startswith("[LLM"):
            badges += '<span class="badge badge-error">解读失败</span>'
        if paper.is_cross_list or len(paper.source_categories) > 1:
            badges += '<span class="badge badge-cross">跨领域</span>'

        source_tags = "".join(
            f'<span class="cat-tag">{_escape_html(cat)}</span>' for cat in paper.source_categories
        )

        papers_html += f"""
        <article class="paper" id="paper-{i}">
            <div class="paper-header">
                <div class="paper-top-row">
                    <span class="paper-index">#{i}</span>
                    <div class="cat-tags">{source_tags}</div>
                </div>
                <h2 class="paper-title">
                    <a href="{paper.abs_url}" target="_blank">{_escape_html(paper.title)}</a>
                    {badges}
                </h2>
                <div class="paper-meta">
                    <div class="meta-item">
                        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2"/><circle cx="12" cy="7" r="4"/></svg>
                        {_escape_html(authors_str)}
                    </div>
                    <div class="meta-item">
                        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M4 19.5A2.5 2.5 0 0 1 6.5 17H20"/><path d="M6.5 2H20v20H6.5A2.5 2.5 0 0 1 4 19.5v-15A2.5 2.5 0 0 1 6.5 2z"/></svg>
                        {_escape_html(paper.subjects)}
                    </div>
                    <div class="meta-item links">
                        <a href="{paper.abs_url}" target="_blank">📄 Abstract</a>
                        <a href="{paper.pdf_url}" target="_blank">📥 PDF</a>
                    </div>
                </div>
            </div>
            <details class="abstract-toggle">
                <summary>查看摘要</summary>
                <div class="abstract-content">{_escape_html(paper.abstract)}</div>
            </details>
            <div class="analysis">
                <h3>📖 深度解读</h3>
                {analysis_html}
            </div>
        </article>"""

    # 目录
    toc_html = ""
    for i, paper in enumerate(papers, 1):
        short_title = paper.title[:80] + ("..." if len(paper.title) > 80 else "")
        cross_mark = " 🔀" if (paper.is_cross_list or len(paper.source_categories) > 1) else ""
        toc_html += f'<li><a href="#paper-{i}">{_escape_html(short_title)}{cross_mark}</a></li>\n'

    html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>arXiv 每日论文精读 — {_escape_html(cat_labels)} | {today}</title>
<style>
  @import url('https://fonts.googleapis.com/css2?family=Noto+Serif+SC:wght@400;600;700&family=JetBrains+Mono:wght@400;500&display=swap');
  :root {{
    --bg:#fafaf8; --fg:#1a1a1a; --fg-muted:#6b7280; --accent:#d14d41;
    --accent-light:#fef2f2; --cross-color:#7c3aed; --cross-bg:#f5f3ff;
    --border:#e5e5e2; --card-bg:#fff;
    --card-shadow:0 1px 3px rgba(0,0,0,.06),0 1px 2px rgba(0,0,0,.04);
    --radius:8px;
    --font-body:'Noto Serif SC',Georgia,serif;
    --font-mono:'JetBrains Mono',monospace;
  }}
  *{{box-sizing:border-box;margin:0;padding:0}}
  body{{font-family:var(--font-body);background:var(--bg);color:var(--fg);line-height:1.8;font-size:15px}}
  .container{{max-width:820px;margin:0 auto;padding:40px 24px}}
  .report-header{{text-align:center;padding:48px 0 40px;border-bottom:2px solid var(--fg);margin-bottom:40px}}
  .report-header h1{{font-size:28px;font-weight:700;letter-spacing:-.5px;margin-bottom:8px}}
  .report-header .subtitle{{color:var(--fg-muted);font-size:15px}}
  .report-header .subtitle-cats{{color:var(--fg-muted);font-size:13px;margin-top:4px}}
  .report-header .date{{display:inline-block;margin-top:16px;padding:4px 16px;background:var(--fg);color:var(--bg);font-size:13px;letter-spacing:1px;font-family:var(--font-mono)}}
  .report-header .model-info{{margin-top:8px;font-size:12px;color:var(--fg-muted);font-family:var(--font-mono)}}
  .stats{{display:flex;gap:24px;justify-content:center;margin-bottom:36px;flex-wrap:wrap}}
  .stat{{text-align:center}}
  .stat-num{{font-size:32px;font-weight:700;color:var(--accent);line-height:1}}
  .stat-num.cross{{color:var(--cross-color)}}
  .stat-label{{font-size:12px;color:var(--fg-muted);text-transform:uppercase;letter-spacing:1px;margin-top:4px}}
  .toc{{background:var(--card-bg);border:1px solid var(--border);border-radius:var(--radius);padding:24px 28px;margin-bottom:40px}}
  .toc h2{{font-size:14px;text-transform:uppercase;letter-spacing:1.5px;color:var(--fg-muted);margin-bottom:12px}}
  .toc ol{{padding-left:20px}}
  .toc li{{margin-bottom:6px;font-size:14px;line-height:1.5}}
  .toc a{{color:var(--fg);text-decoration:none;border-bottom:1px solid var(--border);transition:border-color .2s}}
  .toc a:hover{{border-color:var(--accent);color:var(--accent)}}
  .paper{{background:var(--card-bg);border:1px solid var(--border);border-radius:var(--radius);padding:32px;margin-bottom:32px;box-shadow:var(--card-shadow)}}
  .paper-header{{margin-bottom:20px}}
  .paper-top-row{{display:flex;align-items:center;gap:12px;margin-bottom:8px;flex-wrap:wrap}}
  .paper-index{{font-family:var(--font-mono);font-size:12px;color:var(--accent);font-weight:500}}
  .cat-tags{{display:flex;gap:6px;flex-wrap:wrap}}
  .cat-tag{{display:inline-block;font-family:var(--font-mono);font-size:11px;padding:2px 8px;background:#f0f0ee;border-radius:4px;color:var(--fg-muted)}}
  .paper-title{{font-size:20px;font-weight:700;line-height:1.4;margin-bottom:12px}}
  .paper-title a{{color:inherit;text-decoration:none;border-bottom:2px solid transparent;transition:border-color .2s}}
  .paper-title a:hover{{border-color:var(--accent)}}
  .paper-meta{{display:flex;flex-wrap:wrap;gap:12px 24px;font-size:13px;color:var(--fg-muted)}}
  .meta-item{{display:flex;align-items:center;gap:6px}}
  .meta-item.links{{gap:12px}}
  .meta-item.links a{{color:var(--accent);text-decoration:none;font-weight:500}}
  .badge{{display:inline-block;font-size:11px;padding:2px 8px;border-radius:4px;font-weight:500;vertical-align:middle;margin-left:8px}}
  .badge-error{{background:#fef2f2;color:#dc2626}}
  .badge-cross{{background:var(--cross-bg);color:var(--cross-color)}}
  .abstract-toggle{{margin-bottom:20px;border:1px solid var(--border);border-radius:6px;overflow:hidden}}
  .abstract-toggle summary{{padding:10px 16px;font-size:13px;font-weight:600;cursor:pointer;background:#f9f9f7;color:var(--fg-muted);user-select:none}}
  .abstract-toggle summary:hover{{color:var(--fg)}}
  .abstract-content{{padding:16px;font-size:14px;color:var(--fg-muted);line-height:1.7;border-top:1px solid var(--border)}}
  .analysis h3{{font-size:16px;font-weight:700;margin-bottom:16px;padding-bottom:8px;border-bottom:1px solid var(--border)}}
  .analysis h4{{font-size:15px;font-weight:700;margin:20px 0 8px;color:var(--accent)}}
  .analysis p{{margin-bottom:10px}}
  .analysis ul,.analysis ol{{margin:8px 0 12px 20px}}
  .analysis li{{margin-bottom:4px}}
  .analysis strong{{font-weight:600}}
  .analysis code{{font-family:var(--font-mono);font-size:13px;background:#f3f3f0;padding:2px 6px;border-radius:3px}}
  .report-footer{{text-align:center;padding:32px 0;border-top:2px solid var(--fg);margin-top:40px;font-size:13px;color:var(--fg-muted)}}
  .report-footer a{{color:var(--accent);text-decoration:none}}
  @media(max-width:640px){{.container{{padding:20px 16px}}.paper{{padding:20px}}.report-header h1{{font-size:22px}}.stats{{gap:16px}}}}
</style>
</head>
<body>
<div class="container">
  <header class="report-header">
    <h1>arXiv 每日论文精读</h1>
    <div class="subtitle">📡 {_escape_html(cat_labels)}</div>
    <div class="subtitle-cats">{_escape_html(cat_names)}</div>
    <div class="date">{today}</div>
    <div class="model-info">LLM: {_escape_html(llm_config.model)}</div>
  </header>
  <div class="stats">
    <div class="stat"><div class="stat-num">{len(papers)}</div><div class="stat-label">论文总数</div></div>
    <div class="stat"><div class="stat-num cross">{cross_count}</div><div class="stat-label">跨领域</div></div>
    <div class="stat"><div class="stat-num">{len(successful)}</div><div class="stat-label">成功解读</div></div>
    <div class="stat"><div class="stat-num">{len(failed)}</div><div class="stat-label">待处理</div></div>
  </div>
  <nav class="toc"><h2>目录 (🔀 = 跨领域)</h2><ol>{toc_html}</ol></nav>
  {papers_html}
  <footer class="report-footer">
    <p>数据来源: <a href="https://arxiv.org" target="_blank">arXiv.org</a></p>
    <p>LLM: {_escape_html(llm_config.model)}</p>
  </footer>
</div>
</body>
</html>"""
    return html


def _escape_html(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def _markdown_to_html(md: str) -> str:
    """Markdown → HTML（使用 python-markdown）。"""
    return markdown.markdown(
        md,
        extensions=["tables", "fenced_code", "nl2br", "sane_lists"],
    )

# ─────────────────────────────────────────────
# 邮件发送
# ─────────────────────────────────────────────

def send_email(html_content: str, categories: list[str], config: dict):
    """通过 SMTP 发送 HTML 邮件。"""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    cat_str = " / ".join(categories)
    subject = f"📚 arXiv 每日精读 [{cat_str}] — {today}"

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = config["smtp_user"]
    msg["To"] = config["email_to"]

    msg.attach(MIMEText(
        f"arXiv 每日论文精读报告 ({cat_str})\n\n请使用支持 HTML 的邮件客户端查看完整报告。",
        "plain", "utf-8",
    ))
    msg.attach(MIMEText(html_content, "html", "utf-8"))

    log.info(f"正在发送邮件至 {config['email_to']}...")
    try:
        with smtplib.SMTP(config["smtp_host"]) as server:
            server.starttls()
            server.login(config["smtp_user"], config["smtp_pass"])
            server.sendmail(config["smtp_user"], config["email_to"], msg.as_string())
        log.info("邮件发送成功！")
    except Exception as e:
        log.error(f"邮件发送失败: {e}")
        raise


# ─────────────────────────────────────────────
# 主流程
# ─────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="arXiv 每日论文精读 — 多领域 + OpenAI 兼容 API",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  # 单领域
  python arxiv_daily_digest.py --category eess.AS

  # 多领域（跨领域自动去重）
  python arxiv_daily_digest.py --category eess.AS cs.SD cs.CL

  # 使用 DeepSeek
  python arxiv_daily_digest.py --category eess.AS \\
    --llm-base-url https://api.deepseek.com/v1 \\
    --llm-model deepseek-chat

  # 使用本地 Ollama
  python arxiv_daily_digest.py --category cs.CL \\
    --llm-base-url http://localhost:11434/v1 \\
    --llm-model qwen2.5:72b \\
    --llm-api-key ollama
        """,
    )

    # arXiv
    parser.add_argument("--category", nargs="+", default=["eess.AS"],
                        help="arXiv 分类，支持多个 (默认: eess.AS)")
    parser.add_argument("--max-papers", type=int, default=None, help="最多处理数量")
    parser.add_argument("--no-cross-list", action="store_true", help="排除跨领域论文")

    # LLM (OpenAI 兼容)
    parser.add_argument("--llm-base-url", default=None,
                        help="API Base URL (默认: $LLM_BASE_URL 或 https://api.openai.com/v1)")
    parser.add_argument("--llm-model", default=None,
                        help="模型名 (默认: $LLM_MODEL 或 gpt-4o)")
    parser.add_argument("--llm-api-key", default=None,
                        help="API Key (默认: $LLM_API_KEY)")
    parser.add_argument("--llm-max-tokens", type=int, default=None,
                        help="最大输出 token (默认: 4096)")

    # 输出
    parser.add_argument("--no-email", action="store_true", help="不发送邮件")
    parser.add_argument("--output", default=None, help="HTML 输出路径")
    parser.add_argument("--only-download", action="store_true", help="仅下载 PDF")

    args = parser.parse_args()

    # ── LLM 配置: 命令行 > 环境变量 > 默认值 ──
    llm_config = LLMConfig(
        base_url=args.llm_base_url or os.environ.get("LLM_BASE_URL", DEFAULT_LLM_BASE_URL),
        api_key=args.llm_api_key or os.environ.get("LLM_API_KEY", ""),
        model=args.llm_model or os.environ.get("LLM_MODEL", DEFAULT_LLM_MODEL),
        max_tokens=args.llm_max_tokens or int(os.environ.get("LLM_MAX_TOKENS", str(DEFAULT_MAX_TOKENS))),
    )

    if not llm_config.api_key:
        log.error("请设置 LLM API Key: --llm-api-key 或 export LLM_API_KEY=...")
        sys.exit(1)

    log.info("=" * 60)
    log.info("arXiv 每日论文精读 v2.0")
    log.info("=" * 60)
    log.info(f"分类: {', '.join(args.category)}")
    log.info(f"LLM:  {llm_config.model} @ {llm_config.base_url}")
    log.info(f"端点: {llm_config.chat_completions_url}")

    # 工作目录
    work_dir = Path("arxiv_digest_work")
    pdf_dir = work_dir / "pdfs"
    pdf_dir.mkdir(parents=True, exist_ok=True)

    # ── 获取论文（多分类合并去重）──
    if len(args.category) == 1:
        papers = fetch_paper_list(args.category[0])
    else:
        papers = fetch_all_categories(args.category)

    if args.no_cross_list:
        before = len(papers)
        papers = [p for p in papers if not p.is_cross_list]
        log.info(f"排除跨领域: {len(papers)} 篇 (移除 {before - len(papers)})")

    if not papers:
        log.warning("今日无新论文，退出")
        sys.exit(0)

    if args.max_papers:
        papers = papers[: args.max_papers]
        log.info(f"限制: {len(papers)} 篇")
    if args.only_download:
        log.info("正在下载 PDF...")
        for paper in papers:
            print(paper.title)
            download_pdf(paper, pdf_dir)
        log.info("PDF 下载完成")
        sys.exit(0)
        
    # ── 逐篇处理 ──
    for i, paper in enumerate(papers, 1):
        cross_mark = " [跨领域]" if (paper.is_cross_list or len(paper.source_categories) > 1) else ""
        log.info(f"\n[{i}/{len(papers)}] {paper.title[:60]}...{cross_mark}")

        pdf_path = download_pdf(paper, pdf_dir)
        time.sleep(REQUEST_DELAY)

        if not pdf_path:
            paper.error = "PDF 下载失败"
            paper.analysis = "[PDF 下载失败，无法解读]"
            continue

        paper.full_text = extract_text_from_pdf(pdf_path)
        if not paper.full_text.strip():
            log.warning("  文本提取为空，将使用摘要")

        paper.analysis = analyze_paper_with_llm(paper, llm_config)
        time.sleep(1)

    # ── 生成报告 ──
    log.info("\n正在生成 HTML 报告...")
    html_report = generate_html_report(papers, args.category, llm_config)

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    output_path = args.output or f"report_{today}.html"
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(html_report)
    log.info(f"HTML 报告已保存: {output_path}")

    # ── 邮件 ──
    if not args.no_email:
        email_config = {
            "smtp_host": os.environ.get("SMTP_HOST", "smtp.gmail.com"),
            # "smtp_port": int(os.environ.get("SMTP_PORT", "587")),
            "smtp_user": os.environ.get("SMTP_USER", ""),
            "smtp_pass": os.environ.get("SMTP_PASS", ""),
            "email_to": os.environ.get("EMAIL_TO", ""),
        }
        if not all([email_config["smtp_user"], email_config["smtp_pass"], email_config["email_to"]]):
            log.warning("邮件配置不完整，跳过。设置 SMTP_USER, SMTP_PASS, EMAIL_TO")
        else:
            try:
                send_email(html_report, args.category, email_config)
            except Exception as e:
                log.error(f"邮件发送失败: {e}", exc_info=True) 
    else:
        log.info("已跳过邮件发送 (--no-email)")

    log.info("\n✅ 全部完成！")


if __name__ == "__main__":
    main()