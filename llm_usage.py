"""LLM Token 用量统计（按日持久化：输入 / 输出 / 缓存命中）。"""

from __future__ import annotations

import os
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Optional
from zoneinfo import ZoneInfo

import storage

ROOT = Path(__file__).resolve().parent
DATA_ROOT = ROOT / "data"
BJ_TZ = ZoneInfo("Asia/Shanghai")

_store = storage.Store(
    "llm_usage",
    data_dir=DATA_ROOT,
    backend=storage.resolve_backend(),
    sqlite_path=Path(os.environ.get("SQLITE_PATH") or (DATA_ROOT / storage.DEFAULT_SQLITE_NAME)),
)
_lock = threading.Lock()

_EMPTY_DAY: dict[str, int] = {
    "input": 0,
    "output": 0,
    "cached": 0,
    "total": 0,
    "calls": 0,
}


def parse_usage(usage: Optional[dict]) -> dict[str, int]:
    """从 OpenAI 兼容 usage 字段解析 token 数。"""
    if not usage or not isinstance(usage, dict):
        return dict(_EMPTY_DAY)

    inp = int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0)
    out = int(usage.get("completion_tokens") or usage.get("output_tokens") or 0)
    total = int(usage.get("total_tokens") or (inp + out))

    cached = 0
    details = usage.get("prompt_tokens_details")
    if isinstance(details, dict):
        cached = int(details.get("cached_tokens") or 0)
    cached = max(cached, int(usage.get("prompt_cache_hit_tokens") or 0))
    cached = max(cached, int(usage.get("cache_hit_tokens") or 0))

    return {
        "input": inp,
        "output": out,
        "cached": cached,
        "total": total,
        "calls": 0,
    }


def _normalize_day(day: dict) -> dict[str, int]:
    return {
        "input": int(day.get("input") or 0),
        "output": int(day.get("output") or 0),
        "cached": int(day.get("cached") or 0),
        "total": int(day.get("total") or 0),
        "calls": int(day.get("calls") or 0),
    }


def record_usage(usage: Optional[dict], *, purpose: str = "other") -> None:
    """累加一次 LLM 调用的 token 用量（purpose 仅作扩展预留）。"""
    parsed = parse_usage(usage)
    if parsed["total"] <= 0 and parsed["input"] <= 0 and parsed["output"] <= 0:
        return
    _ = purpose  # 预留分用途统计
    date_str = datetime.now(BJ_TZ).strftime("%Y-%m-%d")
    with _lock:
        day = _normalize_day(_store.get(date_str) or {})
        for key in ("input", "output", "cached", "total"):
            day[key] += parsed[key]
        day["calls"] += 1
        _store.put(date_str, day)


def get_snapshot() -> dict[str, dict[str, int]]:
    with _lock:
        return {k: _normalize_day(dict(v or {})) for k, v in _store.all().items()}


def build_stats() -> dict[str, Any]:
    """供 /stats 页面使用的 token 统计上下文。"""
    snapshot = get_snapshot()
    today_str = datetime.now(BJ_TZ).strftime("%Y-%m-%d")
    today = snapshot.get(today_str) or dict(_EMPTY_DAY)

    sorted_dates = sorted(snapshot.keys(), reverse=True)
    token_recent: list[dict] = []
    for d in sorted_dates[:30]:
        day = snapshot[d]
        token_recent.append({
            "date": d,
            "input": day["input"],
            "output": day["output"],
            "cached": day["cached"],
            "total": day["total"],
            "calls": day["calls"],
            "is_today": d == today_str,
        })
    token_recent.reverse()

    max_total = max((r["total"] for r in token_recent), default=0)
    for r in token_recent:
        r["pct"] = round(r["total"] / max_total * 100, 1) if max_total else 0

    grand_input = sum(r["input"] for r in token_recent)
    grand_output = sum(r["output"] for r in token_recent)
    grand_cached = sum(r["cached"] for r in token_recent)
    grand_total = sum(r["total"] for r in token_recent)
    day_count = len(token_recent)
    avg_total = round(grand_total / day_count, 1) if day_count else 0

    return {
        "token_today_input": today["input"],
        "token_today_output": today["output"],
        "token_today_cached": today["cached"],
        "token_today_total": today["total"],
        "token_today_calls": today["calls"],
        "token_recent": token_recent,
        "token_grand_total": grand_total,
        "token_grand_input": grand_input,
        "token_grand_output": grand_output,
        "token_grand_cached": grand_cached,
        "token_avg_per_day": avg_total,
        "token_day_count": day_count,
    }
