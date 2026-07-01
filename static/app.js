/* ArxivApp — 动态渲染 + 交互 */
var ArxivApp = (function () {
  var appDate = '';
  var appUser = null;
  var papers = [];
  var extraPapers = [];
  var papersById = {};
  var interactions = {};
  var favorites = {};
  var favFolders = ['默认收藏'];
  var comments = {};
  var currentSort = 'index';
  var papersDigest = '';
  var appSinglePaperId = null;
  var appPaperDigest = '';
  var ccfEntries = null;
  var showCommunityMarks = true;
  var arxivVersionCache = {};
  var arxivVersionInflight = {};
  var arxivVersionObserver = null;
  var activeDomainFilter = '';   // 当前选中的领域筛选（主领域名称，空=全部）
  var hideBlacklisted = false;   // 是否隐藏黑名单论文（默认显示但折叠到底部）
  var activeGradeFilter = '';    // 当前选中的推荐等级筛选（must/worth/skip，空=全部）
  var spotlightAuthors = [];     // 重点作者名单（从 /api/highlight-authors 加载）
  var readingListItems = [];     // 当日阅读列表

  // 评分 → 推荐等级映射（参考 dailypaper-skills 的分流表设计）
  // score>=8 → 🔥 必读, 5<=score<8 → 👀 值得看, 0<score<5 → 💤 可跳过
  var GRADES = {
    must:  { key: 'must',  emoji: '🔥', label: '必读',   cls: 'grade-must'  },
    worth: { key: 'worth', emoji: '👀', label: '值得看', cls: 'grade-worth' },
    skip:  { key: 'skip',  emoji: '💤', label: '可跳过', cls: 'grade-skip'  },
  };

  function paperGrade(p) {
    if (!p || p.blacklisted) return GRADES.skip;
    var s = p.score || 0;
    if (s >= 8) return GRADES.must;
    if (s >= 5) return GRADES.worth;
    if (s > 0)  return GRADES.skip;
    return null; // 未评分
  }

  try {
    var storedCommunity = localStorage.getItem('arxivShowCommunityHl');
    if (storedCommunity === '0') showCommunityMarks = false;
  } catch (e) { /* ignore */ }

  var CCF_AREA_LABELS = {
    arch: '体系结构', net: '计算机网络', sec: '网络安全', se: '软件工程',
    db: '数据库', theory: '理论', cg: '图形多媒体', ai: '人工智能',
    hci: '人机交互', cross: '交叉综合'
  };

  function loadCCFCatalog() {
    if (ccfEntries) return Promise.resolve(ccfEntries);
    return fetch('/api/ccf-catalog', { credentials: 'same-origin' })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (meta) {
        if (!meta || !meta.ok || !meta.url) {
          ccfEntries = [];
          return ccfEntries;
        }
        return fetch(meta.url, { credentials: 'same-origin' })
          .then(function (r) { return r.ok ? r.json() : { entries: [] }; })
          .then(function (d) {
            ccfEntries = (d && d.entries) || [];
            return ccfEntries;
          });
      })
      .catch(function () {
        ccfEntries = [];
        return ccfEntries;
      });
  }

  function loadHighlightAuthors() {
    if (spotlightAuthors.length) return Promise.resolve(spotlightAuthors);
    return fetch('/api/highlight-authors', { credentials: 'same-origin' })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (meta) {
        if (!meta || !meta.ok || !meta.url) {
          spotlightAuthors = (meta && meta.names) || [];
          return spotlightAuthors;
        }
        return fetch(meta.url, { credentials: 'same-origin' })
          .then(function (r) { return r.ok ? r.json() : { names: [] }; })
          .then(function (d) {
            spotlightAuthors = (d && d.names) || [];
            return spotlightAuthors;
          });
      })
      .catch(function () {
        spotlightAuthors = [];
        return spotlightAuthors;
      });
  }

  function normalizeAuthorName(s) {
    return (s || '')
      .normalize('NFKC')
      .toLocaleLowerCase('en-US')
      .replace(/[.,]/g, ' ')
      .replace(/\s+/g, ' ')
      .trim();
  }

  function escapeRegExp(s) {
    return String(s).replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  }

  function authorMatchesHighlight(author, highlightName) {
    var a = normalizeAuthorName(author);
    var h = normalizeAuthorName(highlightName);
    if (!a || !h) return false;
    if (a === h || a.indexOf(h) >= 0 || h.indexOf(a) >= 0) return true;
    var hParts = h.split(' ').filter(Boolean);
    if (hParts.length < 2) return a.indexOf(h) >= 0;
    var first = hParts[0];
    var last = hParts[hParts.length - 1];
    if (a.indexOf(last) < 0) return false;
    if (a.indexOf(first) >= 0) return true;
    var initial = first.charAt(0);
    if (initial) {
      var re = new RegExp('(^|\\s)' + escapeRegExp(initial) + '(\\s|\\.|$)', 'i');
      if (re.test(a)) return true;
    }
    return false;
  }

  function paperHasCelebrateAuthor(p) {
    if (!spotlightAuthors.length || !p || !p.authors || !p.authors.length) return false;
    return p.authors.some(function (author) {
      return spotlightAuthors.some(function (name) {
        return authorMatchesHighlight(author, name);
      });
    });
  }

  function paperCardExtraClasses(p) {
    var blClass = p.blacklisted ? ' paper-blacklisted' : '';
    var gr = paperGrade(p);
    var grClass = gr ? ' ' + gr.cls : '';
    var celClass = paperHasCelebrateAuthor(p) ? ' paper-card--celebrate' : '';
    return blClass + grClass + celClass;
  }

  function renderAuthorsHtml(p) {
    var authors = p.authors || [];
    if (!authors.length) return '';
    if (paperHasCelebrateAuthor(p)) {
      return authors.map(function (author) {
        return '<span class="author-spotlight">' + esc(author) + '</span>';
      }).join(', ');
    }
    return esc(authors.join(', '));
  }

  function celebrateCardDecorHtml() {
    return '<span class="paper-celebrate-sparkle" aria-hidden="true"></span>' +
      '<span class="paper-celebrate-confetti" aria-hidden="true"></span>';
  }

  function isWordChar(ch) {
    return /[A-Za-z0-9_-]/.test(ch);
  }

  function exactTokenInText(text, term) {
    if (!text || !term) return false;
    var tl = text.toLowerCase();
    var termL = term.toLowerCase();
    if (tl === termL) return true;
    var start = 0;
    var n = term.length;
    while (true) {
      var idx = tl.indexOf(termL, start);
      if (idx === -1) return false;
      var before = idx > 0 ? text.charAt(idx - 1) : '';
      var after = idx + n < text.length ? text.charAt(idx + n) : '';
      if (!(before && isWordChar(before)) && !(after && isWordChar(after))) return true;
      start = idx + 1;
    }
  }

  function matchCCFTags(comment) {
    var text = (comment || '').trim();
    if (!text || !ccfEntries || !ccfEntries.length) return [];
    var textL = text.toLowerCase();
    var seen = {};
    var hits = [];
    ccfEntries.forEach(function (e) {
      var abbr = e.s || '';
      var full = e.f || '';
      var matched = (
        textL === abbr.toLowerCase()
        || textL === full.toLowerCase()
        || exactTokenInText(text, abbr)
        || (full.length >= 8 && exactTokenInText(text, full))
      );
      if (!matched) return;
      var key = abbr + '|' + e.r + '|' + e.t;
      if (seen[key]) return;
      seen[key] = true;
      hits.push(e);
    });
    var rankOrder = { A: 0, B: 1, C: 2 };
    hits.sort(function (a, b) {
      var ra = rankOrder[a.r] != null ? rankOrder[a.r] : 9;
      var rb = rankOrder[b.r] != null ? rankOrder[b.r] : 9;
      if (ra !== rb) return ra - rb;
      return (a.s || '').localeCompare(b.s || '');
    });
    return hits;
  }

  function formatCCFTags(comment) {
    var tags = matchCCFTags(comment);
    if (!tags.length) return '';
    var html = '<span class="ccf-tag-row">';
    tags.forEach(function (t) {
      var rank = t.r || 'C';
      var typeLabel = t.type_label || (t.t === 'conf' ? '会议' : '期刊');
      var areaLabel = t.area_label || CCF_AREA_LABELS[t.a] || '';
      html += '<span class="ccf-tag ccf-rank-' + rank + '" title="' + esc(t.f || '') + '">';
      html += '<span class="ccf-rank-pill">CCF-' + esc(rank) + '</span>';
      html += '<span class="ccf-abbr">' + esc(t.s) + '</span>';
      html += '<span class="ccf-type-pill">' + esc(typeLabel) + '</span>';
      if (areaLabel) html += '<span class="ccf-area">' + esc(areaLabel) + '</span>';
      html += '</span>';
    });
    html += '</span>';
    return html;
  }

  function init(date, user, digest, singlePaperId) {
    appDate = date;
    appUser = user;
    papersDigest = digest || '';
    appSinglePaperId = singlePaperId || null;
    appPaperDigest = '';
    bootPaperPage();
  }

  function initShare(date, user, paperId, paperDigest) {
    appDate = date;
    appUser = user;
    appSinglePaperId = paperId || null;
    appPaperDigest = paperDigest || '';
    papersDigest = '';
    bootPaperPage();
  }

  function bootPaperPage() {
    var container = document.getElementById('paper-container');
    if (container) {
      // 'toggle' 事件不冒泡，使用捕获阶段统一监听
      container.addEventListener('toggle', function (e) {
        if (e.target.open && e.target.classList && e.target.classList.contains('analysis-section')) {
          renderAnalysis(e.target);
        }
      }, true);
    }
    if (typeof ArxivHL !== 'undefined') ArxivHL.setShowCommunity(showCommunityMarks);
    syncCommunityBtn();
    bindReadingDrawerEvents();
    startStatusPolling();
    loadPapers();
  }

  function paperDomId(pid) {
    return 'paper-' + String(pid || '').replace(/[^\w\-.]/g, '_');
  }

  function loadReadingList() {
    return fetch('/api/reading-list/' + encodeURIComponent(appDate), { credentials: 'same-origin' })
      .then(function (r) { return r.ok ? r.json() : { ok: false }; })
      .then(function (d) {
        readingListItems = (d && d.ok && d.items) ? d.items : [];
        return readingListItems;
      })
      .catch(function () {
        readingListItems = [];
        return readingListItems;
      });
  }

  function updateReadingBadge() {
    var badge = document.getElementById('reading-unread-badge');
    if (!badge) return;
    var unread = readingListItems.filter(function (i) { return !i.read; }).length;
    if (unread > 0) {
      badge.textContent = unread > 99 ? '99+' : String(unread);
      badge.style.display = 'flex';
    } else {
      badge.style.display = 'none';
    }
  }

  function renderReadingListDrawer() {
    var list = document.getElementById('reading-drawer-list');
    if (!list) return;
    if (!readingListItems.length) {
      list.innerHTML = '<li class="reading-empty">点赞 👍 加入列表，取消点赞会移除</li>';
      updateReadingBadge();
      return;
    }
    var html = '';
    readingListItems.forEach(function (item) {
      var read = !!item.read;
      html += '<li class="reading-item' + (read ? ' is-read' : '') + '">';
      html += '<label class="reading-check" title="标记已读">';
      html += '<input type="checkbox"' + (read ? ' checked' : '') + ' data-pid="' + escAttr(item.paper_id) + '">';
      html += '<span class="reading-check-box" aria-hidden="true"></span>';
      html += '</label>';
      html += '<button type="button" class="reading-item-go" data-pid="' + escAttr(item.paper_id) + '">';
      html += '<span class="reading-item-title">' + esc(item.title || item.paper_id) + '</span>';
      html += '</button>';
      html += '</li>';
    });
    list.innerHTML = html;
    updateReadingBadge();
  }

  function bindReadingDrawerEvents() {
    var list = document.getElementById('reading-drawer-list');
    if (!list || list.dataset.bound === '1') return;
    list.dataset.bound = '1';
    list.addEventListener('change', function (e) {
      var cb = e.target;
      if (!cb || !cb.matches || !cb.matches('.reading-check input')) return;
      setReadingRead(cb.dataset.pid, cb.checked, cb);
    });
    list.addEventListener('click', function (e) {
      var btn = e.target.closest ? e.target.closest('.reading-item-go') : null;
      if (btn) gotoReadingPaper(btn.dataset.pid);
    });
  }

  function toggleReadingList(forceOpen) {
    var drawer = document.getElementById('reading-drawer');
    var backdrop = document.getElementById('reading-backdrop');
    if (!drawer) return;
    var open = typeof forceOpen === 'boolean' ? forceOpen : !drawer.classList.contains('open');
    drawer.classList.toggle('open', open);
    if (backdrop) backdrop.classList.toggle('open', open);
    if (open) {
      loadReadingList().then(renderReadingListDrawer);
      var toc = document.getElementById('toc-drawer');
      if (toc && toc.classList.contains('open')) toggleToc();
    }
  }

  function setReadingRead(pid, read, checkbox) {
    fetch('/api/reading-list/read', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      credentials: 'same-origin',
      body: JSON.stringify({ date: appDate, paper_id: pid, read: !!read }),
    }).then(function (r) { return r.json(); }).then(function (d) {
      if (!d.ok) {
        if (checkbox) checkbox.checked = !read;
        if (d.msg && d.msg.indexOf('cookie') >= 0 && typeof showAuthModal === 'function') showAuthModal();
        return;
      }
      var item = d.item;
      readingListItems.forEach(function (it, idx) {
        if (it.paper_id === pid) readingListItems[idx] = item;
      });
      var row = checkbox ? checkbox.closest('.reading-item') : null;
      if (row) row.classList.toggle('is-read', !!read);
      updateReadingBadge();
    }).catch(function () {
      if (checkbox) checkbox.checked = !read;
    });
  }

  function gotoReadingPaper(pid) {
    toggleReadingList(false);
    var card = document.getElementById(paperDomId(pid));
    if (!card) {
      document.querySelectorAll('.paper-card[data-pid]').forEach(function (c) {
        if (c.dataset.pid === pid) card = c;
      });
    }
    if (card) {
      scrollToCard(card);
      return;
    }
    window.location.href = '/date/' + encodeURIComponent(appDate) + '#paper-' + encodeURIComponent(pid);
  }

  function scrollToPaperFromHash() {
    var hash = location.hash || '';
    if (hash.indexOf('#paper-') !== 0) return;
    var pid = decodeURIComponent(hash.slice(7));
    setTimeout(function () {
      gotoReadingPaper(pid);
    }, 120);
  }

  // ── 状态横幅轮询 ──
  var statusPollTimer = null;
  var statusPollInterval = 5000; // 默认 5 秒；运行中时 2 秒

  function startStatusPolling() {
    if (statusPollTimer) clearInterval(statusPollTimer);
    pollDailyStatus(); // 立即执行一次
    statusPollTimer = setInterval(pollDailyStatus, statusPollInterval);
  }

  function pollDailyStatus() {
    fetch('/api/daily-status', { credentials: 'same-origin', cache: 'no-store' })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (d) {
        if (!d || !d.ok) return;
        renderStatusBanner(d);
        // 运行中时加快轮询
        var busy = d.main_running || d.classify_running || d.status === 'waiting_update';
        var newInterval = busy ? (d.status === 'waiting_update' ? 10000 : 2000) : 15000;
        if (newInterval !== statusPollInterval) {
          statusPollInterval = newInterval;
          startStatusPolling();
        }
      })
      .catch(function () {});
  }

  function renderStatusBanner(d) {
    var banner = document.getElementById('status-banner');
    if (!banner) return;
    var status = d.status || 'waiting';
    var msg = d.message || '';

    var icon = '';
    var extra = '';
    if (status === 'fetching') {
      icon = '<span class="sb-spinner"></span>';
    } else if (status === 'processing') {
      icon = '<span class="sb-spinner"></span>';
      if (d.progress && d.progress.total) {
        var bar = d.progress.bar || '';
        extra = '<div class="sb-progress">' + esc(bar) + '</div>';
      }
    } else if (status === 'waiting_update') {
      icon = '<span class="sb-icon">⏳</span>';
    } else if (status === 'complete') {
      icon = '<span class="sb-icon">✅</span>';
    } else if (status === 'partial') {
      icon = '<span class="sb-icon">⚠️</span>';
    } else if (status === 'failed') {
      icon = '<span class="sb-icon">❌</span>';
    } else {
      icon = '<span class="sb-icon">⏳</span>';
    }

    var html = '<div class="sb-content">' + icon +
      '<span class="sb-text">' + esc(msg) + '</span>' +
      '</div>' + extra;

    banner.className = 'status-banner sb-' + status;
    banner.innerHTML = html;
    banner.style.display = '';
  }

  function papersApiUrl() {
    if (appSinglePaperId && appPaperDigest) {
      return '/api/h/' + appPaperDigest + '/paper/' + appDate + '/' + encodeURIComponent(appSinglePaperId);
    }
    if (papersDigest) {
      return '/api/h/' + papersDigest + '/papers/' + appDate;
    }
    return '/api/papers/' + appDate;
  }

  function sharePaperUrl(pid) {
    return window.location.origin + '/paper/' + appDate + '/' + encodeURIComponent(pid);
  }

  function copyToClipboard(text) {
    if (navigator.clipboard && navigator.clipboard.writeText) {
      return navigator.clipboard.writeText(text);
    }
    return new Promise(function (resolve, reject) {
      try {
        var ta = document.createElement('textarea');
        ta.value = text;
        ta.setAttribute('readonly', '');
        ta.style.position = 'fixed';
        ta.style.left = '-9999px';
        document.body.appendChild(ta);
        ta.select();
        var ok = document.execCommand('copy');
        document.body.removeChild(ta);
        ok ? resolve() : reject(new Error('copy failed'));
      } catch (err) {
        reject(err);
      }
    });
  }

  function flashShareBtn(btn, label) {
    if (!btn) return;
    var labelEl = btn.querySelector('.ia-share-label');
    if (!labelEl) return;
    var prev = labelEl.textContent;
    labelEl.textContent = label;
    btn.classList.add('ia-share-done');
    setTimeout(function () {
      labelEl.textContent = prev;
      btn.classList.remove('ia-share-done');
    }, 1600);
  }

  function copyShareLink(btn) {
    var pid = btn && btn.dataset ? btn.dataset.pid : appSinglePaperId;
    if (!pid) return;
    var url = sharePaperUrl(pid);
    copyToClipboard(url).then(function () {
      flashShareBtn(btn, '已复制');
    }).catch(function () {
      flashShareBtn(btn, '复制失败');
    });
  }

  function analysisApiUrl(pid, digest) {
    if (digest) {
      return '/api/h/' + digest + '/analysis/' + appDate + '/' + encodeURIComponent(pid);
    }
    return '/api/analysis/' + appDate + '/' + encodeURIComponent(pid);
  }

  function paperAssetsApiUrl(pid) {
    return '/api/paper-assets/' + appDate + '/' + encodeURIComponent(pid);
  }

  function paperFiguresBlockHtml(pid) {
    return '<div class="paper-figures" data-pid="' + escAttr(pid) + '">' +
      '<div class="paper-figures-head"><span class="paper-figures-icon">🖼</span><span class="paper-figures-title">图表与表格</span></div>' +
      '<div class="paper-figures-body"><div class="paper-figures-loading">加载中…</div></div>' +
      '</div>';
  }

  var figuresObserver = null;

  function setupPaperFiguresLazyLoad() {
    if (figuresObserver) figuresObserver.disconnect();
    var blocks = document.querySelectorAll('.paper-figures[data-pid]');
    if (!blocks.length) return;
    if (!('IntersectionObserver' in window)) {
      blocks.forEach(loadPaperFiguresForBlock);
      return;
    }
    figuresObserver = new IntersectionObserver(function (entries) {
      entries.forEach(function (entry) {
        if (!entry.isIntersecting) return;
        loadPaperFiguresForBlock(entry.target);
        figuresObserver.unobserve(entry.target);
      });
    }, { root: null, rootMargin: '220px 0px', threshold: 0.01 });
    blocks.forEach(function (el) {
      if (el.dataset.loaded !== '1' && el.dataset.loading !== '1') {
        figuresObserver.observe(el);
      }
    });
  }

  function loadPaperFiguresForBlock(block) {
    if (!block || block.dataset.loaded === '1' || block.dataset.loading === '1') return;
    var pid = block.dataset.pid;
    var body = block.querySelector('.paper-figures-body');
    if (!pid || !body) return;
    block.dataset.loading = '1';
    body.innerHTML = '<div class="paper-figures-loading">加载图表…</div>';
    fetch(paperAssetsApiUrl(pid), { credentials: 'same-origin' })
      .then(function (r) { return r.json(); })
      .then(function (d) {
        if (!d || !d.ok) {
          body.innerHTML = '<div class="paper-figures-empty">' + esc((d && d.msg) || '加载失败') + '</div>';
          block.dataset.loaded = '1';
          block.removeAttribute('data-loading');
          return;
        }
        var assets = d.assets || [];
        if (!assets.length) {
          body.innerHTML = '<div class="paper-figures-empty">未检测到图片或表格</div>';
          block.dataset.loaded = '1';
          block.removeAttribute('data-loading');
          return;
        }
        var srcHint = d.source === 'arxiv_html'
          ? '<div class="paper-figures-src">来源：arXiv HTML</div>'
          : (d.source === 'pdf' ? '<div class="paper-figures-src">来源：PDF 解析</div>' : '');
        var html = srcHint + '<div class="paper-figures-grid">';
        assets.forEach(function (a) {
          var typeLabel = a.type === 'table' ? '表' : '图';
          var capPreview = a.caption || a.label || '';
          var btnCls = 'figure-thumb-btn' + (a.type === 'table' ? ' figure-thumb-btn--table' : '');
          html += '<button type="button" class="' + btnCls + '" data-type="' + escAttr(a.type || 'figure') + '" data-full="' + escAttr(a.full_url) + '" data-table="' + escAttr(a.table_url || '') + '" data-label="' + escAttr(a.label || '') + '" data-caption="' + escAttr(a.caption || '') + '">';
          html += '<span class="figure-thumb-wrap' + (a.type === 'table' ? ' figure-thumb-wrap--table' : '') + '"><img class="figure-thumb" src="' + escAttr(a.thumb_url) + '" alt="' + escAttr(capPreview) + '" loading="lazy"></span>';
          html += '<span class="figure-thumb-cap"><span class="figure-thumb-type">' + esc(typeLabel) + '</span> ' + esc(a.label || '') + '</span>';
          if (a.caption && a.type !== 'table') {
            html += '<span class="figure-thumb-caption">' + esc(a.caption) + '</span>';
          }
          html += '</button>';
        });
        html += '</div>';
        body.innerHTML = html;
        body.querySelectorAll('.figure-thumb-btn').forEach(function (btn) {
          btn.addEventListener('click', function () {
            openFigureLightbox({
              type: btn.dataset.type,
              fullUrl: btn.dataset.full,
              tableUrl: btn.dataset.table,
              label: btn.dataset.label,
              caption: btn.dataset.caption,
            });
          });
        });
        block.dataset.loaded = '1';
        block.removeAttribute('data-loading');
      })
      .catch(function () {
        body.innerHTML = '<div class="paper-figures-empty">加载失败，请刷新重试</div>';
        block.removeAttribute('data-loading');
      });
  }

  function ensureFigureLightbox() {
    var el = document.getElementById('figure-lightbox');
    if (el) return el;
    el = document.createElement('div');
    el.id = 'figure-lightbox';
    el.className = 'figure-lightbox';
    el.innerHTML = '<div class="figure-lightbox-card">' +
      '<div class="figure-lightbox-head"><span class="figure-lightbox-title"></span>' +
      '<button type="button" class="figure-lightbox-close" aria-label="关闭">&times;</button></div>' +
      '<div class="figure-lightbox-body"><div class="figure-lightbox-media"><div class="figure-lightbox-loading">加载中…</div></div></div>' +
      '<div class="figure-lightbox-caption"></div>' +
      '</div>';
    document.body.appendChild(el);
    el.addEventListener('click', function (e) {
      if (e.target === el) closeFigureLightbox();
    });
    el.querySelector('.figure-lightbox-close').addEventListener('click', closeFigureLightbox);
    document.addEventListener('keydown', function (e) {
      if (e.key === 'Escape') closeFigureLightbox();
    });
    return el;
  }

  function openFigureLightbox(opts) {
    opts = opts || {};
    var lb = ensureFigureLightbox();
    var label = opts.label || '图表';
    var caption = opts.caption || '';
    lb.querySelector('.figure-lightbox-title').textContent = label;
    var capEl = lb.querySelector('.figure-lightbox-caption');
    capEl.textContent = caption;
    capEl.style.display = caption ? 'block' : 'none';
    var media = lb.querySelector('.figure-lightbox-media');
    media.innerHTML = '<div class="figure-lightbox-loading">加载中…</div>';
    lb.classList.add('open');

    if (opts.type === 'table' && opts.tableUrl) {
      fetch(opts.tableUrl, { credentials: 'same-origin' })
        .then(function (r) { return r.json(); })
        .then(function (d) {
          if (!d || !d.ok || !d.html) {
            media.innerHTML = '<div class="paper-figures-empty">表格加载失败</div>';
            return;
          }
          if (d.caption) {
            capEl.textContent = d.caption;
            capEl.style.display = 'block';
          }
          media.innerHTML = '<div class="figure-lightbox-table-wrap">' + d.html + '</div>';
        })
        .catch(function () {
          media.innerHTML = '<div class="paper-figures-empty">表格加载失败</div>';
        });
      return;
    }

    var img = new Image();
    img.className = 'figure-lightbox-img';
    img.alt = caption || label;
    img.onload = function () {
      media.innerHTML = '';
      media.appendChild(img);
    };
    img.onerror = function () {
      media.innerHTML = '<div class="paper-figures-empty">高清图加载失败</div>';
    };
    img.src = opts.fullUrl || '';
  }

  function closeFigureLightbox() {
    var lb = document.getElementById('figure-lightbox');
    if (lb) lb.classList.remove('open');
  }

  function loadPapers() {
    var paperListPromise = fetch(papersApiUrl(), { credentials: 'same-origin' }).then(function (r) {
      if (!r.ok) throw new Error('papers API ' + r.status);
      return r.json();
    }).then(function (data) {
      if (appSinglePaperId && appPaperDigest && data && data.paper) {
        return { papers: [data.paper] };
      }
      if (appSinglePaperId && data && data.papers) {
        var one = data.papers.filter(function (p) { return p.paper_id === appSinglePaperId; });
        return { papers: one };
      }
      return data;
    });

    Promise.all([
      paperListPromise,
      fetch('/api/interactions/' + appDate, { credentials: 'same-origin' }).then(function (r) {
        if (!r.ok) throw new Error('interactions API ' + r.status);
        return r.json();
      }),
      fetch('/api/favorites/' + appDate, { credentials: 'same-origin' }).then(function (r) {
        return r.ok ? r.json() : { ok: false };
      }).catch(function () { return { ok: false }; }),
      fetch('/api/comments-all/' + appDate, { credentials: 'same-origin' }).then(function (r) {
        return r.ok ? r.json() : { ok: false };
      }).catch(function () { return { ok: false }; }),
      loadCCFCatalog(),
      loadHighlightAuthors(),
      loadReadingList(),
    ]).then(function (results) {
      papers = results[0].papers || [];
      extraPapers = results[0].extra_papers || [];
      interactions = (results[1].ok ? results[1].interactions : {}) || {};
      var favData = results[2] || {};
      favorites = (favData.ok ? favData.favorites : {}) || {};
      if (favData.ok && favData.folders && favData.folders.length) favFolders = favData.folders;
      var cmtData = results[3] || {};
      comments = (cmtData.ok ? cmtData.comments : {}) || {};
      papersById = {};
      papers.forEach(function (p, i) {
        p._idx = i;
        papersById[p.paper_id] = p;
      });
      extraPapers.forEach(function (p, i) {
        p._idx = i;
        p._is_extra = true;
        papersById[p.paper_id] = p;
      });
      var controls = document.getElementById('list-controls');
      if (controls && papers.length) {
        controls.style.display = '';
        syncCommunityBtn();
      }
      var fab = document.getElementById('fab-stack');
      if (fab && papers.length) fab.style.display = '';
      refreshDomainFilterBar();
      refreshGradeFilterBar();
      renderPapers();
      renderExtraPapers();
      renderReadingListDrawer();
      scrollToPaperFromHash();
    }).catch(function (err) {
      console.error('loadPapers error:', err);
      document.getElementById('paper-container').innerHTML =
        '<div class="empty-hint">加载失败，请刷新重试。<br><small>' + (err && err.message ? err.message : '') + '</small></div>';
    });
  }

  function netScore(pid) {
    var ia = interactions[pid] || {};
    return (ia.likes || 0) - (ia.dislikes || 0);
  }

  function commentCount(pid) {
    var ia = interactions[pid] || {};
    return ia.comment_count || 0;
  }

  function paperDomainMajor(p) {
    if (!p.domain_tags || !p.domain_tags.length) return '';
    // 取 "主领域 > 子方向" 中第一个的主领域部分
    var first = p.domain_tags[0] || '';
    return first.split(' > ')[0] || '';
  }

  function sortedPapers() {
    var arr = papers.slice();
    // 领域筛选
    if (activeDomainFilter) {
      arr = arr.filter(function (p) {
        var majors = (p.domain_tags || []).map(function (t) {
          return (t || '').split(' > ')[0];
        });
        return majors.indexOf(activeDomainFilter) !== -1;
      });
    }
    // 推荐等级筛选
    if (activeGradeFilter) {
      arr = arr.filter(function (p) {
        var g = paperGrade(p);
        return g && g.key === activeGradeFilter;
      });
    }
    // 黑名单隐藏
    if (hideBlacklisted) {
      arr = arr.filter(function (p) { return !p.blacklisted; });
    }
    arr.sort(function (a, b) {
      var primary = 0;
      // 黑名单置底（不论排序方式）
      var aBl = a.blacklisted ? 1 : 0;
      var bBl = b.blacklisted ? 1 : 0;
      if (aBl !== bBl) return aBl - bBl;

      if (currentSort === 'likes') {
        primary = netScore(b.paper_id) - netScore(a.paper_id);
      } else if (currentSort === 'comments') {
        primary = commentCount(b.paper_id) - commentCount(a.paper_id);
      } else if (currentSort === 'score') {
        primary = (b.score || 0) - (a.score || 0);
      }
      // 第二关键词：推荐等级（必读 > 值得看 > 可跳过），再退回原始序号
      if (primary !== 0) return primary;
      var ga = paperGrade(a), gb = paperGrade(b);
      var gRank = { must: 0, worth: 1, skip: 2 };
      var gaR = ga ? (gRank[ga.key] != null ? gRank[ga.key] : 3) : 3;
      var gbR = gb ? (gRank[gb.key] != null ? gRank[gb.key] : 3) : 3;
      if (gaR !== gbR) return gaR - gbR;
      return a._idx - b._idx;
    });
    return arr;
  }

  function collectDomainMajors() {
    var seen = {};
    var list = [];
    papers.forEach(function (p) {
      (p.domain_tags || []).forEach(function (t) {
        var major = (t || '').split(' > ')[0];
        if (major && !seen[major]) {
          seen[major] = true;
          list.push(major);
        }
      });
    });
    return list;
  }

  function refreshDomainFilterBar() {
    var bar = document.getElementById('domain-filter-bar');
    if (!bar) return;
    var majors = collectDomainMajors();
    if (!majors.length) {
      bar.style.display = 'none';
      return;
    }
    bar.style.display = '';
    var html = '<button class="df-btn' + (activeDomainFilter ? '' : ' active') + '" onclick="ArxivApp.setDomainFilter(\'\',this)">全部</button>';
    majors.forEach(function (m) {
      html += '<button class="df-btn' + (activeDomainFilter === m ? ' active' : '') + '" onclick="ArxivApp.setDomainFilter(\'' + m.replace(/'/g, "\\'") + '\',this)">' + esc(m) + '</button>';
    });
    bar.innerHTML = html;
  }

  function refreshGradeFilterBar() {
    var bar = document.getElementById('grade-filter-bar');
    if (!bar) return;
    // 统计各等级数量
    var counts = { must: 0, worth: 0, skip: 0 };
    papers.forEach(function (p) {
      var g = paperGrade(p);
      if (g && counts[g.key] != null) counts[g.key] += 1;
    });
    // 有评分论文为 0 时不显示等级筛选条
    var total = counts.must + counts.worth + counts.skip;
    if (!total) {
      bar.style.display = 'none';
      return;
    }
    bar.style.display = '';
    var html = '<span class="gf-label">推荐等级</span>';
    html += '<button class="gf-btn' + (activeGradeFilter ? '' : ' active') + '" onclick="ArxivApp.setGradeFilter(\'\',this)">全部 ' + total + '</button>';
    ['must', 'worth', 'skip'].forEach(function (k) {
      var g = GRADES[k];
      html += '<button class="gf-btn gf-' + k + (activeGradeFilter === k ? ' active' : '') + '" onclick="ArxivApp.setGradeFilter(\'' + k + '\',this)">' + g.emoji + ' ' + esc(g.label) + ' ' + counts[k] + '</button>';
    });
    bar.innerHTML = html;
  }

  function setGradeFilter(grade, btn) {
    activeGradeFilter = grade || '';
    var bar = document.getElementById('grade-filter-bar');
    if (bar) {
      bar.querySelectorAll('.gf-btn').forEach(function (b) { b.classList.remove('active'); });
    }
    if (btn) btn.classList.add('active');
    renderPapers();
    var drawer = document.getElementById('toc-drawer');
    if (drawer && drawer.classList.contains('open')) buildToc();
  }

  function renderPapers() {
    var container = document.getElementById('paper-container');
    if (!papers.length) {
      container.innerHTML = '<div class="empty-hint">暂无论文数据。</div>';
      return;
    }
    var html = '';
    sortedPapers().forEach(function (p) {
      var idx = p._idx;
      var ia = interactions[p.paper_id] || { likes: 0, dislikes: 0, user_liked: false, user_disliked: false };
      html += '<article class="paper-card' + paperCardExtraClasses(p) + '" id="' + escAttr(paperDomId(p.paper_id)) + '" data-pid="' + p.paper_id + '">';
      if (paperHasCelebrateAuthor(p)) html += celebrateCardDecorHtml();
      html += '<div class="paper-card-header">';
      html += '<div class="paper-card-title-row">';
      html += '<h2><span class="paper-index">#' + (idx + 1) + '</span><a href="' + esc(p.abs_url) + '" target="_blank">' + esc(p.title) + '</a></h2>';
      html += '<button type="button" class="ia-btn ia-share" data-pid="' + escAttr(p.paper_id) + '" onclick="ArxivApp.copyShareLink(this)" title="复制分享链接">';
      html += '<span class="ia-icon" aria-hidden="true">🔗</span><span class="ia-label ia-share-label">分享</span></button>';
      html += '</div>';
      html += '<div class="paper-meta-line">';
      if (p.source_categories) p.source_categories.forEach(function (c) { html += '<span class="cat-tag">' + esc(c) + '</span>'; });
      if (p.is_cross_list) html += '<span class="badge badge-cross">跨领域</span>';
      if (p.blacklisted) html += '<span class="badge badge-blacklist" title="' + escAttr(p.blacklist_reason || '') + '">黑名单</span>';
      var gr = paperGrade(p);
      if (gr) {
        html += '<span class="badge badge-grade ' + gr.cls + '" title="基于 LLM 评分的推荐等级">' + gr.emoji + ' ' + esc(gr.label) + '</span>';
      }
      if (p.score && p.score > 0) {
        var scoreCls = p.score >= 8 ? 'score-high' : (p.score >= 5 ? 'score-mid' : 'score-low');
        html += '<span class="badge badge-score ' + scoreCls + '">⭐ ' + (Math.round(p.score * 10) / 10) + '</span>';
      }
      html += '<span class="arxiv-version-badge is-pending" aria-hidden="true"></span>';
      html += '</div>';
      html += '</div>';
      // Domain tags
      if (p.domain_tags && p.domain_tags.length) {
        html += '<div class="domain-tag-row">';
        p.domain_tags.forEach(function (t) { html += '<span class="domain-tag">' + esc(t) + '</span>'; });
        html += '</div>';
      }
      // Innovation method
      if (p.innovation_method) {
        html += '<div class="innovation-row"><span class="innovation-label">💡 创新</span><span class="innovation-text">' + esc(p.innovation_method) + '</span></div>';
      }
      // Related organization tags
      if (p.related_org_titles && p.related_org_titles.length) {
        html += '<div class="org-tag-row">';
        p.related_org_titles.forEach(function (org) { html += '<span class="org-tag">' + esc(org) + '</span>'; });
        html += '</div>';
      }
      html += '<div class="paper-authors">' + renderAuthorsHtml(p) + '</div>';
      if (p.comments) {
        html += '<div class="paper-arxiv-comments">';
        html += '<span class="arxiv-meta-label">Comments:</span>';
        html += '<span class="arxiv-comment-text">' + esc(p.comments) + '</span>';
        html += formatCCFTags(p.comments);
        html += '</div>';
      }
      // Abstract (collapsible)
      if (p.abstract) {
        html += '<details class="abstract"><summary>查看摘要</summary><p>' + esc(p.abstract) + '</p></details>';
      }
      html += paperFiguresBlockHtml(p.paper_id);
      // Analysis section (collapsible, 精读懒加载)
      if (p.has_analysis) {
        html += '<details class="analysis-section"><summary>📖 查看深度解读</summary>';
        html += '<div class="analysis-content" data-pid="' + p.paper_id + '"></div>';
        html += '</details>';
      }
      // Interaction bar
      html += '<div class="interaction-bar">';
      html += '<button class="ia-btn ia-like' + (ia.user_liked ? ' active' : '') + '" data-pid="' + p.paper_id + '" onclick="ArxivApp.vote(\'like\',this)">';
      html += '<span class="ia-icon">👍</span><span class="ia-count">' + ia.likes + '</span></button>';
      html += '<button class="ia-btn ia-dislike' + (ia.user_disliked ? ' active' : '') + '" data-pid="' + p.paper_id + '" onclick="ArxivApp.vote(\'dislike\',this)">';
      html += '<span class="ia-icon">👎</span><span class="ia-count">' + ia.dislikes + '</span></button>';
      var fav = favorites[p.paper_id];
      html += '<button class="ia-btn ia-fav' + (fav ? ' active' : '') + '" data-pid="' + p.paper_id + '" onclick="ArxivApp.toggleFavMenu(this)">';
      html += '<span class="ia-icon">' + (fav ? '★' : '☆') + '</span>';
      html += '<span class="ia-label ia-fav-label">' + (fav ? esc(fav.folder) : '收藏') + '</span></button>';
      html += '<button class="ia-btn ia-askai" data-pid="' + p.paper_id + '" onclick="ArxivApp.openAskAI(\'' + escAttr(p.paper_id) + '\')" title="用你自己的 API Key 向 AI 提问（纯本地，对话不经过本服务器）">';
      html += '<span class="ia-icon">🤖</span><span class="ia-label">问AI</span></button>';
      html += '</div>';
      // Comments area (默认展开)
      html += '<div class="comments-area" id="comments-' + p.paper_id + '"></div>';
      html += '</article>';
    });
    container.innerHTML = html;
    // 默认渲染每篇论文的评论（数据已随列表批量拉取，无需逐个请求）
    papers.forEach(function (p) {
      var area = document.getElementById('comments-' + p.paper_id);
      if (area) renderComments(p.paper_id, area, comments[p.paper_id] || []);
    });
    if (appSinglePaperId) {
      var card = container.querySelector('.paper-card[data-pid="' + appSinglePaperId + '"]');
      if (card) {
        var abs = card.querySelector('.abstract');
        if (abs) abs.open = true;
        var analysis = card.querySelector('.analysis-section');
        if (analysis) {
          analysis.open = true;
          renderAnalysis(analysis);
        }
      }
    }
    setupArxivVersionLazyLoad();
    setupPaperFiguresLazyLoad();
  }

  function renderExtraPapers() {
    var existing = document.getElementById('extra-papers-container');
    if (existing) existing.remove();
    if (!extraPapers.length) return;
    if (appSinglePaperId) return; // 单篇分享页不显示

    var container = document.getElementById('paper-container');
    if (!container) return;

    var section = document.createElement('div');
    section.id = 'extra-papers-container';
    section.className = 'extra-papers-section';

    var header = document.createElement('div');
    header.className = 'extra-papers-header';
    header.innerHTML = '<span class="extra-papers-icon">🍱</span>' +
      '<span class="extra-papers-title">额外论文解读</span>' +
      '<span class="extra-papers-count">' + extraPapers.length + ' 篇</span>';
    section.appendChild(header);

    var listDiv = document.createElement('div');
    listDiv.className = 'paper-list-dynamic';

    var html = '';
    extraPapers.forEach(function (p) {
      var idx = p._idx;
      var ia = interactions[p.paper_id] || { likes: 0, dislikes: 0, user_liked: false, user_disliked: false };
      var gr = paperGrade(p);
      html += '<article class="paper-card' + paperCardExtraClasses(p) + '" id="' + escAttr(paperDomId(p.paper_id)) + '" data-pid="' + p.paper_id + '">';
      if (paperHasCelebrateAuthor(p)) html += celebrateCardDecorHtml();
      html += '<div class="paper-card-header">';
      html += '<div class="paper-card-title-row">';
      html += '<h2><span class="paper-index paper-index-extra">加餐</span><a href="' + esc(p.abs_url) + '" target="_blank">' + esc(p.title) + '</a></h2>';
      html += '<button type="button" class="ia-btn ia-share" data-pid="' + escAttr(p.paper_id) + '" onclick="ArxivApp.copyShareLink(this)" title="复制分享链接">';
      html += '<span class="ia-icon" aria-hidden="true">🔗</span><span class="ia-label ia-share-label">分享</span></button>';
      html += '</div>';
      html += '<div class="paper-meta-line">';
      if (p.source_categories) p.source_categories.forEach(function (c) { html += '<span class="cat-tag">' + esc(c) + '</span>'; });
      if (gr) {
        html += '<span class="badge badge-grade ' + gr.cls + '">' + gr.emoji + ' ' + esc(gr.label) + '</span>';
      }
      if (p.score && p.score > 0) {
        var scoreCls = p.score >= 8 ? 'score-high' : (p.score >= 5 ? 'score-mid' : 'score-low');
        html += '<span class="badge badge-score ' + scoreCls + '">⭐ ' + (Math.round(p.score * 10) / 10) + '</span>';
      }
      html += '</div>';
      html += '</div>';
      if (p.domain_tags && p.domain_tags.length) {
        html += '<div class="domain-tag-row">';
        p.domain_tags.forEach(function (t) { html += '<span class="domain-tag">' + esc(t) + '</span>'; });
        html += '</div>';
      }
      if (p.innovation_method) {
        html += '<div class="innovation-row"><span class="innovation-label">💡 创新</span><span class="innovation-text">' + esc(p.innovation_method) + '</span></div>';
      }
      html += '<div class="paper-authors">' + renderAuthorsHtml(p) + '</div>';
      if (p.abstract) {
        html += '<details class="abstract"><summary>查看摘要</summary><p>' + esc(p.abstract) + '</p></details>';
      }
      html += paperFiguresBlockHtml(p.paper_id);
      if (p.has_analysis) {
        html += '<details class="analysis-section"><summary>📖 查看深度解读</summary>';
        html += '<div class="analysis-content" data-pid="' + p.paper_id + '"></div>';
        html += '</details>';
      }
      html += '<div class="interaction-bar">';
      html += '<button class="ia-btn ia-like' + (ia.user_liked ? ' active' : '') + '" data-pid="' + p.paper_id + '" onclick="ArxivApp.vote(\'like\',this)">';
      html += '<span class="ia-icon">👍</span><span class="ia-count">' + ia.likes + '</span></button>';
      html += '<button class="ia-btn ia-dislike' + (ia.user_disliked ? ' active' : '') + '" data-pid="' + p.paper_id + '" onclick="ArxivApp.vote(\'dislike\',this)">';
      html += '<span class="ia-icon">👎</span><span class="ia-count">' + ia.dislikes + '</span></button>';
      html += '<button class="ia-btn ia-askai" data-pid="' + p.paper_id + '" onclick="ArxivApp.openAskAI(\'' + escAttr(p.paper_id) + '\')" title="用你自己的 API Key 向 AI 提问（纯本地）">';
      html += '<span class="ia-icon">🤖</span><span class="ia-label">问AI</span></button>';
      html += '</div>';
      html += '<div class="comments-area" id="comments-' + p.paper_id + '"></div>';
      html += '</article>';
    });
    listDiv.innerHTML = html;
    section.appendChild(listDiv);
    container.appendChild(section);

    extraPapers.forEach(function (p) {
      var area = document.getElementById('comments-' + p.paper_id);
      if (area) renderComments(p.paper_id, area, comments[p.paper_id] || []);
    });
    setupPaperFiguresLazyLoad();
  }

  function arxivVersionCacheKey(date, paperId) {
    return String(date || '') + '/' + String(paperId || '');
  }

  function fetchArxivVersionUncached(date, paperId) {
    return fetch(
      '/api/arxiv-version/' + encodeURIComponent(date) + '/' + encodeURIComponent(paperId),
      { credentials: 'same-origin' }
    ).then(function (r) {
      return r.json().then(function (d) {
        if (!r.ok || !d.ok) throw new Error((d && d.msg) || 'version ' + r.status);
        return d.version;
      });
    });
  }

  function fetchArxivVersion(date, paperId) {
    if (!date || !paperId) return Promise.resolve(1);
    var key = arxivVersionCacheKey(date, paperId);
    if (arxivVersionCache[key] !== undefined) return Promise.resolve(arxivVersionCache[key]);
    if (arxivVersionInflight[key]) return arxivVersionInflight[key];
    var p = fetchArxivVersionUncached(date, paperId).then(function (ver) {
      arxivVersionCache[key] = ver;
      delete arxivVersionInflight[key];
      return ver;
    }).catch(function (err) {
      delete arxivVersionInflight[key];
      throw err;
    });
    arxivVersionInflight[key] = p;
    return p;
  }

  function applyArxivVersionBadge(badge, ver) {
    if (!badge) return;
    badge.classList.remove('is-pending', 'is-failed');
    if (ver <= 1) {
      badge.textContent = '新发布';
      badge.classList.add('is-new');
    } else {
      badge.textContent = '更新 V' + ver;
      badge.classList.add('is-update');
    }
    badge.removeAttribute('aria-hidden');
  }

  function loadArxivVersionForCard(card) {
    if (!card || card.dataset.arxivVerLoaded) return;
    var badge = card.querySelector('.arxiv-version-badge');
    if (!badge) return;
    var pid = card.dataset.pid;
    if (!appDate || !pid) return;
    card.dataset.arxivVerLoaded = '1';
    fetchArxivVersion(appDate, pid).then(function (ver) {
      applyArxivVersionBadge(badge, ver);
    }).catch(function () {
      badge.classList.remove('is-pending');
      badge.classList.add('is-failed');
      badge.setAttribute('aria-hidden', 'true');
    });
  }

  function setupArxivVersionLazyLoad() {
    if (arxivVersionObserver) arxivVersionObserver.disconnect();
    if (!('IntersectionObserver' in window)) {
      document.querySelectorAll('.paper-card[data-pid]').forEach(loadArxivVersionForCard);
      return;
    }
    arxivVersionObserver = new IntersectionObserver(function (entries) {
      entries.forEach(function (entry) {
        if (!entry.isIntersecting) return;
        loadArxivVersionForCard(entry.target);
        arxivVersionObserver.unobserve(entry.target);
      });
    }, { root: null, rootMargin: '280px 0px', threshold: 0.01 });
    document.querySelectorAll('#paper-container .paper-card[data-pid]').forEach(function (card) {
      if (!card.dataset.arxivVerLoaded) arxivVersionObserver.observe(card);
    });
  }

  // 首次展开某篇 <details> 时按需拉取精读 HTML（并发、各自缓存）
  function renderAnalysis(detailsEl) {
    var contentDiv = detailsEl.querySelector('.analysis-content');
    if (!contentDiv || contentDiv.dataset.rendered || contentDiv.dataset.loading) return;
    var pid = contentDiv.dataset.pid;
    contentDiv.dataset.loading = '1';
    contentDiv.innerHTML = '<div class="loading-indicator">加载解读中...</div>';
    var paper = papersById[pid];
    var digest = paper && paper.analysis_digest;
    fetch(analysisApiUrl(pid, digest), { credentials: 'same-origin' })
      .then(function (r) { return r.json(); })
      .then(function (d) {
        var ok = d && d.ok && d.analysis_html;
        contentDiv.innerHTML = ok ? d.analysis_html : '<div class="empty-hint">暂无解读。</div>';
        contentDiv.dataset.rendered = '1';
        contentDiv.removeAttribute('data-loading');
        if (ok && typeof ArxivHL !== 'undefined') {
          ArxivHL.attach(contentDiv, { date: appDate, paperId: pid, user: appUser || null });
        }
      })
      .catch(function () {
        contentDiv.innerHTML = '<div class="empty-hint">加载失败，请重试。</div>';
        contentDiv.removeAttribute('data-loading');
      });
  }

  // setSort / expandAll / collapseAll
  function setSort(mode, btn) {
    currentSort = mode;
    var group = document.querySelector('.lc-sort');
    if (group) {
      group.querySelectorAll('.lc-btn').forEach(function (b) { b.classList.remove('active'); });
    }
    if (btn) btn.classList.add('active');
    renderPapers();
    // 目录打开时按新排序刷新
    var drawer = document.getElementById('toc-drawer');
    if (drawer && drawer.classList.contains('open')) buildToc();
  }

  function setDomainFilter(domain, btn) {
    activeDomainFilter = domain || '';
    var bar = document.getElementById('domain-filter-bar');
    if (bar) {
      bar.querySelectorAll('.df-btn').forEach(function (b) { b.classList.remove('active'); });
    }
    if (btn) btn.classList.add('active');
    renderPapers();
    var drawer = document.getElementById('toc-drawer');
    if (drawer && drawer.classList.contains('open')) buildToc();
  }

  function toggleBlacklistHide(btn) {
    hideBlacklisted = !hideBlacklisted;
    if (btn) btn.classList.toggle('active', hideBlacklisted);
    renderPapers();
  }

  function expandAll() {
    var container = document.getElementById('paper-container');
    container.querySelectorAll('.analysis-section').forEach(function (d) {
      d.open = true;
      renderAnalysis(d);
    });
  }

  function collapseAll() {
    var container = document.getElementById('paper-container');
    container.querySelectorAll('details.analysis-section').forEach(function (d) { d.open = false; });
  }

  function syncCommunityBtn() {
    var btn = document.getElementById('lc-community-btn');
    if (btn) btn.classList.toggle('active', showCommunityMarks);
  }

  function toggleCommunityMarks(btn) {
    showCommunityMarks = !showCommunityMarks;
    try { localStorage.setItem('arxivShowCommunityHl', showCommunityMarks ? '1' : '0'); } catch (e) { /* ignore */ }
    if (btn) btn.classList.toggle('active', showCommunityMarks);
    else syncCommunityBtn();
    if (typeof ArxivHL !== 'undefined') ArxivHL.setShowCommunity(showCommunityMarks);
  }

  // ── 浮动按钮：目录 / 上下篇 / 回到顶部 ──
  var SCROLL_OFFSET = 80; // 顶部吸附 header 的高度

  function getCards() {
    var c = document.getElementById('paper-container');
    return c ? Array.prototype.slice.call(c.querySelectorAll('.paper-card')) : [];
  }

  function scrollToCard(card) {
    if (!card) return;
    var y = card.getBoundingClientRect().top + window.scrollY - SCROLL_OFFSET;
    window.scrollTo({ top: Math.max(0, y), behavior: 'smooth' });
  }

  function scrollTop() {
    window.scrollTo({ top: 0, behavior: 'smooth' });
  }

  // dir>0 下一篇（视口上沿之下第一篇的开头）；dir<0 上一篇（视口上沿之上最后一篇的开头）
  function gotoPaper(dir) {
    var cards = getCards();
    if (!cards.length) return;
    if (dir > 0) {
      for (var i = 0; i < cards.length; i++) {
        if (cards[i].getBoundingClientRect().top - SCROLL_OFFSET > 1) { scrollToCard(cards[i]); return; }
      }
      scrollToCard(cards[cards.length - 1]);
    } else {
      for (var j = cards.length - 1; j >= 0; j--) {
        if (cards[j].getBoundingClientRect().top - SCROLL_OFFSET < -1) { scrollToCard(cards[j]); return; }
      }
      scrollTop();
    }
  }

  function toggleToc() {
    var drawer = document.getElementById('toc-drawer');
    var backdrop = document.getElementById('toc-backdrop');
    if (!drawer) return;
    if (drawer.classList.contains('open')) {
      drawer.classList.remove('open');
      if (backdrop) backdrop.classList.remove('open');
    } else {
      buildToc();
      drawer.classList.add('open');
      if (backdrop) backdrop.classList.add('open');
    }
  }

  function buildToc() {
    var listEl = document.getElementById('toc-list');
    if (!listEl) return;
    var cards = getCards(); // DOM 顺序即当前排序顺序
    var html = '';
    cards.forEach(function (card, i) {
      var pid = card.dataset.pid;
      var p = papersById[pid] || {};
      html += '<li><button class="toc-item" type="button" data-pid="' + escAttr(pid) + '">';
      html += '<span class="toc-num">' + (i + 1) + '</span>';
      html += '<span class="toc-origin">#' + ((p._idx != null ? p._idx : i) + 1) + '</span>';
      var tocGr = paperGrade(p);
      if (tocGr) html += '<span class="toc-grade ' + tocGr.cls + '">' + tocGr.emoji + '</span>';
      if (card.classList.contains('paper-card--celebrate')) {
        html += '<span class="toc-celebrate" aria-hidden="true"></span>';
      }
      html += '<span class="toc-title">' + esc(p.title || pid) + '</span>';
      html += '</button></li>';
    });
    listEl.innerHTML = html;
    listEl.querySelectorAll('.toc-item').forEach(function (btn) {
      btn.addEventListener('click', function () {
        var pid = btn.dataset.pid;
        var card = null;
        getCards().some(function (c) { if (c.dataset.pid === pid) { card = c; return true; } return false; });
        if (card) { scrollToCard(card); toggleToc(); }
      });
    });
  }

  function vote(type, btn) {
    var pid = btn.dataset.pid;
    fetch('/api/' + type, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      credentials: 'same-origin',
      body: JSON.stringify({ date: appDate, paper_id: pid }),
    }).then(function (r) { return r.json(); }).then(function (d) {
      if (!d.ok) {
        if (d.msg && d.msg.indexOf('cookie') >= 0) showAuthModal();
        return;
      }
      // 同步内存中的交互数据，保证重新排序时计数正确
      var ia = interactions[pid] || {};
      ia.likes = d.likes;
      ia.dislikes = d.dislikes;
      ia.user_liked = d.user_liked;
      ia.user_disliked = d.user_disliked;
      interactions[pid] = ia;
      // Update UI
      var card = btn.closest('.paper-card');
      var likeBtn = card.querySelector('.ia-like');
      var dislikeBtn = card.querySelector('.ia-dislike');
      likeBtn.classList.toggle('active', d.user_liked);
      dislikeBtn.classList.toggle('active', d.user_disliked);
      likeBtn.querySelector('.ia-count').textContent = d.likes;
      dislikeBtn.querySelector('.ia-count').textContent = d.dislikes;
      loadReadingList().then(function () {
        renderReadingListDrawer();
      });
    });
  }

  function loadComments(pid, area) {
    fetch('/api/comments/' + appDate + '/' + pid, { credentials: 'same-origin' })
      .then(function (r) { return r.json(); })
      .then(function (d) {
        var list = d.comments || [];
        comments[pid] = list;
        var ia = interactions[pid] || {};
        ia.comment_count = list.length;
        interactions[pid] = ia;
        renderComments(pid, area, list);
      });
  }

  function renderComments(pid, area, list) {
    list = list || [];
    var html = '';
    if (list.length) {
      html += '<div class="comments-list">';
      list.forEach(function (c) {
        html += '<div class="comment-item">';
        html += '<span class="comment-user">' + esc(c.username) + '</span>';
        html += '<span class="comment-text">' + esc(c.text) + '</span>';
        html += '<span class="comment-time">' + c.created_at.substring(0, 16) + '</span>';
        if (appUser === c.username) {
          html += '<button class="comment-delete" onclick="ArxivApp.deleteComment(\'' + c.id + '\',\'' + pid + '\',this)">&times;</button>';
        }
        html += '</div>';
      });
      html += '</div>';
    } else {
      html += '<div class="comments-empty">还没有评论，来抢沙发～</div>';
    }
    if (appUser) {
      html += '<div class="comment-form">';
      html += '<input type="text" maxlength="50" placeholder="友善交流，理性讨论～（最多 50 字）" class="comment-input">';
      html += '<span class="comment-counter">0/50</span>';
      html += '<button class="comment-submit" onclick="ArxivApp.postComment(\'' + pid + '\',this)">发送</button>';
      html += '</div>';
    } else {
      html += '<div class="comment-login-hint"><a href="#" onclick="showAuthModal();return false">登录</a>后可以评论</div>';
    }
    area.innerHTML = html;

    // Counter
    var input = area.querySelector('.comment-input');
    if (input) {
      input.addEventListener('input', function () {
        area.querySelector('.comment-counter').textContent = this.value.length + '/50';
      });
      input.addEventListener('keydown', function (e) {
        if (e.key === 'Enter') {
          area.querySelector('.comment-submit').click();
        }
      });
    }
  }

  function postComment(pid, btn) {
    var area = btn.closest('.comments-area');
    var input = area.querySelector('.comment-input');
    var text = input.value.trim();
    if (!text) return;
    fetch('/api/comment', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      credentials: 'same-origin',
      body: JSON.stringify({ date: appDate, paper_id: pid, text: text }),
    }).then(function (r) { return r.json(); }).then(function (d) {
      if (d.ok) {
        input.value = '';
        area.querySelector('.comment-counter').textContent = '0/50';
        loadComments(pid, area);
      }
    });
  }

  function deleteComment(cid, pid, btn) {
    fetch('/api/comment/' + cid, { method: 'DELETE', credentials: 'same-origin' })
      .then(function (r) { return r.json(); })
      .then(function (d) {
        if (d.ok) {
          var area = btn.closest('.comments-area');
          loadComments(pid, area);
        }
      });
  }

  // ── Favorites（收藏 + 文件夹分类） ──

  var favMenuEl = null;

  function closeFavMenu() {
    if (favMenuEl) { favMenuEl.remove(); favMenuEl = null; }
  }

  document.addEventListener('mousedown', function (e) {
    if (favMenuEl && !favMenuEl.contains(e.target) && !(e.target.closest && e.target.closest('.ia-fav'))) {
      closeFavMenu();
    }
  });

  function toggleFavMenu(btn) {
    if (!appUser) { if (typeof showAuthModal === 'function') showAuthModal(); return; }
    var pid = btn.dataset.pid;
    if (favMenuEl && favMenuEl.dataset.pid === pid) { closeFavMenu(); return; }
    closeFavMenu();
    var fav = favorites[pid];
    var menu = document.createElement('div');
    menu.className = 'fav-menu';
    menu.dataset.pid = pid;
    var html = '';
    html += '<div class="fav-menu-title">' + (fav ? '已收藏 · 移动到' : '收藏到文件夹') + '</div>';
    html += '<div class="fav-folder-list">';
    favFolders.forEach(function (f) {
      var active = fav && fav.folder === f;
      html += '<button class="fav-folder-item' + (active ? ' active' : '') + '" data-folder="' + escAttr(f) + '" onclick="ArxivApp.favTo(\'' + escAttr(pid) + '\',this)">';
      html += '<span class="fav-folder-name">' + esc(f) + '</span>' + (active ? '<span class="fav-check">✓</span>' : '');
      html += '</button>';
    });
    html += '</div>';
    html += '<div class="fav-newfolder"><input type="text" class="fav-newfolder-input" maxlength="50" placeholder="新建文件夹…"><button class="fav-newfolder-btn" onclick="ArxivApp.favNewFolder(\'' + escAttr(pid) + '\',this)">新建</button></div>';
    if (fav) {
      html += '<button class="fav-remove" onclick="ArxivApp.favRemove(\'' + escAttr(pid) + '\')">取消收藏</button>';
    }
    menu.innerHTML = html;
    document.body.appendChild(menu);
    var rect = btn.getBoundingClientRect();
    menu.style.top = (rect.bottom + window.scrollY + 6) + 'px';
    var left = rect.left + window.scrollX;
    var overflow = (left + menu.offsetWidth) - (window.scrollX + document.documentElement.clientWidth - 8);
    if (overflow > 0) left -= overflow;
    menu.style.left = Math.max(8, left) + 'px';
    favMenuEl = menu;
    var input = menu.querySelector('.fav-newfolder-input');
    if (input) {
      input.addEventListener('keydown', function (e) {
        if (e.key === 'Enter') { e.preventDefault(); menu.querySelector('.fav-newfolder-btn').click(); }
      });
    }
  }

  function favTo(pid, el) {
    addFavorite(pid, el.dataset.folder);
  }

  function favNewFolder(pid, btn) {
    var input = btn.parentNode.querySelector('.fav-newfolder-input');
    var name = (input.value || '').trim();
    if (!name) return;
    fetch('/api/favorite-folder', {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, credentials: 'same-origin',
      body: JSON.stringify({ name: name }),
    }).then(function (r) { return r.json(); }).then(function (d) {
      if (d.ok || (d.msg && d.msg.indexOf('已存在') >= 0)) {
        if (favFolders.indexOf(name) < 0) favFolders.push(name);
        addFavorite(pid, name);
      }
    });
  }

  function addFavorite(pid, folder) {
    var p = papersById[pid] || {};
    fetch('/api/favorite', {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, credentials: 'same-origin',
      body: JSON.stringify({ date: appDate, paper_id: pid, title: p.title || '', abs_url: p.abs_url || '', folder: folder }),
    }).then(function (r) { return r.json(); }).then(function (d) {
      if (d.ok) {
        favorites[pid] = { folder: d.folder };
        if (favFolders.indexOf(d.folder) < 0) favFolders.push(d.folder);
        updateFavButton(pid);
        closeFavMenu();
      } else if (d.msg && d.msg.indexOf('登录') >= 0) {
        if (typeof showAuthModal === 'function') showAuthModal();
      }
    });
  }

  function favRemove(pid) {
    fetch('/api/favorite/' + appDate + '/' + encodeURIComponent(pid), { method: 'DELETE', credentials: 'same-origin' })
      .then(function (r) { return r.json(); }).then(function (d) {
        if (d.ok) { delete favorites[pid]; updateFavButton(pid); closeFavMenu(); }
      });
  }

  function updateFavButton(pid) {
    var btns = document.querySelectorAll('.ia-fav');
    for (var i = 0; i < btns.length; i++) {
      if (btns[i].dataset.pid !== pid) continue;
      var fav = favorites[pid];
      btns[i].classList.toggle('active', !!fav);
      btns[i].querySelector('.ia-icon').textContent = fav ? '★' : '☆';
      btns[i].querySelector('.ia-fav-label').textContent = fav ? fav.folder : '收藏';
    }
  }

  function esc(s) {
    var d = document.createElement('div');
    d.textContent = s || '';
    return d.innerHTML;
  }

  function escAttr(s) {
    return (s || '').replace(/&/g, '&amp;').replace(/"/g, '&quot;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
  }

  // 「问 AI」入口：把当前论文对象 + 日期传给 askai 模块（若已加载）
  function openAskAI(pid) {
    var p = papersById[pid];
    if (!p) return;
    if (window.ArxivAskAI && typeof window.ArxivAskAI.open === 'function') {
      window.ArxivAskAI.open(pid, appDate, p);
    } else {
      alert('问 AI 模块未加载，请刷新页面重试。');
    }
  }

  return {
    init: init,
    initShare: initShare,
    copyShareLink: copyShareLink,
    vote: vote,
    postComment: postComment,
    deleteComment: deleteComment,
    setSort: setSort,
    setDomainFilter: setDomainFilter,
    setGradeFilter: setGradeFilter,
    toggleBlacklistHide: toggleBlacklistHide,
    expandAll: expandAll,
    collapseAll: collapseAll,
    toggleCommunityMarks: toggleCommunityMarks,
    toggleToc: toggleToc,
    toggleReadingList: toggleReadingList,
    setReadingRead: setReadingRead,
    gotoReadingPaper: gotoReadingPaper,
    gotoPaper: gotoPaper,
    scrollTop: scrollTop,
    toggleFavMenu: toggleFavMenu,
    favTo: favTo,
    favNewFolder: favNewFolder,
    favRemove: favRemove,
    openAskAI: openAskAI,
  };
})();
