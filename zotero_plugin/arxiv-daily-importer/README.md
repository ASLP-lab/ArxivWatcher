# arXiv Daily Importer（Zotero 插件）

Zotero 7+ 插件：自动把 ArxivWatcher 每日报告的 HTML 下载为本地网页快照并导入库中。

## 行为

- 启动 Zotero 后约 8 秒自动同步当天报告
- 每 `intervalMinutes` 分钟（默认 60）检查 `/api/dates` 是否有新日期
- 已导入日期记录在偏好 `importedDates`，不重复导入
- **首次安装只导入今天**，不会批量导入历史

## 安装

1. 先启动本项目的 Web 服务（`bash start_web.sh`）
2. 浏览器打开 Web 页的 **Zotero** 栏目，下载 `arxiv-daily-importer.xpi`（需配置 `WEB_PUBLIC_URL` 为 HTTPS 公网地址时，才满足 Zotero 9 的 `update_url` 要求）
3. Zotero：`工具` → `插件` → 齿轮 → `Install Add-on From File...`
4. 重启 Zotero

开发调试可手动打包：

```bash
cd zotero_plugin/arxiv-daily-importer
zip -r arxiv-daily-importer.xpi manifest.json bootstrap.js
```

## 配置

偏好前缀：`extensions.arxiv-daily-importer.*`

| key | 说明 | 默认 |
|-----|------|------|
| `reportURLTemplate` | 报告下载 URL，含 `{date}` | `http://127.0.0.1:8091/download/{date}.html` |
| `datesAPI` | 可用日期列表 | `http://127.0.0.1:8091/api/dates` |
| `intervalMinutes` | 检查间隔（分钟，≥5） | `60` |
| `autoImport` | 是否自动导入 | `true` |
| `defaultCollectionPath` | 默认 Collection，如 `arXiv/每日精读` | 空（库根目录） |

在 Zotero「Run JavaScript」中修改示例：

```javascript
const b = Services.prefs.getBranch("extensions.arxiv-daily-importer.");
b.setStringPref("reportURLTemplate", "https://your-host/download/{date}.html");
b.setStringPref("datesAPI", "https://your-host/api/dates");
```

## 依赖

Web 服务需提供：

- `GET /api/dates` → `{"dates": ["2026-05-28", ...]}`
- `GET /download/<date>.html` → 报告 HTML
