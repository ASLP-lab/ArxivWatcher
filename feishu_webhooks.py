"""飞书机器人 webhook 配置解析（支持单地址与多地址，保持向后兼容）。"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Iterable, Mapping


def parse_webhook_urls(*values: object) -> list[str]:
    """解析 JSON 数组或逗号/分号/空白分隔的 webhook 地址并去重。"""
    urls: list[str] = []
    seen: set[str] = set()

    def add(value: object) -> None:
        url = str(value or "").strip()
        if url and url not in seen:
            seen.add(url)
            urls.append(url)

    for value in values:
        if value is None:
            continue
        if isinstance(value, Iterable) and not isinstance(value, (str, bytes, bytearray)):
            for item in value:
                add(item)
            continue
        raw = str(value).strip()
        if not raw:
            continue
        if raw.startswith("["):
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError:
                parsed = None
            if isinstance(parsed, list):
                for item in parsed:
                    add(item)
                continue
        for item in re.split(r"[\s,;]+", raw):
            add(item)
    return urls


def configured_webhook_urls(environ: Mapping[str, str] | None = None) -> list[str]:
    """读取新旧环境变量；复数变量在前，重复地址只发送一次。"""
    env = os.environ if environ is None else environ
    return parse_webhook_urls(
        env.get("FEISHU_WEBHOOK_URLS", ""),
        env.get("FEISHU_WEBHOOK_URL", ""),
    )
