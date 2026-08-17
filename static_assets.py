"""将 static/ 下的 JS/CSS 复制为内容哈希文件名，供模板长期缓存引用。

逻辑名 ``app.js`` → ``_h/app.<sha256前10位>.js``；内容变化则哈希变化，URL 自动更新。
"""

from __future__ import annotations

import fcntl
import logging
import os
import threading
from contextlib import contextmanager
from pathlib import Path

from content_digest import digest_bytes

log = logging.getLogger("static_assets")

HASH_LEN = 10
HASHED_DIR_NAME = "_h"
MANIFEST_FILES = ("style.css", "askai.css", "app.js", "highlights.js", "askai.js", "geo_stats.js")

# 子目录下的图片资源也纳入哈希管理，确保 CDN 缓存可自动更新
MANIFEST_IMG_FILES = (
    "logos/favicon.ico",
    "logos/favicon-32.png",
    "logos/favicon-16.png",
    "logos/apple-touch-icon.png",
    "logos/logo-nav.png",
)

_lock = threading.Lock()
_static_dir: Path | None = None
_manifest: dict[str, str] = {}
_source_mtimes: tuple[float, ...] = ()


def init(static_dir: Path) -> None:
    global _static_dir
    _static_dir = static_dir


def _read_source_mtimes(static_dir: Path) -> tuple[float, ...]:
    names = MANIFEST_FILES + MANIFEST_IMG_FILES
    return tuple(
        static_dir.joinpath(name).stat().st_mtime
        if static_dir.joinpath(name).is_file()
        else 0.0
        for name in names
    )


@contextmanager
def _refresh_file_lock(lock_path: Path):
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def refresh() -> dict[str, str]:
    """根据源文件内容重建哈希副本与 manifest。

    使用基于 ``_h/.refresh_lock`` 的 fcntl 文件锁，保证多 Gunicorn
    worker 同时启动时只有一个执行重建；其余 worker 进入后检测到已是最新
    版本则直接返回，避免重复工作与 stale unlink 竞态。
    """
    global _manifest, _source_mtimes

    if _static_dir is None:
        return {}
    static_dir = _static_dir
    hashed_dir = static_dir / HASHED_DIR_NAME
    hashed_dir.mkdir(parents=True, exist_ok=True)
    lock_path = hashed_dir / ".refresh_lock"

    with _refresh_file_lock(lock_path):
        mtimes_now = _read_source_mtimes(static_dir)
        if mtimes_now == _source_mtimes and _manifest:
            return dict(_manifest)

        manifest: dict[str, str] = {}
        active_names: set[str] = set()

        for name in MANIFEST_FILES + MANIFEST_IMG_FILES:
            src = static_dir / name
            if not src.is_file():
                log.warning("静态资源不存在，跳过: %s", src)
                continue
            data = src.read_bytes()
            digest = digest_bytes(data)
            if "." in name:
                stem, suffix = name.rsplit(".", 1)
            else:
                stem, suffix = name, ""
            # 将子目录路径扁平化：logos/favicon.ico -> logos_favicon
            stem = stem.replace("/", "_")
            hashed_name = f"{stem}.{digest}.{suffix}" if suffix else f"{stem}.{digest}"
            rel = f"{HASHED_DIR_NAME}/{hashed_name}"
            dst = static_dir / rel
            if not dst.exists() or dst.read_bytes() != data:
                dst.write_bytes(data)
                log.info("静态资源已哈希: %s -> %s", name, rel)
            manifest[name] = rel
            active_names.add(hashed_name)

        for stale in hashed_dir.iterdir():
            if not stale.is_file() or stale.name in active_names:
                continue
            if stale.name.startswith(".nfs") or stale.name == ".refresh_lock":
                continue
            try:
                stale.unlink(missing_ok=True)
            except OSError:
                pass
            else:
                log.info("已清理过期哈希静态资源: %s", stale.name)

        with _lock:
            _manifest = manifest
            _source_mtimes = mtimes_now
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
