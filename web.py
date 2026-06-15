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
  WEB_THREADS   Waitress 工作线程数，默认 8
  RUN_SCRIPT    每日执行的脚本，默认 ./run.sh
  DAILY_HOUR    每日运行小时（24h，北京时间），默认 10
  DAILY_MINUTE  每日运行分钟，默认 0
  ARXIV_CHECK_CATEGORIES  开跑前检查是否已更新到今天的分类（空格分隔）
  ADMIN_TOKEN   /admin/* 管理接口的 Bearer token
  WEB_PUBLIC_URL  对外暴露的访问地址，注入到 run.sh 进程供飞书消息使用
  FEISHU_WEBHOOK_URL  飞书 webhook，注入到 run.sh 进程
  ICP_BEIAN       ICP 备案号（可选）；未设置时页脚不显示备案链接
  STATIC_CACHE_SECONDS       /static/_h/* 哈希静态资源浏览器与 CDN 缓存秒数，默认 31536000（1 年）
  IMMUTABLE_CACHE_SECONDS    带 ETag 的不可变内容浏览器缓存秒数，默认 86400（1 天）
  IMMUTABLE_SMAXAGE_SECONDS  同上内容的 CDN（s-maxage）缓存秒数，默认 604800（7 天）

启动:
    python web.py
"""

from __future__ import annotations

import hashlib
import io
import gzip
import json
import logging
import os
import re

try:
    import brotli  # 可选：更优的文本压缩（br）
    _HAS_BROTLI = True
except ImportError:  # 未安装时自动退回 gzip
    brotli = None
    _HAS_BROTLI = False
import secrets
import subprocess
import sys
import threading
# tomllib 是 Python 3.11+ 标准库；3.10 退回 tomli（若装了），否则降级为 None，
# 下文读 pyproject.toml 取版本号的地方已有 try/except 兜底为 "dev"
try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover
    try:
        import tomli as tomllib  # type: ignore
    except ModuleNotFoundError:
        tomllib = None  # type: ignore
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
from flask import Flask, Response, abort, make_response, redirect, render_template, request, send_from_directory, url_for, session, jsonify

import bcrypt
import markdown as md_lib

import aslp_feed
import arxiv_version
import ccf_catalog
import content_digest
import static_assets
import auth
import storage

ROOT = Path(__file__).resolve().parent


def _read_app_version() -> str:
    try:
        from importlib.metadata import version as pkg_version

        return pkg_version("arxivwatcher")
    except Exception:
        pass
    if tomllib is None:
        return "dev"
    try:
        # tomllib.load() 同时接受二进制文件对象，兼容 stdlib(3.11+) 与 tomli
        with open(ROOT / "pyproject.toml", "rb") as f:
            data = tomllib.load(f)
        return str(data.get("project", {}).get("version", "dev"))
    except Exception:
        return "dev"


APP_VERSION = _read_app_version()

DATA_ROOT = ROOT / "data"
STORAGE_BACKEND = storage.resolve_backend()
SQLITE_PATH = Path(os.environ.get("SQLITE_PATH") or (DATA_ROOT / storage.DEFAULT_SQLITE_NAME))


def _make_store(name: str) -> "storage.Store":
    return storage.Store(name, data_dir=DATA_ROOT, backend=STORAGE_BACKEND, sqlite_path=SQLITE_PATH)


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

SECRET_KEY_FILE = ROOT / "data" / ".secret_key"
SECRET_KEY_FILE.parent.mkdir(parents=True, exist_ok=True)
if SECRET_KEY_FILE.exists():
    app.secret_key = SECRET_KEY_FILE.read_text().strip()
else:
    app.secret_key = secrets.token_hex(32)
    SECRET_KEY_FILE.write_text(app.secret_key)
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(days=30)


def current_user() -> Optional[str]:
    return session.get("username")


def require_user() -> str:
    u = current_user()
    if not u:
        abort(401)
    return u


def _admin_auth_error():
    """Return an error response when the management token is missing or invalid."""
    expected = os.environ.get("ADMIN_TOKEN", "").strip()
    if not expected:
        log.warning("ADMIN_TOKEN 未配置，拒绝访问管理接口")
        return {"ok": False, "msg": "ADMIN_TOKEN is not configured"}, 403

    auth_header = request.headers.get("Authorization", "")
    prefix = "Bearer "
    provided = ""
    if auth_header.startswith(prefix):
        provided = auth_header[len(prefix) :].strip()
    if not provided:
        provided = request.headers.get("X-Admin-Token", "").strip()

    if not secrets.compare_digest(provided, expected):
        return {"ok": False, "msg": "unauthorized"}, 401
    return None


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

VISITOR_COOKIE_NAME = "arxiv_visitor_id"
VISITOR_COOKIE_MAX_AGE = 60 * 60 * 24 * 400
_visits_store = _make_store("visits")
_visits_lock = threading.Lock()


def _normalize_day(day: dict) -> dict:
    """统一每日访问数据结构，兼容旧版本只包含 PV 的记录。"""
    hourly = day.get("hourly")
    if isinstance(hourly, dict):
        hourly = [int(hourly.get(f"{h:02d}", 0) or 0) for h in range(24)]
    if not isinstance(hourly, list) or len(hourly) != 24:
        hourly = [0] * 24
    day["hourly"] = [int(x or 0) for x in hourly]
    users = day.get("users")
    if isinstance(users, dict):
        users = list(users.keys())
    if not isinstance(users, list):
        users = []
    day["users"] = sorted({str(u) for u in users if u})

    tabs = day.get("tabs")
    if isinstance(tabs, dict):
        tabs = list(tabs.keys())
    if not isinstance(tabs, list):
        tabs = []
    day["tabs"] = sorted({str(t) for t in tabs if t})

    day["active_users"] = len(day["users"])
    if day["tabs"]:
        day["total"] = len(day["tabs"])
    else:
        day["total"] = int(day.get("total") or 0)
    return day


def _make_visitor_id() -> str:
    return secrets.token_urlsafe(24)


def _valid_visitor_id(value: str) -> bool:
    return 16 <= len(value) <= 256


def _hash_visitor_id(visitor_id: str) -> str:
    return hashlib.sha256(visitor_id.encode("utf-8")).hexdigest()


def _valid_tab_id(value: str) -> bool:
    return 8 <= len(value) <= 128


def _hash_tab_key(visitor_id: str, tab_id: str) -> str:
    return hashlib.sha256(f"{visitor_id}:{tab_id}".encode("utf-8")).hexdigest()


def record_tab_visit(visitor_id: str, tab_id: str) -> bool:
    """按 tab 计访问：同一 tab 当天只计 1 次；活跃用户按 visitor 去重。"""
    now = datetime.now(BJ_TZ)
    date_str = now.strftime("%Y-%m-%d")
    hour_idx = now.hour
    visitor_hash = _hash_visitor_id(visitor_id)
    tab_hash = _hash_tab_key(visitor_id, tab_id)
    with _visits_lock:
        _visits_data = _visits_store.all()
        day = _visits_data.setdefault(date_str, {"total": 0, "hourly": [0] * 24, "users": [], "tabs": []})
        _normalize_day(day)

        tabs = set(day.get("tabs") or [])
        if not tabs and int(day.get("total") or 0) > 0:
            # 兼容旧版只有 total 的数据：保留历史基数，后续按新 tab 继续增长。
            tabs = {f"legacy-{i}" for i in range(int(day.get("total") or 0))}
        if tab_hash in tabs:
            return False
        tabs.add(tab_hash)
        day["tabs"] = sorted(tabs)
        day["total"] = len(tabs)
        day["hourly"][hour_idx] += 1

        users = set(day.get("users") or [])
        users.add(visitor_hash)
        day["users"] = sorted(users)
        day["active_users"] = len(users)

        _visits_store.put(date_str, day)
        return True


def get_today_visit_total() -> int:
    today = datetime.now(BJ_TZ).strftime("%Y-%m-%d")
    with _visits_lock:
        return int((_visits_store.get(today) or {}).get("total", 0))


def get_today_active_user_total() -> int:
    today = datetime.now(BJ_TZ).strftime("%Y-%m-%d")
    with _visits_lock:
        day = _visits_store.get(today) or {}
        return int(_normalize_day(dict(day)).get("active_users", 0))


def get_grand_visit_total() -> int:
    with _visits_lock:
        return int(sum(int((d or {}).get("total", 0)) for d in _visits_store.all().values()))


def get_visits_snapshot(*, include_users: bool = False) -> dict:
    with _visits_lock:
        out: dict = {}
        for k, v in _visits_store.all().items():
            day = _normalize_day(dict(v or {}))
            if not include_users:
                day.pop("users", None)
                day.pop("tabs", None)
            out[k] = day
        return out


# ─────────────────────────────────────────────
# 用户认证（bcrypt + Flask session）
# ─────────────────────────────────────────────

_users_store = _make_store("users")
_users_lock = threading.Lock()


def _get_users() -> dict:
    return _users_store.all()


def _ensure_ldap_user(username: str) -> None:
    """LDAP 登录成功后，确保本地有一条记录（占位、标记来源），避免与本地账号混淆。"""
    with _users_lock:
        users = _get_users()
        if username not in users:
            users[username] = {
                "source": "ldap",
                "created_at": datetime.now(BJ_TZ).isoformat(),
            }
            _users_store.put(username, users[username])


def authenticate(username: str, password: str) -> tuple[bool, str]:
    """按 AUTH_METHODS 配置的优先级依次尝试认证。返回 (是否成功, 失败原因)。"""
    last_msg = "用户名或密码错误"
    for method in auth.auth_methods():
        if method == "local":
            with _users_lock:
                user = _get_users().get(username)
            pwd_hash = user.get("password_hash") if user else None
            if pwd_hash and bcrypt.checkpw(password.encode("utf-8"), pwd_hash.encode("utf-8")):
                return True, ""
        elif method == "ldap":
            ok, msg = auth.ldap_authenticate(username, password)
            if ok:
                _ensure_ldap_user(username)
                return True, ""
            if msg:
                last_msg = msg
    return False, last_msg


# ─────────────────────────────────────────────
# 互动数据（赞/踩、评论、标记、收藏）
# ─────────────────────────────────────────────

DEFAULT_FAVORITE_FOLDER = "默认收藏"

_interactions_store = _make_store("interactions")
_interactions_lock = threading.Lock()

_comments_store = _make_store("comments")
_comments_lock = threading.Lock()

_highlights_store = _make_store("highlights")
_highlights_lock = threading.Lock()

_favorites_store = _make_store("favorites")
_favorites_lock = threading.Lock()

_arxiv_versions_store = _make_store("arxiv_versions")
_arxiv_version_cache = arxiv_version.ArxivVersionCache(_arxiv_versions_store)


def _get_interactions() -> dict:
    return _interactions_store.all()


def _get_comments() -> dict:
    return _comments_store.all()


def _get_highlights() -> dict:
    return _highlights_store.all()


def _get_favorites() -> dict:
    return _favorites_store.all()


def _get_user_favorites(data: dict, username: str) -> dict:
    """获取（并按需初始化）某用户的收藏结构。"""
    entry = data.setdefault(username, {})
    folders = entry.setdefault("folders", [])
    if DEFAULT_FAVORITE_FOLDER not in folders:
        folders.insert(0, DEFAULT_FAVORITE_FOLDER)
    entry.setdefault("items", {})
    return entry


def _interaction_key(date: str, paper_id: str) -> str:
    return f"{date}/{paper_id}"


def _get_voter_identity() -> str:
    """Get voter identity: username if logged in, otherwise visitor cookie hash."""
    username = current_user()
    if username:
        return f"user:{username}"
    visitor_id = (request.cookies.get(VISITOR_COOKIE_NAME) or "").strip()
    if not _valid_visitor_id(visitor_id):
        return ""
    return f"anon:{_hash_visitor_id(visitor_id)}"


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

STATIC_CACHE_SECONDS = int(os.environ.get("STATIC_CACHE_SECONDS", str(365 * 24 * 3600)))
static_assets.init(STATIC_DIR)
static_assets.refresh()
IMMUTABLE_CACHE_SECONDS = int(os.environ.get("IMMUTABLE_CACHE_SECONDS", str(24 * 3600)))
IMMUTABLE_SMAXAGE_SECONDS = int(os.environ.get("IMMUTABLE_SMAXAGE_SECONDS", str(7 * 24 * 3600)))


def _immutable_cache_control(*, stale_while_revalidate: int = 86400) -> str:
    """不可变内容（按文件 mtime 做 ETag）：浏览器 + CDN 均可长期缓存。"""
    return (
        f"public, max-age={IMMUTABLE_CACHE_SECONDS}, "
        f"s-maxage={IMMUTABLE_SMAXAGE_SECONDS}, "
        f"stale-while-revalidate={stale_while_revalidate}"
    )


def _apply_304_cache(resp: Response) -> Response:
    resp.headers["Cache-Control"] = _immutable_cache_control()
    return resp


def _apply_no_cdn_cache(resp: Response) -> Response:
    """易变内容：浏览器可校验，CDN 边缘不长期缓存。"""
    resp.headers["Cache-Control"] = "private, no-cache, must-revalidate"
    resp.headers["CDN-Cache-Control"] = "no-store"
    resp.headers["Surrogate-Control"] = "no-store"
    return resp


def _apply_hashed_content_cache(resp: Response) -> Response:
    """内容寻址 API / 静态资源：哈希在 URL 中，可长期 CDN + 浏览器缓存。"""
    resp.headers["Cache-Control"] = (
        f"public, max-age={STATIC_CACHE_SECONDS}, "
        f"s-maxage={STATIC_CACHE_SECONDS}, immutable"
    )
    return resp


@app.context_processor
def inject_globals():
    icp_beian = os.environ.get("ICP_BEIAN", "").strip()
    return {
        "now_bj": datetime.now(BJ_TZ),
        "all_dates": list_all_dates(),
        "today_visits": get_today_visit_total(),
        "today_active_users": get_today_active_user_total(),
        "total_visits": get_grand_visit_total(),
        "current_user": current_user(),
        "static_asset": lambda name: url_for("static", filename=static_assets.get_rel(name)),
        "allow_register": auth.registration_enabled(),
        "ldap_enabled": auth.ldap_enabled(),
        "icp_beian": icp_beian,
        "app_version": APP_VERSION,
    }


# 可被压缩的响应类型（JSON / 文本 / JS / CSS / XML / SVG）
_COMPRESSIBLE_PREFIXES = ("application/json", "text/", "application/xml", "image/svg")

# 不可变响应（带强 ETag 的 papers/analysis 等）压缩结果缓存：key=(etag, encoding)
_compress_cache: dict = {}
_compress_cache_lock = threading.Lock()
_COMPRESS_CACHE_MAX = 512


def _pick_encoding(accept: str) -> Optional[str]:
    """按客户端 Accept-Encoding 协商压缩算法：优先 br，回退 gzip。"""
    accept = (accept or "").lower()
    if _HAS_BROTLI and "br" in accept:
        return "br"
    if "gzip" in accept:
        return "gzip"
    return None


def _compress_bytes(data: bytes, encoding: str, best: bool) -> bytes:
    """best=True 用最高压缩比（用于已缓存的不可变内容），否则用较快级别。"""
    if encoding == "br":
        return brotli.compress(data, quality=(11 if best else 5))
    return gzip.compress(data, compresslevel=(9 if best else 6))


@app.after_request
def _compress_response(response: Response) -> Response:
    """对较大的文本类响应做 br/gzip 压缩。
    不可变内容（带强 ETag + public 缓存）只压一次并按 ETag 缓存压缩字节、且用最高压缩比。
    """
    try:
        encoding = _pick_encoding(request.headers.get("Accept-Encoding", ""))
        if not encoding:
            return response
        if response.direct_passthrough or response.status_code >= 300:
            return response
        if response.headers.get("Content-Encoding"):
            return response
        ct = (response.content_type or "")
        if not (ct.startswith(_COMPRESSIBLE_PREFIXES) or "javascript" in ct):
            return response
        data = response.get_data()
        if len(data) < 1024:  # 太小压缩反而不划算
            return response

        etag = response.get_etag()[0]
        # 带强 ETag 的内容（papers/analysis）内容唯一，压缩结果按 ETag 缓存复用
        cacheable = bool(etag)

        compressed = None
        if cacheable:
            key = (etag, encoding)
            with _compress_cache_lock:
                compressed = _compress_cache.get(key)
            if compressed is None:
                compressed = _compress_bytes(data, encoding, best=True)
                with _compress_cache_lock:
                    if len(_compress_cache) >= _COMPRESS_CACHE_MAX:
                        _compress_cache.clear()
                    _compress_cache[key] = compressed
        else:
            compressed = _compress_bytes(data, encoding, best=False)

        response.set_data(compressed)
        response.headers["Content-Encoding"] = encoding
        response.headers["Content-Length"] = str(len(compressed))
        if "accept-encoding" not in (response.headers.get("Vary", "") or "").lower():
            response.headers.add("Vary", "Accept-Encoding")
    except Exception as e:  # 压缩失败时退回原始响应
        log.debug(f"压缩跳过: {e}")
    return response


@app.after_request
def _set_cache_headers(response: Response) -> Response:
    """按路径类型设置 CDN / 浏览器缓存策略（未显式设置 Cache-Control 时生效）。"""
    if response.status_code >= 400:
        return response

    path = request.path

    # 内容哈希 API / 静态资源：URL 含 digest，可长期 CDN + 浏览器缓存
    if content_digest.is_hashed_api_path(path) or static_assets.is_hashed_static_path(path):
        return _apply_hashed_content_cache(response)

    # 未哈希的 /static/*（直连源文件）：短缓存，避免与哈希副本不一致
    if path.startswith("/static/"):
        response.headers["Cache-Control"] = "public, max-age=3600"
        return response

    if response.headers.get("Cache-Control"):
        return response

    ct = response.content_type or ""

    # HTML 含登录态，禁止共享 CDN 缓存
    if ct.startswith("text/html"):
        response.headers["Cache-Control"] = "private, no-cache"
        response.headers.add("Vary", "Cookie")
        return response

    # 用户相关 / 写操作 API：不缓存
    if path.startswith(("/auth/", "/admin/")) or request.method != "GET":
        response.headers["Cache-Control"] = "private, no-store"
        return response

    dynamic_api_prefixes = (
        "/api/interactions/",
        "/api/papers-digest/",
        "/api/comments",
        "/api/comment",
        "/api/favorites",
        "/api/favorite",
        "/api/highlights",
        "/api/highlights-community",
        "/api/highlight",
        "/api/tab-visit",
        "/api/visits",
        "/api/search",
    )
    if path.startswith(dynamic_api_prefixes):
        response.headers["Cache-Control"] = "private, no-cache"
        response.headers.add("Vary", "Cookie")
        return response

    # 实验室动态：每小时刷新，禁止 CDN 共享缓存（避免边缘节点长期返回旧条目）
    if path == "/api/lab-feed":
        return _apply_no_cdn_cache(response)

    # RSS：中等缓存
    if path == "/rss/feed.xml":
        response.headers["Cache-Control"] = "public, max-age=600, s-maxage=3600"
        return response

    return response


# ─────────────────────────────────────────────
# 认证路由
# ─────────────────────────────────────────────

_USERNAME_RE = re.compile(r"^[A-Za-z0-9_]{3,32}$")


@app.route("/auth/register", methods=["POST"])
def auth_register():
    if not auth.registration_enabled():
        return jsonify({"ok": False, "msg": "本站已关闭注册"}), 403
    payload = request.get_json(silent=True) or {}
    username = str(payload.get("username") or "").strip()
    password = str(payload.get("password") or "")
    if not _USERNAME_RE.fullmatch(username):
        return jsonify({"ok": False, "msg": "用户名需 3-32 位字母、数字或下划线"}), 400
    if len(password) < 6:
        return jsonify({"ok": False, "msg": "密码至少 6 位"}), 400
    with _users_lock:
        users = _get_users()
        if username in users:
            return jsonify({"ok": False, "msg": "用户名已存在"}), 409
        users[username] = {
            "password_hash": bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt(rounds=12)).decode("utf-8"),
            "created_at": datetime.now(BJ_TZ).isoformat(),
        }
        _users_store.put(username, users[username])
    session.permanent = True
    session["username"] = username
    return jsonify({"ok": True, "username": username})


@app.route("/auth/login", methods=["POST"])
def auth_login():
    payload = request.get_json(silent=True) or {}
    username = str(payload.get("username") or "").strip()
    password = str(payload.get("password") or "")
    if not auth.valid_login_name(username) or not password:
        return jsonify({"ok": False, "msg": "用户名或密码错误"}), 401
    ok, msg = authenticate(username, password)
    if not ok:
        return jsonify({"ok": False, "msg": msg or "用户名或密码错误"}), 401
    session.permanent = True
    session["username"] = username
    return jsonify({"ok": True, "username": username})


@app.route("/auth/logout", methods=["POST"])
def auth_logout():
    session.pop("username", None)
    return jsonify({"ok": True})


@app.route("/auth/me")
def auth_me():
    username = current_user()
    if username:
        return jsonify({"ok": True, "username": username})
    return jsonify({"ok": True, "username": None})


# ─────────────────────────────────────────────
# 论文数据 API（动态渲染）
# ─────────────────────────────────────────────

def _find_paper(index: dict, paper_id: str) -> Optional[dict]:
    for p in index.get("papers", []):
        if str(p.get("paper_id")) == str(paper_id):
            return p
    return None


@app.route("/api/ccf-catalog")
def api_ccf_catalog_meta():
    """返回 CCF 目录内容哈希与可长期缓存的 URL。"""
    ccf_catalog.ensure_fresh()
    digest = ccf_catalog.get_digest()
    if not digest:
        return jsonify({"ok": False, "msg": "CCF 目录暂不可用"}), 503
    return _apply_no_cdn_cache(jsonify({
        "ok": True,
        "digest": digest,
        "url": url_for("api_ccf_catalog_hashed", digest=digest),
        "source": ccf_catalog.CCF_PAGE_URL,
    }))


@app.route("/api/h/<digest>/ccf-catalog")
def api_ccf_catalog_hashed(digest: str):
    """内容寻址：CCF 推荐会议/期刊目录（供前端 Comments 匹配）。"""
    ccf_catalog.ensure_fresh()
    expected = ccf_catalog.get_digest()
    if not expected or digest != expected:
        abort(404)
    entries = [
        {
            "s": e["s"],
            "f": e["f"],
            "r": e["r"],
            "t": e["t"],
            "type_label": e["type_label"],
            "area_label": e["area_label"],
        }
        for e in ccf_catalog.get_entries()
    ]
    return _apply_hashed_content_cache(jsonify({
        "ok": True,
        "digest": digest,
        "entries": entries,
    }))


@app.route("/api/papers-digest/<date>")
def api_papers_digest(date: str):
    """返回某日论文列表的内容哈希（短缓存，供收藏页等动态拼 hashed URL）。"""
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
        abort(404)
    index = load_index(date)
    if not index:
        abort(404)
    digest = content_digest.papers_list_digest(index)
    resp = jsonify({
        "ok": True,
        "digest": digest,
        "url": url_for("api_papers_hashed", digest=digest, date=date),
    })
    return _apply_no_cdn_cache(resp)


@app.route("/api/h/<digest>/paper/<date>/<paper_id>")
def api_single_paper_hashed(digest: str, date: str, paper_id: str):
    """内容寻址：单篇论文元数据（分享页用，可长期 CDN 缓存）。"""
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
        abort(404)
    index = load_index(date)
    if not index:
        abort(404)
    target = _find_paper(index, paper_id)
    if target is None:
        return jsonify({"ok": False, "msg": "paper not found"}), 404
    expected = content_digest.single_paper_digest(target)
    if digest != expected:
        abort(404)
    return _apply_hashed_content_cache(
        jsonify(content_digest.build_single_paper_payload(target))
    )


@app.route("/api/h/<digest>/papers/<date>")
def api_papers_hashed(digest: str, date: str):
    """内容寻址：某日论文列表（不含精读正文）。"""
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
        abort(404)
    index = load_index(date)
    if not index:
        abort(404)
    expected = content_digest.papers_list_digest(index)
    if digest != expected:
        abort(404)
    return _apply_hashed_content_cache(jsonify(content_digest.build_papers_list_payload(index)))


@app.route("/api/h/<digest>/analysis/<date>/<paper_id>")
def api_analysis_hashed(digest: str, date: str, paper_id: str):
    """内容寻址：单篇论文精读 HTML。"""
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
        abort(404)
    index = load_index(date)
    if not index:
        abort(404)
    target = _find_paper(index, paper_id)
    if target is None:
        return jsonify({"ok": False, "msg": "paper not found"}), 404
    expected = content_digest.paper_analysis_digest(target)
    if not expected or digest != expected:
        abort(404)
    html = content_digest.build_analysis_html(target)
    return _apply_hashed_content_cache(jsonify({"ok": True, "analysis_html": html}))


@app.route("/api/papers/<date>")
def api_papers(date: str):
    """兼容旧客户端；请改用 /api/h/<digest>/papers/<date>。"""
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
        abort(404)
    index = load_index(date)
    if not index:
        abort(404)
    return _apply_no_cdn_cache(jsonify(content_digest.build_papers_list_payload(index)))


@app.route("/api/analysis/<date>/<paper_id>")
def api_analysis(date: str, paper_id: str):
    """兼容旧客户端；请改用 /api/h/<digest>/analysis/<date>/<paper_id>。"""
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
        abort(404)
    index = load_index(date)
    if not index:
        abort(404)
    target = _find_paper(index, paper_id)
    if target is None:
        return jsonify({"ok": False, "msg": "paper not found"}), 404
    html = content_digest.build_analysis_html(target)
    return _apply_no_cdn_cache(jsonify({"ok": True, "analysis_html": html}))


# ─────────────────────────────────────────────
# 赞/踩 API
# ─────────────────────────────────────────────

@app.route("/api/like", methods=["POST"])
def api_like():
    payload = request.get_json(silent=True) or {}
    date = str(payload.get("date") or "").strip()
    paper_id = str(payload.get("paper_id") or "").strip()
    if not date or not paper_id:
        return jsonify({"ok": False, "msg": "missing date or paper_id"}), 400
    voter = _get_voter_identity()
    if not voter:
        return jsonify({"ok": False, "msg": "需要 cookie 或登录才能投票"}), 401
    key = _interaction_key(date, paper_id)
    with _interactions_lock:
        data = _get_interactions()
        entry = data.setdefault(key, {"likes": 0, "dislikes": 0, "liked_by": {}, "disliked_by": {}})
        # 取消之前的踩
        if voter in entry.get("disliked_by", {}):
            del entry["disliked_by"][voter]
            entry["dislikes"] = max(0, entry.get("dislikes", 0) - 1)
        # 切换赞
        if voter in entry.get("liked_by", {}):
            del entry["liked_by"][voter]
            entry["likes"] = max(0, entry.get("likes", 0) - 1)
            user_liked = False
        else:
            entry.setdefault("liked_by", {})[voter] = True
            entry["likes"] = entry.get("likes", 0) + 1
            user_liked = True
        _interactions_store.put(key, entry)
    return jsonify({
        "ok": True, "likes": entry["likes"], "dislikes": entry["dislikes"],
        "user_liked": user_liked, "user_disliked": False,
    })


@app.route("/api/dislike", methods=["POST"])
def api_dislike():
    payload = request.get_json(silent=True) or {}
    date = str(payload.get("date") or "").strip()
    paper_id = str(payload.get("paper_id") or "").strip()
    if not date or not paper_id:
        return jsonify({"ok": False, "msg": "missing date or paper_id"}), 400
    voter = _get_voter_identity()
    if not voter:
        return jsonify({"ok": False, "msg": "需要 cookie 或登录才能投票"}), 401
    key = _interaction_key(date, paper_id)
    with _interactions_lock:
        data = _get_interactions()
        entry = data.setdefault(key, {"likes": 0, "dislikes": 0, "liked_by": {}, "disliked_by": {}})
        # 取消之前的赞
        if voter in entry.get("liked_by", {}):
            del entry["liked_by"][voter]
            entry["likes"] = max(0, entry.get("likes", 0) - 1)
        # 切换踩
        if voter in entry.get("disliked_by", {}):
            del entry["disliked_by"][voter]
            entry["dislikes"] = max(0, entry.get("dislikes", 0) - 1)
            user_disliked = False
        else:
            entry.setdefault("disliked_by", {})[voter] = True
            entry["dislikes"] = entry.get("dislikes", 0) + 1
            user_disliked = True
        _interactions_store.put(key, entry)
    return jsonify({
        "ok": True, "likes": entry["likes"], "dislikes": entry["dislikes"],
        "user_liked": False, "user_disliked": user_disliked,
    })


@app.route("/api/interactions/<date>")
def api_interactions(date: str):
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
        abort(404)
    voter = _get_voter_identity()
    result: dict = {}
    prefix = date + "/"
    with _comments_lock:
        comments_data = _get_comments()
        comment_counts = {
            key[len(prefix):]: len(comments)
            for key, comments in comments_data.items()
            if key.startswith(prefix)
        }
    with _interactions_lock:
        data = _get_interactions()
        for key, entry in data.items():
            if key.startswith(prefix):
                pid = key[len(prefix):]
                result[pid] = {
                    "likes": entry.get("likes", 0),
                    "dislikes": entry.get("dislikes", 0),
                    "user_liked": voter in entry.get("liked_by", {}),
                    "user_disliked": voter in entry.get("disliked_by", {}),
                    "comment_count": comment_counts.get(pid, 0),
                }
    # 仅有评论、没有赞踩记录的论文也要带上评论数
    for pid, cnt in comment_counts.items():
        if pid not in result:
            result[pid] = {
                "likes": 0,
                "dislikes": 0,
                "user_liked": False,
                "user_disliked": False,
                "comment_count": cnt,
            }
    return jsonify({"ok": True, "interactions": result})


# ─────────────────────────────────────────────
# 评论 API
# ─────────────────────────────────────────────

@app.route("/api/comment", methods=["POST"])
def api_comment_post():
    username = require_user()
    payload = request.get_json(silent=True) or {}
    date = str(payload.get("date") or "").strip()
    paper_id = str(payload.get("paper_id") or "").strip()
    text = str(payload.get("text") or "").strip()
    if not date or not paper_id:
        return jsonify({"ok": False, "msg": "missing date or paper_id"}), 400
    if not text or len(text) > 50:
        return jsonify({"ok": False, "msg": "评论内容 1-50 字"}), 400
    key = _interaction_key(date, paper_id)
    comment_id = secrets.token_urlsafe(12)
    comment = {
        "id": comment_id,
        "username": username,
        "text": text,
        "created_at": datetime.now(BJ_TZ).isoformat(),
    }
    with _comments_lock:
        data = _get_comments()
        data.setdefault(key, []).append(comment)
        _comments_store.put(key, data[key])
    return jsonify({"ok": True, "comment": comment})


@app.route("/api/comment/<comment_id>", methods=["DELETE"])
def api_comment_delete(comment_id: str):
    username = require_user()
    with _comments_lock:
        data = _get_comments()
        for key, comments in data.items():
            for i, c in enumerate(comments):
                if c.get("id") == comment_id and c.get("username") == username:
                    comments.pop(i)
                    _comments_store.put(key, comments)
                    return jsonify({"ok": True})
    return jsonify({"ok": False, "msg": "评论不存在或无权删除"}), 404


@app.route("/api/comments/<date>/<paper_id>")
def api_comments_get(date: str, paper_id: str):
    key = _interaction_key(date, paper_id)
    with _comments_lock:
        data = _get_comments()
        comments = data.get(key, [])
    return jsonify({"ok": True, "comments": comments})


@app.route("/api/comments-all/<date>")
def api_comments_all(date: str):
    """一次性返回某天全部论文的评论（按 paper_id 分组），供默认展开评论时批量加载。"""
    prefix = f"{date}/"
    out: dict = {}
    with _comments_lock:
        data = _get_comments()
        for key, comments in data.items():
            if key.startswith(prefix) and comments:
                out[key[len(prefix):]] = comments
    return jsonify({"ok": True, "comments": out})


# ─────────────────────────────────────────────
# 标记（沉浸式阅读）API
# ─────────────────────────────────────────────

@app.route("/api/highlight", methods=["POST"])
def api_highlight_post():
    username = require_user()
    payload = request.get_json(silent=True) or {}
    date = str(payload.get("date") or "").strip()
    paper_id = str(payload.get("paper_id") or "").strip()
    text = str(payload.get("text") or "").strip()
    color = str(payload.get("color") or "#fef08a").strip()
    if not date or not paper_id:
        return jsonify({"ok": False, "msg": "missing date or paper_id"}), 400
    if not text or len(text) > 500:
        return jsonify({"ok": False, "msg": "标记文本 1-500 字"}), 400
    highlight_id = secrets.token_urlsafe(12)
    comment = _parse_highlight_comment(payload)
    highlight = {
        "id": highlight_id,
        "text": text,
        "color": color,
        "comment": comment,
        "created_at": datetime.now(BJ_TZ).isoformat(),
    }
    if comment:
        highlight["comment_updated_at"] = highlight["created_at"]
    with _highlights_lock:
        data = _get_highlights()
        user_key = username
        paper_key = _interaction_key(date, paper_id)
        data.setdefault(user_key, {}).setdefault(paper_key, []).append(highlight)
        _highlights_store.put(user_key, data[user_key])
    return jsonify({"ok": True, "highlight": highlight})


def _parse_highlight_comment(payload: dict) -> str:
    """从请求体解析标记评论，超长时截断而非报错（避免 CDN/客户端差异导致 400）。"""
    raw = payload.get("comment")
    if raw is None:
        return ""
    if not isinstance(raw, str):
        return ""
    comment = raw.strip()
    if len(comment) > 200:
        comment = comment[:200]
    return comment


def _update_highlight_comment(username: str, highlight_id: str, comment: str) -> Optional[dict]:
    with _highlights_lock:
        data = _get_highlights()
        user_data = data.get(username, {})
        for highlights in user_data.values():
            for h in highlights:
                if h.get("id") == highlight_id:
                    h["comment"] = comment
                    if comment:
                        h["comment_updated_at"] = datetime.now(BJ_TZ).isoformat()
                    else:
                        h.pop("comment_updated_at", None)
                    _highlights_store.put(username, user_data)
                    return h
    return None


@app.route("/api/highlight/<highlight_id>/comment", methods=["POST"])
def api_highlight_comment_post(highlight_id: str):
    """更新标记评论（POST，兼容 CDN 对 PATCH 的限制）。"""
    username = require_user()
    payload = request.get_json(silent=True) or {}
    comment = _parse_highlight_comment(payload)
    h = _update_highlight_comment(username, highlight_id, comment)
    if h is None:
        return jsonify({"ok": False, "msg": "标记不存在或无权修改"}), 404
    return jsonify({"ok": True, "highlight": h})


@app.route("/api/highlight/<highlight_id>", methods=["PATCH", "POST"])
def api_highlight_patch(highlight_id: str):
    """更新标记评论（PATCH 保留兼容；POST 需带 comment 字段）。"""
    username = require_user()
    payload = request.get_json(silent=True) or {}
    if request.method == "POST" and "comment" not in payload:
        return jsonify({"ok": False, "msg": "missing comment"}), 400
    comment = _parse_highlight_comment(payload)
    h = _update_highlight_comment(username, highlight_id, comment)
    if h is None:
        return jsonify({"ok": False, "msg": "标记不存在或无权修改"}), 404
    return jsonify({"ok": True, "highlight": h})


@app.route("/api/highlight/<highlight_id>", methods=["DELETE"])
def api_highlight_delete(highlight_id: str):
    username = require_user()
    with _highlights_lock:
        data = _get_highlights()
        user_data = data.get(username, {})
        for paper_key, highlights in list(user_data.items()):
            for i, h in enumerate(highlights):
                if h.get("id") == highlight_id:
                    highlights.pop(i)
                    if not highlights:
                        del user_data[paper_key]
                    _highlights_store.put(username, user_data)
                    return jsonify({"ok": True})
    return jsonify({"ok": False, "msg": "标记不存在或无权删除"}), 404


@app.route("/api/arxiv-version/<date>/<paper_id>")
def api_arxiv_version(date: str, paper_id: str):
    """arXiv 论文当前版本（服务端拉 abs 页，按 日期/arXiv号 永久缓存到 SQLite）。"""
    base_id = arxiv_version.normalize_base_id(paper_id)
    if not arxiv_version.is_arxiv_base_id(base_id):
        return jsonify({"ok": False, "msg": "无效的 arXiv ID"}), 400
    try:
        ver, from_cache = _arxiv_version_cache.get_version(date, paper_id)
    except Exception as exc:
        log.warning(
            "arxiv version fetch failed for %s: %s",
            arxiv_version.cache_key(date, paper_id),
            exc,
        )
        return jsonify({"ok": False, "msg": "无法获取 arXiv 版本"}), 502
    resp = make_response(
        jsonify({
            "ok": True,
            "version": ver,
            "date": date,
            "paper_id": paper_id,
            "cached": from_cache,
        })
    )
    resp.headers["Cache-Control"] = "public, max-age=86400, s-maxage=86400"
    return resp


@app.route("/api/highlights-community/<date>/<paper_id>")
def api_highlights_community(date: str, paper_id: str):
    """某篇论文上所有用户标记过的文本（去重），不暴露标注者身份。"""
    paper_key = _interaction_key(date, paper_id)
    seen: set[str] = set()
    marks: list[dict] = []
    with _highlights_lock:
        data = _get_highlights()
        for user_data in data.values():
            for h in user_data.get(paper_key, []):
                text = str(h.get("text") or "").strip()
                if not text or text in seen:
                    continue
                seen.add(text)
                marks.append({"text": text})
    return jsonify({"ok": True, "marks": marks})


@app.route("/api/highlights/<date>/<paper_id>")
def api_highlights_get(date: str, paper_id: str):
    username = current_user()
    if not username:
        return jsonify({"ok": False, "msg": "需要登录"}), 401
    paper_key = _interaction_key(date, paper_id)
    with _highlights_lock:
        data = _get_highlights()
        highlights = data.get(username, {}).get(paper_key, [])
    return jsonify({"ok": True, "highlights": highlights})


@app.route("/api/highlights-all")
def api_highlights_all():
    """获取当前用户的所有标记，用于"我的标记"页面。"""
    username = current_user()
    if not username:
        return jsonify({"ok": False, "msg": "需要登录"}), 401
    with _highlights_lock:
        data = _get_highlights()
        user_data = data.get(username, {})
    result: list[dict] = []
    for paper_key, highlights in user_data.items():
        parts = paper_key.split("/", 1)
        d = parts[0] if len(parts) > 0 else ""
        pid = parts[1] if len(parts) > 1 else ""
        for h in highlights:
            result.append({**h, "date": d, "paper_id": pid})
    result.sort(key=lambda x: x.get("created_at", ""), reverse=True)
    return jsonify({"ok": True, "highlights": result})


@app.route("/my-highlights")
def my_highlights_page():
    username = current_user()
    if not username:
        return redirect(url_for("index"))
    return render_template("my_highlights.html", username=username)


# ─────────────────────────────────────────────
# 收藏（可选文件夹分类）API
# ─────────────────────────────────────────────

def _favorite_key(date: str, paper_id: str) -> str:
    return f"{date}/{paper_id}"


@app.route("/api/favorite", methods=["POST"])
def api_favorite_post():
    """收藏一篇论文（可指定文件夹）。重复收藏视为更新文件夹。"""
    username = require_user()
    payload = request.get_json(silent=True) or {}
    date = str(payload.get("date") or "").strip()
    paper_id = str(payload.get("paper_id") or "").strip()
    title = str(payload.get("title") or "").strip()
    abs_url = str(payload.get("abs_url") or "").strip()
    folder = str(payload.get("folder") or "").strip() or DEFAULT_FAVORITE_FOLDER
    if not date or not paper_id:
        return jsonify({"ok": False, "msg": "missing date or paper_id"}), 400
    if len(folder) > 50:
        return jsonify({"ok": False, "msg": "文件夹名过长"}), 400
    key = _favorite_key(date, paper_id)
    with _favorites_lock:
        data = _get_favorites()
        entry = _get_user_favorites(data, username)
        if folder not in entry["folders"]:
            entry["folders"].append(folder)
        existing = entry["items"].get(key)
        entry["items"][key] = {
            "date": date,
            "paper_id": paper_id,
            "title": title or (existing or {}).get("title", ""),
            "abs_url": abs_url or (existing or {}).get("abs_url", ""),
            "folder": folder,
            "created_at": (existing or {}).get("created_at") or datetime.now(BJ_TZ).isoformat(),
        }
        _favorites_store.put(username, entry)
    return jsonify({"ok": True, "favorited": True, "folder": folder})


@app.route("/api/favorite/<date>/<paper_id>", methods=["DELETE"])
def api_favorite_delete(date: str, paper_id: str):
    username = require_user()
    key = _favorite_key(date, paper_id)
    with _favorites_lock:
        data = _get_favorites()
        entry = data.get(username, {})
        items = entry.get("items", {})
        if key in items:
            del items[key]
            _favorites_store.put(username, entry)
            return jsonify({"ok": True, "favorited": False})
    return jsonify({"ok": False, "msg": "收藏不存在"}), 404


@app.route("/api/favorites/<date>")
def api_favorites_get(date: str):
    """返回当前用户在该日期已收藏的论文及文件夹列表（供论文页渲染状态）。"""
    username = current_user()
    if not username:
        return jsonify({"ok": True, "favorites": {}, "folders": [DEFAULT_FAVORITE_FOLDER]})
    prefix = date + "/"
    with _favorites_lock:
        data = _get_favorites()
        entry = data.get(username, {})
        folders = list(entry.get("folders", [])) or [DEFAULT_FAVORITE_FOLDER]
        if DEFAULT_FAVORITE_FOLDER not in folders:
            folders.insert(0, DEFAULT_FAVORITE_FOLDER)
        favorites = {}
        for key, item in entry.get("items", {}).items():
            if key.startswith(prefix):
                favorites[key[len(prefix):]] = {"folder": item.get("folder", DEFAULT_FAVORITE_FOLDER)}
    return jsonify({"ok": True, "favorites": favorites, "folders": folders})


@app.route("/api/favorites-all")
def api_favorites_all():
    """当前用户的全部收藏，按文件夹分组，供"我的收藏"页面使用。"""
    username = current_user()
    if not username:
        return jsonify({"ok": False, "msg": "需要登录"}), 401
    with _favorites_lock:
        data = _get_favorites()
        entry = data.get(username, {})
        folders = list(entry.get("folders", [])) or [DEFAULT_FAVORITE_FOLDER]
        if DEFAULT_FAVORITE_FOLDER not in folders:
            folders.insert(0, DEFAULT_FAVORITE_FOLDER)
        items = list(entry.get("items", {}).values())
    items.sort(key=lambda x: x.get("created_at", ""), reverse=True)
    return jsonify({"ok": True, "folders": folders, "items": items})


@app.route("/api/favorite-folder", methods=["POST"])
def api_favorite_folder_create():
    username = require_user()
    payload = request.get_json(silent=True) or {}
    name = str(payload.get("name") or "").strip()
    if not name:
        return jsonify({"ok": False, "msg": "文件夹名不能为空"}), 400
    if len(name) > 50:
        return jsonify({"ok": False, "msg": "文件夹名过长"}), 400
    with _favorites_lock:
        data = _get_favorites()
        entry = _get_user_favorites(data, username)
        if name in entry["folders"]:
            return jsonify({"ok": False, "msg": "文件夹已存在"}), 409
        entry["folders"].append(name)
        _favorites_store.put(username, entry)
    return jsonify({"ok": True, "name": name})


@app.route("/api/favorite-folder/<path:name>", methods=["DELETE"])
def api_favorite_folder_delete(name: str):
    username = require_user()
    name = (name or "").strip()
    if name == DEFAULT_FAVORITE_FOLDER:
        return jsonify({"ok": False, "msg": "默认收藏夹不可删除"}), 400
    with _favorites_lock:
        data = _get_favorites()
        entry = _get_user_favorites(data, username)
        if name not in entry["folders"]:
            return jsonify({"ok": False, "msg": "文件夹不存在"}), 404
        entry["folders"].remove(name)
        # 该文件夹下的论文移回默认收藏夹
        for item in entry["items"].values():
            if item.get("folder") == name:
                item["folder"] = DEFAULT_FAVORITE_FOLDER
        _favorites_store.put(username, entry)
    return jsonify({"ok": True})


@app.route("/my-favorites")
def my_favorites_page():
    username = current_user()
    if not username:
        return redirect(url_for("index"))
    return render_template("my_favorites.html", username=username)


# ─────────────────────────────────────────────
# ASLP 实验室新闻 / 公告 API
# ─────────────────────────────────────────────

@app.route("/api/lab-feed")
def api_aslp_feed():
    aslp_feed.ensure_started()
    try:
        limit = int(request.args.get("limit", 5))
    except (TypeError, ValueError):
        limit = 5
    limit = max(1, min(limit, 20))
    items = aslp_feed.get_items(limit=limit)
    updated = aslp_feed.last_updated_iso() or "pending"
    etag = f"lab-feed-{updated}-{limit}"
    if request.headers.get("If-None-Match") == f'"{etag}"':
        resp304 = Response(status=304)
        resp304.set_etag(etag)
        return _apply_no_cdn_cache(resp304)
    resp = jsonify({
        "ok": True,
        "items": items,
        "updated_at": updated if updated != "pending" else None,
    })
    resp.set_etag(etag)
    return _apply_no_cdn_cache(resp)


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

    papers_digest = content_digest.papers_list_digest(index) if index else None

    return render_template(
        "date.html",
        date=date,
        is_today=(date == today_str),
        index=index,
        papers_digest=papers_digest,
        html_exists=html_exists,
        prev_date=prev_date,
        next_date=next_date,
    )


@app.route("/paper/<date>/<paper_id>")
def view_paper_share(date: str, paper_id: str):
    """单篇论文分享页：仅展示一篇卡片，HTML 按内容 ETag 可供 CDN 缓存。"""
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
        abort(404)
    if date not in list_all_dates():
        abort(404)
    index = load_index(date)
    if not index:
        abort(404)
    target = _find_paper(index, paper_id)
    if target is None:
        abort(404)

    paper_digest = content_digest.single_paper_digest(target)
    etag = f"paper-{date}-{paper_id}-{paper_digest}"
    if request.headers.get("If-None-Match") == etag:
        r304 = Response(status=304)
        r304.set_etag(etag)
        r304.headers["Cache-Control"] = (
            f"public, max-age=3600, s-maxage={IMMUTABLE_SMAXAGE_SECONDS}, "
            "stale-while-revalidate=86400"
        )
        return r304

    resp = make_response(
        render_template(
            "paper_share.html",
            date=date,
            paper_id=paper_id,
            paper_title=target.get("title") or paper_id,
            index=index,
            paper_digest=paper_digest,
        )
    )
    resp.set_etag(etag)
    resp.headers["Cache-Control"] = (
        f"public, max-age=3600, s-maxage={IMMUTABLE_SMAXAGE_SECONDS}, "
        "stale-while-revalidate=86400"
    )
    return resp


@app.route("/raw/<date>.html")
def raw_html(date: str):
    """直接 serve 原始报告文件，被 iframe 嵌入。"""
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
        abort(404)
    path = discover_report_html(date)
    if not path:
        abort(404)
    try:
        mtime = int(path.stat().st_mtime)
        etag = f'"report-{date}-{mtime}"'
    except OSError:
        etag = None
    if etag and request.headers.get("If-None-Match") == etag:
        r304 = Response(status=304)
        r304.set_etag(etag.strip('"'))
        return _apply_304_cache(r304)
    resp = send_from_directory(path.parent, path.name)
    if etag:
        resp.set_etag(etag.strip('"'))
        resp.headers["Cache-Control"] = _immutable_cache_control()
    return resp


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
    return render_template("stats.html", **build_visit_stats())


def build_visit_stats() -> dict:
    snapshot = get_visits_snapshot()
    today_str = datetime.now(BJ_TZ).strftime("%Y-%m-%d")
    today_day = snapshot.get(today_str) or {"total": 0, "hourly": [0] * 24, "active_users": 0}

    sorted_dates = sorted(snapshot.keys(), reverse=True)
    recent: list[dict] = []
    for d in sorted_dates[:30]:
        day = snapshot[d]
        recent.append({
            "date": d,
            "total": int(day.get("total", 0)),
            "active_users": int(day.get("active_users", 0)),
        })
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
    total_active_users = sum(int(d.get("active_users", 0)) for d in snapshot.values())
    day_count = len(snapshot)
    avg_per_day = round(total / day_count, 1) if day_count else 0
    avg_active_users_per_day = round(total_active_users / day_count, 1) if day_count else 0

    return {
        "today_total": int(today_day.get("total", 0)),
        "today_active_users": int(today_day.get("active_users", 0)),
        "today_hourly": hourly,
        "hourly_pct": hourly_pct,
        "peak_hour_idx": peak_hour_idx,
        "peak_hour_count": max_hour,
        "recent": recent,
        "total": total,
        "total_active_users": total_active_users,
        "day_count": day_count,
        "avg_per_day": avg_per_day,
        "avg_active_users_per_day": avg_active_users_per_day,
        "today_str": today_str,
    }


@app.route("/api/visits")
def api_visits():
    return {"visits": get_visits_snapshot()}


@app.route("/api/tab-visit", methods=["POST"])
def api_tab_visit():
    payload = request.get_json(silent=True) or {}
    tab_id = str(payload.get("tab_id") or "").strip()
    if not _valid_tab_id(tab_id):
        return {"ok": False, "msg": "invalid tab_id"}, 400

    visitor_id = (request.cookies.get(VISITOR_COOKIE_NAME) or "").strip()
    should_set_cookie = False
    if not _valid_visitor_id(visitor_id):
        visitor_id = _make_visitor_id()
        should_set_cookie = True

    try:
        counted = record_tab_visit(visitor_id, tab_id)
    except Exception as e:
        log.warning(f"记录 tab 访问失败: {e}")
        return {"ok": False, "msg": "failed to record visit"}, 500

    response = {"ok": True, "counted": counted}
    if should_set_cookie:
        resp = app.response_class(
            response=json.dumps(response, ensure_ascii=False),
            status=200,
            mimetype="application/json",
        )
        resp.set_cookie(
            VISITOR_COOKIE_NAME,
            visitor_id,
            max_age=VISITOR_COOKIE_MAX_AGE,
            httponly=True,
            samesite="Lax",
        )
        return resp
    return response


@app.route("/admin/stats")
def admin_stats():
    auth_error = _admin_auth_error()
    if auth_error:
        return auth_error
    return {"ok": True, "stats": build_visit_stats(), "visits": get_visits_snapshot()}


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


@app.route("/admin/run-now", methods=["POST"])
def admin_run_now():
    """手动触发一次每日任务。"""
    auth_error = _admin_auth_error()
    if auth_error:
        return auth_error
    if not _scheduler:
        return {"ok": False, "msg": "scheduler not started"}, 500
    threading.Thread(
        target=_scheduler.run_once, kwargs={"reason": "manual"}, daemon=True
    ).start()
    return {"ok": True, "msg": "已触发，可在日志中查看进度"}


@app.route("/admin/status")
def admin_status():
    auth_error = _admin_auth_error()
    if auth_error:
        return auth_error
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
    threads = int(os.environ.get("WEB_THREADS", "8"))
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

    static_assets.refresh()

    ccf_catalog.start_background_refresh()

    # 启动 ASLP 新闻/公告每小时刷新
    aslp_feed.start_background_refresh()

    try:
        from waitress import serve
    except ImportError as e:
        raise SystemExit("缺少生产 WSGI server：请先安装 waitress（例如 uv sync 或 pip install waitress）") from e

    log.info(f"🌐 启动 Web 服务 http://{host}:{port} (waitress, threads={threads})")
    log.info(f"🔐 认证: {auth.config_summary()}")
    serve(app, host=host, port=port, threads=threads)


if __name__ == "__main__":
    main()
