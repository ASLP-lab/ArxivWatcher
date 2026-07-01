"""
arXiv Daily Digest — 多领域论文精读工具

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

    # 或 DeepSeek
    export LLM_BASE_URL="https://api.deepseek.com/v1"
    export LLM_MODEL="deepseek-chat"

    # 或本地 Ollama
    export LLM_BASE_URL="http://localhost:11434/v1"
    export LLM_MODEL="qwen2.5:72b"
    export LLM_API_KEY="ollama"

    # 或 Anthropic (通过 LiteLLM proxy)
    export LLM_BASE_URL="http://localhost:4000/v1"
    export LLM_MODEL="claude-sonnet-4-20250514"

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
import random
import logging
import argparse
import smtplib
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from difflib import SequenceMatcher
from pathlib import Path
from datetime import datetime, timezone
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from dataclasses import dataclass, asdict, field, fields
from typing import Callable, Optional

try:
    from zoneinfo import ZoneInfo
except ImportError:
    from backports.zoneinfo import ZoneInfo  # type: ignore

import requests
from bs4 import BeautifulSoup
from pypdf import PdfReader
import markdown

BJ_TZ = ZoneInfo("Asia/Shanghai")


def now_bj() -> datetime:
    """返回北京时区当前时间。"""
    return datetime.now(BJ_TZ)


# 项目根目录（用于定位数据目录），优先使用 SEND_EMAIL_ROOT 环境变量
PROJECT_ROOT = Path(os.environ.get("SEND_EMAIL_ROOT", Path(__file__).resolve().parent))
DATA_DIR = PROJECT_ROOT / "data" / "papers"
REPORTS_DIR = PROJECT_ROOT / "reports"
ORG_KB_PATH = PROJECT_ROOT / "universities_companies_levels.jsonl"
TAXONOMY_PATH = PROJECT_ROOT / "speech_audio_taxonomy.json"
BLACKLIST_PATH = PROJECT_ROOT / "blacklist.txt"
# 飞书 / 离线测试用：默认使用已导出的论文快照（元数据 + 摘要等「总结」字段）
DEFAULT_TEST_FEISHU_JSON = DATA_DIR / "2026-05-15.json"
# ─────────────────────────────────────────────
# 配置 & 常量
# ─────────────────────────────────────────────

ARXIV_NEW_URL = "https://arxiv.org/list/{category}/new"
ARXIV_PDF_URL = "https://arxiv.org/pdf/{paper_id}"
ARXIV_ABS_URL = "https://arxiv.org/abs/{paper_id}"

# arXiv ID 格式：现代 YYMM.NNNNN（可带版本号 vN）
_ARXIV_ID_RE = re.compile(r"^\d{4}\.\d{4,5}(v\d+)?$", re.I)
_URL_RE = re.compile(r"^https?://", re.I)

# LLM 默认配置（均可通过环境变量或命令行覆盖）
DEFAULT_LLM_BASE_URL = "https://api.openai.com/v1"
DEFAULT_LLM_MODEL = "gpt-4o"
DEFAULT_MAX_TOKENS = 4096
DEFAULT_LLM_CONCURRENCY = 4

REQUEST_DELAY = 2        # arXiv 礼貌爬取间隔（秒）
PDF_DOWNLOAD_TIMEOUT = 60
MAX_TEXT_LENGTH = 80000  # 发送给 LLM 的最大字符数
CLASSIFY_RETRY_WAIT = 4   # 429 限流后等待秒数
CLASSIFY_MAX_RETRIES = 3    # 429 最多重试次数
ORG_SNIPPET_LENGTH = 300
ORG_MATCH_MIN_SCORE = 0.84
ORG_MATCH_MAX_RESULTS = 6
ORG_GENERIC_TOKENS = {
    "university", "universities", "institute", "institutes", "college", "school",
    "academy", "department", "faculty", "center", "centre", "laboratory", "lab",
    "research", "group", "hospital", "clinic", "company", "corporation", "corp",
    "inc", "ltd", "limited", "llc", "gmbh", "of", "the", "and", "for", "at", "in",
}

# 推荐等级阈值（与前端 app.js 的 paperGrade 保持一致）
GRADE_MUST_THRESHOLD = 8.0   # score >= 8 → 🔥 必读
GRADE_WORTH_THRESHOLD = 5.0  # 5 <= score < 8 → 👀 值得看；0 < score < 5 → 💤 可跳过

# 推荐等级定义：key → (emoji, label, css_class)
RECOMMEND_GRADES = {
    "must":  ("🔥", "必读",   "grade-must"),
    "worth": ("👀", "值得看", "grade-worth"),
    "skip":  ("💤", "可跳过", "grade-skip"),
}

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
    comments: str  # arXiv「Comments:」元数据（会议投稿说明等），无则为空
    subjects: str
    abstract: str
    pdf_url: str
    abs_url: str
    source_categories: list[str]   # 来自哪些分类页面
    is_cross_list: bool = False    # 是否为跨领域论文
    primary_category: str = ""     # 主分类
    full_text: str = ""
    analysis: str = ""
    related_org_titles: list[str] = field(default_factory=list)
    related_org_levels: list[str] = field(default_factory=list)
    org_detection_labels: list[str] = field(default_factory=list)
    # ── 筛选模块字段 ──
    domain_tags: list[str] = field(default_factory=list)       # 领域分类标签，如 ["语音合成 (TTS) > Zero-shot TTS / 声音克隆"]
    innovation_method: str = ""                                 # 最具创新性的方法描述
    score: float = 0.0                                          # LLM 综合评分 1-10
    blacklisted: bool = False                                   # 是否命中黑名单
    blacklist_reason: str = ""                                  # 命中黑名单的原因
    error: Optional[str] = None


@dataclass
class OrgRecord:
    name: str
    levels: list[str]
    aliases: list[str]


@dataclass
class LLMConfig:
    """OpenAI 兼容 API 配置。"""
    base_url: str = DEFAULT_LLM_BASE_URL
    api_key: str = ""
    model: str = DEFAULT_LLM_MODEL
    max_tokens: int = DEFAULT_MAX_TOKENS
    temperature: float = 0.3
    enable_thinking: bool = False
    concurrency: int = DEFAULT_LLM_CONCURRENCY

    @property
    def chat_completions_url(self) -> str:
        """构造 /chat/completions 端点 URL。"""
        base = self.base_url.rstrip("/")
        # 如果用户已经给了完整的 /chat/completions 路径，直接用
        if base.endswith("/chat/completions"):
            return base
        # 如果结尾是 /vN（如 /v1, /v4），拼上 /chat/completions
        import re as _re
        if _re.search(r"/v\d+$", base):
            return f"{base}/chat/completions"
        # 否则尝试拼完整路径
        return f"{base}/v1/chat/completions"


def apply_chat_payload_options(payload: dict, llm_config: LLMConfig) -> dict:
    """按 LLMConfig 补充 chat/completions 可选参数（如关闭思考模式）。"""
    if not llm_config.enable_thinking:
        payload["thinking"] = {"type": "disabled"}
    return payload


def env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return str(raw).strip().lower() in ("1", "true", "yes", "on")


def env_int(name: str, default: int, *, minimum: int = 1) -> int:
    raw = os.environ.get(name)
    if raw is None or not str(raw).strip():
        return default
    try:
        return max(minimum, int(str(raw).strip()))
    except ValueError:
        return default


def resolve_llm_concurrency(llm_config: LLMConfig) -> int:
    return max(1, int(getattr(llm_config, "concurrency", DEFAULT_LLM_CONCURRENCY) or DEFAULT_LLM_CONCURRENCY))


def run_with_concurrency(
    items: list,
    worker_fn: Callable,
    concurrency: int,
    *,
    progress_callback: Optional[Callable[[int, int, object], None]] = None,
    log_label: str = "",
) -> list:
    """并发执行 worker_fn(item)，保持返回顺序与 items 一致。"""
    if not items:
        return []

    workers = min(max(1, int(concurrency or DEFAULT_LLM_CONCURRENCY)), len(items))
    if workers <= 1:
        results = []
        for i, item in enumerate(items, 1):
            if progress_callback:
                progress_callback(i, len(items), item)
            results.append(worker_fn(item))
        return results

    results: list = [None] * len(items)
    progress_lock = threading.Lock()
    done_count = 0

    def wrapped(index: int, item):
        nonlocal done_count
        result = worker_fn(item)
        with progress_lock:
            done_count += 1
            if progress_callback:
                progress_callback(done_count, len(items), item)
            elif log_label:
                log.info(f"  {log_label} {format_progress_bar(done_count, len(items))}")
        return index, result

    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = [executor.submit(wrapped, i, item) for i, item in enumerate(items)]
        for fut in as_completed(futures):
            index, result = fut.result()
            results[index] = result
    return results


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

    # Comments（与 abs 页 metatable 中 Comments 同源，列表页常为 list-comments）
    comments_div = meta.find("div", class_=lambda c: c and "list-comments" in c)
    comments = ""
    if comments_div:
        comments = comments_div.get_text(separator=" ", strip=True)
        comments = re.sub(r"^Comments:\s*", "", comments, flags=re.IGNORECASE)

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
        comments=comments,
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
# 步骤 2.5: 按 paper_id 抓取单篇论文元数据（额外论文）
# ─────────────────────────────────────────────

def fetch_paper_by_id(paper_id: str) -> Optional[Paper]:
    """从 arXiv abs 页面抓取单篇论文的元数据（标题、作者、摘要等）。"""
    paper_id = paper_id.strip()
    if not paper_id:
        return None

    abs_url = ARXIV_ABS_URL.format(paper_id=paper_id)
    log.info(f"正在抓取论文元数据: {abs_url}")

    headers = {"User-Agent": "arXiv-Daily-Digest/2.0 (Academic Research Tool)"}
    try:
        resp = requests.get(abs_url, headers=headers, timeout=30)
        resp.raise_for_status()
    except Exception as e:
        log.error(f"抓取 abs 页失败 [{paper_id}]: {e}")
        return None

    soup = BeautifulSoup(resp.text, "html.parser")

    title = ""
    title_tag = soup.find("h1", class_="title")
    if title_tag:
        title = title_tag.get_text(strip=True)
        title = re.sub(r"^Title:\s*", "", title)

    authors: list[str] = []
    authors_div = soup.find("div", class_="authors")
    if authors_div:
        for a_tag in authors_div.find_all("a"):
            name = a_tag.get_text(strip=True)
            if name:
                authors.append(name)
    if not authors:
        authors = ["Unknown"]

    abstract = ""
    abstract_tag = soup.find("blockquote", class_="abstract")
    if abstract_tag:
        abstract = abstract_tag.get_text(separator=" ", strip=True)
        abstract = re.sub(r"^Abstract:\s*", "", abstract)

    subjects = ""
    subjects_td = soup.find("td", class_="tablecell subjects")
    if subjects_td:
        subjects = subjects_td.get_text(separator=" ", strip=True)

    primary_category = ""
    primary_span = soup.find("span", class_="primary-subject")
    if primary_span:
        cat_match = re.search(r"\(([^)]+)\)", primary_span.get_text(strip=True))
        if cat_match:
            primary_category = cat_match.group(1)

    comments = ""
    comments_td = soup.find("td", class_="tablecell comments")
    if comments_td:
        comments = comments_td.get_text(strip=True)

    if not title:
        log.warning(f"未能解析标题 [{paper_id}]，可能论文不存在")
        return None

    paper = Paper(
        paper_id=paper_id,
        title=title,
        authors=authors,
        comments=comments,
        subjects=subjects,
        abstract=abstract,
        pdf_url=ARXIV_PDF_URL.format(paper_id=paper_id),
        abs_url=abs_url,
        source_categories=[primary_category] if primary_category else ["manual"],
        primary_category=primary_category,
        is_cross_list=False,
    )
    log.info(f"  抓取成功: {title[:60]}")
    return paper


# ─────────────────────────────────────────────
# 通用 URL 抓取（加餐：支持 openreview 等非 arXiv 来源）
# ─────────────────────────────────────────────

def is_url_identifier(s: str) -> bool:
    """判断加餐输入是否为 http(s) 链接（而非 arXiv id）。"""
    return bool(_URL_RE.match((s or "").strip()))


def is_arxiv_id(s: str) -> bool:
    """判断是否为 arXiv id 格式（YYMM.NNNNN，可带版本号）。"""
    return bool(_ARXIV_ID_RE.fullmatch((s or "").strip()))


def _url_to_paper_id(url: str) -> str:
    """把任意下载链接转成一个稳定的 paper_id（用作文件名 / 去重 key）。

    例：
      https://openreview.net/pdf?id=992yMPvMqV  ->  openreview_992yMPvMqV
      https://example.com/a.pdf                  ->  example_a
    同一 URL 多次输入会得到同一 id，保证去重生效。
    """
    from urllib.parse import urlsplit, parse_qs

    u = urlsplit(url.strip())
    host = (u.hostname or "paper").replace("www.", "")
    path_tail = ""
    if u.path:
        path_tail = u.path.rstrip("/").split("/")[-1]
        # 去掉 .pdf 后缀
        if path_tail.lower().endswith(".pdf"):
            path_tail = path_tail[:-4]
    # query 参数里常带真正标识（如 openreview 的 ?id=xxx）
    query_id = ""
    qs = parse_qs(u.query)
    for key in ("id", "paper", "p"):
        if key in qs and qs[key]:
            query_id = qs[key][0]
            break

    raw = "_".join(x for x in [host, path_tail, query_id] if x)
    if not raw:
        raw = host
    # 清理成安全的文件名片段
    slug = re.sub(r"[^\w.\-]", "_", raw).strip("_.-")
    return slug or "external_paper"


def _guess_title_from_pdf(pdf_path: Path) -> tuple[str, str]:
    """从 PDF 元数据 / 首页文本猜测标题与（粗略）作者。

    返回 (title, authors_str)。title 为空表示没猜到。
    PDF /Title 元数据优先；否则取首页第一行非空文本的前若干字。
    """
    try:
        reader = PdfReader(str(pdf_path))
        meta = reader.metadata
        title = ""
        # 1) PDF 元数据里的 Title
        if meta and meta.title:
            t = meta.title.strip()
            # 很多 PDF 的 Title 是 "untitled" / 文件名，过滤掉明显无意义的
            if t and not re.fullmatch(r"(untitled|.*\.pdf)", t, re.I):
                title = t
        # 2) 退而求其次：首页首行
        if not title and reader.pages:
            first = reader.pages[0].extract_text() or ""
            for line in first.splitlines():
                line = line.strip()
                # 跳过太短（页眉、页码）和太长的行
                if 8 <= len(line) <= 200 and not re.fullmatch(r"[\d\s,.\-]+", line):
                    title = line
                    break
        authors = ""
        if meta:
            a = (meta.author or "").strip()
            if a and a.lower() not in ("unknown", ""):
                authors = a
        return title, authors
    except Exception as e:
        log.warning(f"  从 PDF 猜测标题失败: {e}")
        return "", ""


def fetch_paper_by_url(url: str, pdf_dir: Path) -> Optional[Paper]:
    """从任意 http(s) PDF 链接抓取一篇论文（如 openreview）。

    与 fetch_paper_by_id 不同：这里无法解析统一的元数据页，
    因此先下载 PDF，再用 PDF 元数据/首页文本尽力猜测标题与作者，
    其余信息（摘要/分类）留空，交给后续 LLM 解读补全。
    """
    url = url.strip()
    if not is_url_identifier(url):
        return None

    paper_id = _url_to_paper_id(url)
    log.info(f"正在抓取外部论文（通用链接）: {url}  -> id={paper_id}")

    # 先构造一个临时 Paper 以复用 download_pdf
    paper = Paper(
        paper_id=paper_id,
        title="",
        authors=["Unknown"],
        comments="",
        subjects="",
        abstract="",
        pdf_url=url,
        abs_url=url,
        source_categories=["manual"],
        primary_category="",
        is_cross_list=False,
    )
    pdf_path = download_pdf(paper, pdf_dir)
    if not pdf_path:
        log.error(f"外部论文下载失败: {url}")
        return None

    title, authors_str = _guess_title_from_pdf(pdf_path)
    if title:
        paper.title = title
    if authors_str:
        paper.authors = [a.strip() for a in re.split(r"[;,/]| and ", authors_str) if a.strip()] or ["Unknown"]

    log.info(f"  抓取成功（外部）: {paper.title[:60] or '(无标题)'}")
    # 调用方会再次 download_pdf，但 download_pdf 命中本地缓存会直接返回，不会重复下载
    return paper


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

    apply_chat_payload_options(payload, llm_config)

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
            try:
                import llm_usage
                llm_usage.record_usage(usage, purpose="analysis")
            except Exception:
                pass
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
# 步骤 5.1: 筛选模块（领域分类 + 创新方法 + 评分 + 黑名单）
# ─────────────────────────────────────────────

TAXONOMY_PROMPT_TEMPLATE = """你是一位语音与音频领域的资深审稿专家。请基于以下论文的标题、元数据和详细解读，完成四项筛选任务。

## 论文信息
- 标题: {title}
- 作者: {authors}
- 分类: {subjects}
- Comments: {comments}

## 详细解读
{analysis}

## 可选领域分类
{taxonomy_text}

## 黑名单描述（用于语义匹配判断）
{blacklist_text}

## 附加参考信息（辅助评分）
- 相关单位: {org_titles}
- 单位级别: {org_levels}

---

请严格输出以下 JSON 格式（不要输出 JSON 以外的任何文字，不要用 markdown 代码块包裹）：

{{
  "domain_tags": ["主领域名称 > 子方向名称", ...],
  "innovation_method": "什么技术——基于什么改的",
  "score": 7.5,
  "blacklisted": false,
  "blacklist_reason": ""
}}

### 各字段说明

1. **domain_tags** (数组，通常 1 个，最多 3 个):
   - 从上面的「可选领域分类」中选择最匹配的项
   - 格式为 "主领域名称 > 子方向名称"（注意中间的 " > "）
   - 只能使用上面列出的主领域和子方向，不要自行创造
   - 一般情况给 1 个即可，只有论文确实横跨多个领域时才给 2-3 个

2. **innovation_method** (字符串):
   - 用一句话概括文中最具创新性的方法或对已有趋势的探究
   - 格式: "什么技术——基于什么改的"
   - 例如: "全双工语音对话——基于 Turn-Taking 机制改进，引入了可中断的流式架构"
   - 例如: "零样本 TTS——基于大语言模型，用参考音频提示实现声音克隆"

3. **score** (数字，1-10):
   - 对论文的综合质量打分。这个分数会直接决定推荐等级（>=8 必读 / 5-7 值得看 / <5 可跳过），请认真区分层次
   - 评分依据（综合考虑）:
     - 方法的创新性和贡献大小（最重要）
     - 实验充分性和结果质量
     - 如果 Comments 中标明了顶级会议（如 Interspeech, ICASSP, ACL, NeurIPS, ICLR 等）或顶级期刊，适当加分
     - 如果相关单位为知名大学/企业/研究机构（级别高的），适当加分
     - 写作质量和可复现性
   - 评分参考: 9-10=顶级工作, 7-8=优秀, 5-6=中等, 3-4=一般, 1-2=较差
   - 评分原则（参考资深审稿人视角，避免和稀泥）:
     - 不要"总体还行"式的中庸打分，要有明确的层次区分
     - 即使论文结果好、分数高，也要在心里质疑过评估范围、假设强度、数据需求后才给高分
     - 方法 incremental / 只在已有 benchmark 上刷点 / 增量贡献小的，不要给到 8 分以上
     - 真正有新意、有启发性的工作才值得 >=8（必读等级）
     - 与已有工作高度重复的，分数应明显偏低

4. **blacklisted** (布尔值):
   - 如果论文语义上命中了上面「黑名单描述」中的任何一条，设为 true
   - 否则设为 false

5. **blacklist_reason** (字符串):
   - 如果 blacklisted 为 true，说明命中了哪条黑名单描述及原因
   - 如果为 false，留空字符串
"""


def _load_taxonomy() -> list[dict]:
    """加载语音/音频领域分类表，返回展平的「主领域 > 子方向」字符串列表和原始结构。"""
    if not TAXONOMY_PATH.exists():
        log.warning(f"领域分类文件不存在: {TAXONOMY_PATH}")
        return []
    try:
        with open(TAXONOMY_PATH, encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        log.warning(f"领域分类文件加载失败: {e}")
        return []
    return data.get("speech_audio_research_directions", [])


def _format_taxonomy_for_prompt(taxonomy: list[dict]) -> str:
    """将分类表格式化为 prompt 中可读的文本。"""
    if not taxonomy:
        return "（分类文件未加载）"
    lines = []
    for item in taxonomy:
        major = item.get("major", "")
        desc = item.get("description", "")
        subs = item.get("sub", [])
        lines.append(f"### {major}")
        if desc:
            lines.append(f"  说明: {desc}")
        for sub in subs:
            sub_name = sub.get("name", "")
            sub_desc = sub.get("desc", "")
            lines.append(f"  - {sub_name}: {sub_desc}")
        lines.append("")
    return "\n".join(lines)


def _load_blacklist() -> list[str]:
    """加载黑名单描述（一行一条）。"""
    if not BLACKLIST_PATH.exists():
        log.warning(f"黑名单文件不存在: {BLACKLIST_PATH}")
        return []
    lines = []
    with open(BLACKLIST_PATH, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            lines.append(line)
    return lines


def _format_blacklist_for_prompt(entries: list[str]) -> str:
    if not entries:
        return "（黑名单为空）"
    return "\n".join(f"- {e}" for e in entries)


def _extract_json_from_llm_response(text: str) -> dict:
    """从 LLM 响应文本中提取 JSON 对象，容忍前后多余的标记。"""
    text = (text or "").strip()
    if not text:
        return {}
    # 去掉 markdown 代码块包裹
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
        text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    m = re.search(r"\{.*\}", text, flags=re.S)
    if m:
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError:
            return {}
    return {}


def format_progress_bar(current: int, total: int, width: int = 30) -> str:
    """生成文本进度条，如 [████░░░░] 3/10 (30%)。"""
    if total <= 0:
        return f"[{'░' * width}] 0/0 (0%)"
    current = max(0, min(current, total))
    filled = int(width * current / total)
    bar = "█" * filled + "░" * (width - filled)
    pct = int(100 * current / total)
    return f"[{bar}] {current}/{total} ({pct}%)"


def classify_and_score_with_llm(
    paper: Paper,
    llm_config: LLMConfig,
    taxonomy: list[dict],
    blacklist: list[str],
) -> bool:
    """调用 LLM 对论文进行领域分类、创新方法标注、评分和黑名单检测。
    结果直接写入 paper 对象；成功返回 True，失败返回 False。
    """
    if not paper.analysis or paper.analysis.startswith("[LLM"):
        log.warning(f"  跳过筛选（解读为空或失败）: {paper.title[:40]}...")
        return False

    taxonomy_text = _format_taxonomy_for_prompt(taxonomy)
    blacklist_text = _format_blacklist_for_prompt(blacklist)

    prompt = TAXONOMY_PROMPT_TEMPLATE.format(
        title=paper.title,
        authors=", ".join(paper.authors[:8]),
        subjects=paper.subjects or "",
        comments=paper.comments or "",
        analysis=paper.analysis,
        taxonomy_text=taxonomy_text,
        blacklist_text=blacklist_text,
        org_titles="; ".join(paper.related_org_titles) if paper.related_org_titles else "未知",
        org_levels="; ".join(paper.related_org_levels) if paper.related_org_levels else "未知",
    )

    headers = {"Content-Type": "application/json"}
    if llm_config.api_key:
        headers["Authorization"] = f"Bearer {llm_config.api_key}"

    payload = {
        "model": llm_config.model,
        "max_tokens": 1024,
        "temperature": 0.2,
        "messages": [
            {"role": "system", "content": "你是一位严谨的学术助手，只输出 JSON。"},
            {"role": "user", "content": prompt},
        ],
    }

    apply_chat_payload_options(payload, llm_config)

    api_url = llm_config.chat_completions_url

    try:
        resp = None
        for attempt in range(CLASSIFY_MAX_RETRIES + 1):
            resp = requests.post(api_url, headers=headers, json=payload, timeout=120)
            if resp.status_code == 429:
                if attempt < CLASSIFY_MAX_RETRIES:
                    log.warning(
                        f"  筛选 API 429，{CLASSIFY_RETRY_WAIT}s 后重试 "
                        f"({attempt + 1}/{CLASSIFY_MAX_RETRIES}): {paper.paper_id}"
                    )
                    time.sleep(CLASSIFY_RETRY_WAIT)
                    continue
            resp.raise_for_status()
            break

        if resp is None:
            return False

        data = resp.json()
        try:
            import llm_usage
            llm_usage.record_usage(data.get("usage"), purpose="classify")
        except Exception:
            pass
        choices = data.get("choices", [])
        content = choices[0].get("message", {}).get("content", "") if choices else ""

        if not content:
            log.warning(f"  筛选 LLM 返回为空: {paper.title[:40]}...")
            return False

        result = _extract_json_from_llm_response(content)

        domain_tags = result.get("domain_tags", [])
        if isinstance(domain_tags, list):
            paper.domain_tags = [str(t).strip() for t in domain_tags if str(t).strip()][:3]

        paper.innovation_method = str(result.get("innovation_method", "")).strip()

        try:
            score = float(result.get("score", 0))
            paper.score = max(0.0, min(10.0, score))
        except (ValueError, TypeError):
            paper.score = 0.0

        paper.blacklisted = bool(result.get("blacklisted", False))
        paper.blacklist_reason = str(result.get("blacklist_reason", "")).strip()

        bl_mark = " [黑名单]" if paper.blacklisted else ""
        log.info(
            f"  筛选完成: 评分={paper.score:.1f}, "
            f"领域={paper.domain_tags}, "
            f"创新={paper.innovation_method[:50]}..."
            f"{bl_mark}"
        )
        return True

    except requests.exceptions.HTTPError as e:
        status = e.response.status_code if e.response is not None else "?"
        log.error(f"  筛选 API 错误 (HTTP {status}): {e}")
        return False
    except Exception as e:
        log.error(f"  筛选异常: {e}")
        return False


# ─────────────────────────────────────────────
# 步骤 5.5: 相关单位检测（LLM 标签 + 本地匹配）
# ─────────────────────────────────────────────

def _normalize_org_text(text: str) -> str:
    text = (text or "").lower().strip()
    text = re.sub(r"[\(\)\[\]\{\},.;:!?'\"`~@#$%^&*_+=|\\/<>-]+", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text


def _meaningful_org_tokens(text: str) -> set[str]:
    norm = _normalize_org_text(text)
    tokens = set(norm.split())
    return {tok for tok in tokens if tok and tok not in ORG_GENERIC_TOKENS}


def _is_overly_generic_org_label(text: str) -> bool:
    norm = _normalize_org_text(text)
    if len(norm) < 2:
        return True
    tokens = [tok for tok in norm.split() if tok]
    if not tokens:
        return True
    if len(tokens) == 1 and tokens[0] in ORG_GENERIC_TOKENS:
        return True
    meaningful = _meaningful_org_tokens(norm)
    # "the university"/"research institute" 这类泛化标签直接跳过
    return not meaningful and len(tokens) <= 4


def _build_aliases(name: str) -> list[str]:
    aliases: list[str] = [name]
    without_paren = re.sub(r"\s*\([^)]*\)", "", name).strip()
    if without_paren and without_paren not in aliases:
        aliases.append(without_paren)
    for part in re.findall(r"\(([^)]+)\)", name):
        p = part.strip()
        if p and p not in aliases:
            aliases.append(p)
    return aliases


def load_org_knowledge_base(path: Path = ORG_KB_PATH) -> list[OrgRecord]:
    records: list[OrgRecord] = []
    if not path.exists():
        log.warning(f"单位知识库不存在，跳过单位检测: {path}")
        return records
    with open(path, encoding="utf-8") as f:
        for line_no, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError as e:
                log.warning(f"单位知识库第 {line_no} 行 JSON 解析失败: {e}")
                continue
            name = str(item.get("name") or "").strip()
            if not name:
                continue
            levels = [str(x).strip() for x in (item.get("level") or []) if str(x).strip()]
            aliases = _build_aliases(name)
            records.append(OrgRecord(name=name, levels=levels, aliases=aliases))
    return records


def _extract_json_from_text(text: str) -> dict:
    text = (text or "").strip()
    if not text:
        return {}
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    m = re.search(r"\{.*\}", text, flags=re.S)
    if not m:
        return {}
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError:
        return {}


def _build_org_lookup_maps(kb: list[OrgRecord]) -> tuple[dict[str, OrgRecord], dict[str, OrgRecord]]:
    name_map: dict[str, OrgRecord] = {}
    alias_map: dict[str, OrgRecord] = {}
    for rec in kb:
        norm_name = _normalize_org_text(rec.name)
        if norm_name:
            name_map[norm_name] = rec
        for alias in rec.aliases:
            norm_alias = _normalize_org_text(alias)
            if norm_alias and norm_alias not in alias_map:
                alias_map[norm_alias] = rec
    return name_map, alias_map


def _resolve_org_names_to_records(names: list[str], kb: list[OrgRecord]) -> list[OrgRecord]:
    if not names or not kb:
        return []
    name_map, alias_map = _build_org_lookup_maps(kb)
    resolved: list[OrgRecord] = []
    seen: set[str] = set()
    for item in names:
        key = _normalize_org_text(item)
        if not key:
            continue
        rec = name_map.get(key) or alias_map.get(key)
        if not rec or rec.name in seen:
            continue
        seen.add(rec.name)
        resolved.append(rec)
        if len(resolved) >= ORG_MATCH_MAX_RESULTS:
            break
    return resolved


def _build_org_candidates_text(kb: list[OrgRecord]) -> str:
    lines: list[str] = []
    for i, rec in enumerate(kb, 1):
        levels = ", ".join(rec.levels) if rec.levels else "无"
        aliases = ", ".join(rec.aliases[:3]) if rec.aliases else rec.name
        lines.append(f"{i}. {rec.name} | levels: {levels} | aliases: {aliases}")
    return "\n".join(lines)


def detect_org_labels_with_llm(paper: Paper, kb: list[OrgRecord], llm_config: LLMConfig) -> list[str]:
    source = paper.full_text.strip()
    if not source:
        source = (
            f"Title: {paper.title}\n"
            f"Authors: {', '.join(paper.authors)}\n"
            f"Abstract: {paper.abstract}\n"
            f"Comments: {paper.comments}"
        )
    snippet = source[:ORG_SNIPPET_LENGTH]
    if not snippet:
        return []
    if not kb:
        return []

    candidates_text = _build_org_candidates_text(kb)

    system_prompt = (
        "你是学术单位识别助手。"
        "你会收到一份候选单位列表和论文片段。"
        "请只从候选单位列表里选择最相关的单位，最多 6 个。"
        "如果没有明确证据，返回空列表。"
        "严禁输出候选列表之外的名称。"
        "不要输出其他文本。"
    )
    user_message = (
        "候选单位列表（仅可从此处选择）:\n"
        f"{candidates_text}\n\n"
        f"论文前 {ORG_SNIPPET_LENGTH} 字符片段:\n"
        f"<snippet>\n{snippet}\n</snippet>"
        '\n请返回 JSON：{"org_labels": ["候选名称1", "候选名称2"]}'
    )

    headers = {"Content-Type": "application/json"}
    if llm_config.api_key:
        headers["Authorization"] = f"Bearer {llm_config.api_key}"

    payload = {
        "model": llm_config.model,
        "max_tokens": 512,
        "temperature": 0.0,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_message},
        ],
    }
    apply_chat_payload_options(payload, llm_config)

    try:
        resp = requests.post(llm_config.chat_completions_url, headers=headers, json=payload, timeout=120)
        resp.raise_for_status()
        data = resp.json()
        try:
            import llm_usage
            llm_usage.record_usage(data.get("usage"), purpose="org")
        except Exception:
            pass
        content = ""
        choices = data.get("choices", [])
        if choices:
            content = choices[0].get("message", {}).get("content", "")
        parsed = _extract_json_from_text(content)
        labels = parsed.get("org_labels", []) if isinstance(parsed, dict) else []
        if not isinstance(labels, list):
            return []
        dedup: list[str] = []
        seen: set[str] = set()
        for x in labels:
            label = str(x).strip()
            if not label:
                continue
            key = _normalize_org_text(label)
            if not key or key in seen:
                continue
            seen.add(key)
            dedup.append(label)
        records = _resolve_org_names_to_records(dedup, kb)
        return [rec.name for rec in records]
    except Exception as e:
        log.warning(f"  单位标签提取失败: {e}")
        return []


def match_related_orgs(labels: list[str], kb: list[OrgRecord], max_results: int = ORG_MATCH_MAX_RESULTS) -> list[OrgRecord]:
    if not labels or not kb:
        return []

    best_score_by_name: dict[str, float] = {}
    record_by_name: dict[str, OrgRecord] = {}

    for label in labels:
        label_norm = _normalize_org_text(label)
        if _is_overly_generic_org_label(label_norm):
            continue
        label_tokens = _meaningful_org_tokens(label_norm)
        for rec in kb:
            rec_best = 0.0
            for alias in rec.aliases:
                alias_norm = _normalize_org_text(alias)
                if len(alias_norm) < 2:
                    continue
                alias_tokens = _meaningful_org_tokens(alias_norm)
                if label_norm == alias_norm:
                    rec_best = max(rec_best, 1.0)
                    continue
                # 仅保留 alias in label 的方向，避免 "University" 反向命中大量学校
                if (
                    len(alias_norm) >= 8
                    and alias_norm in label_norm
                    and (not alias_tokens or bool(label_tokens & alias_tokens))
                ):
                    rec_best = max(rec_best, 0.95)
                    continue
                if min(len(label_norm), len(alias_norm)) >= 8:
                    sim = SequenceMatcher(None, label_norm, alias_norm).ratio()
                    if not alias_tokens or bool(label_tokens & alias_tokens):
                        rec_best = max(rec_best, sim)
            if rec_best >= ORG_MATCH_MIN_SCORE:
                prev = best_score_by_name.get(rec.name, 0.0)
                if rec_best > prev:
                    best_score_by_name[rec.name] = rec_best
                    record_by_name[rec.name] = rec

    ranked = sorted(
        record_by_name.values(),
        key=lambda r: best_score_by_name.get(r.name, 0.0),
        reverse=True,
    )
    return ranked[:max_results]


def attach_related_orgs_to_paper(paper: Paper, kb: list[OrgRecord], llm_config: LLMConfig) -> None:
    labels = detect_org_labels_with_llm(paper, kb, llm_config)
    paper.org_detection_labels = labels
    matches = _resolve_org_names_to_records(labels, kb)

    paper.related_org_titles = []
    all_levels: list[str] = []
    for rec in matches:
        if rec.levels:
            title = f"{rec.name} ({', '.join(rec.levels)})"
        else:
            title = rec.name
        paper.related_org_titles.append(title)
        for lv in rec.levels:
            if lv not in all_levels:
                all_levels.append(lv)
    paper.related_org_levels = all_levels


# ─────────────────────────────────────────────
# 步骤 6: 生成 HTML 报告
# ─────────────────────────────────────────────

def generate_html_report(
    papers: list[Paper],
    categories: list[str],
    llm_config: LLMConfig,
    *,
    skip_llm_analysis: bool = False,
) -> str:
    """汇总为 HTML 报告。"""
    today = now_bj().strftime("%Y年%m月%d日")
    successful = [p for p in papers if p.analysis and not p.analysis.startswith("[LLM")]
    failed = [p for p in papers if not skip_llm_analysis and (not p.analysis or p.analysis.startswith("[LLM"))]
    cross_count = sum(1 for p in papers if p.is_cross_list or len(p.source_categories) > 1)

    cat_labels = " / ".join(categories)
    cat_names = ", ".join(CATEGORY_NAMES.get(c, c) for c in categories)

    if skip_llm_analysis:
        llm_model_display = "未使用（--no-llm，未调用 API）"
        stats_llm_block = """    <div class="stat"><div class="stat-num">—</div><div class="stat-label">深度解读</div></div>
    <div class="stat"><div class="stat-num">—</div><div class="stat-label">未调用 LLM</div></div>"""
    else:
        llm_model_display = llm_config.model
        stats_llm_block = f"""    <div class="stat"><div class="stat-num">{len(successful)}</div><div class="stat-label">成功解读</div></div>
    <div class="stat"><div class="stat-num">{len(failed)}</div><div class="stat-label">待处理</div></div>"""

    papers_html = ""
    for i, paper in enumerate(papers, 1):
        authors_str = ", ".join(paper.authors[:5])
        if len(paper.authors) > 5:
            authors_str += f" 等 ({len(paper.authors)} 人)"

        if skip_llm_analysis:
            analysis_block = ""
        else:
            analysis_html = _markdown_to_html(paper.analysis) if paper.analysis else "<p>解读暂不可用</p>"

            # 筛选信息块
            filter_parts = []
            grade = paper_recommend_grade(paper)
            if grade:
                emoji, label, gcls = grade
                filter_parts.append(
                    f'<div class="filter-row"><span class="filter-label">{emoji} 推荐</span>'
                    f'<span class="filter-grade {gcls}">{_escape_html(label)}</span></div>'
                )
            if paper.domain_tags:
                domain_tags_html = "".join(
                    f'<span class="filter-tag filter-domain">{_escape_html(t)}</span>'
                    for t in paper.domain_tags
                )
                filter_parts.append(f'<div class="filter-row"><span class="filter-label">🏷️ 领域</span>{domain_tags_html}</div>')
            if paper.innovation_method:
                filter_parts.append(
                    f'<div class="filter-row"><span class="filter-label">💡 创新</span>'
                    f'<span class="filter-innovation">{_escape_html(paper.innovation_method)}</span></div>'
                )
            if paper.score > 0:
                score_color = "score-high" if paper.score >= 8 else ("score-mid" if paper.score >= 5 else "score-low")
                filter_parts.append(
                    f'<div class="filter-row"><span class="filter-label">⭐ 评分</span>'
                    f'<span class="filter-score {score_color}">{paper.score:.1f}</span></div>'
                )
            filter_block = f'<div class="filter-section">{"".join(filter_parts)}</div>' if filter_parts else ""

            analysis_block = f"""
            <div class="analysis">
                <h3>📖 深度解读</h3>
                {analysis_html}
            </div>
            {filter_block}"""

        badges = ""
        if not skip_llm_analysis and (paper.error or (paper.analysis and paper.analysis.startswith("[LLM"))):
            badges += '<span class="badge badge-error">解读失败</span>'
        if paper.is_cross_list or len(paper.source_categories) > 1:
            badges += '<span class="badge badge-cross">跨领域</span>'
        if paper.blacklisted:
            badges += f'<span class="badge badge-blacklist" title="{_escape_html(paper.blacklist_reason)}">黑名单</span>'

        source_tags = "".join(
            f'<span class="cat-tag">{_escape_html(cat)}</span>' for cat in paper.source_categories
        )
        org_tags = "".join(
            f'<span class="org-tag">{_escape_html(title)}</span>' for title in paper.related_org_titles
        )

        papers_html += f"""
        <article class="paper" id="paper-{i}">
            <div class="paper-header">
                <div class="paper-top-row">
                    <span class="paper-index">#{i}</span>
                    <div class="cat-tags">{source_tags}</div>
                </div>
                {f'<div class="org-tags">{org_tags}</div>' if org_tags else ""}
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
                    {f'''<div class="meta-item">
                        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/></svg>
                        <span class="meta-label">Comments:</span> {_escape_html(paper.comments)}
                    </div>''' if paper.comments else ""}
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
            {analysis_block}
        </article>"""

    # 目录
    toc_html = ""
    for i, paper in enumerate(papers, 1):
        short_title = paper.title[:80] + ("..." if len(paper.title) > 80 else "")
        cross_mark = " 🔀" if (paper.is_cross_list or len(paper.source_categories) > 1) else ""
        toc_grade = paper_recommend_grade(paper)
        grade_mark = f" {toc_grade[0]}" if toc_grade else ""
        toc_html += f'<li><a href="#paper-{i}">{_escape_html(short_title)}{cross_mark}{grade_mark}</a></li>\n'

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
  .org-tags{{display:flex;gap:6px;flex-wrap:wrap;margin-bottom:8px}}
  .org-tag{{display:inline-block;font-size:11px;padding:2px 8px;background:#ecfeff;border-radius:999px;color:#0f766e;border:1px solid #99f6e4}}
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
  .filter-section{{margin:16px 0;padding:12px 16px;background:#fafaf8;border:1px solid var(--border);border-radius:6px}}
  .filter-row{{display:flex;align-items:center;gap:8px;margin-bottom:6px;font-size:13px}}
  .filter-row:last-child{{margin-bottom:0}}
  .filter-label{{color:var(--fg-muted);font-weight:600;min-width:64px}}
  .filter-tag{{display:inline-block;font-size:11px;padding:2px 8px;background:#f0f9ff;color:#0369a1;border:1px solid #bae6fd;border-radius:4px}}
  .filter-innovation{{color:var(--fg);font-weight:500}}
  .filter-score{{font-family:var(--font-mono);font-weight:700;font-size:15px}}
  .filter-score.score-high{{color:#b45309}}
  .filter-score.score-mid{{color:#0369a1}}
  .filter-score.score-low{{color:#6b7280}}
  .filter-grade{{font-weight:700;font-size:13px;padding:2px 8px;border-radius:4px;border:1px solid transparent}}
  .filter-grade.grade-must{{background:#fef2f2;color:#b91c1c;border-color:#fca5a5}}
  .filter-grade.grade-worth{{background:#eff6ff;color:#1d4ed8;border-color:#93c5fd}}
  .filter-grade.grade-skip{{background:#f3f4f6;color:#6b7280;border-color:#e5e7eb}}
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
    <div class="model-info">LLM: {_escape_html(llm_model_display)}</div>
  </header>
  <div class="stats">
    <div class="stat"><div class="stat-num">{len(papers)}</div><div class="stat-label">论文总数</div></div>
    <div class="stat"><div class="stat-num cross">{cross_count}</div><div class="stat-label">跨领域</div></div>
{stats_llm_block}
  </div>
  <nav class="toc"><h2>目录 (🔀 = 跨领域)</h2><ol>{toc_html}</ol></nav>
  {papers_html}
  <footer class="report-footer">
    <p>数据来源: <a href="https://arxiv.org" target="_blank">arXiv.org</a></p>
    <p>LLM: {_escape_html(llm_model_display)}</p>
  </footer>
</div>
</body>
</html>"""
    return html


def _escape_html(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def paper_recommend_grade(paper: Paper) -> Optional[tuple[str, str, str]]:
    """根据 LLM 评分 + 黑名单判定推荐等级。

    返回 (emoji, label, css_class) 或 None（未评分论文）。
    与前端 app.js 的 paperGrade() 逻辑保持一致。
    """
    if paper.blacklisted:
        return RECOMMEND_GRADES["skip"]
    score = paper.score or 0.0
    if score >= GRADE_MUST_THRESHOLD:
        return RECOMMEND_GRADES["must"]
    if score >= GRADE_WORTH_THRESHOLD:
        return RECOMMEND_GRADES["worth"]
    if score > 0:
        return RECOMMEND_GRADES["skip"]
    return None


def _markdown_to_html(md: str) -> str:
    """Markdown → HTML（使用 python-markdown）。"""
    return markdown.markdown(
        md,
        extensions=["tables", "fenced_code", "nl2br", "sane_lists"],
    )

# ─────────────────────────────────────────────
# JSON 元数据导出（供 Web 应用搜索/索引使用）
# ─────────────────────────────────────────────

def export_papers_json(
    papers: list[Paper],
    categories: list[str],
    date_str: str,
    llm_config: LLMConfig,
    *,
    skip_llm_analysis: bool = False,
    in_progress: bool = False,
) -> Path:
    """将论文元数据落地为 JSON，便于 Web 端做搜索 / 历史归档（原子替换，抓取中可安全读取）。"""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    out_path = DATA_DIR / f"{date_str}.json"

    paper_records = []
    for p in papers:
        record = asdict(p)
        record.pop("full_text", None)
        paper_records.append(record)

    payload = {
        "date": date_str,
        "generated_at": now_bj().isoformat(),
        "categories": categories,
        "llm_model": "" if skip_llm_analysis else llm_config.model,
        "skip_llm_analysis": skip_llm_analysis,
        "in_progress": in_progress,
        "total": len(papers),
        "cross_count": sum(
            1 for p in papers if p.is_cross_list or len(p.source_categories) > 1
        ),
        "papers": paper_records,
    }

    tmp_path = Path(str(out_path) + ".tmp")
    tmp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp_path.replace(out_path)
    if in_progress:
        log.info(f"📦 论文元数据检查点: {out_path} ({len(papers)} 篇)")
    else:
        log.info(f"📦 论文元数据已导出: {out_path}")
    return out_path


def load_papers_from_export_json(path: Path) -> tuple[list[Paper], list[str], str]:
    """从 export_papers_json 产出的快照加载论文列表（离线测试飞书 / HTML 等）。"""
    path = Path(path)
    if not path.is_file():
        raise FileNotFoundError(f"论文 JSON 不存在: {path.resolve()}")
    with open(path, encoding="utf-8") as f:
        payload = json.load(f)
    categories = list(payload.get("categories") or [])
    date_str = str(payload.get("date") or "")
    allowed = {f.name for f in fields(Paper)}
    papers: list[Paper] = []
    for rec in payload.get("papers") or []:
        if not isinstance(rec, dict):
            continue
        kwargs = {k: v for k, v in rec.items() if k in allowed}
        kwargs.setdefault("full_text", "")
        kwargs.setdefault("analysis", "")
        kwargs.setdefault("comments", "")
        kwargs.setdefault("primary_category", "")
        kwargs.setdefault("is_cross_list", False)
        papers.append(Paper(**kwargs))
    return papers, categories, date_str


def _paper_has_valid_analysis(paper: Paper) -> bool:
    analysis = (paper.analysis or "").strip()
    if not analysis:
        return False
    if analysis.startswith("[LLM") or analysis.startswith("[PDF"):
        return False
    return True


def _paper_has_valid_classify(paper: Paper) -> bool:
    """判断论文是否已成功筛选（有评分或领域标签）。"""
    if not _paper_has_valid_analysis(paper):
        return False
    return bool(paper.domain_tags) or paper.score > 0


def _paper_needs_retry(paper: Paper, *, include_classify: bool = True) -> bool:
    """判断论文是否需要重试（解读失败，或解读成功但筛选失败）。"""
    analysis_ok = _paper_has_valid_analysis(paper)
    if not analysis_ok:
        return True
    if include_classify and not _paper_has_valid_classify(paper):
        return True
    return False


def _process_paper_llm_pipeline(
    paper: Paper,
    *,
    llm_config: LLMConfig,
    org_kb: list[OrgRecord],
    taxonomy: list[dict],
    blacklist_entries: list[str],
    pdf_dir: Path,
) -> None:
    """下载 PDF 并对单篇论文执行 LLM 解读与筛选（可并发调用）。"""
    if org_kb and llm_config.api_key:
        attach_related_orgs_to_paper(paper, org_kb, llm_config)
        if paper.related_org_titles:
            log.info(f"  [{paper.paper_id}] 相关单位: {', '.join(paper.related_org_titles)}")

    pdf_path = download_pdf(paper, pdf_dir)
    time.sleep(REQUEST_DELAY)

    if not pdf_path:
        paper.error = "PDF 下载失败"
        paper.analysis = "[PDF 下载失败，无法解读]"
        return

    paper.full_text = extract_text_from_pdf(pdf_path)
    if not paper.full_text.strip():
        log.warning(f"  [{paper.paper_id}] 文本提取为空，将使用摘要")
    elif org_kb and llm_config.api_key:
        attach_related_orgs_to_paper(paper, org_kb, llm_config)

    paper.analysis = analyze_paper_with_llm(paper, llm_config)

    if taxonomy and llm_config.api_key:
        classify_and_score_with_llm(paper, llm_config, taxonomy, blacklist_entries)


def run_classify_on_papers(
    papers: list[Paper],
    llm_config: LLMConfig,
    *,
    taxonomy: Optional[list[dict]] = None,
    blacklist: Optional[list[str]] = None,
    progress_callback: Optional[Callable[[int, int, Paper], None]] = None,
) -> dict:
    """对已有解读的论文运行第二次 LLM 筛选（领域/创新/评分/黑名单）。"""
    taxonomy = _load_taxonomy() if taxonomy is None else taxonomy
    blacklist = _load_blacklist() if blacklist is None else blacklist
    stats = {"total": len(papers), "processed": 0, "skipped": 0, "failed": 0}

    if not llm_config.api_key:
        raise ValueError("LLM API Key 未配置")

    to_classify = [p for p in papers if _paper_has_valid_analysis(p)]
    stats["to_classify"] = len(to_classify)
    workers = resolve_llm_concurrency(llm_config)
    if workers > 1:
        log.info(f"LLM 筛选并发数: {workers}")

    def classify_one(paper: Paper) -> bool:
        return classify_and_score_with_llm(paper, llm_config, taxonomy, blacklist)

    def on_progress(done: int, total: int, paper: Paper) -> None:
        if progress_callback:
            progress_callback(done, total, paper)
        else:
            log.info(
                f"  筛选进度 {format_progress_bar(done, total)} "
                f"{paper.paper_id} {paper.title[:50]}"
            )

    results = run_with_concurrency(
        to_classify,
        classify_one,
        workers,
        progress_callback=on_progress,
    )

    for ok in results:
        if ok:
            stats["processed"] += 1
        else:
            stats["failed"] += 1
    stats["skipped"] = len(papers) - len(to_classify)

    if progress_callback and to_classify:
        progress_callback(len(to_classify), len(to_classify), to_classify[-1])

    return stats


def _apply_classify_fields_to_record(rec: dict, paper: Paper) -> None:
    rec["domain_tags"] = paper.domain_tags
    rec["innovation_method"] = paper.innovation_method
    rec["score"] = paper.score
    rec["blacklisted"] = paper.blacklisted
    rec["blacklist_reason"] = paper.blacklist_reason


def run_classify_from_json(
    date_str: str,
    llm_config: LLMConfig,
    *,
    paper_id: Optional[str] = None,
    max_papers: Optional[int] = None,
    progress_callback: Optional[Callable[[int, int, Paper], None]] = None,
) -> dict:
    """从 data/papers/{date}.json 加载论文，运行第二次 LLM 筛选并写回 JSON。"""
    path = DATA_DIR / f"{date_str}.json"
    if not path.is_file():
        raise FileNotFoundError(f"论文 JSON 不存在: {path.resolve()}")

    with open(path, encoding="utf-8") as f:
        payload = json.load(f)

    papers, categories, loaded_date = load_papers_from_export_json(path)
    if loaded_date and loaded_date != date_str:
        log.warning(f"JSON 内 date={loaded_date} 与请求 date={date_str} 不一致，以文件为准")

    targets = papers
    if paper_id:
        targets = [p for p in papers if p.paper_id == paper_id]
        if not targets:
            raise ValueError(f"未找到 paper_id: {paper_id}")
    if max_papers:
        targets = targets[:max_papers]

    stats = run_classify_on_papers(
        targets, llm_config, progress_callback=progress_callback
    )
    updated = {p.paper_id: p for p in targets}

    for rec in payload.get("papers") or []:
        if not isinstance(rec, dict):
            continue
        pid = str(rec.get("paper_id") or "")
        if pid in updated:
            _apply_classify_fields_to_record(rec, updated[pid])

    payload["classified_at"] = now_bj().isoformat()
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)

    stats["date"] = date_str
    stats["categories"] = categories
    stats["path"] = str(path)
    log.info(
        f"筛选写回完成: date={date_str}, processed={stats['processed']}, "
        f"skipped={stats['skipped']}, failed={stats['failed']}"
    )
    return stats


def _retry_one_paper(
    paper: Paper,
    *,
    llm_config: LLMConfig,
    taxonomy: list[dict],
    blacklist: list[str],
    pdf_dir: Path,
) -> dict[str, int]:
    """重试单篇论文的解读/筛选，返回计数片段。"""
    counts = {
        "analysis_ok": 0,
        "analysis_failed": 0,
        "classify_ok": 0,
        "classify_failed": 0,
    }
    title_short = (paper.title or paper.paper_id)[:50]
    need_analysis = not _paper_has_valid_analysis(paper)
    need_classify = _paper_has_valid_analysis(paper) and not _paper_has_valid_classify(paper)

    if need_analysis:
        log.info(f"  重跑解读: {paper.paper_id} {title_short}")
        pdf_path = download_pdf(paper, pdf_dir)
        if not pdf_path:
            paper.error = "PDF 下载失败（重试）"
            paper.analysis = "[PDF 下载失败，无法解读]"
            counts["analysis_failed"] += 1
            return counts

        full_text = extract_text_from_pdf(pdf_path)
        if full_text.strip():
            paper.full_text = full_text
        else:
            log.warning(f"  [{paper.paper_id}] 文本提取为空，将使用摘要")

        new_analysis = analyze_paper_with_llm(paper, llm_config)
        if new_analysis and not new_analysis.startswith("[LLM") and not new_analysis.startswith("[PDF"):
            paper.analysis = new_analysis
            paper.error = None
            counts["analysis_ok"] += 1
            need_classify = True
        else:
            paper.analysis = new_analysis or "[LLM 解读失败]"
            counts["analysis_failed"] += 1
            return counts

    if need_classify:
        log.info(f"  重跑筛选: {paper.paper_id} {title_short}")
        if classify_and_score_with_llm(paper, llm_config, taxonomy, blacklist):
            counts["classify_ok"] += 1
        else:
            counts["classify_failed"] += 1
    return counts


def retry_failed_from_json(
    date_str: str,
    llm_config: LLMConfig,
    *,
    paper_id: Optional[str] = None,
    progress_callback: Optional[Callable[[int, int, Paper], None]] = None,
) -> dict:
    """重试失败论文：解读缺失/失败 → 重新下载PDF→提取文本→LLM解读；
    筛选缺失/失败 → 重新筛选。成功后写回 JSON。

    判断逻辑：
      - analysis 为空 / 以 [LLM / [PDF 开头 → 重跑解读
      - 解读正常但 domain_tags 和 score 都缺失 → 重跑筛选
    """
    import dataclasses

    path = DATA_DIR / f"{date_str}.json"
    if not path.is_file():
        raise FileNotFoundError(f"论文 JSON 不存在: {path.resolve()}")

    with open(path, encoding="utf-8") as f:
        payload = json.load(f)

    papers, categories, loaded_date = load_papers_from_export_json(path)
    if loaded_date and loaded_date != date_str:
        log.warning(f"JSON 内 date={loaded_date} 与请求 date={date_str} 不一致，以文件为准")

    targets = papers
    if paper_id:
        targets = [p for p in papers if p.paper_id == paper_id]
        if not targets:
            raise ValueError(f"未找到 paper_id: {paper_id}")

    to_retry = [p for p in targets if _paper_needs_retry(p, include_classify=True)]
    stats: dict = {
        "total": len(targets),
        "to_retry": len(to_retry),
        "analysis_ok": 0,
        "analysis_failed": 0,
        "classify_ok": 0,
        "classify_failed": 0,
        "skipped": len(targets) - len(to_retry),
    }

    if not to_retry:
        log.info(f"没有需要重试的论文: date={date_str}")
        stats["date"] = date_str
        stats["path"] = str(path)
        return stats

    if not llm_config.api_key:
        raise ValueError("LLM API Key 未配置")

    taxonomy = _load_taxonomy()
    blacklist = _load_blacklist()

    work_dir = PROJECT_ROOT / "arxiv_digest_work"
    pdf_dir = work_dir / "pdfs"
    pdf_dir.mkdir(parents=True, exist_ok=True)

    workers = resolve_llm_concurrency(llm_config)
    if workers > 1:
        log.info(f"LLM 重试并发数: {workers}")

    def retry_one(paper: Paper) -> dict[str, int]:
        return _retry_one_paper(
            paper,
            llm_config=llm_config,
            taxonomy=taxonomy,
            blacklist=blacklist,
            pdf_dir=pdf_dir,
        )

    def on_progress(done: int, total: int, paper: Paper) -> None:
        title_short = (paper.title or paper.paper_id)[:50]
        if progress_callback:
            progress_callback(done, total, paper)
        else:
            log.info(f"  重试进度 {format_progress_bar(done, total)} {paper.paper_id} {title_short}")

    partial_stats = run_with_concurrency(
        to_retry,
        retry_one,
        workers,
        progress_callback=on_progress,
    )
    for part in partial_stats:
        if not part:
            continue
        for key in ("analysis_ok", "analysis_failed", "classify_ok", "classify_failed"):
            stats[key] += int(part.get(key) or 0)

    updated = {p.paper_id: p for p in to_retry}
    allowed_fields = {f.name for f in fields(Paper)}

    for rec in payload.get("papers") or []:
        if not isinstance(rec, dict):
            continue
        pid = str(rec.get("paper_id") or "")
        if pid not in updated:
            continue
        paper = updated[pid]
        for fname in allowed_fields:
            if fname == "full_text":
                continue
            rec[fname] = getattr(paper, fname)

    payload["retried_at"] = now_bj().isoformat()
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)

    stats["date"] = date_str
    stats["path"] = str(path)
    log.info(
        f"重试写回完成: date={date_str}, "
        f"解读成功={stats['analysis_ok']}, 解读失败={stats['analysis_failed']}, "
        f"筛选成功={stats['classify_ok']}, 筛选失败={stats['classify_failed']}, "
        f"无需重试={stats['skipped']}"
    )
    return stats


def _process_extra_paper_input(
    pid: str,
    *,
    llm_config: LLMConfig,
    taxonomy: list[dict],
    blacklist: list[str],
    pdf_dir: Path,
    org_kb: list[OrgRecord],
) -> tuple[Optional[dict], dict[str, int], Optional[Paper]]:
    """抓取并 LLM 处理单条额外论文输入，返回 (JSON 记录, 计数, Paper)。"""
    part = {
        "fetched": 0,
        "analysis_ok": 0,
        "analysis_failed": 0,
        "classify_ok": 0,
        "classify_failed": 0,
    }
    kind = "URL" if is_url_identifier(pid) else "arXiv"
    log.info(f"[额外] ({kind}) {pid}")

    if is_url_identifier(pid):
        paper = fetch_paper_by_url(pid, pdf_dir)
    else:
        paper = fetch_paper_by_id(pid)
    if not paper:
        part["analysis_failed"] = 1
        return None, part, None

    part["fetched"] = 1

    try:
        if org_kb and llm_config.api_key:
            attach_related_orgs_to_paper(paper, org_kb, llm_config)
    except Exception:
        pass

    pdf_path = download_pdf(paper, pdf_dir)
    time.sleep(REQUEST_DELAY)
    if pdf_path:
        paper.full_text = extract_text_from_pdf(pdf_path)

    analysis = analyze_paper_with_llm(paper, llm_config)
    if analysis and not analysis.startswith("[LLM") and not analysis.startswith("[PDF"):
        paper.analysis = analysis
        part["analysis_ok"] = 1
    else:
        paper.analysis = analysis or "[LLM 解读失败]"
        part["analysis_failed"] = 1

    if taxonomy and _paper_has_valid_analysis(paper):
        if classify_and_score_with_llm(paper, llm_config, taxonomy, blacklist):
            part["classify_ok"] = 1
        else:
            part["classify_failed"] = 1

    record = asdict(paper)
    record.pop("full_text", None)
    return record, part, paper


def add_extra_papers(
    date_str: str,
    paper_ids: list[str],
    llm_config: LLMConfig,
    *,
    progress_callback: Optional[Callable[[int, int, Paper], None]] = None,
) -> dict:
    """管理员添加额外论文（加餐）：抓取元数据→下载PDF→LLM解读→筛选→写回 JSON。

    结果写入 data/papers/{date}.json 的 extra_papers 字段（独立于 papers）。
    已存在的 extra_paper_id 会跳过。
    """
    path = DATA_DIR / f"{date_str}.json"
    if not path.is_file():
        raise FileNotFoundError(f"论文 JSON 不存在: {path.resolve()}")

    with open(path, encoding="utf-8") as f:
        payload = json.load(f)

    extra_papers: list[dict] = list(payload.get("extra_papers") or [])
    existing_ids = {str(p.get("paper_id") or "") for p in extra_papers if isinstance(p, dict)}

    # 同时检查普通论文列表，避免重复
    normal_ids = {str(p.get("paper_id") or "") for p in (payload.get("papers") or []) if isinstance(p, dict)}

    def _normalize_input(s: str) -> str:
        """统一把加餐输入归一化为存储用的 paper_id，URL 与其生成的 slug 视作同一篇。"""
        s = s.strip()
        if is_url_identifier(s):
            return _url_to_paper_id(s)
        return s

    seen_inputs: set[str] = set()
    new_ids: list[str] = []
    skipped_ids: list[str] = []
    for pid in paper_ids:
        pid = (pid or "").strip()
        if not pid:
            continue
        norm = _normalize_input(pid)
        if norm in existing_ids or norm in normal_ids or norm in seen_inputs:
            skipped_ids.append(pid)
            continue
        seen_inputs.add(norm)
        new_ids.append(pid)

    stats: dict = {
        "requested": len(paper_ids),
        "new": len(new_ids),
        "skipped_existing": len(skipped_ids),
        "fetched": 0,
        "analysis_ok": 0,
        "analysis_failed": 0,
        "classify_ok": 0,
        "classify_failed": 0,
    }

    if not new_ids:
        log.info(f"没有需要新增的额外论文: date={date_str}")
        return stats

    if not llm_config.api_key:
        raise ValueError("LLM API Key 未配置")

    taxonomy = _load_taxonomy()
    blacklist = _load_blacklist()

    work_dir = PROJECT_ROOT / "arxiv_digest_work"
    pdf_dir = work_dir / "pdfs"
    pdf_dir.mkdir(parents=True, exist_ok=True)
    org_kb = load_org_knowledge_base()

    workers = resolve_llm_concurrency(llm_config)
    if workers > 1:
        log.info(f"LLM 额外论文并发数: {workers}")

    progress_papers: dict[str, Paper] = {}
    progress_lock = threading.Lock()

    def process_one(pid: str) -> tuple[Optional[dict], dict[str, int], Optional[Paper]]:
        record, part, paper = _process_extra_paper_input(
            pid,
            llm_config=llm_config,
            taxonomy=taxonomy,
            blacklist=blacklist,
            pdf_dir=pdf_dir,
            org_kb=org_kb,
        )
        if paper:
            with progress_lock:
                progress_papers[pid] = paper
        return record, part, paper

    def on_progress(done: int, total: int, pid: str) -> None:
        paper = progress_papers.get(pid)
        if progress_callback:
            stub = paper if paper else type("_ProgressPaper", (), {"paper_id": pid, "title": pid})()
            progress_callback(done, total, stub)
        else:
            log.info(f"  额外论文进度 {format_progress_bar(done, total)} {pid}")

    raw_results = run_with_concurrency(
        new_ids,
        process_one,
        workers,
        progress_callback=on_progress,
    )

    for record, part, _paper in raw_results:
        if not part:
            continue
        for key in ("fetched", "analysis_ok", "analysis_failed", "classify_ok", "classify_failed"):
            stats[key] += int(part.get(key) or 0)
        if record:
            extra_papers.append(record)

    if progress_callback and new_ids:
        last = progress_papers.get(new_ids[-1])
        if last:
            progress_callback(len(new_ids), len(new_ids), last)

    payload["extra_papers"] = extra_papers
    payload["extra_updated_at"] = now_bj().isoformat()
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)

    stats["date"] = date_str
    log.info(
        f"额外论文写回完成: date={date_str}, fetched={stats['fetched']}, "
        f"解读成功={stats['analysis_ok']}, 解读失败={stats['analysis_failed']}, "
        f"筛选成功={stats['classify_ok']}, 筛选失败={stats['classify_failed']}, "
        f"重复跳过={stats['skipped_existing']}"
    )
    return stats


# ─────────────────────────────────────────────
# 飞书 Webhook 推送
# ─────────────────────────────────────────────

FEISHU_MAX_RETRIES = 10


def _feishu_response_ok(data: dict) -> bool:
    """判断飞书 webhook JSON 响应是否表示成功。"""
    if not data:
        return True
    sc = data.get("StatusCode")
    code = data.get("code")
    if sc is not None and sc != 0:
        return False
    if code is not None and code != 0:
        return False
    return True


def send_feishu_message(
    papers: list[Paper],
    categories: list[str],
    date_str: str,
    webhook_url: str,
    public_url: str = "",
    max_titles: int = 100,
) -> None:
    """向飞书群机器人 webhook 发送一条文本汇总消息。"""
    if not webhook_url:
        log.info("未设置 FEISHU_WEBHOOK_URL，跳过飞书推送")
        return

    cat_str = " / ".join(categories)
    cross_count = sum(
        1 for p in papers if p.is_cross_list or len(p.source_categories) > 1
    )
    lines = [
        f"📚 arXiv 每日精读 — {date_str}",
        f"📡 分类: {cat_str}",
        f"📊 共 {len(papers)} 篇 (跨领域 {cross_count} 篇)",
    ]
    if public_url:
        lines.append(f"🌐 在线查看解读报告: {public_url}")
    lines.append("")

    shown = papers[:max_titles]
    for i, p in enumerate(shown, 1):
        flag = " 🔀" if (p.is_cross_list or len(p.source_categories) > 1) else ""
        lines.append(f"{i}. {p.title}{flag}")
        lines.append(f"   {p.abs_url}")
    if len(papers) > max_titles:
        lines.append(f"... 等 {len(papers) - max_titles} 篇")

    message_text = "\n".join(lines)
    content = {"text": message_text}
    payload = {"msg_type": "text", "content": content}
    json_payload = json.dumps(payload, ensure_ascii=False)
    headers = {"Content-Type": "application/json"}

    log.info(
        f"Sending {len(papers)} papers to webhook for category {cat_str}..."
    )
    last_err: Optional[Exception] = None
    for attempt in range(1, FEISHU_MAX_RETRIES + 1):
        try:
            resp = requests.post(
                webhook_url,
                data=json_payload,
                headers=headers,
                timeout=15,
            )
            resp.raise_for_status()
            data = (
                resp.json()
                if resp.headers.get("content-type", "").startswith("application/json")
                else {}
            )
            if not _feishu_response_ok(data):
                raise RuntimeError(f"飞书返回非成功状态: {data}")
            log.info("飞书消息发送成功")
            return
        except Exception as e:
            last_err = e
            if attempt < FEISHU_MAX_RETRIES:
                delay = random.uniform(2, 10)
                log.warning(
                    f"飞书消息发送失败 (第 {attempt}/{FEISHU_MAX_RETRIES} 次): {e}，"
                    f"{delay:.1f}s 后重试"
                )
                time.sleep(delay)
            else:
                log.error(
                    f"飞书消息发送失败，已重试 {FEISHU_MAX_RETRIES} 次: {e}"
                )
                raise last_err


# ─────────────────────────────────────────────
# 邮件发送
# ─────────────────────────────────────────────

def send_email(html_content: str, categories: list[str], config: dict):
    """通过 SMTP 发送 HTML 邮件。"""
    today = now_bj().strftime("%Y-%m-%d")
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
    parser.add_argument("--llm-concurrency", type=int, default=None,
                        help="LLM 并发请求数 (默认: $LLM_CONCURRENCY 或 4)")

    # 输出
    parser.add_argument("--no-email", action="store_true", help="不发送邮件")
    parser.add_argument("--output", default=None, help="HTML 输出路径")
    parser.add_argument("--only-download", action="store_true", help="仅下载 PDF")
    parser.add_argument(
        "--no-llm",
        action="store_true",
        help="不进行深度解读：不下载 PDF、不调用 LLM API，仅生成含元数据与摘要的 HTML",
    )
    parser.add_argument("--no-feishu", action="store_true", help="不发送飞书消息")
    parser.add_argument("--no-json", action="store_true", help="不导出 JSON 元数据文件")
    parser.add_argument(
        "--test-feishu",
        action="store_true",
        help="从论文快照 JSON 向 FEISHU_WEBHOOK_URL 发测试消息（不调 arXiv / LLM / 邮件）",
    )
    parser.add_argument(
        "--test-feishu-json",
        type=Path,
        default=DEFAULT_TEST_FEISHU_JSON,
        help=f"与 --test-feishu 合用：快照路径（默认: {DEFAULT_TEST_FEISHU_JSON}）",
    )
    parser.add_argument(
        "--run-classify-only",
        action="store_true",
        help="仅运行第二次 LLM 筛选（领域/创新/评分/黑名单），基于已有 JSON 中的解读结果",
    )
    parser.add_argument(
        "--retry-failed",
        action="store_true",
        help="仅重试失败论文：解读失败→重跑解读（下载PDF→LLM）；筛选失败→重跑筛选",
    )
    parser.add_argument(
        "--date",
        default=None,
        help="与 --run-classify-only / --retry-failed 合用：目标日期 YYYY-MM-DD（默认今天）",
    )
    parser.add_argument(
        "--paper-id",
        default=None,
        help="与 --run-classify-only / --retry-failed 合用：仅处理指定 paper_id",
    )

    args = parser.parse_args()

    if args.test_feishu:
        today = now_bj().strftime("%Y-%m-%d")
        feishu_webhook = os.environ.get("FEISHU_WEBHOOK_URL", "").strip()
        public_url = os.environ.get("WEB_PUBLIC_URL", "").strip()
        json_path = Path(args.test_feishu_json)
        log.info("=" * 60)
        log.info("--test-feishu：从 %s 加载论文并发往飞书 webhook", json_path)
        log.info("=" * 60)
        try:
            papers, cats, digest_date = load_papers_from_export_json(json_path)
        except FileNotFoundError as e:
            log.error("%s", e)
            sys.exit(1)
        if not papers:
            log.error("JSON 中 papers 为空，退出")
            sys.exit(1)
        date_for_msg = digest_date or today
        send_feishu_message(papers, cats, date_for_msg, feishu_webhook, public_url)
        log.info("--test-feishu 结束")
        sys.exit(0)

    # ── LLM 配置: 命令行 > 环境变量 > 默认值 ──
    llm_config = LLMConfig(
        base_url=args.llm_base_url or os.environ.get("LLM_BASE_URL", DEFAULT_LLM_BASE_URL),
        api_key=args.llm_api_key or os.environ.get("LLM_API_KEY", ""),
        model=args.llm_model or os.environ.get("LLM_MODEL", DEFAULT_LLM_MODEL),
        max_tokens=args.llm_max_tokens or int(os.environ.get("LLM_MAX_TOKENS", str(DEFAULT_MAX_TOKENS))),
        enable_thinking=env_bool("LLM_ENABLE_THINKING", False),
        concurrency=args.llm_concurrency or env_int("LLM_CONCURRENCY", DEFAULT_LLM_CONCURRENCY),
    )

    if (not args.no_llm or args.run_classify_only or args.retry_failed) and not llm_config.api_key:
        log.error("请设置 LLM API Key: --llm-api-key 或 export LLM_API_KEY=...")
        sys.exit(1)

    if args.run_classify_only:
        date_str = args.date or now_bj().strftime("%Y-%m-%d")
        log.info("=" * 60)
        log.info(f"第二次 LLM 筛选 — {date_str}")
        if args.paper_id:
            log.info(f"仅处理: {args.paper_id}")
        log.info("=" * 60)
        try:
            stats = run_classify_from_json(
                date_str,
                llm_config,
                paper_id=args.paper_id,
                max_papers=args.max_papers,
            )
        except FileNotFoundError as e:
            log.error(str(e))
            sys.exit(1)
        except ValueError as e:
            log.error(str(e))
            sys.exit(1)
        except Exception as e:
            log.error(f"筛选任务失败: {e}", exc_info=True)
            sys.exit(1)
        log.info(
            f"✅ 筛选完成: processed={stats['processed']}, "
            f"skipped={stats['skipped']}, failed={stats['failed']}"
        )
        sys.exit(0)

    if args.retry_failed:
        date_str = args.date or now_bj().strftime("%Y-%m-%d")
        log.info("=" * 60)
        log.info(f"重试失败论文 — {date_str}")
        if args.paper_id:
            log.info(f"仅处理: {args.paper_id}")
        log.info("=" * 60)
        try:
            stats = retry_failed_from_json(
                date_str,
                llm_config,
                paper_id=args.paper_id,
            )
        except FileNotFoundError as e:
            log.error(str(e))
            sys.exit(1)
        except ValueError as e:
            log.error(str(e))
            sys.exit(1)
        except Exception as e:
            log.error(f"重试任务失败: {e}", exc_info=True)
            sys.exit(1)
        log.info(
            f"✅ 重试完成: 解读成功={stats['analysis_ok']}, "
            f"解读失败={stats['analysis_failed']}, "
            f"筛选成功={stats['classify_ok']}, 筛选失败={stats['classify_failed']}, "
            f"无需重试={stats['skipped']}"
        )
        sys.exit(0)

    log.info("=" * 60)
    log.info("arXiv 每日论文精读 v2.0")
    log.info("=" * 60)
    log.info(f"分类: {', '.join(args.category)}")
    if args.no_llm:
        log.info("深度解读: 已关闭 (--no-llm)，将不下载 PDF、不调用 API")
    else:
        log.info(f"LLM:  {llm_config.model} @ {llm_config.base_url}")
        log.info(f"端点: {llm_config.chat_completions_url}")
        log.info(f"并发: {resolve_llm_concurrency(llm_config)}")

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

    org_kb: list[OrgRecord] = load_org_knowledge_base()
    if org_kb:
        log.info(f"单位知识库已加载: {len(org_kb)} 条")
    else:
        log.warning("单位知识库为空，后续将跳过单位匹配")

    # 加载领域分类表和黑名单
    taxonomy = _load_taxonomy()
    if taxonomy:
        log.info(f"领域分类表已加载: {len(taxonomy)} 个主领域")
    else:
        log.warning("领域分类表为空，将跳过领域分类")
    blacklist_entries = _load_blacklist()
    if blacklist_entries:
        log.info(f"黑名单已加载: {len(blacklist_entries)} 条")

    # ── 逐篇处理 ──
    workers = resolve_llm_concurrency(llm_config)
    today = now_bj().strftime("%Y-%m-%d")

    if not args.no_json:
        try:
            export_papers_json(
                papers, args.category, today, llm_config,
                skip_llm_analysis=args.no_llm,
                in_progress=True,
            )
        except Exception as e:
            log.warning(f"初始 JSON 导出失败: {e}")

    if args.no_llm:
        for i, paper in enumerate(papers, 1):
            cross_mark = " [跨领域]" if (paper.is_cross_list or len(paper.source_categories) > 1) else ""
            log.info(f"\n[{i}/{len(papers)}] {paper.title[:60]}...{cross_mark}")
            if org_kb and llm_config.api_key:
                attach_related_orgs_to_paper(paper, org_kb, llm_config)
                if paper.related_org_titles:
                    log.info(f"  相关单位: {', '.join(paper.related_org_titles)}")
    else:
        def process_one(paper: Paper) -> None:
            cross_mark = " [跨领域]" if (paper.is_cross_list or len(paper.source_categories) > 1) else ""
            log.info(f"  开始 {paper.paper_id} {paper.title[:60]}...{cross_mark}")
            _process_paper_llm_pipeline(
                paper,
                llm_config=llm_config,
                org_kb=org_kb,
                taxonomy=taxonomy,
                blacklist_entries=blacklist_entries,
                pdf_dir=pdf_dir,
            )

        last_json_checkpoint = 0

        def on_paper_progress(done: int, total: int, paper: Paper) -> None:
            nonlocal last_json_checkpoint
            log.info(
                f"  处理进度 {format_progress_bar(done, total)} "
                f"{paper.paper_id} {paper.title[:50]}"
            )
            if args.no_json:
                return
            if done < total and done - last_json_checkpoint < 3:
                return
            last_json_checkpoint = done
            try:
                export_papers_json(
                    papers, args.category, today, llm_config,
                    skip_llm_analysis=False,
                    in_progress=(done < total),
                )
            except Exception as e:
                log.warning(f"检查点 JSON 导出失败: {e}")

        log.info(f"\n开始 LLM 处理 {len(papers)} 篇论文（并发 {workers}）…")
        run_with_concurrency(
            papers,
            process_one,
            workers,
            progress_callback=on_paper_progress,
        )

    # ── 生成报告 ──
    log.info("\n正在生成 HTML 报告...")
    html_report = generate_html_report(
        papers, args.category, llm_config, skip_llm_analysis=args.no_llm
    )

    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    default_html_path = REPORTS_DIR / f"report_{today}.html"
    output_path = Path(args.output) if args.output else default_html_path
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(html_report)
    log.info(f"HTML 报告已保存: {output_path}")

    # ── 导出 JSON 元数据 (Web 检索/历史索引使用) ──
    if not args.no_json:
        try:
            export_papers_json(
                papers, args.category, today, llm_config,
                skip_llm_analysis=args.no_llm,
                in_progress=False,
            )
        except Exception as e:
            log.error(f"JSON 导出失败: {e}", exc_info=True)

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

    # ── 飞书 ──
    if not args.no_feishu:
        feishu_webhook = os.environ.get("FEISHU_WEBHOOK_URL", "").strip()
        public_url = os.environ.get("WEB_PUBLIC_URL", "").strip()
        if not feishu_webhook:
            log.info("未配置 FEISHU_WEBHOOK_URL，跳过飞书推送")
        else:
            try:
                send_feishu_message(
                    papers, args.category, today, feishu_webhook, public_url
                )
            except Exception as e:
                log.error(f"飞书推送失败: {e}", exc_info=True)
    else:
        log.info("已跳过飞书推送 (--no-feishu)")

    log.info("\n✅ 全部完成！")


if __name__ == "__main__":
    main()