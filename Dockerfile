# =============================================================================
# ArxivWatcher — 多阶段 Dockerfile（国内友好，无 syntax 头、无外网 ADD）
#   阶段 1: builder  —— pip 在 /opt/venv 装好所有依赖
#   阶段 2: runtime  —— 精简运行镜像（非 root、Asia/Shanghai 时区）
# 注意：第一行不要写 "# syntax=..."，否则 BuildKit 会去拉 docker/dockerfile 镜像，
#       国内拉 Docker Hub 会超时。配好 daemon.json 镜像加速器即可。
# =============================================================================

ARG PYTHON_VERSION=3.12

# -----------------------------------------------------------------------------
# 阶段 1: builder
# -----------------------------------------------------------------------------
FROM python:${PYTHON_VERSION}-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    # 构建期走国内 pip 源，避免 PyPI 拉不动
    PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple \
    PIP_TRUSTED_HOST=pypi.tuna.tsinghua.edu.cn

WORKDIR /app

# 先只拷依赖清单，最大化利用 docker layer cache
COPY pyproject.toml requirements.txt ./

# 建 venv 并安装依赖；额外补 brotli/ldap3（pyproject 里有，requirements 没列全）
RUN python -m venv /opt/venv \
    && /opt/venv/bin/pip install --upgrade pip \
    && /opt/venv/bin/pip install -r requirements.txt \
    && /opt/venv/bin/pip install brotli ldap3 bcrypt markdown

# -----------------------------------------------------------------------------
# 阶段 2: runtime
# -----------------------------------------------------------------------------
FROM python:${PYTHON_VERSION}-slim AS runtime

LABEL org.opencontainers.image.title="ArxivWatcher" \
      org.opencontainers.image.version="2.1.0" \
      org.opencontainers.image.description="arXiv 每日论文监控与精读工具（Web + 定时调度）" \
      org.opencontainers.image.source="https://github.com/ASLP-lab/ArxivWatcher" \
      org.opencontainers.image.url="https://hub.docker.com/r/aslplab/arxivwatcher" \
      org.opencontainers.image.documentation="https://github.com/ASLP-lab/ArxivWatcher#readme" \
      org.opencontainers.image.vendor="ASLP-lab" \
      org.opencontainers.image.authors="ASLP-lab" \
      org.opencontainers.image.maintainer="ASLP-lab" \
      org.opencontainers.image.licenses="CC-BY-4.0"

# 运行期系统依赖：tzdata 时区数据；tini 作 PID 1 正确转发信号；curl 健康检查
RUN apt-get update && apt-get install -y --no-install-recommends \
        tzdata \
        tini \
        bash \
        curl \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && ln -snf /usr/share/zoneinfo/Asia/Shanghai /etc/localtime \
    && echo "Asia/Shanghai" > /etc/timezone

# 从 builder 拷贝已装好依赖的 venv
COPY --from=builder /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:${PATH}" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TZ=Asia/Shanghai \
    ARXIVWATCHER_CONFIG_DIR=/app/config

# 运行时默认环境变量（用户可覆盖）
ENV WEB_HOST=0.0.0.0 \
    WEB_PORT=8091 \
    WEB_WORKERS=48 \
    WEB_SERVER=gunicorn \
    # Docker 部署默认 SQLite（高并发更稳，避免每次写全量 JSON）
    STORAGE_BACKEND=sqlite \
    SQLITE_PATH=/app/data/app.db

# 非 root 用户运行
RUN groupadd -r app && useradd -r -g app -d /app -s /sbin/nologin app

WORKDIR /app

# 拷贝源代码（.dockerignore 会过滤掉 .venv/.git/data 等）
COPY --chown=app:app . /app

RUN chmod +x /app/run_web.sh

# 运行期可写目录：data / reports / logs / arxiv_digest_work
RUN mkdir -p /app/config /app/data /app/reports /app/logs /app/arxiv_digest_work \
    && chown -R app:app /app

USER app

EXPOSE 8091

# 健康检查：极简 204 探针
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD curl -fsS "http://127.0.0.1:${WEB_PORT}/health" || exit 1

# tini 负责正确转发 SIGTERM（gunicorn 会优雅退出）
ENTRYPOINT ["/usr/bin/tini", "--"]

# 默认启动 Web 服务；想跑单次抓取可覆盖为：python send.py --category eess.AS cs.SD --no-email
CMD ["./run_web.sh"]
