"""每日论文综述：汇总当天大佬论文与常规论文，生成纯文本综述。

综述由 LLM 根据论文标题、解读内容、领域标签和评分生成，
按方向（TTS/音乐、ASR、理解、SV/SVC/FE 等）分组介绍，
存储到数据库（Store），并用于邮件 / 飞书 / RSS 推送。
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import requests

# send.py 的基础设施在函数内部延迟导入，避免循环依赖
import storage

# PROJECT_ROOT / DATA_DIR 与 send.py 保持一致
PROJECT_ROOT = Path(__file__).resolve().parent
DATA_DIR = PROJECT_ROOT / "data" / "papers"

# 类型别名（仅用于注解，运行时不需要实际导入）
LLMConfig = None  # type: ignore[assignment]

log = logging.getLogger("daily_digest")


def _now_bj():
    """延迟导入 now_bj 避免循环依赖。"""
    from send import now_bj
    return now_bj()

DIGEST_MAX_RETRIES = 3
DIGEST_RETRY_WAIT = 5

# Store 名：key=date_str -> {text, generated_at, paper_count, ...}
DIGEST_STORE_NAME = "daily_digests"


def _make_digest_store() -> storage.Store:
    return storage.make_store(
        DIGEST_STORE_NAME,
        data_dir=PROJECT_ROOT / "data",
        backend=storage.resolve_backend(),
        sqlite_path=PROJECT_ROOT / "data" / storage.DEFAULT_SQLITE_NAME,
    )


# ─────────────────────────────────────────────
# 方向分组映射
# ─────────────────────────────────────────────

# 将 taxonomy 中的主领域映射到综述的方向分组
DIRECTION_MAP: dict[str, list[str]] = {
    "TTS / 音乐生成": [
        "语音合成 (TTS)",
        "歌唱合成 (SVS)",
        "音乐生成",
        "通用音频生成",
    ],
    "语音识别 (ASR)": [
        "语音识别 (ASR)",
        "语音翻译",
    ],
    "语音理解与大模型": [
        "语音大模型与对话 (Speech LLM / SDM)",
        "音频理解",
        "自监督表征学习 (SSL)",
        "声音事件与场景",
        "神经音频编解码器 (Neural Codec)",
    ],
    "说话人 / 转换 / 增强": [
        "说话人技术",
        "语音转换 (VC / SVC)",
        "语音增强与分离",
    ],
    "其他方向": [
        "副语言与健康 (Paralinguistics)",
        "语音安全与隐私",
        "多模态视听",
        "空间音频",
        "评估与口语学习",
    ],
}


def _map_domain_to_direction(domain_tag: str) -> str:
    """将 domain_tag（如 '语音合成 (TTS) > Zero-shot TTS'）映射到方向分组。"""
    major = domain_tag.split(" > ")[0].strip()
    for direction, majors in DIRECTION_MAP.items():
        if major in majors:
            return direction
    return "其他方向"


# ─────────────────────────────────────────────
# 论文信息提取
# ─────────────────────────────────────────────

@dataclass
class PaperDigestInfo:
    """综述用的单篇论文摘要信息。"""
    index: int           # 在当天论文列表中的序号（从 1 开始，与网页 #N 一致）
    paper_id: str
    title: str
    authors: list[str]
    score: float
    domain_tags: list[str]
    innovation_method: str
    featured_authors: list[str]
    analysis: str        # LLM 解读全文
    is_featured: bool
    is_cross_list: bool


def _extract_paper_info(
    papers: list,
    featured_papers: list,
) -> list[PaperDigestInfo]:
    """从 Paper 对象列表提取综述需要的信息，并分配序号。

    序号规则：featured_papers 在前（#1, #2, ...），常规 papers 在后，
    与 send.py 中 processing_papers = [*featured_papers, *papers] 的顺序一致。
    """
    infos: list[PaperDigestInfo] = []
    idx = 1
    for p in featured_papers:
        infos.append(PaperDigestInfo(
            index=idx,
            paper_id=p.paper_id,
            title=p.title,
            authors=list(p.authors[:5]),
            score=p.score,
            domain_tags=list(p.domain_tags),
            innovation_method=p.innovation_method,
            featured_authors=list(p.featured_authors),
            analysis=p.analysis or "",
            is_featured=True,
            is_cross_list=p.is_cross_list,
        ))
        idx += 1
    for p in papers:
        infos.append(PaperDigestInfo(
            index=idx,
            paper_id=p.paper_id,
            title=p.title,
            authors=list(p.authors[:5]),
            score=p.score,
            domain_tags=list(p.domain_tags),
            innovation_method=p.innovation_method,
            featured_authors=list(p.featured_authors),
            analysis=p.analysis or "",
            is_featured=False,
            is_cross_list=p.is_cross_list,
        ))
        idx += 1
    return infos


def _truncate_analysis(analysis: str, max_chars: int = 800) -> str:
    """截取解读的前若干字符，保留核心摘要部分。"""
    if not analysis:
        return ""
    text = analysis.strip()
    if len(text) <= max_chars:
        return text
    # 尝试在段落边界截断
    truncated = text[:max_chars]
    last_newline = truncated.rfind("\n")
    if last_newline > max_chars // 2:
        truncated = truncated[:last_newline]
    return truncated + "..."


# ─────────────────────────────────────────────
# LLM Prompt 构造
# ─────────────────────────────────────────────

DIGEST_SYSTEM_PROMPT = """你是一位语音与音频领域的资深研究者，负责撰写每日论文综述。

你的任务是根据提供的论文信息（标题、编号、解读摘要、领域标签、评分），生成一份结构清晰、信息密度高的纯文本综述。

## 输出格式要求

综述使用纯文本格式，不使用 Markdown 或 HTML。结构如下：

1. 开头：简短日期与统计（共几篇，大佬论文几篇）

2. 「今日重点」：挑选 3-5 篇最重要的论文（大佬论文优先，高分论文次之），每篇用 1-2 句话点出亮点，标注编号 [N]

3. 按方向分节介绍：
   - TTS / 音乐生成
   - 语音识别 (ASR)
   - 语音理解与大模型
   - 说话人 / 转换 / 增强
   - 其他方向（如果有）

   每个方向下，列出该方向的论文，每篇用编号 [N] + 标题 + 一句话概括。
   如果某个方向没有论文，跳过该方向，不要输出空节。

4. 结尾：简短总结今日趋势

## 写作风格
- 纯文本，用「═══」「──」「•」等 ASCII 字符做分隔和缩进，不使用 Markdown
- 语言简洁有力，每篇论文最多两句话
- 论文仅用编号 [N] 引用，不重复标题全文（重点部分除外）
- 保持客观，区分「论文声称的」和「实际展示的」
- 如果某方向只有 1 篇论文，也要列出"""


def _build_digest_prompt(
    infos: list[PaperDigestInfo],
    date_str: str,
    categories: list[str],
) -> str:
    """构造发送给 LLM 的综述生成 prompt。"""
    total = len(infos)
    featured_count = sum(1 for i in infos if i.is_featured)
    cat_str = " / ".join(categories)

    lines = [
        f"日期: {date_str}",
        f"分类: {cat_str}",
        f"论文总数: {total}（大佬论文 {featured_count} 篇）",
        "",
        "═══════════════════════════════════════════════════════════",
        "论文列表（编号 [N] + 标题 + 领域 + 评分 + 解读摘要）",
        "═══════════════════════════════════════════════════════════",
        "",
    ]

    for info in infos:
        featured_mark = " [大佬论文]" if info.is_featured else ""
        cross_mark = " [跨领域]" if info.is_cross_list else ""
        authors_str = ", ".join(info.authors[:3])
        if len(info.authors) > 3:
            authors_str += " 等"

        lines.append(f"[{info.index}] {info.title}{featured_mark}{cross_mark}")
        lines.append(f"    作者: {authors_str}")
        if info.featured_authors:
            lines.append(f"    大佬: {', '.join(info.featured_authors)}")
        if info.domain_tags:
            lines.append(f"    领域: {'; '.join(info.domain_tags)}")
        if info.innovation_method:
            lines.append(f"    创新: {info.innovation_method}")
        lines.append(f"    评分: {info.score:.1f}")
        analysis_summary = _truncate_analysis(info.analysis, 800)
        if analysis_summary:
            lines.append(f"    解读: {analysis_summary}")
        lines.append("")

    lines.append("═══════════════════════════════════════════════════════════")
    lines.append("请根据以上信息生成今日论文综述。")
    lines.append("注意：论文仅用编号 [N] 引用，方向分节的标题用「──」分隔。")

    return "\n".join(lines)


# ─────────────────────────────────────────────
# LLM 调用
# ─────────────────────────────────────────────

def _call_llm_for_digest(
    prompt: str,
    llm_config,
) -> Optional[str]:
    """调用 LLM 生成综述文本，带重试。"""
    from send import apply_chat_payload_options
    headers = {"Content-Type": "application/json"}
    if llm_config.api_key:
        headers["Authorization"] = f"Bearer {llm_config.api_key}"

    payload = {
        "model": llm_config.model,
        "max_tokens": 4096,
        "temperature": 0.4,
        "messages": [
            {"role": "system", "content": DIGEST_SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
    }

    apply_chat_payload_options(payload, llm_config)
    api_url = llm_config.chat_completions_url

    for attempt in range(DIGEST_MAX_RETRIES + 1):
        try:
            resp = requests.post(api_url, headers=headers, json=payload, timeout=180)
            if resp.status_code == 429:
                if attempt < DIGEST_MAX_RETRIES:
                    log.warning(
                        f"  综述 API 429，{DIGEST_RETRY_WAIT}s 后重试 "
                        f"({attempt + 1}/{DIGEST_MAX_RETRIES})"
                    )
                    time.sleep(DIGEST_RETRY_WAIT)
                    continue
            resp.raise_for_status()
            data = resp.json()

            try:
                import llm_usage
                llm_usage.record_usage(data.get("usage"), purpose="digest")
            except Exception:
                pass

            choices = data.get("choices", [])
            content = choices[0].get("message", {}).get("content", "") if choices else ""
            if not content:
                log.warning("  综述 LLM 返回为空")
                return None
            return content.strip()

        except Exception as e:
            if attempt < DIGEST_MAX_RETRIES:
                delay = min(DIGEST_RETRY_WAIT * (attempt + 1), 30)
                log.warning(
                    f"  综述 LLM 调用失败 (第 {attempt + 1}/{DIGEST_MAX_RETRIES} 次): {e}，"
                    f"{delay}s 后重试"
                )
                time.sleep(delay)
            else:
                log.error(f"  综述 LLM 调用失败，已重试 {DIGEST_MAX_RETRIES} 次: {e}")
                return None

    return None


# ─────────────────────────────────────────────
# 综述生成主入口
# ─────────────────────────────────────────────

def generate_daily_digest(
    papers: list,
    featured_papers: list,
    categories: list[str],
    date_str: str,
    llm_config,
) -> Optional[str]:
    """生成每日论文综述并存储到数据库。

    Args:
        papers: 常规论文列表（Paper 对象）
        featured_papers: 大佬论文列表（Paper 对象）
        categories: arXiv 分类列表
        date_str: 日期字符串 YYYY-MM-DD
        llm_config: LLM 配置

    Returns:
        综述文本，失败返回 None
    """
    infos = _extract_paper_info(papers, featured_papers)
    if not infos:
        log.warning("  无论文可生成综述")
        return None

    # 只保留有有效解读的论文
    valid_infos = [
        i for i in infos
        if i.analysis and not i.analysis.startswith("[LLM") and not i.analysis.startswith("[PDF")
    ]
    if not valid_infos:
        log.warning("  无有效解读的论文，跳过综述生成")
        return None

    log.info(f"  开始生成每日综述（{len(valid_infos)}/{len(infos)} 篇有效解读）...")

    prompt = _build_digest_prompt(valid_infos, date_str, categories)
    digest_text = _call_llm_for_digest(prompt, llm_config)
    if not digest_text:
        log.error("  综述生成失败")
        return None

    # 存储到数据库
    store = _make_digest_store()
    record = {
        "text": digest_text,
        "generated_at": _now_bj().isoformat(),
        "date": date_str,
        "categories": categories,
        "paper_count": len(infos),
        "valid_count": len(valid_infos),
        "featured_count": sum(1 for i in valid_infos if i.is_featured),
        "llm_model": llm_config.model,
    }
    store.put(date_str, record)
    log.info(f"  ✅ 综述已存储: {date_str} ({len(digest_text)} 字符)")

    return digest_text


def get_daily_digest(date_str: str) -> Optional[dict]:
    """从数据库读取指定日期的综述。"""
    store = _make_digest_store()
    return store.get(date_str)


def get_recent_digests(limit: int = 10) -> list[dict]:
    """获取最近若干天的综述列表（按日期倒序）。"""
    store = _make_digest_store()
    all_data = store.all()
    items = []
    for date_str, record in all_data.items():
        if not isinstance(record, dict):
            continue
        items.append({"date": date_str, **record})
    items.sort(key=lambda x: x.get("date", ""), reverse=True)
    return items[:limit]


# ─────────────────────────────────────────────
# 从 JSON 快照生成综述（离线 / 重跑用）
# ─────────────────────────────────────────────

def generate_digest_from_json(
    date_str: str,
    llm_config,
) -> Optional[str]:
    """从 data/papers/{date_str}.json 加载论文并生成综述。"""
    from send import load_papers_from_export_json, Paper
    from dataclasses import fields as dc_fields

    json_path = DATA_DIR / f"{date_str}.json"
    if not json_path.exists():
        log.error(f"论文 JSON 不存在: {json_path}")
        return None

    with open(json_path, encoding="utf-8") as f:
        payload = json.load(f)

    categories = list(payload.get("categories") or [])

    # 用 load_papers_from_export_json 加载常规论文
    papers, cats, loaded_date = load_papers_from_export_json(json_path)
    categories = cats or categories

    # 大佬论文在 JSON 的 featured_papers 字段
    featured_papers = []
    allowed = {f.name for f in dc_fields(Paper)}
    for rec in payload.get("featured_papers") or []:
        if not isinstance(rec, dict):
            continue
        kwargs = {k: v for k, v in rec.items() if k in allowed}
        kwargs.setdefault("full_text", "")
        kwargs.setdefault("analysis", "")
        kwargs.setdefault("comments", "")
        kwargs.setdefault("primary_category", "")
        kwargs.setdefault("is_cross_list", False)
        featured_papers.append(Paper(**kwargs))

    return generate_daily_digest(
        papers, featured_papers, categories, date_str, llm_config
    )
