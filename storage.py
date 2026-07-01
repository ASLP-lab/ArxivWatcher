"""统一存储后端：JSON 文件或 SQLite，可通过环境变量 ``STORAGE_BACKEND`` 切换。

设计要点
--------
应用里的几类数据本质都是 "key(TEXT) -> JSON 值" 的映射：

    interactions : "日期/paper_id" -> {likes, dislikes, liked_by, disliked_by}
    comments     : "日期/paper_id" -> [comment, ...]
    highlights   : username        -> {paper_key: [highlight, ...]}
    favorites    : username        -> {folders: [...], items: {...}}
    reading_list : identity        -> {items: {date/paper_id: {...}}}
    users        : username        -> {password_hash, created_at}
    visits       : 日期            -> {total, hourly, users, tabs, ...}

``Store`` 用内存缓存承载全量数据（读快），写操作只持久化"单个 key"：

    - SQLite 后端：行级 ``INSERT OR REPLACE`` / ``DELETE``，访问量大时不再整文件重写；
    - JSON 后端：与原行为一致（原子地整文件重写）。

两种后端可通过 ``storage_tool.py`` 互相转换。
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from pathlib import Path
from typing import Any, Optional

# 各存储集合名 -> 默认 JSON 文件名（SQLite 中作为表名）
STORE_FILES: dict[str, str] = {
    "interactions": "interactions.json",
    "comments": "comments.json",
    "highlights": "highlights.json",
    "favorites": "favorites.json",
    "reading_list": "reading_list.json",
    "users": "users.json",
    "visits": "visits.json",
    "arxiv_versions": "arxiv_versions.json",
}

DEFAULT_SQLITE_NAME = "app.db"


def resolve_backend(explicit: Optional[str] = None) -> str:
    """决定使用的后端：显式参数 > 环境变量 STORAGE_BACKEND > 默认 json。"""
    val = (explicit or os.environ.get("STORAGE_BACKEND") or "json").strip().lower()
    return "sqlite" if val in ("sqlite", "sqlite3", "db") else "json"


# ─────────────────────────────────────────────
# 后端实现
# ─────────────────────────────────────────────

class _JsonBackend:
    def __init__(self, path: Path):
        self.path = Path(path)

    def load(self) -> dict:
        if not self.path.exists():
            return {}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def flush(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = Path(str(self.path) + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.path)


class _SqliteBackend:
    """每个 Store 对应 SQLite 中的一张表 (k TEXT PRIMARY KEY, v TEXT)。"""

    # 同一个 db 文件共享一个连接（按 db 路径缓存）
    _conns: dict[str, sqlite3.Connection] = {}
    _conns_lock = threading.Lock()

    def __init__(self, db_path: Path, table: str):
        self.db_path = Path(db_path)
        self.table = table
        self.conn = self._get_conn(self.db_path)
        with self.conn:
            self.conn.execute(
                f'CREATE TABLE IF NOT EXISTS "{table}" (k TEXT PRIMARY KEY, v TEXT NOT NULL)'
            )

    @classmethod
    def _get_conn(cls, db_path: Path) -> sqlite3.Connection:
        key = str(db_path.resolve())
        with cls._conns_lock:
            conn = cls._conns.get(key)
            if conn is None:
                db_path.parent.mkdir(parents=True, exist_ok=True)
                conn = sqlite3.connect(str(db_path), check_same_thread=False, isolation_level=None)
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("PRAGMA synchronous=NORMAL")
                cls._conns[key] = conn
            return conn

    def load(self) -> dict:
        out: dict = {}
        cur = self.conn.execute(f'SELECT k, v FROM "{self.table}"')
        for k, v in cur.fetchall():
            try:
                out[k] = json.loads(v)
            except Exception:
                continue
        return out

    def put(self, key: str, value: Any) -> None:
        self.conn.execute(
            f'INSERT OR REPLACE INTO "{self.table}" (k, v) VALUES (?, ?)',
            (key, json.dumps(value, ensure_ascii=False)),
        )

    def delete(self, key: str) -> None:
        self.conn.execute(f'DELETE FROM "{self.table}" WHERE k = ?', (key,))

    def replace_all(self, data: dict) -> None:
        with self.conn:
            self.conn.execute(f'DELETE FROM "{self.table}"')
            self.conn.executemany(
                f'INSERT OR REPLACE INTO "{self.table}" (k, v) VALUES (?, ?)',
                [(k, json.dumps(v, ensure_ascii=False)) for k, v in data.items()],
            )


# ─────────────────────────────────────────────
# Store：对外统一接口
# ─────────────────────────────────────────────

class Store:
    """一个 key -> JSON 值 的集合，带内存缓存，写操作只持久化单个 key。"""

    def __init__(self, name: str, *, data_dir: Path, backend: str = "json",
                 sqlite_path: Optional[Path] = None):
        self.name = name
        self.backend = backend
        self._lock = threading.RLock()
        self._cache: Optional[dict] = None
        json_file = STORE_FILES.get(name, f"{name}.json")
        if backend == "sqlite":
            db = Path(sqlite_path) if sqlite_path else Path(data_dir) / DEFAULT_SQLITE_NAME
            self._sql: Optional[_SqliteBackend] = _SqliteBackend(db, name)
            self._json: Optional[_JsonBackend] = None
        else:
            self._sql = None
            self._json = _JsonBackend(Path(data_dir) / json_file)

    def _ensure(self) -> None:
        if self._cache is None:
            self._cache = self._sql.load() if self._sql else self._json.load()

    def all(self) -> dict:
        """返回内存缓存的全量 dict（同一对象，可原地修改后再调用 put 持久化）。"""
        with self._lock:
            self._ensure()
            return self._cache  # type: ignore[return-value]

    def get(self, key: str, default: Any = None) -> Any:
        with self._lock:
            self._ensure()
            return self._cache.get(key, default)  # type: ignore[union-attr]

    def put(self, key: str, value: Any) -> None:
        with self._lock:
            self._ensure()
            self._cache[key] = value  # type: ignore[index]
            if self._sql:
                self._sql.put(key, value)
            else:
                self._json.flush(self._cache)  # type: ignore[arg-type]

    def delete(self, key: str) -> None:
        with self._lock:
            self._ensure()
            self._cache.pop(key, None)  # type: ignore[union-attr]
            if self._sql:
                self._sql.delete(key)
            else:
                self._json.flush(self._cache)  # type: ignore[arg-type]

    def replace(self, data: dict) -> None:
        """整体替换（用于数据导入/转换）。"""
        with self._lock:
            self._cache = dict(data)
            if self._sql:
                self._sql.replace_all(self._cache)
            else:
                self._json.flush(self._cache)


def make_store(name: str, data_dir: Path, backend: Optional[str] = None,
               sqlite_path: Optional[Path] = None) -> Store:
    return Store(name, data_dir=data_dir, backend=resolve_backend(backend), sqlite_path=sqlite_path)


# ─────────────────────────────────────────────
# 互转工具（供 storage_tool.py 调用）
# ─────────────────────────────────────────────

def _read_all(backend: str, name: str, data_dir: Path, sqlite_path: Path) -> dict:
    if backend == "sqlite":
        return _SqliteBackend(sqlite_path, name).load()
    return _JsonBackend(Path(data_dir) / STORE_FILES.get(name, f"{name}.json")).load()


def convert(src: str, dst: str, data_dir: Path, sqlite_path: Optional[Path] = None,
            names: Optional[list[str]] = None) -> dict[str, int]:
    """在 json 与 sqlite 之间转换全部（或指定）存储集合。

    返回每个集合写入的 key 数量。
    """
    src = resolve_backend(src)
    dst = resolve_backend(dst)
    if src == dst:
        raise ValueError("源与目标后端相同，无需转换")
    data_dir = Path(data_dir)
    sqlite_path = Path(sqlite_path) if sqlite_path else data_dir / DEFAULT_SQLITE_NAME
    names = names or list(STORE_FILES.keys())

    stats: dict[str, int] = {}
    for name in names:
        data = _read_all(src, name, data_dir, sqlite_path)
        if dst == "sqlite":
            _SqliteBackend(sqlite_path, name).replace_all(data)
        else:
            _JsonBackend(data_dir / STORE_FILES.get(name, f"{name}.json")).flush(data)
        stats[name] = len(data)
    return stats
