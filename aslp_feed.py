"""ASLP 实验室新闻 / 公告抓取模块。

每隔 1 小时从 ASLP 官网后端接口抓取一次新闻与公告，缓存到内存，
并对外提供查询接口（``get_items`` / ``last_updated_iso``）。

数据来源（与官网 ``/article/news``、``/article/notice`` 页面一致）::

    POST https://www.npu-aslp.org/api/get-item-list
    {"collection": "article", "need": {"type": "news"}, "page": 1, "numInOnePage": 50}
    {"collection": "article", "need": {"type": "notice"}, "page": 1, "numInOnePage": 50}

返回条目字段：``name``（标题）、``date``、``type``、``link``、``_id``。
"""

from __future__ import annotations

import json
import logging
import threading
import time
import urllib.request
from datetime import datetime, timezone
from typing import Optional

log = logging.getLogger("aslp_feed")

SITE_ORIGIN = "https://www.npu-aslp.org"
API_URL = f"{SITE_ORIGIN}/api/get-item-list"
DEFAULT_INTERVAL = 3600  # 每 1 小时刷新一次
FETCH_TIMEOUT = 20

_TYPE_LABELS = {"news": "新闻", "notice": "公告", "recommend": "推荐"}
_WANTED_TYPES = ("news", "notice")

_lock = threading.Lock()
_cache: list[dict] = []
_last_updated: Optional[datetime] = None
_thread: Optional[threading.Thread] = None
_started = False


def _post_item_list(body: dict) -> list[dict]:
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        API_URL,
        data=data,
        headers={"Content-Type": "application/json", "User-Agent": "ArxivWatcher/1.0"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT) as resp:
        raw = resp.read().decode("utf-8")
    items = json.loads(raw)
    return items if isinstance(items, list) else []


def _fetch_by_type(item_type: str, num: int = 50) -> list[dict]:
    body = {
        "collection": "article",
        "need": {"type": item_type},
        "page": 1,
        "numInOnePage": num,
    }
    return _post_item_list(body)


def _fetch_raw(num: int = 50) -> list[dict]:
    merged: list[dict] = []
    seen_ids: set[str] = set()
    for item_type in _WANTED_TYPES:
        try:
            batch = _fetch_by_type(item_type, num)
        except Exception as e:  # noqa: BLE001 - 单类失败不影响另一类
            log.warning("ASLP feed 抓取 %s 失败: %s", item_type, e)
            continue
        for item in batch:
            id_ = str(item.get("_id") or "")
            if id_ and id_ in seen_ids:
                continue
            if id_:
                seen_ids.add(id_)
            merged.append(item)
    return merged


def _normalize(item: dict) -> Optional[dict]:
    t = item.get("type")
    if t not in _WANTED_TYPES:
        return None
    title = (item.get("name") or "").strip()
    if not title:
        return None
    raw_date = str(item.get("date") or "")
    # 形如 2026-05-19T00:00:00.000Z，只取日期部分
    date = raw_date[:10] if len(raw_date) >= 10 else raw_date
    link = str(item.get("link") or "").strip()
    if not link and item.get("_id"):
        link = f"{SITE_ORIGIN}/article/{t}/{item['_id']}"
    return {
        "id": str(item.get("_id") or ""),
        "type": t,
        "type_label": _TYPE_LABELS.get(t, t),
        "title": title,
        "date": date,
        "link": link,
    }


def refresh() -> bool:
    """同步抓取一次并刷新缓存。成功返回 True。"""
    try:
        raw = _fetch_raw()
    except Exception as e:  # noqa: BLE001 - 网络异常不应影响主服务
        log.warning("ASLP feed 抓取失败: %s", e)
        return False

    items = [n for n in (_normalize(i) for i in raw) if n]
    # 按日期倒序（新的在前）
    items.sort(key=lambda x: x["date"], reverse=True)

    global _cache, _last_updated
    with _lock:
        _cache = items
        _last_updated = datetime.now(timezone.utc)
    log.info("ASLP feed 已更新，共 %d 条", len(items))
    return True


def get_items(limit: int = 5, types: Optional[tuple] = None) -> list[dict]:
    """查询接口：返回按日期倒序排列的新闻/公告。

    :param limit: 返回条数上限（<=0 表示全部）
    :param types: 仅返回指定类别，如 ``("news",)``；None 表示全部
    """
    with _lock:
        items = list(_cache)
    if types:
        items = [i for i in items if i["type"] in types]
    if limit and limit > 0:
        items = items[:limit]
    return items


def last_updated() -> Optional[datetime]:
    with _lock:
        return _last_updated


def last_updated_iso() -> Optional[str]:
    dt = last_updated()
    return dt.isoformat() if dt else None


def _loop(interval: int) -> None:
    while True:
        refresh()
        time.sleep(interval)


def start_background_refresh(interval: int = DEFAULT_INTERVAL) -> None:
    """启动后台定时刷新线程（幂等，多次调用只会启动一次）。

    线程内会先立即抓取一次，随后每 ``interval`` 秒刷新一次。
    """
    global _thread, _started
    with _lock:
        if _started:
            return
        _started = True
    _thread = threading.Thread(
        target=_loop, args=(interval,), daemon=True, name="aslp-feed-refresh"
    )
    _thread.start()
    log.info("ASLP feed 后台刷新已启动（间隔 %d 秒）", interval)


# 兼容惰性启动（开发 / 测试环境首次访问接口时启动）
ensure_started = start_background_refresh


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    ok = refresh()
    print(f"refresh ok={ok}, 共 {len(get_items(limit=0))} 条，最近 5 条：")
    for it in get_items(limit=5):
        print(f"  [{it['type_label']}] {it['date']}  {it['title']}")
