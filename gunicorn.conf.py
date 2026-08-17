"""Gunicorn 配置（生产默认 48 worker 进程）。"""

from __future__ import annotations

import os

bind = f"{os.environ.get('WEB_HOST', '127.0.0.1')}:{os.environ.get('WEB_PORT', '8080')}"
workers = int(os.environ.get("WEB_WORKERS", "48"))
worker_class = "gthread"
threads = int(os.environ.get("WEB_THREADS", "4"))
timeout = int(os.environ.get("WEB_TIMEOUT", "120"))
graceful_timeout = int(os.environ.get("WEB_GRACEFUL_TIMEOUT", "30"))
keepalive = int(os.environ.get("WEB_KEEPALIVE", "5"))
accesslog = "-"
errorlog = "-"
capture_output = True


def post_worker_init(worker):  # noqa: ARG001
    from web import init_web_worker

    init_web_worker()
