"""MCP (Model Context Protocol) Server for ArxivWatcher.

向 AI 终端工具（Cursor / Claude Code / Codex）暴露本站论文数据的只读工具，
让本地 AI 直接从网页读取每日论文、精读解读、全文等信息。

实现采用 Streamable HTTP 传输（MCP 2025-03-26+）：
  • 单一端点 POST（可选 GET/DELETE）
  • 客户端发送 JSON-RPC 2.0 请求
  • 服务端以 application/json 返回单条 JSON-RPC 响应

本实现是无状态的（每个请求独立处理），因此不强制要求 Mcp-Session-Id，
但会在 initialize 时返回一个 session id 以兼容严格客户端。工具调用本身不依赖会话。

工具列表：
  list_dates          — 列出所有可用日期
  list_papers         — 列出某日论文（含评分/分类，不含正文）
  get_paper           — 取单篇论文完整元数据 + 精读解读
  search_papers       — 跨日期按关键词搜索论文
  get_paper_fulltext  — 取单篇论文的 PDF 全文（按需提取，可能较慢）

所有工具均为只读，复用 web.py 中已有的数据访问函数。
"""

from __future__ import annotations

import json
import logging
import re
import secrets
from typing import Any

from flask import Response, request

log = logging.getLogger("mcp_server")

# MCP 协议版本（2025-06-18 规范，广泛兼容）
PROTOCOL_VERSION = "2025-06-18"
SERVER_NAME = "arxivwatcher"
SERVER_VERSION = "1.0.0"

# JSON-RPC 错误码
ERR_PARSE_ERROR = -32700
ERR_INVALID_REQUEST = -32600
ERR_METHOD_NOT_FOUND = -32601
ERR_INVALID_PARAMS = -32602
ERR_INTERNAL = -32603


# ─────────────────────────────────────────────
# JSON-RPC 基础
# ─────────────────────────────────────────────

def _jsonrpc_error(code: int, message: str, req_id: Any) -> dict:
    return {
        "jsonrpc": "2.0",
        "error": {"code": code, "message": message},
        "id": req_id,
    }


def _jsonrpc_result(result: Any, req_id: Any) -> dict:
    return {"jsonrpc": "2.0", "result": result, "id": req_id}


def _new_session_id() -> str:
    return secrets.token_hex(16)


# ─────────────────────────────────────────────
# MCP 方法
# ─────────────────────────────────────────────

def _mcp_initialize(req_id: Any) -> dict:
    return _jsonrpc_result(
        {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
        },
        req_id,
    )


def _mcp_ping(req_id: Any) -> dict:
    return _jsonrpc_result({}, req_id)


def _mcp_tools_list(req_id: Any) -> dict:
    return _jsonrpc_result({"tools": _tool_definitions()}, req_id)


def _mcp_tools_call(req_id: Any, params: dict) -> dict:
    name = params.get("name", "")
    args = params.get("arguments") or {}
    handler = _TOOL_HANDLERS.get(name)
    if not handler:
        return _jsonrpc_error(ERR_METHOD_NOT_FOUND, f"未知工具: {name}", req_id)
    try:
        text = handler(args)
    except _McpUserError as e:
        # 业务错误：作为 tool result 的 isError 返回，而非 JSON-RPC error
        return _jsonrpc_result(
            {"content": [{"type": "text", "text": f"⚠️ {e}"}], "isError": True},
            req_id,
        )
    except Exception as e:  # noqa: BLE001
        log.exception("MCP 工具 %s 执行异常", name)
        return _jsonrpc_result(
            {"content": [{"type": "text", "text": f"⚠️ 内部错误: {e}"}], "isError": True},
            req_id,
        )
    return _jsonrpc_result(
        {"content": [{"type": "text", "text": text}]},
        req_id,
    )


class _McpUserError(Exception):
    """工具参数/数据校验错误，转成 isError 给客户端。"""


# ─────────────────────────────────────────────
# 工具定义与实现（延迟引用 web 模块，避免循环导入）
# ─────────────────────────────────────────────

def _tool_definitions() -> list[dict]:
    return [
        {
            "name": "list_dates",
            "description": (
                "列出本站所有可用的论文日期（倒序，最新在前）。"
                "用于了解有哪些天的数据，再结合 list_papers / get_paper 取详情。"
            ),
            "inputSchema": {"type": "object", "properties": {}, "required": []},
        },
        {
            "name": "list_papers",
            "description": (
                "列出指定日期的全部论文（含标题、作者、评分、领域标签、推荐等级等）。"
                "默认不含精读解读正文（精简模式，仅元数据）。"
                "若想一次性拿到全部精读解读以便 AI 直接按内容筛选推荐，"
                "传 include_analysis=true —— 这样一轮调用即可读完当天所有解读，"
                "避免对每篇逐一调 get_paper。"
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "date": {
                        "type": "string",
                        "description": '日期，格式 YYYY-MM-DD，如 "2026-06-18"。',
                    },
                    "include_analysis": {
                        "type": "boolean",
                        "default": False,
                        "description": (
                            "是否在结果中包含每篇论文的 LLM 精读解读全文。"
                            "默认 false（只列元数据摘要，省 token）。"
                            "需要让 AI 基于精读内容做筛选/推荐时设为 true。"
                        ),
                    },
                },
                "required": ["date"],
            },
        },
        {
            "name": "get_paper",
            "description": (
                "取单篇论文的完整信息：元数据（标题/作者/评分等）+ LLM 精读解读全文（Markdown）。"
                "这是了解一篇论文最详细的单一来源。"
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "date": {"type": "string", "description": "论文所属日期 YYYY-MM-DD。"},
                    "paper_id": {
                        "type": "string",
                        "description": "arXiv 论文 id，如 2402.12345（可带版本号）。",
                    },
                },
                "required": ["date", "paper_id"],
            },
        },
        {
            "name": "search_papers",
            "description": (
                "跨所有日期按关键词搜索论文。匹配标题、作者、arXiv 分类码、id 等。"
                "适合「找某作者最近发了什么」「找某方向的论文」。"
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "搜索关键词。"},
                    "date": {
                        "type": "string",
                        "description": "可选，限定某一天 YYYY-MM-DD。",
                    },
                    "category": {
                        "type": "string",
                        "description": "可选，限定分类，如 cs.SD。",
                    },
                },
                "required": ["query"],
            },
        },
        {
            "name": "get_paper_fulltext",
            "description": (
                "取单篇论文 PDF 提取的全文文本。首次调用会下载 PDF 并提取，可能较慢（数秒）。"
                "仅当你需要论文方法/实验的细节时才调用。一般 get_paper 的精读解读已足够。"
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "date": {"type": "string", "description": "论文所属日期 YYYY-MM-DD。"},
                    "paper_id": {"type": "string", "description": "arXiv 论文 id。"},
                },
                "required": ["date", "paper_id"],
            },
        },
        {
            "name": "get_interactions",
            "description": (
                "取某天所有论文的社区互动统计：点赞数、踩数、评论数。"
                "用于了解哪些论文受关注、被讨论。注意：只返回汇总数字，不返回具体是谁投的票。"
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "date": {"type": "string", "description": "日期 YYYY-MM-DD。"},
                },
                "required": ["date"],
            },
        },
        {
            "name": "get_comments",
            "description": (
                "取某篇论文的全部评论（公开）。包含评论者用户名、内容、时间。"
                "用于看大家在讨论这篇论文的什么。也可只传 date 取某天所有论文的评论。"
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "date": {"type": "string", "description": "日期 YYYY-MM-DD。"},
                    "paper_id": {
                        "type": "string",
                        "description": "可选。传了则只取该论文评论；不传则取该天全部论文评论（按论文分组）。",
                    },
                },
                "required": ["date"],
            },
        },
        {
            "name": "get_community_highlights",
            "description": (
                "取某篇论文上所有用户标注/划过的重点文本（去重，匿名）。"
                "这是社区智慧的聚合——大家都标记了哪些句子，往往是论文的关键点。"
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "date": {"type": "string", "description": "日期 YYYY-MM-DD。"},
                    "paper_id": {"type": "string", "description": "arXiv 论文 id。"},
                },
                "required": ["date", "paper_id"],
            },
        },
        {
            "name": "get_site_stats",
            "description": (
                "取本站的访问统计：今日访问量/活跃用户、累计访问量，以及最近若干天每日访问情况。"
                "用于了解站点的活跃度。"
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "days": {
                        "type": "number",
                        "default": 14,
                        "description": "返回最近多少天的逐日统计，默认 14。",
                    },
                },
                "required": [],
            },
        },
    ]


# 延迟导入 web 模块的数据函数，避免在 Flask app 构建前的循环导入。
def _web():
    import web
    return web


def _grade_emoji(score: float, blacklisted: bool) -> str:
    """评分→推荐等级 emoji，与前端逻辑一致。"""
    if blacklisted:
        return "💤 可跳过"
    if score >= 8:
        return "🔥 必读"
    if score >= 5:
        return "👀 值得看"
    if score > 0:
        return "💤 可跳过"
    return ""


def _summarize_paper(p: dict) -> str:
    """把单篇论文压缩成一行摘要（用于列表）。"""
    pid = p.get("paper_id", "?")
    title = (p.get("title") or "").strip().replace("\n", " ")
    score = p.get("score") or 0.0
    grade = _grade_emoji(score, bool(p.get("blacklisted")))
    tags = p.get("domain_tags") or []
    tags_s = "/" .join(tags[:3]) if tags else ""
    authors = p.get("authors") or []
    authors_s = ", ".join(authors[:3])
    if len(authors) > 3:
        authors_s += f" 等{len(authors)}人"
    parts = [f"[{pid}]"]
    if grade:
        parts.append(grade)
    if tags_s:
        parts.append(f"({tags_s})")
    line = " ".join(parts) + f" {title}"
    if authors_s:
        line += f" — {authors_s}"
    if score:
        line += f"  评分 {score}"
    return line


def _tool_list_dates(args: dict) -> str:
    web = _web()
    dates = web.list_all_dates()
    lines = [f"共 {len(dates)} 天可用（倒序）："]
    for d in dates[:60]:
        lines.append(f"- {d}")
    if len(dates) > 60:
        lines.append(f"... 等共 {len(dates)} 天")
    return "\n".join(lines)


def _analysis_of(p: dict) -> str:
    """安全提取某篇论文的精读解读文本。返回空串表示无解读。"""
    analysis = (p.get("analysis") or "").strip()
    # 兼容未生成/失败占位（与 get_paper 的判定一致）
    if analysis and not analysis.startswith("[LLM") and not analysis.startswith("[PDF"):
        return analysis
    return ""


def _tool_list_papers(args: dict) -> str:
    web = _web()
    date = str(args.get("date") or "").strip()
    if not date:
        raise _McpUserError("缺少参数 date")
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
        raise _McpUserError("date 格式应为 YYYY-MM-DD")
    index = web.load_index(date)
    if not index:
        raise _McpUserError(f"{date} 没有数据")
    papers = index.get("papers") or []
    extras = index.get("extra_papers") or []

    include_analysis = bool(args.get("include_analysis"))

    lines = [f"📅 {date} 共 {len(papers)} 篇论文（另有 {len(extras)} 篇额外）"]
    if index.get("llm_model"):
        lines.append(f"（LLM 模型: {index['llm_model']}）")
    if include_analysis:
        lines.append("（已包含精读解读全文，可直接据此筛选）")
    lines.append("")

    def _emit(lst: list, heading: str | None = None) -> None:
        if heading:
            lines.append("")
            lines.append(heading)
        for p in lst:
            lines.append(_summarize_paper(p))
            if include_analysis:
                analysis = _analysis_of(p)
                if analysis:
                    lines.append("    精读解读：")
                    for ln in analysis.splitlines():
                        lines.append("    " + ln)
                    lines.append("")
                else:
                    lines.append("    精读解读：（暂无）")
                    lines.append("")

    _emit(papers)
    if extras:
        _emit(extras, f"── 额外论文（{len(extras)} 篇）──")

    lines.append("")
    if include_analysis:
        lines.append("提示：已附精读解读。如需某篇 PDF 全文细节，用 get_paper_fulltext。")
    else:
        lines.append("提示：本结果只含元数据。想让 AI 基于精读内容筛选，请重调用并传 include_analysis=true；"
                     "对单篇感兴趣用 get_paper 取精读，或 get_paper_fulltext 取 PDF 全文。")
    return "\n".join(lines)


def _tool_get_paper(args: dict) -> str:
    web = _web()
    date = str(args.get("date") or "").strip()
    paper_id = str(args.get("paper_id") or "").strip()
    if not date or not paper_id:
        raise _McpUserError("需要 date 和 paper_id 两个参数")
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
        raise _McpUserError("date 格式应为 YYYY-MM-DD")
    index = web.load_index(date)
    if not index:
        raise _McpUserError(f"{date} 没有数据")
    p = web._find_paper(index, paper_id)
    if not p:
        raise _McpUserError(f"在 {date} 找不到论文 {paper_id}")

    out = []
    out.append(f"# {p.get('title', '').strip()}")
    out.append("")
    authors = p.get("authors") or []
    if authors:
        out.append("**作者**: " + ", ".join(authors))
    if p.get("subjects"):
        out.append(f"**arXiv 分类**: {p['subjects']}")
    if p.get("comments"):
        out.append(f"**Comments**: {p['comments']}")
    if p.get("abs_url"):
        out.append(f"**链接**: {p['abs_url']}")
    score = p.get("score") or 0.0
    grade = _grade_emoji(score, bool(p.get("blacklisted")))
    out.append(f"**LLM 评分**: {score}" + (f"  {grade}" if grade else ""))
    tags = p.get("domain_tags") or []
    if tags:
        out.append("**领域标签**: " + " | ".join(tags))
    if p.get("innovation_method"):
        out.append(f"**创新方法**: {p['innovation_method']}")
    orgs = p.get("related_org_titles") or []
    if orgs:
        out.append("**相关单位**: " + ", ".join(orgs))
    if p.get("blacklisted"):
        out.append(f"**⚠️ 命中黑名单**: {p.get('blacklist_reason') or '是'}")
    out.append("")
    out.append("## 摘要")
    out.append(p.get("abstract") or "（无摘要）")
    out.append("")
    analysis = _analysis_of(p)
    out.append("## LLM 精读解读")
    if analysis:
        out.append(analysis)
    else:
        out.append(f"（暂无解读：{(p.get('analysis') or '').strip() or '未生成'}）")
    return "\n".join(out)


def _tool_search_papers(args: dict) -> str:
    web = _web()
    query = str(args.get("query") or "").strip()
    if not query:
        raise _McpUserError("缺少参数 query")
    results = web.search_papers(
        query,
        date=str(args.get("date") or ""),
        category=str(args.get("category") or ""),
    )
    if not results:
        return f'未找到与「{query}」相关的论文。'
    lines = [f'搜索「{query}」命中 {len(results)} 篇（最多显示前 50）：', ""]
    for p in results[:50]:
        d = p.get("date", "?")
        lines.append(f"- [{d}] {_summarize_paper(p)}")
    if len(results) > 50:
        lines.append("")
        lines.append(f"... 共 {len(results)} 条，已截断。可用 date 参数缩小范围。")
    return "\n".join(lines)


def _tool_get_paper_fulltext(args: dict) -> str:
    web = _web()
    date = str(args.get("date") or "").strip()
    paper_id = str(args.get("paper_id") or "").strip()
    if not date or not paper_id:
        raise _McpUserError("需要 date 和 paper_id 两个参数")
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
        raise _McpUserError("date 格式应为 YYYY-MM-DD")
    index = web.load_index(date)
    if not index:
        raise _McpUserError(f"{date} 没有数据")
    p = web._find_paper(index, paper_id)
    if not p:
        raise _McpUserError(f"在 {date} 找不到论文 {paper_id}")
    # 复用 web 模块已实现的全文提取逻辑（含磁盘缓存）
    text = web.extract_paper_fulltext(date, p)
    if not text:
        raise _McpUserError("PDF 全文提取失败（可能下载失败或无法解析）。")
    if len(text) > 60000:
        text = text[:60000] + "\n\n[... 全文过长，已截断 ...]"
    header = f"# {p.get('title', '').strip()} — PDF 全文\n\n"
    return header + text


# ── 社区互动（只读）──

def _require_valid_date(date: str) -> None:
    if not date or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
        raise _McpUserError("date 格式应为 YYYY-MM-DD")


def _tool_get_interactions(args: dict) -> str:
    """某天所有论文的点赞/踩/评论数统计。复用 web.py 的 store。"""
    web = _web()
    date = str(args.get("date") or "").strip()
    _require_valid_date(date)
    prefix = date + "/"
    result: dict[str, dict] = {}
    # 评论数
    with web._comments_lock:
        comments_data = web._get_comments()
        comment_counts = {
            key[len(prefix):]: len(comments)
            for key, comments in comments_data.items()
            if key.startswith(prefix)
        }
    # 赞/踩
    with web._interactions_lock:
        data = web._get_interactions()
        for key, entry in data.items():
            if key.startswith(prefix):
                pid = key[len(prefix):]
                result[pid] = {
                    "likes": entry.get("likes", 0),
                    "dislikes": entry.get("dislikes", 0),
                    "comment_count": comment_counts.get(pid, 0),
                }
    # 补上只有评论、没有赞踩的论文
    for pid, cnt in comment_counts.items():
        if pid not in result:
            result[pid] = {"likes": 0, "dislikes": 0, "comment_count": cnt}

    if not result:
        return f"{date} 暂无任何互动数据。"
    # 按热度排序：赞 - 踩 + 评论数*2
    ranked = sorted(
        result.items(),
        key=lambda kv: (kv[1]["likes"] - kv[1]["dislikes"] + kv[1]["comment_count"] * 2),
        reverse=True,
    )
    lines = [f"📊 {date} 互动统计（按热度排序）", ""]
    for pid, v in ranked:
        star = ""
        if v["likes"] or v["comment_count"]:
            star = " 🔥"
        lines.append(
            f"- [{pid}] 👍{v['likes']} 👎{v['dislikes']} 💬{v['comment_count']}{star}"
        )
    return "\n".join(lines)


def _tool_get_comments(args: dict) -> str:
    """取评论：单篇或某天全部。"""
    web = _web()
    date = str(args.get("date") or "").strip()
    _require_valid_date(date)
    paper_id = str(args.get("paper_id") or "").strip()
    with web._comments_lock:
        data = web._get_comments()
        if paper_id:
            key = web._interaction_key(date, paper_id)
            comments = data.get(key, [])
            if not comments:
                return f"{date}/{paper_id} 暂无评论。"
            lines = [f"💬 {paper_id}（{date}）的 {len(comments)} 条评论：", ""]
            for c in comments:
                ts = (c.get("created_at") or "")[:16].replace("T", " ")
                lines.append(f"- [{c.get('username', '?')}] {c.get('text', '')}  ({ts})")
            return "\n".join(lines)
        # 某天全部
        prefix = date + "/"
        grouped = {
            key[len(prefix):]: comments
            for key, comments in data.items()
            if key.startswith(prefix)
        }
        if not grouped:
            return f"{date} 暂无任何评论。"
        total = sum(len(c) for c in grouped.values())
        lines = [f"💬 {date} 共 {total} 条评论，涉及 {len(grouped)} 篇论文：", ""]
        for pid, comments in grouped.items():
            lines.append(f"── [{pid}]（{len(comments)} 条）──")
            for c in comments:
                ts = (c.get("created_at") or "")[:16].replace("T", " ")
                lines.append(f"  • [{c.get('username', '?')}] {c.get('text', '')}  ({ts})")
            lines.append("")
        return "\n".join(lines)


def _tool_get_community_highlights(args: dict) -> str:
    """某篇论文的社区标注聚合（去重、匿名）。复用 api_highlights_community 的逻辑。"""
    web = _web()
    date = str(args.get("date") or "").strip()
    paper_id = str(args.get("paper_id") or "").strip()
    if not date or not paper_id:
        raise _McpUserError("需要 date 和 paper_id")
    _require_valid_date(date)
    paper_key = web._interaction_key(date, paper_id)
    seen: set[str] = set()
    marks: list[str] = []
    with web._highlights_lock:
        data = web._get_highlights()
        for user_data in data.values():
            for h in user_data.get(paper_key, []):
                text = str(h.get("text") or "").strip()
                if not text or text in seen:
                    continue
                seen.add(text)
                marks.append(text)
    if not marks:
        return f"{date}/{paper_id} 暂无社区标注。"
    lines = [f"🖍️ {paper_id}（{date}）的社区标注（{len(marks)} 条，已去重）：", ""]
    for i, m in enumerate(marks, 1):
        # 单条过长截断显示
        if len(m) > 200:
            m = m[:200] + "…"
        lines.append(f"{i}. {m}")
    return "\n".join(lines)


def _tool_get_site_stats(args: dict) -> str:
    """站点访问统计。"""
    web = _web()
    try:
        days = int(args.get("days") or 14)
    except (TypeError, ValueError):
        days = 14
    days = max(1, min(days, 60))
    today_total = web.get_today_visit_total()
    today_users = web.get_today_active_user_total()
    grand = web.get_grand_visit_total()
    snapshot = web.get_visits_snapshot()
    today_str = web.datetime.now(web.BJ_TZ).strftime("%Y-%m-%d")

    lines = [
        f"📈 站点访问统计",
        f"- 今日访问: {today_total}（活跃用户 {today_users}）",
        f"- 累计访问: {grand}",
        "",
        f"最近 {days} 天逐日访问：",
    ]
    sorted_days = sorted(snapshot.keys(), reverse=True)[:days]
    for d in sorted_days:
        info = snapshot.get(d) or {}
        total = info.get("total", 0)
        users = info.get("active_users", 0)
        bar = "█" * min(30, total // 5) if total else "·"
        marker = " ← 今天" if d == today_str else ""
        lines.append(f"- {d}: {total:>4} 访问, {users:>3} 用户  {bar}{marker}")
    return "\n".join(lines)


_TOOL_HANDLERS = {
    "list_dates": _tool_list_dates,
    "list_papers": _tool_list_papers,
    "get_paper": _tool_get_paper,
    "search_papers": _tool_search_papers,
    "get_paper_fulltext": _tool_get_paper_fulltext,
    "get_interactions": _tool_get_interactions,
    "get_comments": _tool_get_comments,
    "get_community_highlights": _tool_get_community_highlights,
    "get_site_stats": _tool_get_site_stats,
}


# ─────────────────────────────────────────────
# 请求分发
# ─────────────────────────────────────────────

def _handle_jsonrpc(message: Any, session_id: str) -> dict | None:
    """处理单条 JSON-RPC 请求/通知，返回响应 dict（通知返回 None）。"""
    if not isinstance(message, dict):
        return _jsonrpc_error(ERR_INVALID_REQUEST, "请求必须是 JSON 对象", None)

    req_id = message.get("id")
    is_notification = req_id is None and "id" not in message

    method = message.get("method")
    params = message.get("params") or {}

    if method == "initialize":
        return _mcp_initialize(req_id)
    if method == "ping":
        return _mcp_ping(req_id)
    if method == "tools/list":
        return _mcp_tools_list(req_id)
    if method == "tools/call":
        return _mcp_tools_call(req_id, params)
    if method in ("notifications/initialized", "notifications/cancelled"):
        # 通知，无需响应
        return None
    # 未知方法
    if is_notification:
        return None
    return _jsonrpc_error(ERR_METHOD_NOT_FOUND, f"未知方法: {method}", req_id)


def handle_mcp_request() -> Response:
    """Flask 视图：处理 MCP Streamable HTTP 的 POST 请求。

    无状态实现：每个请求独立处理；initialize 返回 Mcp-Session-Id 但不强制后续校验。
    """
    # 校验 Accept 头（规范要求客户端声明支持 application/json 和 text/event-stream）
    accept = request.headers.get("Accept", "")
    if accept and "application/json" not in accept and "text/event-stream" not in accept and "*/*" not in accept:
        return Response(
            json.dumps(_jsonrpc_error(ERR_INVALID_REQUEST, "Accept 头需包含 application/json 或 text/event-stream", None)),
            status=406,
            mimetype="application/json",
        )

    # 解析请求体
    try:
        raw = request.get_data(as_text=True)
        message = json.loads(raw) if raw else None
    except (json.JSONDecodeError, UnicodeDecodeError):
        return Response(
            json.dumps(_jsonrpc_error(ERR_PARSE_ERROR, "JSON 解析失败", None)),
            status=400,
            mimetype="application/json",
        )

    if message is None:
        return Response(
            json.dumps(_jsonrpc_error(ERR_INVALID_REQUEST, "请求体为空", None)),
            status=400,
            mimetype="application/json",
        )

    # 简单的 session id：若客户端带了就用，否则新生成（initialize 时）
    incoming_sid = request.headers.get("Mcp-Session-Id") or request.headers.get("mcp-session-id")
    session_id = incoming_sid or _new_session_id()

    try:
        response_msg = _handle_jsonrpc(message, session_id)
    except Exception as e:  # noqa: BLE001
        log.exception("MCP 处理异常")
        response_msg = _jsonrpc_error(ERR_INTERNAL, f"内部错误: {e}", message.get("id") if isinstance(message, dict) else None)

    # 通知（无 id）→ HTTP 202，无响应体
    if response_msg is None:
        resp = Response(status=202)
    else:
        resp = Response(
            json.dumps(response_msg, ensure_ascii=False),
            status=200,
            mimetype="application/json",
        )
        # initialize 响应附带 session id
        if isinstance(message, dict) and message.get("method") == "initialize":
            resp.headers["Mcp-Session-Id"] = session_id
    # 允许跨域（本地工具直连场景）+ 禁止缓存
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Cache-Control"] = "no-store"
    return resp


def handle_mcp_get() -> Response:
    """GET 请求：本服务不支持 SSE 流（无服务端推送需求），按规范返回 405。

    这完全合规：规范允许 Streamable HTTP 服务端对 GET 返回 405 Method Not Allowed。
    """
    return Response(status=405, headers={"Allow": "POST"})


def handle_mcp_delete() -> Response:
    """DELETE 请求：终止会话。无状态实现下直接返回 200。"""
    return Response(status=200)


def handle_mcp_options() -> Response:
    """CORS 预检。"""
    resp = Response(status=204)
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Methods"] = "POST, GET, DELETE, OPTIONS"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type, Accept, Mcp-Session-Id, MCP-Protocol-Version"
    resp.headers["Access-Control-Max-Age"] = "86400"
    return resp


def register(app) -> None:
    """把 MCP 端点注册到 Flask app。端点固定为 /mcp。"""
    app.add_url_rule("/mcp", "mcp_post", handle_mcp_request, methods=["POST"])
    app.add_url_rule("/mcp", "mcp_get", handle_mcp_get, methods=["GET"])
    app.add_url_rule("/mcp", "mcp_delete", handle_mcp_delete, methods=["DELETE"])
    app.add_url_rule("/mcp", "mcp_options", handle_mcp_options, methods=["OPTIONS"])
