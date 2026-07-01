"""论文图表懒提取与缓存：优先 arXiv HTML（含图注/表注），失败再回退 PDF。

缓存: data/papers/_assets/<date>/<paper_id>/
  manifest.json
  {id}_thumb.webp
  {id}_full.webp      图片高清（>700DPI 等效像素上限时压缩）
  {id}_table.html     HTML 表格（来自 arXiv HTML）
"""

from __future__ import annotations

import io
import json
import logging
import re
import threading
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urljoin

import requests

log = logging.getLogger("paper_assets")

MANIFEST_VERSION = 4
MAX_DPI = 700
MAX_FULL_EDGE = 4200  # 约 700DPI × 6 英寸边长
THUMB_MAX_PX = 240
MIN_IMAGE_PX = 96
MAX_ASSETS = 80
ARXIV_HTML_BASE = "https://arxiv.org/html"
REQUEST_HEADERS = {
    "User-Agent": "ArxivWatcher/1.0 (Academic; +https://arxiv.npu-aslp.org)",
}

_ARXIV_ID_RE = re.compile(r"^\d{4}\.\d{4,5}(v\d+)?$", re.I)

_lock_guard = threading.Lock()
_asset_locks: dict[str, threading.Lock] = {}


def _lock_for(key: str) -> threading.Lock:
    with _lock_guard:
        if key not in _asset_locks:
            _asset_locks[key] = threading.Lock()
        return _asset_locks[key]


def safe_paper_dir_name(paper_id: str) -> str:
    return re.sub(r"[^\w\-.]", "_", paper_id)


def assets_cache_dir(data_dir: Path, date: str, paper_id: str) -> Path:
    d = data_dir / "_assets" / date / safe_paper_dir_name(paper_id)
    d.mkdir(parents=True, exist_ok=True)
    return d


def manifest_path(cache_dir: Path) -> Path:
    return cache_dir / "manifest.json"


def thumb_file(cache_dir: Path, asset_id: int) -> Path:
    return cache_dir / f"{asset_id}_thumb.webp"


def full_file(cache_dir: Path, asset_id: int) -> Path:
    return cache_dir / f"{asset_id}_full.webp"


def table_file(cache_dir: Path, asset_id: int) -> Path:
    return cache_dir / f"{asset_id}_table.html"


def _load_manifest(cache_dir: Path) -> Optional[dict]:
    path = manifest_path(cache_dir)
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        log.warning("paper-assets: 读取 manifest 失败 %s: %s", path, e)
        return None


def _save_manifest(cache_dir: Path, manifest: dict) -> None:
    manifest_path(cache_dir).write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _manifest_fresh(manifest: dict) -> bool:
    return int(manifest.get("manifest_version") or 0) >= MANIFEST_VERSION


def is_arxiv_paper_id(paper_id: str) -> bool:
    return bool(_ARXIV_ID_RE.fullmatch((paper_id or "").strip()))


def arxiv_html_urls(paper_id: str) -> list[str]:
    pid = (paper_id or "").strip()
    candidates = [pid]
    m = re.match(r"^(\d{4}\.\d{4,5})v\d+$", pid, re.I)
    if m:
        candidates.append(m.group(1))
    seen: set[str] = set()
    urls: list[str] = []
    for c in candidates:
        u = f"{ARXIV_HTML_BASE}/{c}"
        if u not in seen:
            seen.add(u)
            urls.append(u)
    return urls


def _normalize_arxiv_html_asset_url(url: str) -> str:
    """修正 /html/YYMM.NNNNN/YYMM.NNNNNvN/file → /html/YYMM.NNNNNvN/file。"""
    return re.sub(
        r"(https://arxiv\.org/html/\d{4}\.\d{4,5})/(\d{4}\.\d{4,5}v\d+)(/.*)?$",
        r"https://arxiv.org/html/\2\3",
        url,
        flags=re.I,
    )


def _resolve_arxiv_image_url(page_url: str, src: str, *, html_base: str = "") -> str:
    src = (src or "").strip()
    if not src or src.startswith("data:"):
        return ""
    if src.startswith(("http://", "https://")):
        return _normalize_arxiv_html_asset_url(src)
    if src.startswith("//"):
        return _normalize_arxiv_html_asset_url("https:" + src)
    if src.startswith("/"):
        return _normalize_arxiv_html_asset_url(urljoin("https://arxiv.org", src))

    if html_base:
        return _normalize_arxiv_html_asset_url(urljoin(html_base, src))

    # src 形如 2606.20001v1/x2.png —— 相对 /html/ 根，而非无版本页面目录
    m = re.match(r"^(\d{4}\.\d{4,5}v\d+)/(.*)$", src, re.I)
    if m:
        return f"{ARXIV_HTML_BASE}/{m.group(1)}/{m.group(2)}"

    page = (page_url or "").rstrip("/")
    vm = re.search(r"/html/(\d{4}\.\d{4,5}v\d+)$", page, re.I)
    if vm:
        return f"{ARXIV_HTML_BASE}/{vm.group(1)}/{src.lstrip('/')}"

    joined = urljoin(page + "/", src)
    return _normalize_arxiv_html_asset_url(joined)


def _arxiv_image_url_candidates(url: str) -> list[str]:
    url = (url or "").strip()
    if not url:
        return []
    out: list[str] = []
    seen: set[str] = set()

    def add(u: str) -> None:
        u = _normalize_arxiv_html_asset_url(u)
        if u and u not in seen:
            seen.add(u)
            out.append(u)

    add(url)
    add(_normalize_arxiv_html_asset_url(url))

    m = re.match(
        r"https://arxiv\.org/html/(\d{4}\.\d{4,5})/(\d{4}\.\d{4,5}v\d+)(/.*)$",
        url,
        re.I,
    )
    if m:
        add(f"{ARXIV_HTML_BASE}/{m.group(2)}{m.group(3)}")

    m2 = re.match(r"https://arxiv\.org/html/(\d{4}\.\d{4,5}v\d+)(/.*)$", url, re.I)
    if m2:
        base_id = re.sub(r"v\d+$", "", m2.group(1), flags=re.I)
        add(f"{ARXIV_HTML_BASE}/{base_id}{m2.group(2)}")

    return out


def _paper_from_target(target: dict, send_mod) -> Any:
    paper_id = str(target.get("paper_id", ""))
    return send_mod.Paper(
        paper_id=paper_id,
        title=target.get("title", ""),
        authors=target.get("authors", []) or [],
        comments=target.get("comments", ""),
        subjects=target.get("subjects", ""),
        abstract=target.get("abstract", ""),
        pdf_url=target.get("pdf_url") or f"https://arxiv.org/pdf/{paper_id}",
        abs_url=target.get("abs_url") or f"https://arxiv.org/abs/{paper_id}",
        source_categories=target.get("source_categories", []) or [],
    )


def _download_pdf(target: dict, send_mod) -> Optional[Path]:
    paper_id = str(target.get("paper_id", ""))
    pdf_dir = send_mod.PROJECT_ROOT / "arxiv_digest_work" / "pdfs"
    pdf_dir.mkdir(parents=True, exist_ok=True)
    paper = _paper_from_target(target, send_mod)
    try:
        return send_mod.download_pdf(paper, pdf_dir)
    except Exception as e:
        log.warning("paper-assets: PDF 下载失败 %s: %s", paper_id, e)
        return None


def _fetch_arxiv_html(url: str) -> Optional[tuple[str, str]]:
    try:
        resp = requests.get(url, headers=REQUEST_HEADERS, timeout=45, allow_redirects=True)
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        text = resp.text or ""
        if not text.lstrip().startswith("<"):
            return None
        return text, resp.url
    except Exception as e:
        log.debug("paper-assets: HTML 获取失败 %s: %s", url, e)
        return None


def _caption_text(el) -> str:
    if el is None:
        return ""
    return " ".join(el.stripped_strings)


def _caption_label(caption: str, fallback: str) -> str:
    caption = (caption or "").strip()
    if not caption:
        return fallback
    idx = caption.find(":")
    if 0 < idx <= 32:
        return caption[:idx].strip()
    return caption[:56] + ("…" if len(caption) > 56 else "")


def _parse_arxiv_html(html: str, page_url: str) -> list[dict]:
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "html.parser")
    html_base = ""
    base_tag = soup.find("base", href=True)
    if base_tag:
        html_base = str(base_tag.get("href") or "").strip()
    assets: list[dict] = []
    aid = 0

    for fig in soup.select("figure.ltx_figure"):
        if aid >= MAX_ASSETS:
            break
        img = fig.find("img")
        if not img:
            continue
        src = (img.get("src") or "").strip()
        if not src or src.startswith("data:"):
            continue
        image_url = _resolve_arxiv_image_url(page_url, src, html_base=html_base)
        if not image_url:
            continue
        cap_el = fig.find("figcaption", class_="ltx_caption")
        caption = _caption_text(cap_el)
        label = _caption_label(caption, f"图 {aid + 1}")
        try:
            w = int(float(img.get("width") or 0))
            h = int(float(img.get("height") or 0))
        except (TypeError, ValueError):
            w, h = 0, 0
        if w and h and (w < MIN_IMAGE_PX or h < MIN_IMAGE_PX):
            continue
        assets.append({
            "id": aid,
            "type": "figure",
            "source": "arxiv_html",
            "image_url": image_url,
            "caption": caption,
            "label": label,
        })
        aid += 1

    for fig in soup.select("figure.ltx_table, div.ltx_table"):
        if aid >= MAX_ASSETS:
            break
        table = fig.find("table")
        if not table:
            continue
        cap_el = fig.find("figcaption", class_="ltx_caption")
        caption = _caption_text(cap_el)
        label = _caption_label(caption, f"表 {aid + 1}")
        assets.append({
            "id": aid,
            "type": "table",
            "source": "arxiv_html",
            "table_html": str(table),
            "caption": caption,
            "label": label,
        })
        aid += 1

    return assets


def _try_scan_arxiv_html(paper_id: str) -> tuple[list[dict], Optional[str]]:
    if not is_arxiv_paper_id(paper_id):
        return [], None
    for url in arxiv_html_urls(paper_id):
        fetched = _fetch_arxiv_html(url)
        if not fetched:
            continue
        html, final_url = fetched
        assets = _parse_arxiv_html(html, final_url)
        if assets:
            log.info("paper-assets: HTML 提取 %s 共 %d 项 (%s)", paper_id, len(assets), final_url)
            return assets, final_url
    return [], None


def _download_image_bytes(url: str) -> bytes:
    last_err: Optional[Exception] = None
    for candidate in _arxiv_image_url_candidates(url):
        try:
            resp = requests.get(candidate, headers=REQUEST_HEADERS, timeout=90)
            if resp.status_code == 404:
                continue
            resp.raise_for_status()
            return resp.content
        except Exception as e:
            last_err = e
            continue
    if last_err:
        raise last_err
    raise requests.HTTPError(f"404 for {url}")


def _open_image(raw: bytes) -> "Any":
    from PIL import Image

    return Image.open(io.BytesIO(raw))


def _save_webp(img: "Any", path: Path, *, quality: int = 82) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if img.mode == "RGBA":
        img.save(path, "WEBP", quality=quality, method=4)
        return
    if img.mode != "RGB":
        img = img.convert("RGB")
    img.save(path, "WEBP", quality=quality, method=4)


def _resize_max(img: "Any", max_px: int) -> "Any":
    w, h = img.size
    scale = min(max_px / w, max_px / h, 1.0)
    if scale >= 1.0:
        return img
    return img.resize((max(1, int(w * scale)), max(1, int(h * scale))), resample=3)


def _cap_max_edge(img: "Any", max_edge: int = MAX_FULL_EDGE) -> "Any":
    w, h = img.size
    m = max(w, h)
    if m <= max_edge:
        return img
    scale = max_edge / m
    return img.resize((max(1, int(w * scale)), max(1, int(h * scale))), resample=3)


def _cap_dpi(img: "Any", dpi_x: float, dpi_y: float, max_dpi: float = MAX_DPI) -> "Any":
    cur = max(float(dpi_x or 0), float(dpi_y or 0))
    if cur <= max_dpi or cur <= 0:
        return img
    scale = max_dpi / cur
    w, h = img.size
    return img.resize((max(1, int(w * scale)), max(1, int(h * scale))), resample=3)


def _placeholder_thumb(label: str, path: Path, *, kind: str = "table") -> None:
    from PIL import Image, ImageDraw

    w, h = 280, 168
    bg = (248, 250, 252) if kind == "table" else (238, 242, 255)
    img = Image.new("RGB", (w, h), color=bg)
    draw = ImageDraw.Draw(img)
    if kind == "table":
        # 简易网格示意
        cols, rows = 4, 3
        cw, rh = w // cols, h // rows
        for ri in range(rows):
            for ci in range(cols):
                x0, y0 = ci * cw, ri * rh
                fill = (226, 232, 240) if ri == 0 else (255, 255, 255)
                draw.rectangle([x0, y0, x0 + cw - 1, y0 + rh - 1], fill=fill, outline=(203, 213, 225))
    else:
        draw.rectangle([20, 20, w - 20, h - 20], outline=(167, 139, 250), width=2)
    title = (label or ("表" if kind == "table" else "图"))[:24]
    draw.text((12, h - 22), title, fill=(71, 85, 105))
    img = _resize_max(img, THUMB_MAX_PX)
    _save_webp(img, path, quality=78)


def _render_table_html_thumb(table_html: str, path: Path) -> None:
    """把 HTML 表格渲染为网格缩略图（截取前几行/列）。"""
    from bs4 import BeautifulSoup
    from PIL import Image, ImageDraw, ImageFont

    soup = BeautifulSoup(table_html or "", "html.parser")
    table = soup.find("table")
    if table is None:
        _placeholder_thumb("表", path, kind="table")
        return

    max_rows, max_cols = 7, 5
    grid: list[list[str]] = []
    for tr in table.find_all("tr"):
        if len(grid) >= max_rows:
            break
        row: list[str] = []
        for cell in tr.find_all(["th", "td"]):
            if len(row) >= max_cols:
                break
            row.append(" ".join(cell.stripped_strings))
        if row:
            grid.append(row)
    if not grid:
        _placeholder_thumb("表", path, kind="table")
        return

    cols = max(len(r) for r in grid)
    for row in grid:
        while len(row) < cols:
            row.append("")

    pad = 10
    cell_w = 88
    cell_h = 28
    img_w = pad * 2 + cols * cell_w
    img_h = pad * 2 + len(grid) * cell_h
    img = Image.new("RGB", (img_w, img_h), (255, 255, 255))
    draw = ImageDraw.Draw(img)
    try:
        font = ImageFont.load_default(size=11)
    except TypeError:
        font = ImageFont.load_default()

    def _fit_text(text: str, max_chars: int = 10) -> str:
        t = (text or "").strip()
        if not t:
            return ""
        if len(t) > max_chars:
            return t[: max_chars - 1] + "…"
        return t

    trs = table.find_all("tr")
    for ri, row in enumerate(grid):
        for ci, text in enumerate(row):
            x0 = pad + ci * cell_w
            y0 = pad + ri * cell_h
            x1, y1 = x0 + cell_w - 1, y0 + cell_h - 1
            tr = trs[ri] if ri < len(trs) else None
            is_header_row = ri == 0 or bool(tr and tr.find_all("th"))
            fill = (241, 245, 249) if is_header_row else (255, 255, 255)
            draw.rectangle([x0, y0, x1, y1], fill=fill, outline=(203, 213, 225))
            label = _fit_text(text)
            if label:
                draw.text((x0 + 5, y0 + 7), label, fill=(51, 65, 85), font=font)

    img = _resize_max(img, THUMB_MAX_PX)
    _save_webp(img, path, quality=80)


def _persist_table_files(cache_dir: Path, assets: list[dict]) -> None:
    for asset in assets:
        if asset.get("type") != "table":
            continue
        html = asset.pop("table_html", None)
        if not html:
            continue
        path = table_file(cache_dir, int(asset["id"]))
        if not path.is_file():
            path.write_text(html, encoding="utf-8")


def _build_thumbnails_html(cache_dir: Path, assets: list[dict]) -> None:
    _persist_table_files(cache_dir, assets)
    for asset in assets:
        aid = int(asset["id"])
        thumb = thumb_file(cache_dir, aid)
        is_table = asset.get("type") == "table"
        # 表格缩略图算法会迭代，允许覆盖旧占位图
        if thumb.is_file() and not is_table:
            continue
        if asset.get("type") == "figure" and asset.get("image_url"):
            try:
                raw = _download_image_bytes(str(asset["image_url"]))
                img = _resize_max(_open_image(raw), THUMB_MAX_PX)
                _save_webp(img, thumb, quality=78)
            except Exception as e:
                log.warning("paper-assets: HTML 缩略图失败 #%s: %s", aid, e)
                _placeholder_thumb(asset.get("label", "图"), thumb, kind="figure")
        elif is_table:
            table_path = table_file(cache_dir, aid)
            html = ""
            if table_path.is_file():
                try:
                    html = table_path.read_text(encoding="utf-8")
                except OSError:
                    html = ""
            try:
                if html.strip():
                    _render_table_html_thumb(html, thumb)
                else:
                    _placeholder_thumb(asset.get("label", "表"), thumb, kind="table")
            except Exception as e:
                log.warning("paper-assets: 表格缩略图失败 #%s: %s", aid, e)
                _placeholder_thumb(asset.get("label", "表"), thumb, kind="table")


def _scan_pdf(pdf_path: Path) -> list[dict]:
    import fitz

    assets: list[dict] = []
    seen_xrefs: set[int] = set()
    aid = 0

    doc = fitz.open(pdf_path)
    try:
        for page_num in range(len(doc)):
            page = doc[page_num]

            for img in page.get_images(full=True):
                if aid >= MAX_ASSETS:
                    break
                xref = int(img[0])
                if xref in seen_xrefs:
                    continue
                seen_xrefs.add(xref)
                try:
                    info = doc.extract_image(xref)
                except Exception:
                    continue
                w, h = info.get("width", 0), info.get("height", 0)
                if w < MIN_IMAGE_PX or h < MIN_IMAGE_PX:
                    continue
                rects = page.get_image_rects(xref)
                if not rects:
                    continue
                bbox = rects[0]
                bw, bh = bbox.width, bbox.height
                if bw <= 0 or bh <= 0:
                    continue
                dpi_x = w / (bw / 72.0)
                dpi_y = h / (bh / 72.0)
                label = f"第{page_num + 1}页 图"
                assets.append({
                    "id": aid,
                    "type": "figure",
                    "source": "pdf",
                    "page": page_num + 1,
                    "xref": xref,
                    "width_px": w,
                    "height_px": h,
                    "dpi_x": round(dpi_x, 1),
                    "dpi_y": round(dpi_y, 1),
                    "label": label,
                    "caption": "",
                })
                aid += 1

            if aid >= MAX_ASSETS:
                break

            try:
                tables = page.find_tables()
            except Exception:
                tables = None
            if tables:
                for ti, table in enumerate(tables.tables):
                    if aid >= MAX_ASSETS:
                        break
                    bbox = fitz.Rect(table.bbox)
                    if bbox.width < 60 or bbox.height < 40:
                        continue
                    label = f"第{page_num + 1}页 表{ti + 1}"
                    assets.append({
                        "id": aid,
                        "type": "table",
                        "source": "pdf",
                        "page": page_num + 1,
                        "bbox": [bbox.x0, bbox.y0, bbox.x1, bbox.y1],
                        "label": label,
                        "caption": "",
                    })
                    aid += 1
    finally:
        doc.close()
    return assets


def _extract_figure_image(doc, asset: dict) -> "Any":
    info = doc.extract_image(int(asset["xref"]))
    img = _open_image(info["image"])
    return _cap_dpi(img, asset.get("dpi_x", 0), asset.get("dpi_y", 0))


def _extract_table_image(doc, asset: dict, *, render_dpi: float = MAX_DPI) -> "Any":
    import fitz

    page = doc[int(asset["page"]) - 1]
    bbox = fitz.Rect(asset["bbox"])
    zoom = min(render_dpi, MAX_DPI) / 72.0
    mat = fitz.Matrix(zoom, zoom)
    pix = page.get_pixmap(matrix=mat, clip=bbox, alpha=False)
    return _open_image(pix.tobytes("png"))


def _build_thumbnails_pdf(pdf_path: Path, cache_dir: Path, assets: list[dict]) -> None:
    import fitz

    doc = fitz.open(pdf_path)
    try:
        for asset in assets:
            thumb = thumb_file(cache_dir, int(asset["id"]))
            if thumb.is_file():
                continue
            try:
                if asset["type"] == "figure":
                    img = _extract_figure_image(doc, asset)
                else:
                    img = _extract_table_image(doc, asset, render_dpi=200)
                img = _resize_max(img, THUMB_MAX_PX)
                _save_webp(img, thumb, quality=78)
            except Exception as e:
                log.warning(
                    "paper-assets: PDF 缩略图失败 %s #%s: %s",
                    pdf_path.name, asset.get("id"), e,
                )
    finally:
        doc.close()


def _rebuild_missing_thumbs(
    cache_dir: Path,
    manifest: dict,
    target: dict,
    send_mod,
) -> None:
    assets = manifest.get("assets") or []
    if not assets:
        return
    source = manifest.get("source") or "pdf"
    if source == "arxiv_html":
        _build_thumbnails_html(cache_dir, assets)
        return
    pdf_path = _download_pdf(target, send_mod)
    if pdf_path and pdf_path.is_file():
        try:
            _build_thumbnails_pdf(pdf_path, cache_dir, assets)
        except Exception as e:
            log.warning("paper-assets: 补全 PDF 缩略图失败: %s", e)


def ensure_manifest(
    data_dir: Path,
    date: str,
    target: dict,
    *,
    send_mod=None,
) -> tuple[Optional[dict], Optional[str]]:
    """扫描 arXiv HTML（优先）或 PDF，生成 manifest + 缩略图。"""
    if send_mod is None:
        try:
            import send as send_mod  # type: ignore
        except Exception as e:
            return None, f"导入 send 失败: {e}"

    paper_id = str(target.get("paper_id", ""))
    cache_dir = assets_cache_dir(data_dir, date, paper_id)
    lock_key = str(cache_dir)
    with _lock_for(lock_key):
        existing = _load_manifest(cache_dir)
        if existing and _manifest_fresh(existing) and existing.get("assets") is not None:
            _rebuild_missing_thumbs(cache_dir, existing, target, send_mod)
            return existing, None

        assets: list[dict] = []
        source = "pdf"
        html_url: Optional[str] = None

        html_assets, html_url = _try_scan_arxiv_html(paper_id)
        if html_assets:
            assets = html_assets
            source = "arxiv_html"
        else:
            try:
                import fitz  # noqa: F401
                from PIL import Image  # noqa: F401
            except ImportError:
                return None, "服务端未安装 pymupdf 或 Pillow，无法提取图表"

            pdf_path = _download_pdf(target, send_mod)
            if not pdf_path or not pdf_path.is_file():
                return None, "未找到 arXiv HTML，且 PDF 下载失败"
            try:
                assets = _scan_pdf(pdf_path)
            except Exception as e:
                log.warning("paper-assets: PDF 扫描失败 %s: %s", paper_id, e)
                return None, "PDF 图表扫描失败"
            if assets:
                try:
                    _build_thumbnails_pdf(pdf_path, cache_dir, assets)
                except Exception as e:
                    log.warning("paper-assets: PDF 缩略图失败 %s: %s", paper_id, e)

        if source == "arxiv_html" and assets:
            try:
                _build_thumbnails_html(cache_dir, assets)
            except Exception as e:
                log.warning("paper-assets: HTML 缩略图失败 %s: %s", paper_id, e)

        manifest = {
            "manifest_version": MANIFEST_VERSION,
            "paper_id": paper_id,
            "date": date,
            "source": source,
            "html_url": html_url,
            "assets": assets,
        }
        _save_manifest(cache_dir, manifest)
        return manifest, None


def ensure_full_image(
    data_dir: Path,
    date: str,
    target: dict,
    asset_id: int,
    *,
    send_mod=None,
) -> tuple[Optional[Path], Optional[str]]:
    """按需生成高清图（HTML 图片或 PDF 提取，超大则压缩）。"""
    manifest, err = ensure_manifest(data_dir, date, target, send_mod=send_mod)
    if err or manifest is None:
        return None, err or "manifest 不可用"

    asset = get_asset_entry(manifest, asset_id)
    if asset is None:
        return None, "asset not found"
    if asset.get("type") != "figure":
        return None, "not a figure asset"

    paper_id = str(target.get("paper_id", ""))
    cache_dir = assets_cache_dir(data_dir, date, paper_id)
    full = full_file(cache_dir, asset_id)
    if full.is_file():
        return full, None

    lock_key = f"{cache_dir}:full:{asset_id}"
    with _lock_for(lock_key):
        if full.is_file():
            return full, None
        try:
            if asset.get("source") == "arxiv_html" and asset.get("image_url"):
                raw = _download_image_bytes(str(asset["image_url"]))
                img = _cap_max_edge(_open_image(raw))
                _save_webp(img, full, quality=85)
            else:
                if send_mod is None:
                    import send as send_mod  # type: ignore
                pdf_path = _download_pdf(target, send_mod)
                if not pdf_path or not pdf_path.is_file():
                    return None, "PDF 下载失败"
                import fitz

                doc = fitz.open(pdf_path)
                try:
                    img = _extract_figure_image(doc, asset)
                    _save_webp(img, full, quality=85)
                finally:
                    doc.close()
        except Exception as e:
            log.warning("paper-assets: 高清图失败 %s #%s: %s", paper_id, asset_id, e)
            return None, "高清图生成失败"

    return full, None


def ensure_full_table_image(
    data_dir: Path,
    date: str,
    target: dict,
    asset_id: int,
    *,
    send_mod=None,
) -> tuple[Optional[Path], Optional[str]]:
    """PDF 表格按需渲染为图片；HTML 表格请用 get_table_content。"""
    manifest, err = ensure_manifest(data_dir, date, target, send_mod=send_mod)
    if err or manifest is None:
        return None, err or "manifest 不可用"

    asset = get_asset_entry(manifest, asset_id)
    if asset is None or asset.get("type") != "table":
        return None, "asset not found"

    if asset.get("source") == "arxiv_html":
        return None, "html table"

    paper_id = str(target.get("paper_id", ""))
    cache_dir = assets_cache_dir(data_dir, date, paper_id)
    full = full_file(cache_dir, asset_id)
    if full.is_file():
        return full, None

    if send_mod is None:
        import send as send_mod  # type: ignore
    pdf_path = _download_pdf(target, send_mod)
    if not pdf_path or not pdf_path.is_file():
        return None, "PDF 下载失败"

    lock_key = f"{cache_dir}:full:{asset_id}"
    with _lock_for(lock_key):
        if full.is_file():
            return full, None
        try:
            import fitz

            doc = fitz.open(pdf_path)
            try:
                img = _extract_table_image(doc, asset, render_dpi=MAX_DPI)
                _save_webp(img, full, quality=85)
            finally:
                doc.close()
        except Exception as e:
            log.warning("paper-assets: PDF 表格高清图失败 %s #%s: %s", paper_id, asset_id, e)
            return None, "表格图片生成失败"
    return full, None


def get_table_content(
    data_dir: Path,
    date: str,
    target: dict,
    asset_id: int,
    *,
    send_mod=None,
) -> tuple[Optional[dict], Optional[str]]:
    manifest, err = ensure_manifest(data_dir, date, target, send_mod=send_mod)
    if err or manifest is None:
        return None, err or "manifest 不可用"

    asset = get_asset_entry(manifest, asset_id)
    if asset is None or asset.get("type") != "table":
        return None, "asset not found"

    paper_id = str(target.get("paper_id", ""))
    cache_dir = assets_cache_dir(data_dir, date, paper_id)
    path = table_file(cache_dir, asset_id)
    if path.is_file():
        html = path.read_text(encoding="utf-8")
    else:
        return None, "表格内容不存在"

    return {
        "label": asset.get("label") or "",
        "caption": asset.get("caption") or "",
        "html": html,
    }, None


def get_asset_entry(manifest: dict, asset_id: int) -> Optional[dict]:
    for a in manifest.get("assets") or []:
        if int(a.get("id", -1)) == asset_id:
            return a
    return None


def manifest_cache_digest(manifest: dict) -> str:
    """manifest 内容指纹，用于 CDN URL 版本号（manifest 变更则 URL 变更）。"""
    import hashlib

    fingerprint = {
        "manifest_version": int(manifest.get("manifest_version") or 0),
        "source": manifest.get("source") or "",
        "assets": [
            {
                "id": a.get("id"),
                "type": a.get("type"),
                "source": a.get("source"),
                "page": a.get("page"),
            }
            for a in (manifest.get("assets") or [])
        ],
    }
    canonical = json.dumps(fingerprint, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(canonical.encode()).hexdigest()[:10]
