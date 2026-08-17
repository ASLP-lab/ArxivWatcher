/* global Zotero, Services */

// Zotero 9 基于新版 Gecko，ChromeUtils.import() 已移除；这里只使用全局 Services。
var ZoteroServices = (typeof Services !== "undefined") ? Services : null;

const ADDON_PREFIX = "extensions.arxiv-daily-importer.";

const MENUITEM_AUTO_ID = "arxiv-daily-importer-auto-toggle";
const MENUITEM_SYNC_ID = "arxiv-daily-importer-sync-now";
const MENUITEM_MANUAL_ID = "arxiv-daily-importer-manual";
const MENUITEM_COLLECTION_ID = "arxiv-daily-importer-collection";

const DEFAULT_REPORT_URL_TEMPLATE = "http://127.0.0.1:8091/download/{date}.html";
const DEFAULT_DATES_API = "http://127.0.0.1:8091/api/dates";
const DEFAULT_INTERVAL_MIN = 60;
const STARTUP_DELAY_MS = 8_000;
const PLUGIN_VERSION = "0.1.9";

let _syncTimer = null;

function log(msg) {
  if (typeof Zotero !== "undefined") {
    Zotero.debug(`[arxiv-daily-importer] ${msg}`);
  }
}

// ─── Prefs ───

function prefBranch() {
  if (!ZoteroServices) {
    throw new Error("Services is not available");
  }
  return ZoteroServices.prefs.getBranch(ADDON_PREFIX);
}

function getStrPref(key, def) {
  try { return prefBranch().getStringPref(key); } catch (e) { return def; }
}
function setStrPref(key, val) { prefBranch().setStringPref(key, val); }

function getBoolPref(key, def) {
  try { return prefBranch().getBoolPref(key); } catch (e) { return def; }
}
function setBoolPref(key, val) { prefBranch().setBoolPref(key, val); }

function getIntPref(key, def) {
  try { return prefBranch().getIntPref(key); } catch (e) { return def; }
}

function hasPref(key) {
  return prefBranch().prefHasUserValue(key);
}

function getImportedSet() {
  try { return new Set(JSON.parse(getStrPref("importedDates", "[]"))); }
  catch (e) { return new Set(); }
}
function saveImportedSet(set) {
  setStrPref("importedDates", JSON.stringify([...set].sort()));
}

function formatTodayDate() {
  const now = new Date();
  return `${now.getFullYear()}-${
    String(now.getMonth() + 1).padStart(2, "0")
  }-${
    String(now.getDate()).padStart(2, "0")
  }`;
}

// ─── HTTP ───

async function fetchAvailableDates() {
  const url = getStrPref("datesAPI", DEFAULT_DATES_API);
  const r = await Zotero.HTTP.request("GET", url, {
    responseType: "json",
    timeout: 30_000
  });
  let data = r.response;
  if (!data && r.responseText) {
    try { data = JSON.parse(r.responseText); } catch (e) { data = {}; }
  }
  const dates = Array.isArray(data && data.dates) ? data.dates : [];
  return dates.filter(d => /^\d{4}-\d{2}-\d{2}$/.test(d)).sort();
}

async function downloadReportHTML(date) {
  const template = getStrPref("reportURLTemplate", DEFAULT_REPORT_URL_TEMPLATE);
  const url = template.replace("{date}", date);
  log(`downloading ${url}`);
  const r = await Zotero.HTTP.request("GET", url, {
    responseType: "text",
    timeout: 60_000
  });
  if (!r.responseText || !r.responseText.trim()) {
    throw new Error("网页内容为空");
  }
  const tmp = Zotero.getTempDirectory();
  tmp.append(`arxiv-report-${date}.html`);
  await Zotero.File.putContentsAsync(tmp.path, r.responseText);
  return { file: tmp, downloadURL: url };
}

function reportTitle(date) {
  return `ArxivWatcher 报告 ${date}`;
}

function reportViewURL(date) {
  const downloadURL = getStrPref("reportURLTemplate", DEFAULT_REPORT_URL_TEMPLATE)
    .replace("{date}", date);
  // https://.../download/2026-05-28.html -> https://.../date/2026-05-28
  return downloadURL.replace("/download/", "/date/").replace(/\.html$/, "");
}

async function findExistingReportItem(date) {
  const title = reportTitle(date);
  const url = reportViewURL(date);

  let s = new Zotero.Search();
  s.libraryID = Zotero.Libraries.userLibraryID;
  s.addCondition("itemType", "is", "webpage");
  s.addCondition("title", "is", title);
  let ids = await s.search();
  if (ids.length) {
    return Zotero.Items.get(ids[0]);
  }

  s = new Zotero.Search();
  s.libraryID = Zotero.Libraries.userLibraryID;
  s.addCondition("itemType", "is", "webpage");
  s.addCondition("url", "is", url);
  ids = await s.search();
  return ids.length ? Zotero.Items.get(ids[0]) : null;
}

// ─── Collection / Folder ───

function getDefaultCollectionPath() {
  return getStrPref("defaultCollectionPath", "").trim();
}

function collectionName(collection) {
  if (!collection) return "";
  if (typeof collection.name === "string") return collection.name;
  if (typeof collection.getName === "function") return collection.getName();
  return "";
}

function collectionParentID(collection) {
  return collection && collection.parentID ? collection.parentID : null;
}

function collectionsInUserLibrary() {
  if (Zotero.Collections.getByLibrary) {
    return Zotero.Collections.getByLibrary(Zotero.Libraries.userLibraryID) || [];
  }
  return [];
}

async function getOrCreateCollectionPath(path) {
  const parts = path
    .split("/")
    .map(part => part.trim())
    .filter(Boolean);
  if (!parts.length) return null;

  let parentID = null;
  let current = null;

  for (const part of parts) {
    const collections = collectionsInUserLibrary();
    current = collections.find(collection => (
      collectionName(collection) === part
      && collectionParentID(collection) === parentID
    ));

    if (!current) {
      current = new Zotero.Collection();
      current.libraryID = Zotero.Libraries.userLibraryID;
      current.name = part;
      if (parentID) {
        current.parentID = parentID;
      }
      await current.saveTx();
      log(`created collection "${part}"`);
    }

    parentID = current.id;
  }

  return current;
}

async function addItemToDefaultCollection(itemID) {
  const path = getDefaultCollectionPath();
  if (!path) return null;

  const collection = await getOrCreateCollectionPath(path);
  if (!collection) return null;

  if (typeof collection.hasItem === "function" && collection.hasItem(itemID)) {
    log(`item ${itemID} already in collection "${path}"`);
    return collection.id;
  }

  await collection.addItem(itemID);
  log(`added item ${itemID} to collection "${path}"`);
  return collection.id;
}

// ─── Import ───

async function importDate(_win, date) {
  const existing = await findExistingReportItem(date);
  if (existing) {
    log(`webpage item already exists for ${date}, skip`);
    await addItemToDefaultCollection(existing.id);
    return existing.id;
  }

  const { file, downloadURL } = await downloadReportHTML(date);
  const title = reportTitle(date);
  const pageURL = reportViewURL(date);
  const accessDate = Zotero.Date.dateToSQL(new Date(), true);

  log(`using webpage item + HTML attachment path for ${date}; no import translators`);

  const item = new Zotero.Item("webpage");
  item.libraryID = Zotero.Libraries.userLibraryID;
  item.setField("title", title);
  item.setField("url", pageURL);
  item.setField("accessDate", accessDate);
  const itemID = await item.saveTx();

  await Zotero.Attachments.importFromFile({
    file,
    libraryID: Zotero.Libraries.userLibraryID,
    parentItemID: itemID,
    title: `网页快照 ${date}`,
    contentType: "text/html",
    charset: "UTF-8"
  });

  await addItemToDefaultCollection(itemID);

  log(`saved webpage item ${itemID} with HTML snapshot for ${date} (${downloadURL})`);
  return itemID;
}

async function runAutoSync({ silent = true } = {}) {
  if (!getBoolPref("autoImport", true)) {
    log("auto import disabled, skip");
    return 0;
  }
  const win = Zotero.getMainWindow();
  if (typeof Zotero === "undefined" || !Zotero.Libraries) {
    log("Zotero not ready, skip");
    return 0;
  }

  let dates;
  try {
    dates = await fetchAvailableDates();
  } catch (e) {
    log(`fetch dates failed: ${e}`);
    if (!silent) win.alert(`同步失败：无法访问服务（${e}）`);
    return 0;
  }
  if (!dates.length) {
    log("server returned no dates");
    return 0;
  }

  const imported = getImportedSet();
  const firstRun = !hasPref("importedDates");

  let todo;
  if (firstRun) {
    const today = formatTodayDate();
    todo = dates.includes(today) ? [today] : [dates[dates.length - 1]];
    for (const d of dates) {
      if (!todo.includes(d)) imported.add(d);
    }
    log(`first run, importing only ${todo.join(",")}, skipping ${imported.size} historical`);
  } else {
    todo = dates.filter(d => !imported.has(d));
  }

  let succeeded = 0;
  for (const d of todo) {
    try {
      await importDate(win, d);
      imported.add(d);
      saveImportedSet(imported);
      succeeded++;
      log(`imported ${d}`);
    } catch (e) {
      log(`import ${d} failed: ${e}`);
    }
  }
  saveImportedSet(imported);

  if (!silent) {
    const alertWin = win || Zotero.getMainWindow();
    if (succeeded > 0) {
      alertWin.alert(`已导入 ${succeeded} 个 arXiv 报告网页快照（${todo.slice(0, succeeded).join(", ")}）。`);
    } else {
      alertWin.alert("没有需要导入的新报告。");
    }
  }
  return succeeded;
}

// ─── Timer ───

function startAutoSyncTimer() {
  stopAutoSyncTimer();
  const minutes = Math.max(5, getIntPref("intervalMinutes", DEFAULT_INTERVAL_MIN));
  const ms = minutes * 60_000;
  log(`schedule auto sync every ${minutes} min`);
  _syncTimer = setInterval(() => {
    runAutoSync({ silent: true }).catch(e => log(`auto sync error: ${e}`));
  }, ms);
}

function stopAutoSyncTimer() {
  if (_syncTimer) {
    clearInterval(_syncTimer);
    _syncTimer = null;
  }
}

// ─── Menu ───

function addMenuItems(win) {
  const doc = win.document;
  const menuPopup = doc.getElementById("menu_ToolsPopup");
  if (!menuPopup) return;

  if (!doc.getElementById(MENUITEM_SYNC_ID)) {
    const m = doc.createXULElement("menuitem");
    m.id = MENUITEM_SYNC_ID;
    m.setAttribute("label", "立即同步最新 arXiv 报告");
    m.addEventListener("command", () => {
      runAutoSync({ silent: false }).catch(e => win.alert(`同步失败：${e}`));
    });
    menuPopup.appendChild(m);
  }

  if (!doc.getElementById(MENUITEM_MANUAL_ID)) {
    const m = doc.createXULElement("menuitem");
    m.id = MENUITEM_MANUAL_ID;
    m.setAttribute("label", "导入指定日期的 arXiv 报告...");
    m.addEventListener("command", () => {
      const today = formatTodayDate();
      const input = (win.prompt("输入日期（YYYY-MM-DD），留空为今天：", today) || "").trim();
      const date = input || today;
      if (!/^\d{4}-\d{2}-\d{2}$/.test(date)) {
        win.alert("日期格式错误，请使用 YYYY-MM-DD");
        return;
      }
      importDate(win, date).then(() => {
        const set = getImportedSet();
        set.add(date);
        saveImportedSet(set);
        win.alert(`已导入 ${date} 的报告网页副本。`);
      }).catch(e => win.alert(`导入失败：${e}`));
    });
    menuPopup.appendChild(m);
  }

  if (!doc.getElementById(MENUITEM_COLLECTION_ID)) {
    const m = doc.createXULElement("menuitem");
    m.id = MENUITEM_COLLECTION_ID;
    m.setAttribute("label", "设置 arXiv 报告默认导入文件夹...");
    m.addEventListener("command", () => {
      const current = getDefaultCollectionPath();
      const input = win.prompt(
        "输入 Zotero Collection 路径（例如 arXiv/每日精读）。留空表示导入到库根目录：",
        current
      );
      if (input === null) return;

      const path = input.trim();
      setStrPref("defaultCollectionPath", path);
      if (!path) {
        win.alert("已清除默认导入文件夹；之后会导入到 Zotero 库根目录。");
        return;
      }

      getOrCreateCollectionPath(path)
        .then(() => win.alert(`默认导入文件夹已设置为：${path}`))
        .catch(e => win.alert(`设置失败：${e}`));
    });
    menuPopup.appendChild(m);
  }

  if (!doc.getElementById(MENUITEM_AUTO_ID)) {
    const m = doc.createXULElement("menuitem");
    m.id = MENUITEM_AUTO_ID;
    m.setAttribute("type", "checkbox");
    m.setAttribute("label", "自动每日导入 arXiv 报告");
    m.setAttribute("checked", getBoolPref("autoImport", true) ? "true" : "false");
    m.addEventListener("command", () => {
      const newVal = !getBoolPref("autoImport", true);
      setBoolPref("autoImport", newVal);
      m.setAttribute("checked", newVal ? "true" : "false");
      if (newVal) {
        startAutoSyncTimer();
        runAutoSync({ silent: false }).catch(e => log(`sync after toggle failed: ${e}`));
      } else {
        stopAutoSyncTimer();
      }
    });
    menuPopup.appendChild(m);
  }
}

function removeMenuItems(win) {
  for (const id of [MENUITEM_SYNC_ID, MENUITEM_MANUAL_ID, MENUITEM_COLLECTION_ID, MENUITEM_AUTO_ID]) {
    const el = win.document.getElementById(id);
    if (el) el.remove();
  }
}

function installWindowListener() {
  ZoteroServices.wm.addListener(windowListener);
  const enumerator = ZoteroServices.wm.getEnumerator("navigator:browser");
  while (enumerator.hasMoreElements()) {
    addMenuItems(enumerator.getNext());
  }
}

function uninstallWindowListener() {
  ZoteroServices.wm.removeListener(windowListener);
  const enumerator = ZoteroServices.wm.getEnumerator("navigator:browser");
  while (enumerator.hasMoreElements()) {
    removeMenuItems(enumerator.getNext());
  }
}

var windowListener = {
  onOpenWindow(xulWindow) {
    const win = xulWindow.docShell.domWindow;
    win.addEventListener("load", () => addMenuItems(win), { once: true });
  },
  onCloseWindow() {},
  onWindowTitleChange() {}
};

// ─── Bootstrap ───

function install() {}
function uninstall() {}

async function startup() {
  log(`startup v${PLUGIN_VERSION}`);
  installWindowListener();

  if (typeof Zotero !== "undefined" && Zotero.initializationPromise) {
    try { await Zotero.initializationPromise; } catch (e) { log(`init wait error: ${e}`); }
  }

  setTimeout(() => {
    runAutoSync({ silent: true }).catch(e => log(`startup sync error: ${e}`));
    startAutoSyncTimer();
  }, STARTUP_DELAY_MS);
}

function shutdown() {
  log("shutdown");
  stopAutoSyncTimer();
  uninstallWindowListener();
}
