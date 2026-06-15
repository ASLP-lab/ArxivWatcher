#!/usr/bin/env python3
"""JSON ↔ SQLite 存储互转工具。

用法::

    # JSON 文件 -> SQLite（data/app.db）
    python storage_tool.py json2sqlite

    # SQLite -> JSON 文件
    python storage_tool.py sqlite2json

    # 仅转换部分集合
    python storage_tool.py json2sqlite --only interactions comments

    # 指定数据目录 / 数据库路径
    python storage_tool.py json2sqlite --data-dir ./data --db ./data/app.db

可转换的集合：interactions、comments、highlights、favorites、users、visits。
转换为"全量替换"语义：目标端对应集合会被源端数据覆盖。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import storage

ROOT = Path(__file__).resolve().parent
DEFAULT_DATA_DIR = ROOT / "data"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="JSON ↔ SQLite 存储互转")
    parser.add_argument(
        "direction",
        choices=["json2sqlite", "sqlite2json"],
        help="转换方向",
    )
    parser.add_argument(
        "--data-dir",
        default=str(DEFAULT_DATA_DIR),
        help="JSON 数据目录（默认 ./data）",
    )
    parser.add_argument(
        "--db",
        default=None,
        help=f"SQLite 数据库路径（默认 <data-dir>/{storage.DEFAULT_SQLITE_NAME}）",
    )
    parser.add_argument(
        "--only",
        nargs="*",
        default=None,
        metavar="NAME",
        help="仅转换指定集合（默认全部）",
    )
    args = parser.parse_args(argv)

    data_dir = Path(args.data_dir)
    db_path = Path(args.db) if args.db else data_dir / storage.DEFAULT_SQLITE_NAME

    names = args.only or list(storage.STORE_FILES.keys())
    unknown = [n for n in names if n not in storage.STORE_FILES]
    if unknown:
        parser.error(f"未知集合: {', '.join(unknown)}；可选: {', '.join(storage.STORE_FILES)}")

    if args.direction == "json2sqlite":
        src, dst = "json", "sqlite"
    else:
        src, dst = "sqlite", "json"

    print(f"开始转换: {src} -> {dst}")
    print(f"  数据目录: {data_dir}")
    print(f"  SQLite  : {db_path}")
    print(f"  集合    : {', '.join(names)}")

    stats = storage.convert(src, dst, data_dir=data_dir, sqlite_path=db_path, names=names)

    total = 0
    for name in names:
        cnt = stats.get(name, 0)
        total += cnt
        print(f"    - {name:<14} {cnt} 条")
    print(f"完成，共写入 {total} 条记录。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
