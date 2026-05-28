# ArxivWatcher

arXiv 论文每日自动监控与精读工具：抓取新论文、LLM 结构化解读、Web 浏览、RSS 订阅、Zotero 自动导入，可选邮件与飞书推送。

## 功能

- **自动抓取**：按 arXiv 分类抓取每日新论文（支持多分类合并去重）
- **LLM 解读**：OpenAI 兼容接口，对 PDF 全文做结构化分析
- **Web 浏览**：Flask 界面，按日期浏览、关键词搜索、访问统计
- **RSS 订阅**：`/rss` 配置页与 `/rss/feed.xml` 输出
- **Zotero 插件**：自动把每日报告 HTML 导入为本地网页快照
- **可选推送**：SMTP 邮件、飞书群机器人

## 环境

需要 Python ≥ 3.10，推荐 [uv](https://github.com/astral-sh/uv)：

```bash
uv sync
# 或: pip install -r requirements.txt
```

## 配置

通过环境变量或 `run.sh` / `start_web.sh` 中的注释示例配置：

| 变量 | 说明 |
|------|------|
| `LLM_API_KEY` | LLM API Key（必填，除非 `--no-llm`） |
| `LLM_BASE_URL` | OpenAI 兼容 API 地址 |
| `LLM_MODEL` | 模型名称 |
| `SMTP_*` / `EMAIL_TO` | 邮件（可选） |
| `FEISHU_WEBHOOK_URL` | 飞书机器人（可选） |
| `WEB_PUBLIC_URL` | 对外访问地址（插件 xpi、飞书链接） |
| `ARXIV_CATEGORIES` | 抓取分类，空格分隔，默认 `eess.AS cs.SD` |

## 启动

### Web 服务（含工作日定时任务）

```bash
bash start_web.sh
```

默认：`http://127.0.0.1:8091`

- RSS：`/rss`
- 报告下载：`/download/YYYY-MM-DD.html`
- 日期列表（Zotero）：`/api/dates`

### 手动抓取一次

```bash
export LLM_API_KEY="sk-..."
bash run.sh
```

或向已运行的 Web 服务发送：

```bash
curl -X POST http://127.0.0.1:8091/admin/run-now
```

### 从历史 HTML 重建索引

```bash
python scripts/build_index.py
```

## Zotero 插件

见 [`zotero_plugin/arxiv-daily-importer/README.md`](zotero_plugin/arxiv-daily-importer/README.md)。

部署后从 Web 页「Zotero」下载 xpi（会根据 `WEB_PUBLIC_URL` 写入 `update_url`）。本地调试默认连 `http://127.0.0.1:8091`。

## 外网访问（可选）

本仓库**不包含** [frp](https://github.com/fatedier/frp) 客户端。若需把内网 Web 暴露到公网，请自行：

1. 从 [frp Releases](https://github.com/fatedier/frp/releases) 下载对应平台二进制
2. 编写 `frpc.toml`，将本地 `8091` 映射到公网
3. 将 `WEB_PUBLIC_URL` 设为公网 HTTPS 地址（Zotero 9 要求插件 `update_url` 为 HTTPS）

也可用 Nginx 反向代理、Cloudflare Tunnel 等方案。

## 目录

| 路径 | 说明 |
|------|------|
| `send.py` | 抓取、解读、报告生成、推送 |
| `web.py` | Flask Web 与定时调度 |
| `arxiv_daily_digest.py` | 独立 CLI 版（无 Web） |
| `templates/` / `static/` | 前端 |
| `zotero_plugin/` | Zotero 7+ 插件源码 |
| `scripts/` | 工具脚本（索引重建、macOS launchd 示例） |
| `data/papers/` | 运行时论文索引（git 忽略） |
| `reports/` | 运行时 HTML 报告（git 忽略） |

## License

This project is licensed under the Creative Commons Attribution 4.0 International License (CC BY 4.0).

See [`LICENSE`](LICENSE) or <https://creativecommons.org/licenses/by/4.0/>.
