<div align="center">

<img src="static/logos/landscape_arxiv_watcher.png" alt="ArxivWatcher" width="560">

**arXiv 论文每日自动监控与精读工具**

抓取新论文 · LLM 结构化解读 · Web 浏览 · RSS 订阅 · Zotero 自动导入 · 可选邮件 / 飞书推送

v2.1.0

<!-- 徽章占位：按需替换为真实地址 -->
<!--
[![License: CC BY 4.0](https://img.shields.io/badge/License-CC%20BY%204.0-lightgrey.svg)](https://creativecommons.org/licenses/by/4.0/)
[![Docker](https://img.shields.io/badge/docker-aslplab%2Farxivwatcher-blue)](https://hub.docker.com/r/aslplab/arxivwatcher)
[![Python](https://img.shields.io/badge/python-%E2%89%A53.10-blue)](https://www.python.org/)
-->

<!-- 截图占位：首页 / 论文卡片列表总览图。建议放一张能体现整体效果的大图。 -->
<!-- ![首页总览](docs/images/overview.png) -->

</div>

---

## 📖 目录

- [这是什么](#-这是什么)
- [功能特性](#-功能特性)
- [界面预览](#-界面预览)
- [快速上手](#-快速上手)
  - [方式一：Docker 一键运行（推荐）](#方式一docker-一键运行推荐)
  - [方式二：本地源码运行](#方式二本地源码运行)
- [核心配置速查](#-核心配置速查)
- [本地名单配置](#-本地名单配置)
- [访问节点选择](#-访问节点选择)
- [运行与抓取](#-运行与抓取)
- [Docker 部署详解](#-docker-部署详解)
- [存储后端（JSON / SQLite）](#-存储后端json--sqlite)
- [Zotero 插件](#-zotero-插件)
- [外网访问](#-外网访问可选)
- [常见问题 FAQ](#-常见问题-faq)
- [项目结构](#-项目结构)
- [License](#-license)

---

## 🎯 这是什么

ArxivWatcher 帮你把"每天刷 arXiv"这件事自动化：

1. **按分类定时抓取** arXiv 当日新论文；
2. 调用 **LLM 对 PDF 全文做结构化精读**，生成中文解读；
3. 在 **Web 界面**里按日期浏览、搜索、点赞评论收藏、做高亮标记；
4. 可选地把结果推送到 **邮件 / 飞书**，或自动导入 **Zotero**、输出 **RSS**。

适合实验室、课题组或个人长期跟踪某几个研究方向（默认 `eess.AS cs.SD`，语音相关）。

---

## ✨ 功能特性

| 模块 | 能力 |
|------|------|
| **自动抓取** | 按 arXiv 分类抓取每日新论文，支持多分类合并去重 |
| **LLM 解读** | OpenAI 兼容接口，对 PDF 全文做结构化分析 |
| **Web 浏览** | Flask 界面：按日期浏览、关键词搜索、访问统计；论文卡片动态渲染，支持按 **点赞数 / 评论数 / 原始序号** 排序，一键展开/折叠全部解读 |
| **互动与收藏** | 注册登录后可点赞/点踩、评论、收藏（可分文件夹），并在深度解读中做 **Zotero 同款多色标记**；提供「我的收藏」「我的标记」页面 |
| **实验室动态** | 每小时抓取 [ASLP 实验室](http://www.npu-aslp.org) 新闻/公告并缓存，论文页顶部展示最近 5 条 |
| **RSS 订阅** | `/rss` 配置页与 `/rss/feed.xml` 输出 |
| **Zotero 插件** | 自动把每日报告 HTML 导入为本地网页快照 |
| **可插拔存储** | 互动/评论/标记/收藏/用户/访问数据支持 **JSON 文件或 SQLite** 后端，附带双向转换工具 |
| **可选推送** | SMTP 邮件、飞书群机器人 |

---

## 🖼️ 界面预览

点击访问 [在线Demo](https://arxiv.npu-aslp.org/)

## 🚀 快速上手

需要 **Python ≥ 3.10**。下面两种方式任选其一。

### 方式一：Docker 一键运行（推荐）

```bash
mkdir -p data reports logs

docker run -d --name arxivwatcher \
  -p 8091:8091 \
  -v "$PWD/data:/app/data" \
  -v "$PWD/reports:/app/reports" \
  -v "$PWD/logs:/app/logs" \
  -e ADMIN_TOKEN="$(openssl rand -hex 32)" \
  --restart unless-stopped \
  aslplab/arxivwatcher:latest
```

打开 <http://127.0.0.1:8091> 即可访问。

> 💡 想启用「每日定时抓取 + LLM 解读」，再补上 `LLM_API_KEY` 等变量，详见 [Docker 部署详解](#-docker-部署详解)。

### 方式二：本地源码运行

推荐用 [uv](https://github.com/astral-sh/uv) 管理依赖：

```bash
uv sync
# 或：pip install -r requirements.txt
```

启动 Web 服务（含工作日定时任务）：

```bash
bash start_web.sh
```

默认地址 `http://127.0.0.1:8091`，几个常用入口：

- RSS 配置页：`/rss`
- 报告下载：`/download/YYYY-MM-DD.html`
- 日期列表（供 Zotero 用）：`/api/dates`

---

## ⚙️ 核心配置速查

所有配置均通过**环境变量**传入。下表是最常用的几个，**完整清单**见 [环境变量完整说明](#环境变量完整说明)。

| 变量 | 说明 |
|------|------|
| `LLM_API_KEY` | LLM API Key（必填，除非 `--no-llm`） |
| `LLM_BASE_URL` | OpenAI 兼容 API 地址 |
| `LLM_MODEL` | 模型名称 |
| `ARXIV_CATEGORIES` | 抓取分类，空格分隔，默认 `eess.AS cs.SD` |
| `ADMIN_TOKEN` | `/admin/*` 管理接口的 Bearer token |
| `STORAGE_BACKEND` | 存储后端：`json`（默认）/ `sqlite`（Docker 默认） |
| `SQLITE_PATH` | SQLite 数据库路径，默认 `data/app.db` |
| `SMTP_*` / `EMAIL_TO` | 邮件推送（可选） |
| `FEISHU_WEBHOOK_URL` | 飞书机器人（可选） |
| `WEB_PUBLIC_URL` | 对外访问地址（用于插件 xpi、飞书链接） |
| `ARXIVWATCHER_CONFIG_DIR` | 本地配置目录，默认 `./config`（Docker 为 `/app/config`） |

> 本地运行时也可直接在 `run.sh` / `start_web.sh` 的注释示例里改。

---

## 🗂️ 本地名单配置

本地配置统一放在 `config/` 目录。首次使用可从同目录的 `*.example.*` 复制；实际配置可能反映个人关注方向或包含密钥，因此已加入 `.gitignore` 和 `.dockerignore`。文件不存在或内容为空时，对应功能会自动停用，不影响其他功能。

### `config/blacklist.txt`：论文语义黑名单

一行写一条**自然语言描述**，空行以及以 `#` 开头的注释会被忽略。例如：

```text
# 不希望重点阅读的工作
只做说话人识别数据集上的常规增量改进，且没有新的方法贡献
主要研究语音情感识别，与语音生成或理解无关
仅发布 benchmark 或排行榜，没有实质算法创新
```

这不是标题关键词的硬过滤。程序会把这些描述交给第二阶段 LLM，让模型结合标题、摘要和深度解读进行语义判断。命中后论文仍会保留在报告中，但会标记为“黑名单 / 💤 可跳过”，Web 页面可隐藏黑名单论文。使用 `--no-llm` 或未配置 LLM 时不会执行该判断。

修改后在下一次论文抓取或重新分类时生效，无需改代码。

### `config/highlight_authors.txt`：Web 重点作者高亮

一行一个作者姓名，支持空行和以 `#` 开头的注释：

```text
# 页面中需要醒目标出的作者
Geoffrey Hinton
Yoshua Bengio
```

Web 前端会对作者名做大小写、句点、逗号和空格归一化，也支持完整姓名、名字首字母缩写及包含匹配。命中只改变页面上的作者高亮样式，不会额外抓取论文。文件按修改时间自动重新加载；修改后刷新浏览器页面即可看到结果。

### `config/featured_authors.txt`：主动追踪作者论文

一行一个 arXiv 作者姓名，可在行尾使用 `#` 添加注释：

```text
Geoffrey Hinton
Yoshua Bengio  # 深度学习
```

每日任务除了抓取配置的 arXiv 分类，还会查询名单中作者最近 5 天的论文，并在本地核验完整作者姓名。命中的论文会进入“大佬论文”区域；最近 10 天已经成功解读过的同一 arXiv 论文不会重复处理。姓名匹配忽略大小写、标点、连字符，并兼容 `姓, 名` 顺序。

名单过长会增加 arXiv 请求和 LLM 分析数量；程序默认在不同作者请求之间等待 3 秒。

### 创建本地配置

按需复制示例，不使用的功能无需创建：

```bash
cp config/blacklist.example.txt config/blacklist.txt
cp config/highlight_authors.example.txt config/highlight_authors.txt
cp config/featured_authors.example.txt config/featured_authors.txt
cp config/llm_config.example.sh config/llm_config.sh
```

Docker 不会把实际配置打进镜像。`docker-compose.yml` 已将整个目录只读挂载；使用 `docker run` 时增加一个 volume 即可：

```bash
-v "$PWD/config:/app/config:ro"
```

如需把配置放到其他位置，可设置 `ARXIVWATCHER_CONFIG_DIR`。升级旧部署时，根目录的同名文件仍可兼容读取，但建议迁入 `config/`。

---

## 🚦 访问节点选择

访问 `/select` 可展示多个可选访问节点。页面不发起测速请求，用户可以根据当前所处网络直接选择校园网、内网或公网节点。

先复制示例配置：

```bash
cp config/select_sites.example.txt config/select_sites.txt
```

`config/select_sites.txt` 每行填写一个节点，支持 `显示名称 | URL` 或纯 URL；空行及以 `#` 开头的行会被忽略：

```text
# 名称 | 访问地址
主站 | https://arxiv.example.com/
备用站 | http://arxiv-backup.example.com/
https://another.example.com/
```

仅接受 `http://` 和 `https://` 地址，最多读取 50 个去重后的节点。修改文件后刷新 `/select` 即可，无需重启服务。

实际配置可能包含内部站点，因此 `config/select_sites.txt` 已被 Git 和 Docker 构建上下文忽略。Docker 使用上文统一的 `./config:/app/config:ro` 挂载，无需再单独挂载文件。

> 安全提示：公开部署时只应配置你信任的节点；内网地址只会显示为链接，不会由服务端主动请求。

---

## 🔧 运行与抓取

### 手动抓取一次

```bash
export LLM_API_KEY="sk-..."
bash run.sh
```

### 让已运行的 Web 服务立即抓取

```bash
curl -X POST http://127.0.0.1:8091/admin/run-now \
  -H "Authorization: Bearer $ADMIN_TOKEN"
```

### 从历史 HTML 重建索引

```bash
python scripts/build_index.py
```

---

## 🐳 Docker 部署详解

镜像基于 `python:3.12-slim`，多阶段构建、非 root 用户运行，默认监听 `8091`，内置 `Asia/Shanghai` 时区与 `tini` 作为 PID 1。

### 镜像里的默认约定

| 项 | 值 | 说明 |
|------|------|------|
| 监听地址 | `0.0.0.0:8091` | 容器内必须 `0.0.0.0`，端口映射 `-p 宿主端口:8091` |
| 工作目录 | `/app` | 源代码、`run.sh`、`web.py` 都在这里 |
| 运行用户 | `app`（非 root） | UID/GID 由系统分配，挂载的宿主目录要可被写入 |
| 时区 | `Asia/Shanghai` | 定时任务按北京时间触发 |
| 存储后端 | `sqlite`（`/app/data/app.db`） | Docker 默认 SQLite；想用 JSON 文件设 `-e STORAGE_BACKEND=json` |
| 默认 CMD | `gunicorn -c gunicorn.conf.py web:app` | 启动 Web 服务（48 worker，含工作日 10:00 定时调度） |
| ENTRYPOINT | `tini --` | 正确转发信号，gunicorn 可优雅退出 |

### docker compose 一键启动

仓库已附 `docker-compose.yml`，按需修改其中的环境变量后：

```bash
docker compose up -d                          # 启动
docker compose logs -f                        # 看日志
docker compose down                           # 停止
docker compose pull && docker compose up -d   # 升级
```

`data/`、`reports/`、`logs/` 通过 volume 挂载持久化，重建容器不丢数据。

### 本地构建镜像

```bash
docker build -t aslplab/arxivwatcher:latest .
docker build -t aslplab/arxivwatcher:2.1.0 .   # 带版本号
```

> **国内构建提示**：若拉基础镜像超时，请先配 Docker 镜像加速器：
> ```bash
> sudo tee /etc/docker/daemon.json > /dev/null <<'EOF'
> {
>   "registry-mirrors": [
>     "https://docker.m.daocloud.io",
>     "https://docker.nju.edu.cn",
>     "https://docker.mirrors.ustc.edu.cn"
>   ]
> }
> EOF
> sudo systemctl restart docker
> ```
> Dockerfile 构建期 pip 已默认走清华源，无需额外处理。

### 推送到 Docker Hub

```bash
# 1) 登录（首次）
docker login

# 2) 单架构推送
docker push aslplab/arxivwatcher:latest
docker push aslplab/arxivwatcher:2.1.0

# 3) 多架构（amd64 + arm64）一次性构建并推送，需要 buildx：
docker buildx create --use --name multiarch
docker buildx build \
  --platform linux/amd64,linux/arm64 \
  -t aslplab/arxivwatcher:latest \
  -t aslplab/arxivwatcher:2.1.0 \
  --push .
```

### 可挂载的目录与端口

| 路径 / 端口 | 是否必须挂载 | 说明 |
|------|------|------|
| `/app/data` | ✅ 强烈建议 | 用户/互动/收藏/评论/访问统计（JSON 或 SQLite）、`.secret_key`、`papers/*.json` 索引 |
| `/app/reports` | ✅ 强烈建议 | 生成的每日 HTML 报告 |
| `/app/logs` | 可选 | 运行日志 |
| `/app/arxiv_digest_work` | 可选 | 抓取中间产物（PDF 缓存等） |
| `8091` | — | 容器内 Web 端口，固定，用 `-p 宿主端口:8091` 映射 |

> ⚠️ **`.secret_key` 必须持久化**：它是 Flask session 的加密密钥，丢了所有用户会掉登录、session 失效。务必把 `/app/data` 挂出来。

### 环境变量完整说明

所有配置通过环境变量传入。下表按用途分组，**默认值**列里 `（Dockerfile）` 表示镜像内已硬编码默认，`（代码）` 表示 Python 代码里的兜底值。

#### Web 服务 & 通用

| 变量 | 默认值 | 说明 |
|------|------|------|
| `WEB_HOST` | `0.0.0.0`（Dockerfile） | Web 监听地址。容器内**必须** `0.0.0.0`，否则端口映射不通 |
| `WEB_PORT` | `8091`（Dockerfile） | Web 监听端口。容器内建议保持 `8091`，宿主端口用 `-p` 映射 |
| `WEB_SERVER` | `gunicorn`（Dockerfile） | WSGI 服务：`gunicorn`（默认）或 `waitress`（开发单进程） |
| `WEB_WORKERS` | `48`（Dockerfile） | Gunicorn worker 进程数 |
| `WEB_THREADS` | `8` | 仅 `WEB_SERVER=waitress` 时生效，Waitress 工作线程数 |
| `TZ` | `Asia/Shanghai`（Dockerfile） | 时区。定时任务按此触发 |
| `WEB_PUBLIC_URL` | 空 | 对外暴露的访问地址。用于飞书消息里的链接、Zotero 插件 `update_url`（须 HTTPS）。例：`https://arxiv.example.com` |
| `ICP_BEIAN` | 空 | ICP 备案号，设置后页脚显示备案链接，留空则不显示。例：`陕ICP备XXXXX号-1` |
| `ZOTERO_PLUGIN_UPDATE_URL` | 空 | 显式覆盖 Zotero 插件的 `update_url`；不设则用 `WEB_PUBLIC_URL` |

#### 访客来源地图（可选）

首页底部的"访客来源"统计基于 GeoLite2 本地数据库（`data/GeoLite2-City.mmdb`，缺失时自动从同目录 `.mmdb.gz` 解压）按天落库，展示最近 7 天。真实 IP 从 CDN 回源头 `Ali-Cdn-Real-Ip` 提取；地图使用高德 JS API 2.0 世界简易行政区图层。

| 变量 | 默认值 | 说明 |
|------|------|------|
| `AMAP_JS_KEY` | 空 | 高德开放平台 Web 端（JS API）key。**留空则只显示来源排行榜、不加载地图** |
| `AMAP_JS_SECURITY_CODE` | 空 | 高德 key 对应的安全密钥（securityJsCode），2021 年后创建的 key 必填 |

> 注意：世界地图（`DistrictLayer.World`）属于高德高级能力，需在高德开放平台为该 key 申请世界地图权限；
> 页面会显示高德底图审图号，请勿自行修改国界/海界。无 key 时功能完全降级为榜单展示，不影响其他功能。

##### 获取 GeoLite2 资源文件

GeoLite2 数据库受 MaxMind 许可约束，因此不随源码或 Docker 镜像分发。启用 IP 地理统计时：

1. 注册免费的 [MaxMind GeoLite 账号](https://dev.maxmind.com/geoip/geolite2-free-geolocation-data/) 并在账号后台生成 license key；
2. 下载 **GeoLite2 City** 的二进制 `MMDB` 版本（不是 CSV 版本）；
3. 解压后将数据库重命名/复制为 `data/GeoLite2-City.mmdb`。也可以将 gzip 压缩文件放为 `data/GeoLite2-City.mmdb.gz`，程序首次使用时会自动解压；
4. Docker 部署需把该文件放在宿主机的 `./data/` 中，再按示例挂载到 `/app/data`。不要把数据库、MaxMind 账号或 license key 提交到 Git。

MaxMind 要求 GeoLite 数据保持更新，建议按其[官方更新说明](https://dev.maxmind.com/geoip/updating-databases/)定期替换文件。缺少该文件只会停用 IP 地理定位，不影响论文抓取和 Web 主功能。

##### 其他资源文件

- `speech_audio_taxonomy.json` 与 `universities_companies_levels.jsonl` 已随仓库提供，无需额外下载；
- `config/featured_authors.txt` 是可选的私人关注名单，按 `config/featured_authors.example.txt` 创建；实际文件已被 Git 和 Docker 构建上下文忽略；
- 页面 Logo/Favicon 位于 `static/logos/`，已随仓库提供。

#### 存储

| 变量 | 默认值 | 说明 |
|------|------|------|
| `STORAGE_BACKEND` | `sqlite`（Dockerfile） | 存储后端：`sqlite`（Docker 默认，高并发推荐）或 `json` |
| `SQLITE_PATH` | `/app/data/app.db`（Dockerfile） | SQLite 数据库路径，仅 `STORAGE_BACKEND=sqlite` 时生效 |

> Docker 镜像默认就用 SQLite。如果想退回 JSON 文件后端，设 `-e STORAGE_BACKEND=json`。
> 已有 JSON 数据要迁到 SQLite 时，先在容器里跑一次迁移：
> ```bash
> docker exec -it arxivwatcher python storage_tool.py json2sqlite
> docker compose restart
> ```

#### 认证 & 用户

| 变量 | 默认值 | 说明 |
|------|------|------|
| `AUTH_METHODS` | `local`（代码） | 认证方式（按优先级，逗号分隔）：`local`、`ldap`、`local,ldap`、`ldap,local` |
| `ALLOW_REGISTER` | `true`（代码） | 是否开放注册（`true`/`false`），仅对本地账号有效。生产建议 `false` |
| `ADMIN_TOKEN` | 空（每次启动随机） | `/admin/*` 管理接口的 Bearer token。**强烈建议显式设为长随机串** |

#### LDAP（当 `AUTH_METHODS` 含 `ldap` 时）

| 变量 | 默认值 | 说明 |
|------|------|------|
| `LDAP_URI` | 空 | LDAP 服务地址，如 `ldap://ldap.example.com` |
| `LDAP_URLS` | 空 | 多地址 failover（逗号分隔）。设置后**忽略** `LDAP_URI`。例：`ldap://a,ldap://b` |
| `LDAP_START_TLS` | `false` | 是否启用 STARTTLS（`true`/`false`） |
| `LDAP_USER_DN_TEMPLATE` | 空 | **方式一·直接绑定**：已知 DN 规则时使用，如 `uid={username},ou=people,dc=example,dc=com` |
| `LDAP_BASE_DN` | 空 | **方式二·搜索绑定**：搜索的 base DN，如 `dc=ldapdomain,dc=com` |
| `LDAP_USER_FILTER` | `(uid={username})` | 搜索绑定的用户过滤器 |
| `LDAP_BIND_DN` | 空 | 搜索绑定的服务账号 DN，如 `cn=admin,dc=ldapdomain,dc=com` |
| `LDAP_BIND_PASSWORD` | 空 | 搜索绑定的服务账号密码 |

> 方式一和方式二二选一：配了 `LDAP_USER_DN_TEMPLATE` 走直接绑定，否则走搜索绑定。

#### 定时任务（Web 服务内置，工作日触发）

| 变量 | 默认值 | 说明 |
|------|------|------|
| `DAILY_HOUR` | `10` | 每日触发的小时（24h 制，按 `TZ` 时区） |
| `DAILY_MINUTE` | `0` | 每日触发的分钟 |
| `ARXIV_CHECK_CATEGORIES` | `eess.AS cs.SD` | 开跑前要检查是否已更新到今天的 arXiv 分类（空格分隔） |
| `RUN_SCRIPT` | `/app/run.sh` | 每日触发时执行的脚本。容器内固定为 `/app/run.sh` |

> 触发时间到了会调用 `RUN_SCRIPT`，脚本里读取下面的 LLM/邮件/飞书变量完成抓取和推送。
> 也可立即手动触发：`curl -X POST http://127.0.0.1:8091/admin/run-now -H "Authorization: Bearer $ADMIN_TOKEN"`

#### LLM（抓取解读时需要）

定时任务或单次抓取时，`run.sh` 会读取这些变量调 LLM 做结构化解读。

| 变量 | 默认值 | 说明 |
|------|------|------|
| `LLM_BASE_URL` | `https://api.openai.com/v1` | OpenAI 兼容 API 地址。可换成 DeepSeek、通义、本地 Ollama 等 |
| `LLM_MODEL` | `gpt-4o` | 模型名称。例：`deepseek-chat`、`qwen-max`、`qwen2.5:72b`、`glm-5.1` |
| `LLM_API_KEY` | 空 | API Key（必填，除非 `run.sh` 里加 `--no-llm`） |
| `LLM_MAX_TOKENS` | 代码默认 | 单次请求最大 token 数 |

#### 邮件推送（可选）

| 变量 | 默认值 | 说明 |
|------|------|------|
| `SMTP_HOST` | `smtp.gmail.com` | SMTP 服务器地址 |
| `SMTP_PORT` | `587` | SMTP 端口 |
| `SMTP_USER` | 空 | 发件邮箱账号 |
| `SMTP_PASS` | 空 | 发件邮箱密码或应用专用密码 |
| `EMAIL_TO` | 空 | 收件人地址（多个用逗号分隔） |

> 容器默认 `run.sh` 带 `--no-email`，要启用邮件需去掉该参数或自写脚本。

#### 飞书推送（可选）

| 变量 | 默认值 | 说明 |
|------|------|------|
| `FEISHU_WEBHOOK_URL` | 空 | 飞书群机器人 webhook。设置后每日抓取完成会推送一条汇总消息 |

#### 缓存策略（可选，一般不用动）

| 变量 | 默认值 | 说明 |
|------|------|------|
| `STATIC_CACHE_SECONDS` | `31536000`（1 年） | `/static/_h/*` 哈希静态资源的浏览器与 CDN 缓存秒数 |
| `IMMUTABLE_CACHE_SECONDS` | `86400`（1 天） | 带 ETag 的不可变内容浏览器缓存秒数 |
| `IMMUTABLE_SMAXAGE_SECONDS` | `604800`（7 天） | 同上内容的 CDN（s-maxage）缓存秒数 |

#### 代理（可选）

| 变量 | 默认值 | 说明 |
|------|------|------|
| `http_proxy` / `https_proxy` | 空 | HTTP/HTTPS 代理。容器需访问外网 arXiv / LLM 但走代理时可设 |

### 常见启动场景

<details>
<summary><b>场景 1：最简版（只看 Web，不跑定时任务，本地账号）</b></summary>

```bash
docker run -d --name arxivwatcher \
  -p 8091:8091 \
  -v "$PWD/data:/app/data" \
  -v "$PWD/reports:/app/reports" \
  -e ADMIN_TOKEN="$(openssl rand -hex 32)" \
  -e AUTH_METHODS=local \
  -e ALLOW_REGISTER=true \
  --restart unless-stopped \
  aslplab/arxivwatcher:latest
```
</details>

<details>
<summary><b>场景 2：完整生产（本地+LDAP、SQLite、定时抓取+飞书推送）</b></summary>

```bash
docker run -d --name arxivwatcher \
  -p 8091:8091 \
  -v "$PWD/data:/app/data" \
  -v "$PWD/reports:/app/reports" \
  -v "$PWD/logs:/app/logs" \
  \
  -e ADMIN_TOKEN="$(openssl rand -hex 32)" \
  -e AUTH_METHODS=local,ldap \
  -e ALLOW_REGISTER=false \
  -e LDAP_URI=ldap://ldap.example.com \
  -e LDAP_BASE_DN=dc=ldapdomain,dc=com \
  -e LDAP_USER_FILTER='(uid={username})' \
  -e LDAP_BIND_DN=cn=admin,dc=ldapdomain,dc=com \
  -e LDAP_BIND_PASSWORD=changeme \
  \
  -e WEB_PUBLIC_URL=https://arxiv.example.com \
  -e ICP_BEIAN='陕ICP备XXXXX号-1' \
  \
  -e DAILY_HOUR=10 -e DAILY_MINUTE=0 \
  -e ARXIV_CHECK_CATEGORIES='eess.AS cs.SD' \
  -e LLM_BASE_URL=https://api.deepseek.com/v1 \
  -e LLM_MODEL=deepseek-chat \
  -e LLM_API_KEY=sk-xxxx \
  -e FEISHU_WEBHOOK_URL=https://open.feishu.cn/open-apis/bot/v2/hook/xxxx \
  \
  --restart unless-stopped \
  aslplab/arxivwatcher:latest
```
</details>

<details>
<summary><b>场景 3：容器内手动跑一次抓取（不启 Web）</b></summary>

镜像默认启 Web；想临时抓一次，覆盖 `CMD` 即可：

```bash
docker run --rm \
  -v "$PWD/data:/app/data" -v "$PWD/reports:/app/reports" \
  -e LLM_BASE_URL=https://api.deepseek.com/v1 \
  -e LLM_MODEL=deepseek-chat \
  -e LLM_API_KEY=sk-xxxx \
  aslplab/arxivwatcher:latest \
  python send.py --category eess.AS cs.SD --no-email
```
</details>

<details>
<summary><b>场景 4：让已运行的容器立刻执行一次定时任务</b></summary>

```bash
curl -X POST http://127.0.0.1:8091/admin/run-now \
  -H "Authorization: Bearer $ADMIN_TOKEN"
```
</details>

### 数据持久化与备份

- **必备份**：`data/`（含 `.secret_key`、用户密码哈希、互动数据、SQLite/JSON）、`reports/`（历史报告）
- **`.secret_key` 一旦丢失**：所有用户被强制登出，session 失效，需重新登录
- **SQLite 模式下**：备份前最好 `docker compose stop` 再拷 `data/app.db`，避免 WAL 状态不一致
- **升级镜像**：`docker compose pull && docker compose up -d`，volume 数据自动保留

---

## 💾 存储后端（JSON / SQLite）

互动数据（点赞/踩、评论、标记、收藏、用户、访问统计）默认存为 `data/*.json`。访问量增大后可切换为 SQLite，写入为行级操作，不再每次重写整个文件：

```bash
# 1) 先把现有 JSON 数据迁移到 SQLite（默认 data/app.db）
python storage_tool.py json2sqlite

# 2) 设置后端并重启
export STORAGE_BACKEND=sqlite
bash start_web.sh
```

随时可反向导出回 JSON：

```bash
python storage_tool.py sqlite2json
# 仅转换部分集合：--only interactions visits
# 自定义路径：--data-dir ./data --db ./data/app.db
```

可转换的集合包括：`interactions`、`comments`、`highlights`、`favorites`、`users`、`visits`、`feature_usage`。转换为「全量替换」语义，会用源端数据覆盖目标端对应集合。

---

## 📚 Zotero 插件

见 [`zotero_plugin/arxiv-daily-importer/README.md`](zotero_plugin/arxiv-daily-importer/README.md)。

部署后从 Web 页「Zotero」下载 xpi（会根据 `WEB_PUBLIC_URL` 写入 `update_url`）。本地调试默认连 `http://127.0.0.1:8091`。

---

## 🌐 外网访问（可选）

本仓库**不包含** [frp](https://github.com/fatedier/frp) 客户端。若需把内网 Web 暴露到公网，请自行：

1. 从 [frp Releases](https://github.com/fatedier/frp/releases) 下载对应平台二进制；
2. 编写 `frpc.toml`，将本地 `8091` 映射到公网；
3. 将 `WEB_PUBLIC_URL` 设为公网 HTTPS 地址（Zotero 9 要求插件 `update_url` 为 HTTPS）。

也可用 Nginx 反向代理、Cloudflare Tunnel 等方案。

---

## ❓ 常见问题 FAQ

<details>
<summary><b>Q：不想用 LLM，只想纯抓取列表可以吗？</b></summary>

可以。在 `run.sh` 里加 `--no-llm` 参数即可跳过解读，此时 `LLM_API_KEY` 也不再必填。
</details>

<details>
<summary><b>Q：能换成 DeepSeek / 通义 / 本地 Ollama 吗？</b></summary>

可以，只要是 OpenAI 兼容接口。设置对应的 `LLM_BASE_URL` 和 `LLM_MODEL` 即可，例如 DeepSeek 用 `https://api.deepseek.com/v1` + `deepseek-chat`，本地 Ollama 用对应地址 + `qwen2.5:72b` 等。
</details>

<details>
<summary><b>Q：端口映射后访问不通？</b></summary>

确认容器内 `WEB_HOST=0.0.0.0`（Docker 默认已是），并检查宿主端口是否被占用、防火墙是否放行。映射格式为 `-p 宿主端口:8091`。
</details>

<details>
<summary><b>Q：重启容器后用户全部掉登录了？</b></summary>

多半是 `data/` 没挂出来，导致 `.secret_key` 每次重建。请务必挂载 `/app/data`，详见 [数据持久化与备份](#数据持久化与备份)。
</details>

<details>
<summary><b>Q：怎么改每天抓取的时间和分类？</b></summary>

通过 `DAILY_HOUR` / `DAILY_MINUTE` 改时间（按 `TZ` 时区），通过 `ARXIV_CATEGORIES` / `ARXIV_CHECK_CATEGORIES` 改分类（空格分隔）。
</details>

<details>
<summary><b>Q：国内拉基础镜像很慢 / 超时？</b></summary>

配置 Docker 镜像加速器，参见 [本地构建镜像](#本地构建镜像) 中的「国内构建提示」。
</details>

<details>
<summary><b>Q：JSON 和 SQLite 怎么选？</b></summary>

小规模、单人用 JSON 足够直观；访问量上来、多人并发建议 SQLite（行级写入更稳）。两者可用 `storage_tool.py` 随时互转。
</details>

---

## 📂 项目结构

| 路径 | 说明 |
|------|------|
| `send.py` | 抓取、解读、报告生成、推送 |
| `web.py` | Flask Web 与定时调度 |
| `arxiv_daily_digest.py` | 独立 CLI 版（无 Web） |
| `storage.py` | 统一存储层（JSON / SQLite 后端） |
| `storage_tool.py` | JSON ↔ SQLite 互转工具 |
| `aslp_feed.py` | 实验室新闻/公告每小时抓取缓存 |
| `templates/` / `static/` | 前端 |
| `zotero_plugin/` | Zotero 7+ 插件源码 |
| `scripts/` | 工具脚本（索引重建、macOS launchd 示例） |
| `config/` | 本地配置目录；提交示例文件，忽略实际配置与密钥 |
| `data/papers/` | 运行时论文索引（git 忽略） |
| `data/*.json` / `data/app.db` | 互动/评论/标记/收藏/用户/访问数据（git 忽略） |
| `reports/` | 运行时 HTML 报告（git 忽略） |

---

## 📄 License

本项目采用 **Creative Commons Attribution 4.0 International（CC BY 4.0）** 协议。

详见 [`LICENSE`](LICENSE) 或 <https://creativecommons.org/licenses/by/4.0/>。

---

## ⭐ Star History

<a href="https://www.star-history.com/?repos=ASLP-lab%2FArxivWatcher&type=date&legend=top-left">
 <picture>
   <source media="(prefers-color-scheme: dark)" srcset="https://api.star-history.com/image?repos=ASLP-lab/ArxivWatcher&type=date&theme=dark&legend=top-left" />
   <source media="(prefers-color-scheme: light)" srcset="https://api.star-history.com/image?repos=ASLP-lab/ArxivWatcher&type=date&legend=top-left" />
   <img alt="Star History Chart" src="https://api.star-history.com/image?repos=ASLP-lab/ArxivWatcher&type=date&legend=top-left" />
 </picture>
</a>
