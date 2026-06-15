"""将 static/ 下的 JS/CSS 复制为内容哈希文件名，供模板长期缓存引用。

逻辑名 ``app.js`` → ``_h/app.<sha256前10位>.js``；内容变化则哈希变化，URL 自动更新。
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path

from content_digest import digest_bytes

log = logging.getLogger("static_assets")

HASH_LEN = 10
HASHED_DIR_NAME = "_h"
MANIFEST_FILES = ("style.css", "app.js", "highlights.js")

_lock = threading.Lock()
_static_dir: Path | None = None
_manifest: dict[str, str] = {}
_source_mtimes: tuple[float, ...] = ()


def init(static_dir: Path) -> None:
    global _static_dir
    _static_dir = static_dir


def _read_source_mtimes(static_dir: Path) -> tuple[float, ...]:
    return tuple(
        static_dir.joinpath(name).stat().st_mtime
        if static_dir.joinpath(name).is_file()
        else 0.0
        for name in MANIFEST_FILES
    )


def refresh() -> dict[str, str]:
    """根据源文件内容重建哈希副本与 manifest。"""
    if _static_dir is None:
        return {}
    static_dir = _static_dir
    hashed_dir = static_dir / HASHED_DIR_NAME
    hashed_dir.mkdir(parents=True, exist_ok=True)

    manifest: dict[str, str] = {}
    active_names: set[str] = set()

    for name in MANIFEST_FILES:
        src = static_dir / name
        if not src.is_file():
            log.warning("静态资源不存在，跳过: %s", src)
            continue
        data = src.read_bytes()
        digest = digest_bytes(data)
        if "." in name:
            stem, suffix = name.rsplit(".", 1)
            hashed_name = f"{stem}.{digest}.{suffix}"
        else:
            hashed_name = f"{name}.{digest}"
        rel = f"{HASHED_DIR_NAME}/{hashed_name}"
        dst = static_dir / rel
        if not dst.exists() or dst.read_bytes() != data:
            dst.write_bytes(data)
            log.info("静态资源已哈希: %s -> %s", name, rel)
        manifest[name] = rel
        active_names.add(hashed_name)

    for stale in hashed_dir.iterdir():
        if stale.is_file() and stale.name not in active_names:
            stale.unlink(missing_ok=True)
            log.info("已清理过期哈希静态资源: %s", stale.name)

    global _manifest, _source_mtimes
    with _lock:
        _manifest = manifest
        _source_mtimes = _read_source_mtimes(static_dir)
    return manifest


def ensure_fresh() -> None:
    if _static_dir is None:
        return
    mtimes = _read_source_mtimes(_static_dir)
    if mtimes != _source_mtimes:
        refresh()


def get_rel(logical_name: str) -> str:
    """返回相对 static/ 的路径（含 _h/ 前缀）。"""
    ensure_fresh()
    with _lock:
        return _manifest.get(logical_name, logical_name)


def is_hashed_static_path(path: str) -> bool:
    prefix = f"/static/{HASHED_DIR_NAME}/"
    return path.startswith(prefix)
