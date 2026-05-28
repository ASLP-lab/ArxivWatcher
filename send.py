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
from difflib import SequenceMatcher
from pathlib import Path
from datetime import datetime, timezone
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from dataclasses import dataclass, asdict, field, fields
from typing import Optional

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
# 飞书 / 离线测试用：默认使用已导出的论文快照（元数据 + 摘要等「总结」字段）
DEFAULT_TEST_FEISHU_JSON = DATA_DIR / "2026-05-15.json"
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
ORG_SNIPPET_LENGTH = 300
ORG_MATCH_MIN_SCORE = 0.84
ORG_MATCH_MAX_RESULTS = 6
ORG_GENERIC_TOKENS = {
    "university", "universities", "institute", "institutes", "college", "school",
    "academy", "department", "faculty", "center", "centre", "laboratory", "lab",
    "research", "group", "hospital", "clinic", "company", "corporation", "corp",
    "inc", "ltd", "limited", "llc", "gmbh", "of", "the", "and", "for", "at", "in",
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

    # GLM 系列模型关闭思考模式（仅 GLM 支持此参数）
    if "glm-5.1" in llm_config.model.lower():
        payload["thinking"] = {"type": "disabled"}

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
    if "glm-5.1" in llm_config.model.lower():
        payload["thinking"] = {"type": "disabled"}

    try:
        resp = requests.post(llm_config.chat_completions_url, headers=headers, json=payload, timeout=120)
        resp.raise_for_status()
        data = resp.json()
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
            analysis_block = f"""
            <div class="analysis">
                <h3>📖 深度解读</h3>
                {analysis_html}
            </div>"""

        badges = ""
        if not skip_llm_analysis and (paper.error or (paper.analysis and paper.analysis.startswith("[LLM"))):
            badges += '<span class="badge badge-error">解读失败</span>'
        if paper.is_cross_list or len(paper.source_categories) > 1:
            badges += '<span class="badge badge-cross">跨领域</span>'

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
) -> Path:
    """将论文元数据落地为 JSON，便于 Web 端做搜索 / 历史归档。"""
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
        "total": len(papers),
        "cross_count": sum(
            1 for p in papers if p.is_cross_list or len(p.source_categories) > 1
        ),
        "papers": paper_records,
    }

    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
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
    )

    if not args.no_llm and not llm_config.api_key:
        log.error("请设置 LLM API Key: --llm-api-key 或 export LLM_API_KEY=...")
        sys.exit(1)

    log.info("=" * 60)
    log.info("arXiv 每日论文精读 v2.0")
    log.info("=" * 60)
    log.info(f"分类: {', '.join(args.category)}")
    if args.no_llm:
        log.info("深度解读: 已关闭 (--no-llm)，将不下载 PDF、不调用 API")
    else:
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

    org_kb: list[OrgRecord] = load_org_knowledge_base()
    if org_kb:
        log.info(f"单位知识库已加载: {len(org_kb)} 条")
    else:
        log.warning("单位知识库为空，后续将跳过单位匹配")

    # ── 逐篇处理 ──
    for i, paper in enumerate(papers, 1):
        cross_mark = " [跨领域]" if (paper.is_cross_list or len(paper.source_categories) > 1) else ""
        log.info(f"\n[{i}/{len(papers)}] {paper.title[:60]}...{cross_mark}")

        # 单位检测：优先用 metadata 文本，若后续有全文会再次补充检测
        if org_kb and llm_config.api_key:
            attach_related_orgs_to_paper(paper, org_kb, llm_config)
            if paper.related_org_titles:
                log.info(f"  相关单位: {', '.join(paper.related_org_titles)}")

        if args.no_llm:
            continue

        pdf_path = download_pdf(paper, pdf_dir)
        time.sleep(REQUEST_DELAY)

        if not pdf_path:
            paper.error = "PDF 下载失败"
            paper.analysis = "[PDF 下载失败，无法解读]"
            continue

        paper.full_text = extract_text_from_pdf(pdf_path)
        if not paper.full_text.strip():
            log.warning("  文本提取为空，将使用摘要")
        elif org_kb and llm_config.api_key:
            # 有全文后再跑一次，通常比 metadata 更准
            attach_related_orgs_to_paper(paper, org_kb, llm_config)

        paper.analysis = analyze_paper_with_llm(paper, llm_config)
        time.sleep(1)

    # ── 生成报告 ──
    log.info("\n正在生成 HTML 报告...")
    html_report = generate_html_report(
        papers, args.category, llm_config, skip_llm_analysis=args.no_llm
    )

    today = now_bj().strftime("%Y-%m-%d")

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