"""arXiv Daily Digest Web — 论文每日精读网页 + 定时调度。

特性:
  • 首页展示当天论文（嵌入原始 HTML 报告）
  • 历史页按日期列出所有报告，点击可查看
  • 搜索页支持按标题 / 作者 / 分类码 (如 cs.SD) 检索全部历史论文
  • 后台调度线程：工作日（周一至周五）北京时间 10:00 自动调用 run.sh 抓取最新论文，
    随后刷新缓存并由 send.py 推送飞书

环境变量:
  WEB_HOST      默认 127.0.0.1
  WEB_PORT      默认 8080
  RUN_SCRIPT    每日执行的脚本，默认 ./run.sh
  DAILY_HOUR    每日运行小时（24h，北京时间），默认 10
  DAILY_MINUTE  每日运行分钟，默认 0
  ARXIV_CHECK_CATEGORIES  开跑前检查是否已更新到今天的分类（空格分隔）
  WEB_PUBLIC_URL  对外暴露的访问地址，注入到 run.sh 进程供飞书消息使用
  FEISHU_WEBHOOK_URL  飞书 webhook，注入到 run.sh 进程

启动:
    python web.py
"""

from __future__ import annotations

import io
import json
import logging
import os
import re
import subprocess
import sys
import threading
import time
import zipfile
from email.utils import format_datetime
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional
from xml.sax.saxutils import escape as xml_escape

try:
    from zoneinfo import ZoneInfo
except ImportError:
    from backports.zoneinfo import ZoneInfo  # type: ignore

import requests
from bs4 import BeautifulSoup
from flask import Flask, Response, abort, redirect, render_template, request, send_from_directory, url_for

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data" / "papers"
REPORTS_DIR = ROOT / "reports"
TEMPLATES_DIR = ROOT / "templates"
STATIC_DIR = ROOT / "static"

BJ_TZ = ZoneInfo("Asia/Shanghai")
ARXIV_NEW_URL = "https://arxiv.org/list/{category}/new"
FRESHNESS_MAX_ATTEMPTS = 20
FRESHNESS_RETRY_SECONDS = 30 * 60

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("web")

app = Flask(
    __name__,
    template_folder=str(TEMPLATES_DIR),
    static_folder=str(STATIC_DIR),
)


# ─────────────────────────────────────────────
# 数据加载（带 mtime 缓存）
# ─────────────────────────────────────────────

_index_cache: dict[str, dict] = {}
_index_mtime: dict[str, float] = {}
_index_lock = threading.Lock()

REPORT_FILE_RE = re.compile(r"^report_(\d{4}-\d{2}-\d{2}).*\.html$")


def discover_report_html(date_str: str) -> Optional[Path]:
    """寻找指定日期对应的 HTML 报告。

    先查 reports/report_<date>.html，再查根目录 report_<date>.html。
    """
    cand = REPORTS_DIR / f"report_{date_str}.html"
    if cand.exists():
        return cand
    cand = ROOT / f"report_{date_str}.html"
    if cand.exists():
        return cand
    # 兜底：含日期且最新的文件
    matches = sorted(
        [p for p in REPORTS_DIR.glob(f"report_{date_str}*.html")]
        + [p for p in ROOT.glob(f"report_{date_str}*.html")],
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    return matches[0] if matches else None


def list_all_dates() -> list[str]:
    """列出所有可用的日期（按倒序）。"""
    dates: set[str] = set()
    for p in DATA_DIR.glob("*.json"):
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", p.stem):
            dates.add(p.stem)
    for p in list(REPORTS_DIR.glob("report_*.html")) + list(ROOT.glob("report_*.html")):
        m = REPORT_FILE_RE.match(p.name)
        if m:
            dates.add(m.group(1))
    return sorted(dates, reverse=True)


def load_index(date_str: str) -> Optional[dict]:
    """加载一天的 JSON 元数据（带 mtime 失效缓存）。"""
    path = DATA_DIR / f"{date_str}.json"
    if not path.exists():
        return None
    try:
        mtime = path.stat().st_mtime
    except FileNotFoundError:
        return None
    with _index_lock:
        if _index_mtime.get(date_str) == mtime and date_str in _index_cache:
            return _index_cache[date_str]
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception as e:
            log.warning(f"加载 {path} 失败: {e}")
            return None
        _index_cache[date_str] = data
        _index_mtime[date_str] = mtime
        return data


def load_all_indices() -> list[dict]:
    """加载全部日期的 JSON 索引。"""
    return [d for d in (load_index(ds) for ds in list_all_dates()) if d]


def list_all_categories() -> list[str]:
    """从历史索引中收集全部分类码。"""
    categories: set[str] = set()
    for index in load_all_indices():
        for cat in index.get("categories", []):
            if cat:
                categories.add(cat)
    return sorted(categories)


def collect_recent_papers(*, category: str = "", limit: int = 80) -> list[dict]:
    """按日期倒序收集近期论文，可选分类过滤。"""
    cat_q = category.strip().lower()
    papers: list[dict] = []
    for index in load_all_indices():
        for p in index.get("papers", []):
            if cat_q:
                subjects = (p.get("subjects") or "").lower()
                src_cats = " ".join(p.get("source_categories", [])).lower()
                if cat_q not in subjects and cat_q not in src_cats:
                    continue
            papers.append({
                **p,
                "date": index.get("date", ""),
            })
            if len(papers) >= limit:
                return papers
    return papers


def _guess_pub_dt(paper: dict) -> datetime:
    """将论文日期转换为 RFC2822 需要的时区时间。"""
    date_str = paper.get("date", "")
    try:
        dt = datetime.strptime(date_str, "%Y-%m-%d")
    except ValueError:
        return datetime.now(BJ_TZ)
    return dt.replace(tzinfo=BJ_TZ, hour=9, minute=0, second=0, microsecond=0)


def _clean_ris_text(value: str) -> str:
    """清洗 RIS 字段文本，避免换行破坏格式。"""
    return re.sub(r"\s+", " ", (value or "")).strip()


def build_ris_text(*, papers: list[dict], title_hint: str = "arXiv Daily Digest") -> str:
    """将论文列表导出为 RIS，便于 Zotero 一键导入。"""
    lines: list[str] = []
    for p in papers:
        paper_id = _clean_ris_text(p.get("paper_id") or "")
        title = _clean_ris_text(p.get("title") or f"arXiv:{paper_id}")
        abstract = _clean_ris_text(p.get("abstract") or "")
        date_str = _clean_ris_text(p.get("date") or "")
        abs_url = _clean_ris_text(p.get("abs_url") or f"https://arxiv.org/abs/{paper_id}")
        pdf_url = _clean_ris_text(p.get("pdf_url") or "")
        primary_cat = _clean_ris_text(p.get("primary_category") or "")
        source_categories = p.get("source_categories", [])
        if not isinstance(source_categories, list):
            source_categories = []

        year = ""
        date_ris = ""
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", date_str):
            year = date_str[:4]
            date_ris = date_str.replace("-", "/")

        lines.append("TY  - JOUR")
        lines.append(f"T1  - {title}")
        for author in p.get("authors", []):
            author_name = _clean_ris_text(str(author))
            if author_name:
                lines.append(f"AU  - {author_name}")
        if year:
            lines.append(f"PY  - {year}")
        if date_ris:
            lines.append(f"DA  - {date_ris}")
        if abstract:
            lines.append(f"AB  - {abstract}")
        if abs_url:
            lines.append(f"UR  - {abs_url}")
        if pdf_url:
            lines.append(f"L1  - {pdf_url}")
        if primary_cat:
            lines.append(f"JO  - arXiv {primary_cat}")
        for cat in source_categories:
            c = _clean_ris_text(str(cat))
            if c:
                lines.append(f"KW  - {c}")
        if paper_id:
            lines.append(f"ID  - arXiv:{paper_id}")
            lines.append(f"DO  - 10.48550/arXiv.{paper_id}")
        lines.append(f"N1  - Imported from {title_hint}")
        lines.append("ER  - ")
        lines.append("")
    return "\n".join(lines)


def build_report_page_ris(*, date: str, report_url: str, source_page_url: str) -> str:
    """将某一天报告网页导出为单条 RIS。"""
    date_ris = date.replace("-", "/")
    year = date[:4] if len(date) >= 4 else ""
    title = _clean_ris_text(f"arXiv 每日精读报告 {date}")
    report_url = _clean_ris_text(report_url)
    source_page_url = _clean_ris_text(source_page_url)
    lines = [
        "TY  - ELEC",
        f"TI  - {title}",
        f"T1  - {title}",
        "PB  - arXiv Daily Digest",
    ]
    if year:
        lines.append(f"PY  - {year}")
    lines.append(f"DA  - {date_ris}")
    lines.append(f"UR  - {report_url}")
    lines.append("KW  - arXiv")
    lines.append("KW  - Daily Digest")
    lines.append(f"N1  - Source page: {source_page_url}")
    lines.append("ER  - ")
    lines.append("")
    return "\n".join(lines)


def build_rss_xml(*, base_url: str, papers: list[dict], category: str = "") -> str:
    """构造 RSS 2.0 XML。"""
    cat_hint = category.strip()
    title = "arXiv 每日精读 RSS"
    desc = "聚合最近论文精读，支持 RSS 阅读器订阅。"
    if cat_hint:
        title = f"arXiv 每日精读 RSS · {cat_hint}"
        desc = f"分类 {cat_hint} 的近期论文精读。"
    pub_date = format_datetime(datetime.now(BJ_TZ))
    items: list[str] = []
    for p in papers:
        paper_id = p.get("paper_id") or "unknown"
        item_title = xml_escape(p.get("title") or f"arXiv:{paper_id}")
        item_link = p.get("abs_url") or f"https://arxiv.org/abs/{paper_id}"
        report_link = f"{base_url.rstrip('/')}{url_for('view_date', date=p.get('date', ''))}"
        authors = ", ".join(p.get("authors", []))
        desc_body = (
            f"日期: {p.get('date', '')}\n"
            f"作者: {authors or 'N/A'}\n"
            f"分类: {', '.join(p.get('source_categories', [])) or (p.get('subjects') or 'N/A')}\n"
            f"摘要: {(p.get('abstract') or '').strip()}"
        )
        guid = f"{paper_id}-{p.get('date', '')}"
        items.append(
            "    <item>\n"
            f"      <title>{item_title}</title>\n"
            f"      <link>{xml_escape(item_link)}</link>\n"
            f"      <guid isPermaLink=\"false\">{xml_escape(guid)}</guid>\n"
            f"      <pubDate>{format_datetime(_guess_pub_dt(p))}</pubDate>\n"
            f"      <description>{xml_escape(desc_body)}</description>\n"
            f"      <source url=\"{xml_escape(report_link)}\">arXiv 每日精读</source>\n"
            "    </item>"
        )
    return (
        "<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n"
        "<rss version=\"2.0\">\n"
        "  <channel>\n"
        f"    <title>{xml_escape(title)}</title>\n"
        f"    <link>{xml_escape(base_url.rstrip('/') + url_for('rss_page'))}</link>\n"
        f"    <description>{xml_escape(desc)}</description>\n"
        f"    <lastBuildDate>{pub_date}</lastBuildDate>\n"
        "    <language>zh-cn</language>\n"
        f"{chr(10).join(items)}\n"
        "  </channel>\n"
        "</rss>\n"
    )


# ─────────────────────────────────────────────
# 访问量统计（内存计数 + 文件持久化）
# 仅在用户实际看到的页面端点上累加，避免重复计算 redirect / iframe / 静态资源 / API。
# ─────────────────────────────────────────────

VISITS_FILE = ROOT / "data" / "visits.json"
_visits_data: dict = {}
_visits_loaded: bool = False
_visits_lock = threading.Lock()


def _load_visits_file() -> dict:
    if not VISITS_FILE.exists():
        return {}
    try:
        data = json.loads(VISITS_FILE.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return data
    except Exception as e:
        log.warning(f"加载 {VISITS_FILE} 失败: {e}")
    return {}


def _save_visits_file(data: dict) -> None:
    try:
        VISITS_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = VISITS_FILE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(VISITS_FILE)
    except Exception as e:
        log.warning(f"保存 {VISITS_FILE} 失败: {e}")


def _ensure_visits_loaded() -> None:
    global _visits_loaded, _visits_data
    if not _visits_loaded:
        _visits_data = _load_visits_file()
        _visits_loaded = True


def _normalize_day(day: dict) -> dict:
    """统一每日数据结构为 {"total": int, "hourly": [24 ints]}。"""
    hourly = day.get("hourly")
    if isinstance(hourly, dict):
        hourly = [int(hourly.get(f"{h:02d}", 0) or 0) for h in range(24)]
    if not isinstance(hourly, list) or len(hourly) != 24:
        hourly = [0] * 24
    day["hourly"] = [int(x or 0) for x in hourly]
    day["total"] = int(day.get("total") or 0)
    return day


def record_visit() -> None:
    """累加一次访问到当前北京时间所在小时。"""
    now = datetime.now(BJ_TZ)
    date_str = now.strftime("%Y-%m-%d")
    hour_idx = now.hour
    with _visits_lock:
        _ensure_visits_loaded()
        day = _visits_data.setdefault(date_str, {"total": 0, "hourly": [0] * 24})
        _normalize_day(day)
        day["total"] += 1
        day["hourly"][hour_idx] += 1
        _save_visits_file(_visits_data)


def get_today_visit_total() -> int:
    today = datetime.now(BJ_TZ).strftime("%Y-%m-%d")
    with _visits_lock:
        _ensure_visits_loaded()
        return int((_visits_data.get(today) or {}).get("total", 0))


def get_grand_visit_total() -> int:
    with _visits_lock:
        _ensure_visits_loaded()
        return int(sum(int((d or {}).get("total", 0)) for d in _visits_data.values()))


def get_visits_snapshot() -> dict:
    with _visits_lock:
        _ensure_visits_loaded()
        out: dict = {}
        for k, v in _visits_data.items():
            out[k] = _normalize_day(dict(v or {}))
        return out


COUNTED_ENDPOINTS = {
    "index", "view_date", "history", "search",
    "rss_page", "zotero_plugin_page",
}


@app.after_request
def _count_visit(response):
    if request.endpoint in COUNTED_ENDPOINTS and 200 <= response.status_code < 300:
        try:
            record_visit()
        except Exception as e:
            log.warning(f"记录访问失败: {e}")
    return response


# ─────────────────────────────────────────────
# 搜索
# ─────────────────────────────────────────────

def search_papers(
    query: str,
    *,
    date: str = "",
    category: str = "",
    limit: int = 500,
) -> list[dict]:
    """跨所有日期搜索论文。

    匹配字段:
        - 标题 (含中英文)
        - 作者
        - subjects (含 cs.SD 这样的分类码)
        - source_categories
        - paper_id
    """
    q = query.strip().lower()
    cat_q = category.strip().lower()
    results: list[dict] = []

    for index in load_all_indices():
        if date and index["date"] != date:
            continue
        for p in index["papers"]:
            title = (p.get("title") or "").lower()
            authors = " | ".join(p.get("authors", [])).lower()
            subjects = (p.get("subjects") or "").lower()
            src_cats = " ".join(p.get("source_categories", [])).lower()
            paper_id = (p.get("paper_id") or "").lower()
            abstract = (p.get("abstract") or "").lower()

            if cat_q:
                if cat_q not in subjects and cat_q not in src_cats:
                    continue
            if q:
                haystacks = [title, authors, subjects, src_cats, paper_id, abstract]
                if not any(q in h for h in haystacks):
                    continue

            results.append({
                **p,
                "date": index["date"],
            })
            if len(results) >= limit:
                return results
    return results


# ─────────────────────────────────────────────
# 路由
# ─────────────────────────────────────────────

@app.context_processor
def inject_globals():
    return {
        "now_bj": datetime.now(BJ_TZ),
        "all_dates": list_all_dates(),
        "today_visits": get_today_visit_total(),
        "total_visits": get_grand_visit_total(),
    }


@app.route("/")
def index():
    dates = list_all_dates()
    if not dates:
        return render_template("empty.html")
    today = datetime.now(BJ_TZ).strftime("%Y-%m-%d")
    target = today if today in dates else dates[0]
    return redirect(url_for("view_date", date=target))


@app.route("/today")
def today():
    return redirect(url_for("index"))


@app.route("/date/<date>")
def view_date(date: str):
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
        abort(404)
    dates = list_all_dates()
    if date not in dates:
        abort(404)

    index = load_index(date)
    html_exists = discover_report_html(date) is not None
    today_str = datetime.now(BJ_TZ).strftime("%Y-%m-%d")

    prev_date = None
    next_date = None
    if date in dates:
        i = dates.index(date)
        if i + 1 < len(dates):
            prev_date = dates[i + 1]
        if i > 0:
            next_date = dates[i - 1]

    return render_template(
        "date.html",
        date=date,
        is_today=(date == today_str),
        index=index,
        html_exists=html_exists,
        prev_date=prev_date,
        next_date=next_date,
    )


@app.route("/raw/<date>.html")
def raw_html(date: str):
    """直接 serve 原始报告文件，被 iframe 嵌入。"""
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
        abort(404)
    path = discover_report_html(date)
    if not path:
        abort(404)
    return send_from_directory(path.parent, path.name)


@app.route("/download/<date>.html")
def download_html(date: str):
    """下载指定日期原始 HTML 报告。"""
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
        abort(404)
    path = discover_report_html(date)
    if not path:
        abort(404)
    return send_from_directory(
        path.parent,
        path.name,
        as_attachment=True,
        download_name=f"arxiv-report-{date}.html",
    )


@app.route("/zotero/<date>.ris")
def zotero_ris_date(date: str):
    """按日期导出单条 RIS（报告网页），供 Zotero 导入。"""
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
        abort(404)
    if not discover_report_html(date):
        abort(404)
    report_url = request.host_url.rstrip("/") + url_for("raw_html", date=date)
    source_page_url = request.host_url.rstrip("/") + url_for("view_date", date=date)
    ris_text = build_report_page_ris(
        date=date,
        report_url=report_url,
        source_page_url=source_page_url,
    )
    headers = {
        "Content-Disposition": f'attachment; filename="arxiv-{date}.ris"',
    }
    return Response(
        ris_text,
        headers=headers,
        mimetype="application/x-research-info-systems; charset=utf-8",
    )


@app.route("/zotero/latest.ris")
def zotero_ris_latest():
    """导出最近一天报告网页的单条 RIS。"""
    dates = list_all_dates()
    if not dates:
        return {"ok": False, "msg": "暂无可用报告。"}, 404
    latest = dates[0]
    return zotero_ris_date(latest)


@app.route("/zotero/today.ris")
def zotero_ris_today():
    """严格导出当天（北京时间）报告网页的单条 RIS。"""
    today_str = datetime.now(BJ_TZ).strftime("%Y-%m-%d")
    if not discover_report_html(today_str):
        return {
            "ok": False,
            "msg": f"当天报告 {today_str} 尚未生成，请先运行抓取任务。",
        }, 404
    return zotero_ris_date(today_str)


PLUGIN_DIR = ROOT / "zotero_plugin" / "arxiv-daily-importer"
PLUGIN_FILES = ("manifest.json", "bootstrap.js")


def _ensure_https_url(url: str) -> str:
    """Zotero 9 要求 update_url / update_link 使用 HTTPS。"""
    url = url.strip()
    if url.startswith("http://"):
        return "https://" + url[len("http://"):]
    return url


def _plugin_public_base_url() -> str:
    """解析插件对外 HTTPS 基址（用于 manifest update_url）。"""
    explicit = os.environ.get("ZOTERO_PLUGIN_UPDATE_URL", "").strip()
    if explicit:
        if explicit.endswith("/zotero-plugin/updates.json"):
            return _ensure_https_url(explicit[: -len("/zotero-plugin/updates.json")])
        return _ensure_https_url(explicit.rstrip("/"))

    public = os.environ.get("WEB_PUBLIC_URL", "").strip()
    if public:
        return _ensure_https_url(public.rstrip("/"))

    # 反向代理终止 TLS 时，Flask 看到的可能是 http
    forwarded_proto = request.headers.get("X-Forwarded-Proto", "").split(",")[0].strip()
    forwarded_host = request.headers.get("X-Forwarded-Host", "").split(",")[0].strip()
    if forwarded_proto and forwarded_host:
        return _ensure_https_url(f"{forwarded_proto}://{forwarded_host}")

    return _ensure_https_url(request.host_url.rstrip("/"))


def _plugin_updates_url() -> str:
    return f"{_plugin_public_base_url()}/zotero-plugin/updates.json"


def _plugin_download_url() -> str:
    return f"{_plugin_public_base_url()}/zotero-plugin/download.xpi"


def _build_plugin_manifest_dict() -> dict:
    """生成写入 xpi 的 manifest（注入 HTTPS update_url）。"""
    template_path = PLUGIN_DIR / "manifest.json"
    manifest = json.loads(template_path.read_text(encoding="utf-8"))
    zotero_app = manifest.setdefault("applications", {}).setdefault("zotero", {})
    zotero_app["update_url"] = _plugin_updates_url()
    zotero_app.setdefault("strict_min_version", "6.999")
    zotero_app.setdefault("strict_max_version", "9.*")
    return manifest


def _build_plugin_xpi_bytes() -> bytes:
    """将 zotero_plugin 目录打包为 xpi（manifest 动态注入 HTTPS update_url）。"""
    manifest = _build_plugin_manifest_dict()
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(
            "manifest.json",
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        )
        bootstrap_path = PLUGIN_DIR / "bootstrap.js"
        if bootstrap_path.exists():
            zf.write(bootstrap_path, arcname="bootstrap.js")
    return buf.getvalue()


@app.route("/zotero-plugin")
def zotero_plugin_page():
    """Zotero 插件安装说明页。"""
    plugin_available = all((PLUGIN_DIR / name).exists() for name in PLUGIN_FILES)
    public_host = _plugin_public_base_url()
    return render_template(
        "zotero_plugin.html",
        plugin_available=plugin_available,
        public_host=public_host,
        plugin_update_url=_plugin_updates_url(),
        plugin_download_url=_plugin_download_url(),
    )


@app.route("/zotero-plugin/download.xpi")
def zotero_plugin_xpi():
    """动态打包并下载插件 xpi。"""
    for name in PLUGIN_FILES:
        if not (PLUGIN_DIR / name).exists():
            abort(404)
    data = _build_plugin_xpi_bytes()
    return Response(
        data,
        mimetype="application/x-xpinstall",
        headers={
            "Content-Disposition": 'attachment; filename="arxiv-daily-importer.xpi"',
            "Content-Length": str(len(data)),
        },
    )


@app.route("/zotero-plugin/updates.json")
def zotero_plugin_updates():
    """Zotero 插件更新清单。Zotero 9 安装校验要求 manifest 提供 HTTPS update_url。"""
    manifest = _build_plugin_manifest_dict()
    version = manifest.get("version", "0.1.6")
    addon_id = manifest["applications"]["zotero"]["id"]
    update_link = _plugin_download_url()
    return {
        "addons": {
            addon_id: {
                "updates": [
                    {
                        "version": version,
                        "update_link": update_link,
                    }
                ]
            }
        }
    }


@app.route("/history")
def history():
    dates = list_all_dates()
    items = []
    for d in dates:
        idx = load_index(d)
        if idx:
            items.append({
                "date": d,
                "total": idx.get("total", 0),
                "cross": idx.get("cross_count", 0),
                "categories": idx.get("categories", []),
                "llm_model": idx.get("llm_model", ""),
            })
        else:
            items.append({
                "date": d, "total": "?", "cross": "?",
                "categories": [], "llm_model": "",
            })
    return render_template("history.html", items=items)


@app.route("/search")
def search():
    q = request.args.get("q", "").strip()
    cat = request.args.get("cat", "").strip()
    date = request.args.get("date", "").strip()

    results: list[dict] = []
    truncated = False
    if q or cat or date:
        limit = 500
        results = search_papers(q, date=date, category=cat, limit=limit)
        truncated = len(results) >= limit

    return render_template(
        "search.html",
        q=q, cat=cat, date=date,
        results=results,
        truncated=truncated,
        all_dates=list_all_dates(),
    )


@app.route("/rss")
def rss_page():
    cat = request.args.get("cat", "").strip()
    try:
        limit = max(1, min(int(request.args.get("limit", "80")), 200))
    except ValueError:
        limit = 80
    categories = list_all_categories()
    papers = collect_recent_papers(category=cat, limit=limit)
    feed_url = url_for("rss_feed", _external=True, **({"cat": cat} if cat else {}), limit=limit)
    return render_template(
        "rss.html",
        cat=cat,
        limit=limit,
        categories=categories,
        papers=papers,
        feed_url=feed_url,
    )


@app.route("/rss/feed.xml")
def rss_feed():
    cat = request.args.get("cat", "").strip()
    try:
        limit = max(1, min(int(request.args.get("limit", "80")), 200))
    except ValueError:
        limit = 80
    papers = collect_recent_papers(category=cat, limit=limit)
    xml_text = build_rss_xml(base_url=request.host_url.rstrip("/"), papers=papers, category=cat)
    return Response(xml_text, mimetype="application/rss+xml; charset=utf-8")


@app.route("/api/dates")
def api_dates():
    return {"dates": list_all_dates()}


@app.route("/api/search")
def api_search():
    return {
        "query": request.args.get("q", ""),
        "results": search_papers(
            request.args.get("q", ""),
            date=request.args.get("date", ""),
            category=request.args.get("cat", ""),
        ),
    }


@app.route("/stats")
def stats():
    """访问量看板：今日 24h 分布 + 最近 30 天历史。"""
    snapshot = get_visits_snapshot()
    today_str = datetime.now(BJ_TZ).strftime("%Y-%m-%d")
    today_day = snapshot.get(today_str) or {"total": 0, "hourly": [0] * 24}

    sorted_dates = sorted(snapshot.keys(), reverse=True)
    recent: list[dict] = []
    for d in sorted_dates[:30]:
        day = snapshot[d]
        recent.append({"date": d, "total": int(day.get("total", 0))})
    recent.reverse()  # 时间从左到右

    hourly = today_day.get("hourly", [0] * 24)
    max_hour = max(hourly) if hourly else 0
    hourly_pct = [
        round(h / max_hour * 100, 1) if max_hour else 0
        for h in hourly
    ]
    peak_hour_idx = hourly.index(max_hour) if max_hour else -1

    max_day = max((r["total"] for r in recent), default=0)
    for r in recent:
        r["pct"] = round(r["total"] / max_day * 100, 1) if max_day else 0
        r["is_today"] = (r["date"] == today_str)

    total = sum(int(d.get("total", 0)) for d in snapshot.values())
    day_count = len(snapshot)
    avg_per_day = round(total / day_count, 1) if day_count else 0

    return render_template(
        "stats.html",
        today_total=int(today_day.get("total", 0)),
        today_hourly=hourly,
        hourly_pct=hourly_pct,
        peak_hour_idx=peak_hour_idx,
        peak_hour_count=max_hour,
        recent=recent,
        total=total,
        day_count=day_count,
        avg_per_day=avg_per_day,
        today_str=today_str,
    )


@app.route("/api/visits")
def api_visits():
    return {"visits": get_visits_snapshot()}


# ─────────────────────────────────────────────
# 后台定时调度
# ─────────────────────────────────────────────

class DailyScheduler(threading.Thread):
    """工作日固定时刻（北京时间）调用 run.sh 抓取论文。"""

    def __init__(
        self,
        hour: int,
        minute: int,
        run_script: Path,
        categories: list[str],
        feishu_webhook_url: str,
        max_attempts: int = FRESHNESS_MAX_ATTEMPTS,
        retry_seconds: int = FRESHNESS_RETRY_SECONDS,
    ):
        super().__init__(daemon=True, name="daily-scheduler")
        self.hour = hour
        self.minute = minute
        self.run_script = run_script
        self.categories = categories
        self.feishu_webhook_url = feishu_webhook_url.strip()
        self.max_attempts = max_attempts
        self.retry_seconds = retry_seconds
        self._stop_event = threading.Event()
        self._running_lock = threading.Lock()
        self.last_run: Optional[datetime] = None
        self.last_status: str = "未运行"

    @staticmethod
    def _is_weekday(dt: datetime) -> bool:
        return dt.weekday() < 5  # 0=周一 … 4=周五

    def _next_fire_at(self, now: datetime) -> datetime:
        fire = now.replace(hour=self.hour, minute=self.minute, second=0, microsecond=0)
        if fire <= now:
            fire += timedelta(days=1)
        while not self._is_weekday(fire):
            fire += timedelta(days=1)
        return fire

    @staticmethod
    def _parse_arxiv_listing_date(html: str) -> Optional[datetime]:
        soup = BeautifulSoup(html, "html.parser")
        tag = soup.find("h3", string=re.compile(r"Showing new listings for", flags=re.IGNORECASE))
        if not tag:
            return None
        text = tag.get_text(" ", strip=True)
        # 示例: "Showing new listings for Tuesday, 19 May 2026"
        m = re.search(
            r"showing\s+new\s+listings\s+for\s+[A-Za-z]+,\s+(\d{1,2}\s+[A-Za-z]+\s+\d{4})",
            text,
            flags=re.IGNORECASE,
        )
        if not m:
            return None
        try:
            dt = datetime.strptime(m.group(1), "%d %B %Y")
        except ValueError:
            return None
        return dt.replace(tzinfo=BJ_TZ)

    def _check_categories_are_today(self) -> tuple[bool, list[str]]:
        headers = {"User-Agent": "ArxivWatcher-Web/1.0"}
        today = datetime.now(BJ_TZ).date()
        stale_categories: list[str] = []
        for cat in self.categories:
            url = ARXIV_NEW_URL.format(category=cat)
            try:
                resp = requests.get(url, headers=headers, timeout=20)
                resp.raise_for_status()
                listing_dt = self._parse_arxiv_listing_date(resp.text)
                if not listing_dt or listing_dt.date() != today:
                    stale_categories.append(cat)
            except Exception as e:
                log.warning(f"[freshness] 检查 {cat} 失败: {e}")
                stale_categories.append(cat)
        return len(stale_categories) == 0, stale_categories

    def _send_feishu_text(self, text: str) -> None:
        if not self.feishu_webhook_url:
            return
        payload = {"msg_type": "text", "content": {"text": text}}
        try:
            resp = requests.post(self.feishu_webhook_url, json=payload, timeout=10)
            resp.raise_for_status()
            data = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
            if data.get("code", 0) != 0 or data.get("StatusCode", 0) != 0:
                log.warning(f"飞书提醒返回异常: {data}")
        except Exception as e:
            log.warning(f"飞书提醒发送失败: {e}")

    def _wait_until_fresh_or_retry_exhausted(self, reason: str) -> bool:
        if reason != "scheduled":
            return True
        for attempt in range(1, self.max_attempts + 1):
            ready, stale_categories = self._check_categories_are_today()
            if ready:
                log.info(f"全部分类已更新到今日，尝试 {attempt}/{self.max_attempts}，开始抓取")
                return True
            stale_str = ", ".join(stale_categories)
            wait_min = int(self.retry_seconds / 60)
            msg = (
                f"⏳ arXiv 今日列表尚未全部更新（第 {attempt}/{self.max_attempts} 次检查）。\n"
                f"未更新分类: {stale_str}\n"
                f"稍安勿躁，{wait_min} 分钟后再检查。"
            )
            log.info(msg.replace("\n", " | "))
            self._send_feishu_text(msg)
            if attempt >= self.max_attempts:
                break
            remaining = self.retry_seconds
            while remaining > 0 and not self._stop_event.is_set():
                step = min(remaining, 60)
                time.sleep(step)
                remaining -= step
            if self._stop_event.is_set():
                return False
        fail_msg = (
            f"⚠️ arXiv 今日列表检测已达最大重试次数（{self.max_attempts} 次），"
            "今日任务暂不执行。"
        )
        log.warning(fail_msg)
        self._send_feishu_text(fail_msg)
        self.last_status = f"等待更新超时（{self.max_attempts} 次）"
        return False

    def run_once(self, reason: str = "scheduled") -> int:
        """实际执行 run.sh。"""
        if not self._running_lock.acquire(blocking=False):
            log.warning("上一次任务还在运行，跳过本次")
            return -1
        try:
            start = datetime.now(BJ_TZ)
            self.last_run = start
            log.info(f"🚀 触发每日任务 ({reason}) at {start.isoformat()}")
            self.last_status = f"运行中 since {start.strftime('%H:%M:%S')}"

            if not self._wait_until_fresh_or_retry_exhausted(reason):
                return -3

            if not self.run_script.exists():
                log.error(f"未找到 {self.run_script}")
                self.last_status = f"失败：未找到 {self.run_script}"
                return -2

            env = os.environ.copy()
            env.setdefault("SEND_EMAIL_ROOT", str(ROOT))

            proc = subprocess.run(
                ["bash", str(self.run_script)],
                cwd=str(ROOT),
                env=env,
            )
            log.info(f"🏁 每日任务完成 exit={proc.returncode}")

            # 清缓存，让 web 立即看到新数据
            with _index_lock:
                _index_cache.clear()
                _index_mtime.clear()

            end = datetime.now(BJ_TZ)
            self.last_status = (
                f"{'成功' if proc.returncode == 0 else f'退出码 {proc.returncode}'} "
                f"@ {end.strftime('%Y-%m-%d %H:%M:%S')}"
            )
            return proc.returncode
        finally:
            self._running_lock.release()

    def run(self) -> None:
        log.info(
            f"⏰ 定时调度已启动：工作日（周一至周五）北京时间 "
            f"{self.hour:02d}:{self.minute:02d} 运行 {self.run_script}"
        )
        while not self._stop_event.is_set():
            now = datetime.now(BJ_TZ)
            fire_at = self._next_fire_at(now)
            wait = (fire_at - now).total_seconds()
            log.info(
                f"⏳ 下次触发: {fire_at.strftime('%Y-%m-%d %H:%M:%S')} "
                f"(周{'一二三四五六日'[fire_at.weekday()]}，还剩 {int(wait)}s)"
            )
            # 拆分等待，便于中途响应停止
            while wait > 0 and not self._stop_event.is_set():
                sleep_chunk = min(wait, 60.0)
                time.sleep(sleep_chunk)
                wait -= sleep_chunk
            if self._stop_event.is_set():
                break
            if not self._is_weekday(fire_at):
                log.info("周末，跳过本次定时任务")
                continue
            try:
                self.run_once(reason="scheduled")
            except Exception as e:
                log.exception(f"任务执行异常: {e}")
                self.last_status = f"异常: {e}"

    def stop(self) -> None:
        self._stop_event.set()


@app.route("/admin/run-now", methods=["POST", "GET"])
def admin_run_now():
    """手动触发一次每日任务（默认仅本机可访问）。"""
    if not _scheduler:
        return {"ok": False, "msg": "scheduler not started"}, 500
    threading.Thread(
        target=_scheduler.run_once, kwargs={"reason": "manual"}, daemon=True
    ).start()
    return {"ok": True, "msg": "已触发，可在日志中查看进度"}


@app.route("/admin/status")
def admin_status():
    if not _scheduler:
        return {"running": False}
    return {
        "running": True,
        "fire_hour": _scheduler.hour,
        "fire_minute": _scheduler.minute,
        "last_run": _scheduler.last_run.isoformat() if _scheduler.last_run else None,
        "last_status": _scheduler.last_status,
    }


# ─────────────────────────────────────────────
# 入口
# ─────────────────────────────────────────────

_scheduler: Optional[DailyScheduler] = None


def main() -> None:
    global _scheduler
    host = os.environ.get("WEB_HOST", "127.0.0.1")
    port = int(os.environ.get("WEB_PORT", "8080"))
    hour = int(os.environ.get("DAILY_HOUR", "10"))
    minute = int(os.environ.get("DAILY_MINUTE", "0"))
    run_script = Path(os.environ.get("RUN_SCRIPT", str(ROOT / "run.sh"))).resolve()
    categories = os.environ.get("ARXIV_CHECK_CATEGORIES", "eess.AS cs.SD").split()
    feishu_webhook_url = os.environ.get("FEISHU_WEBHOOK_URL", "")

    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    _scheduler = DailyScheduler(
        hour=hour,
        minute=minute,
        run_script=run_script,
        categories=categories,
        feishu_webhook_url=feishu_webhook_url,
    )
    _scheduler.start()

    log.info(f"🌐 启动 Web 服务 http://{host}:{port}")
    app.run(host=host, port=port, debug=False, use_reloader=False, threaded=True)


if __name__ == "__main__":
    main()
