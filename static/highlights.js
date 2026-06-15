/* ArxivHL — 共享划线（高亮）模块
 * - 全员可见：他人标记 → 浅绿虚线下划线（不显示标注者）
 * - 登录用户自己的标记 → 彩色高亮，可删改/评论
 * 用法：ArxivHL.attach(containerEl, { date, paperId, user });
 */
var ArxivHL = (function () {
  var ZOTERO_COLORS = [
    { name: '黄', value: '#ffd400' },
    { name: '红', value: '#ff6666' },
    { name: '绿', value: '#5fb236' },
    { name: '蓝', value: '#2ea8e5' },
    { name: '紫', value: '#a28ae5' },
    { name: '品红', value: '#e56eee' },
    { name: '橙', value: '#f19837' },
    { name: '灰', value: '#aaaaaa' },
  ];

  var toolbar = null;
  var markPanel = null;
  var pending = null;
  var activeMark = null;
  var showCommunityEnabled = true;
  var attachedContainers = [];

  function isCoarsePointer() {
    return window.matchMedia('(hover: none) and (pointer: coarse)').matches;
  }

  function attach(container, opts) {
    if (!container || container.dataset.hlAttached) return;
    container.dataset.hlAttached = '1';
    opts = opts || {};
    var rec = { container: container, date: opts.date, paperId: opts.paperId, ownTexts: new Set() };
    attachedContainers.push(rec);

    function afterOwn(ownTexts) {
      rec.ownTexts = ownTexts || new Set();
      if (showCommunityEnabled) restoreCommunity(container, opts, rec.ownTexts);
    }

    if (opts.user) {
      bindInteraction(container, opts);
      restoreOwn(container, opts, afterOwn);
    } else {
      afterOwn(null);
    }
  }

  function collectOwnTexts(container) {
    var set = new Set();
    container.querySelectorAll('mark.paper-highlight').forEach(function (m) {
      var t = (m.textContent || '').trim();
      if (t) set.add(t);
    });
    return set;
  }

  function removeAllCommunity(container) {
    container.querySelectorAll('.hl-community-mark').forEach(function (el) { unwrapCommunity(el); });
  }

  function setShowCommunity(enabled) {
    showCommunityEnabled = !!enabled;
    attachedContainers.forEach(function (rec) {
      if (!showCommunityEnabled) {
        removeAllCommunity(rec.container);
      } else {
        var skip = collectOwnTexts(rec.container);
        restoreCommunity(rec.container, { date: rec.date, paperId: rec.paperId }, skip);
      }
    });
  }

  function bindInteraction(container, opts) {
    container.addEventListener('mouseup', function () { onSelectEnd(opts); });
    container.addEventListener('touchend', function () {
      var delay = isCoarsePointer() ? 120 : 10;
      setTimeout(function () { onSelectEnd(opts); }, delay);
    }, { passive: true });
    container.addEventListener('click', function (e) { onClickMark(e, container); });
  }

  function onSelectEnd(opts) {
    var sel = window.getSelection();
    if (!sel || sel.isCollapsed || !sel.rangeCount || !sel.toString().trim()) return;
    var text = sel.toString().trim();
    if (text.length > 500) text = text.substring(0, 500);
    showToolbar(sel, text, opts);
  }

  function showToolbar(sel, text, opts) {
    removeToolbar();
    removeMarkPanel();
    var range = sel.getRangeAt(0);
    var rect = range.getBoundingClientRect();
    var touchMode = isCoarsePointer();
    pending = { date: opts.date, paperId: opts.paperId, text: text, range: range.cloneRange() };
    var el = document.createElement('div');
    el.id = 'hl-toolbar';
    el.className = touchMode ? 'hl-toolbar hl-toolbar-bottom' : 'hl-toolbar';
    var preview = text.length > 72 ? text.substring(0, 72) + '…' : text;
    var swatches = '';
    ZOTERO_COLORS.forEach(function (c) {
      swatches += '<button type="button" class="hl-swatch" title="' + c.name + '" aria-label="' + c.name + '" style="background:' + c.value + '" data-color="' + c.value + '"></button>';
    });
    if (touchMode) {
      el.innerHTML =
        '<div class="hl-toolbar-sheet">' +
        '<div class="hl-toolbar-head">' +
        '<span class="hl-toolbar-label">标记选中文字</span>' +
        '<button type="button" class="hl-toolbar-close" aria-label="关闭">&times;</button>' +
        '</div>' +
        '<p class="hl-toolbar-preview">' + escHtml(preview) + '</p>' +
        '<div class="hl-toolbar-colors">' + swatches + '</div>' +
        '</div>';
    } else {
      el.innerHTML = swatches;
    }
    el.addEventListener('mousedown', function (ev) { ev.preventDefault(); });
    el.addEventListener('touchstart', function (ev) { ev.stopPropagation(); }, { passive: true });
    el.querySelectorAll('.hl-swatch').forEach(function (btn) {
      btn.addEventListener('click', function () {
        if (isCoarsePointer()) return;
        save(btn.dataset.color);
      });
      btn.addEventListener('touchend', function (ev) {
        if (!isCoarsePointer()) return;
        ev.preventDefault();
        save(btn.dataset.color);
      });
    });
    var closeBtn = el.querySelector('.hl-toolbar-close');
    if (closeBtn) closeBtn.addEventListener('click', function () { cancelToolbar(); });
    document.body.appendChild(el);
    if (touchMode) {
      document.body.classList.add('hl-toolbar-open');
      positionBottomToolbar(el);
    } else {
      positionToolbar(el, rect);
    }
    toolbar = el;
  }

  function showMarkPanel(mark) {
    removeMarkPanel();
    removeToolbar();
    activeMark = mark;
    var touchMode = isCoarsePointer();
    var comment = (mark.dataset.hlcomment || '').substring(0, 200);
    var preview = (mark.textContent || '').trim();
    if (preview.length > 120) preview = preview.substring(0, 120) + '…';

    var el = document.createElement('div');
    el.id = 'hl-mark-panel';
    el.className = touchMode ? 'hl-mark-panel hl-mark-panel-bottom' : 'hl-mark-panel';

    var commentBlock = comment
      ? '<div class="hl-mark-comment-show"><span class="hl-mark-comment-label">评论</span><p>' + escHtml(comment) + '</p></div>'
      : '';

    var inner =
      '<div class="hl-mark-sheet">' +
      '<div class="hl-toolbar-head">' +
      '<span class="hl-toolbar-label">标记</span>' +
      '<button type="button" class="hl-toolbar-close hl-mark-close" aria-label="关闭">&times;</button>' +
      '</div>' +
      '<p class="hl-toolbar-preview hl-mark-preview">' + escHtml(preview) + '</p>' +
      commentBlock +
      '<div class="hl-mark-comment-edit" style="display:none">' +
      '<label class="hl-mark-comment-label" for="hl-mark-input">编辑评论</label>' +
      '<textarea id="hl-mark-input" class="hl-mark-input" maxlength="200" rows="3" placeholder="写下你的想法…"></textarea>' +
      '<span class="hl-mark-counter">0/200</span>' +
      '</div>' +
      '<div class="hl-mark-actions">' +
      '<button type="button" class="hl-mark-btn hl-mark-btn-danger" data-action="delete">删除</button>' +
      '<button type="button" class="hl-mark-btn hl-mark-btn-primary" data-action="comment">' + (comment ? '编辑评论' : '评论') + '</button>' +
      '<button type="button" class="hl-mark-btn hl-mark-btn-save" data-action="save" style="display:none">保存</button>' +
      '</div>' +
      '</div>';

    el.innerHTML = inner;
    el.addEventListener('mousedown', function (ev) { ev.preventDefault(); });
    el.addEventListener('touchstart', function (ev) { ev.stopPropagation(); }, { passive: true });

    var input = el.querySelector('.hl-mark-input');
    var editWrap = el.querySelector('.hl-mark-comment-edit');
    var saveBtn = el.querySelector('[data-action="save"]');
    var commentBtn = el.querySelector('[data-action="comment"]');
    var counter = el.querySelector('.hl-mark-counter');

    function syncCounter() {
      if (counter && input) counter.textContent = input.value.length + '/200';
    }
    if (input) {
      input.value = comment;
      syncCounter();
      input.addEventListener('input', syncCounter);
    }

    el.querySelector('.hl-mark-close').addEventListener('click', function () { removeMarkPanel(); });
    el.querySelector('[data-action="delete"]').addEventListener('click', function () { deleteMark(mark); });

    var commentShow = el.querySelector('.hl-mark-comment-show');
    commentBtn.addEventListener('click', function () {
      if (editWrap) editWrap.style.display = '';
      if (commentShow) commentShow.style.display = 'none';
      if (saveBtn) saveBtn.style.display = '';
      commentBtn.style.display = 'none';
      if (input) {
        input.focus();
        if (input.setSelectionRange) {
          var len = input.value.length;
          input.setSelectionRange(len, len);
        }
      }
    });

    if (saveBtn) {
      saveBtn.addEventListener('click', function () {
        saveMarkComment(mark, input ? input.value : '');
      });
    }

    if (!touchMode) {
      var rect = mark.getBoundingClientRect();
      document.body.appendChild(el);
      positionMarkPanel(el, rect);
    } else {
      document.body.appendChild(el);
      document.body.classList.add('hl-mark-panel-open');
    }
    markPanel = el;
  }

  function positionMarkPanel(el, rect) {
    var margin = 8;
    var top = rect.bottom + window.scrollY + 8;
    if (rect.bottom + el.offsetHeight + 16 > window.innerHeight) {
      top = rect.top + window.scrollY - el.offsetHeight - 8;
    }
    var left = rect.left + rect.width / 2 + window.scrollX;
    var half = el.offsetWidth / 2;
    var minL = window.scrollX + margin + half;
    var maxL = window.scrollX + document.documentElement.clientWidth - margin - half;
    left = Math.max(minL, Math.min(left, maxL));
    el.style.top = top + 'px';
    el.style.left = left + 'px';
    el.style.transform = 'translateX(-50%)';
  }

  function removeMarkPanel() {
    if (markPanel) { markPanel.remove(); markPanel = null; }
    activeMark = null;
    document.body.classList.remove('hl-mark-panel-open');
  }

  function escHtml(s) {
    var d = document.createElement('div');
    d.textContent = s || '';
    return d.innerHTML;
  }

  function cancelToolbar() {
    removeToolbar();
    pending = null;
    var sel = window.getSelection();
    if (sel) sel.removeAllRanges();
  }

  function positionBottomToolbar(el) {
    el.style.top = '';
    el.style.left = '';
    el.style.transform = '';
  }

  function positionToolbar(el, rect) {
    var margin = 8;
    var top = rect.top + window.scrollY - el.offsetHeight - 8;
    if (rect.top - el.offsetHeight - 8 < 0) {
      top = rect.bottom + window.scrollY + 8;
    }
    var half = el.offsetWidth / 2;
    var center = rect.left + rect.width / 2;
    var minCenter = window.scrollX + margin + half;
    var maxCenter = window.scrollX + document.documentElement.clientWidth - margin - half;
    center = Math.max(minCenter, Math.min(center + window.scrollX, maxCenter));
    el.style.top = top + 'px';
    el.style.left = center + 'px';
  }

  function removeToolbar() {
    if (toolbar) { toolbar.remove(); toolbar = null; }
    document.body.classList.remove('hl-toolbar-open');
  }

  function isHlPanelTarget(target) {
    return target && target.closest && (
      target.closest('#hl-toolbar') || target.closest('#hl-mark-panel')
    );
  }

  document.addEventListener('mousedown', function (e) {
    if ((toolbar || markPanel) && !isHlPanelTarget(e.target)) {
      removeToolbar();
      removeMarkPanel();
    }
  });
  document.addEventListener('touchstart', function (e) {
    if ((toolbar || markPanel) && !isHlPanelTarget(e.target)) {
      removeToolbar();
      removeMarkPanel();
    }
  }, { passive: true });

  function save(color) {
    if (!pending) return;
    var p = pending;
    var range = p.range ? p.range.cloneRange() : null;
    var sel = window.getSelection();
    fetch('/api/highlight', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      credentials: 'same-origin',
      body: JSON.stringify({ date: p.date, paper_id: p.paperId, text: p.text, color: color }),
    }).then(function (r) { return r.json(); }).then(function (d) {
      removeToolbar();
      if (d.ok && range) {
        var scope = getAnalysisContainer(range);
        if (scope) removeCommunityByText(scope, p.text);
        try {
          var mark = document.createElement('mark');
          mark.className = 'paper-highlight';
          mark.style.background = color;
          if (d.highlight && d.highlight.id) mark.dataset.hlid = d.highlight.id;
          range.surroundContents(mark);
          if (sel) sel.removeAllRanges();
        } catch (e) { /* 跨元素选区无法包裹 */ }
      }
    }).catch(function () { removeToolbar(); });
  }

  function getAnalysisContainer(range) {
    if (!range) return null;
    var node = range.commonAncestorContainer;
    if (node.nodeType === 3) node = node.parentElement;
    return node && node.closest ? node.closest('.analysis-content') : null;
  }

  function findMarkFromTarget(target, container) {
    if (!target || !container) return null;
    var display = target.closest ? target.closest('.hl-comment-display') : null;
    if (display) {
      var prev = display.previousElementSibling;
      if (prev && prev.classList && prev.classList.contains('paper-highlight')) return prev;
    }
    var mark = target.closest ? target.closest('mark.paper-highlight') : null;
    if (mark && container.contains(mark)) return mark;
    return null;
  }

  function onClickMark(e, container) {
    var mark = findMarkFromTarget(e.target, container);
    if (!mark) return;
    e.preventDefault();
    e.stopPropagation();
    var hlid = mark.dataset.hlid;
    if (!hlid) { unwrapOwn(mark); return; }
    showMarkPanel(mark);
  }

  function deleteMark(mark) {
    var hlid = mark.dataset.hlid;
    if (!hlid) { unwrapOwn(mark); removeMarkPanel(); return; }
    fetch('/api/highlight/' + encodeURIComponent(hlid), { method: 'DELETE', credentials: 'same-origin' })
      .then(function (r) { return r.json(); })
      .then(function (d) {
        if (d.ok) {
          var text = (mark.textContent || '').trim();
          var container = mark.closest('.analysis-content') || mark.parentElement;
          unwrapOwn(mark);
          removeMarkPanel();
          if (container && text && showCommunityEnabled) {
            underlineText(container, text);
          }
        }
      })
      .catch(function () {});
  }

  function saveMarkComment(mark, text) {
    var hlid = mark.dataset.hlid;
    if (!hlid) return;
    var comment = (text || '').trim();
    if (comment.length > 200) comment = comment.substring(0, 200);
    var url = '/api/highlight/' + encodeURIComponent(hlid) + '/comment';
    fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      credentials: 'same-origin',
      body: JSON.stringify({ comment: comment }),
    }).then(function (r) {
      return r.json().then(function (d) { return { ok: r.ok, d: d }; });
    }).then(function (res) {
      if (res.d && res.d.ok) {
        if (comment) {
          mark.dataset.hlcomment = comment;
          mark.classList.add('has-hl-comment');
        } else {
          delete mark.dataset.hlcomment;
          mark.classList.remove('has-hl-comment');
        }
        updateCommentDisplay(mark, comment);
        removeMarkPanel();
      } else if (res.d && res.d.msg) {
        alert(res.d.msg);
      }
    }).catch(function () { alert('保存评论失败，请稍后重试'); });
  }

  function updateCommentDisplay(mark, comment) {
    var existing = mark.nextElementSibling;
    if (existing && existing.classList.contains('hl-comment-display')) {
      existing.remove();
    }
    if (!comment) return;
    var display = document.createElement('span');
    display.className = 'hl-comment-display';
    display.setAttribute('role', 'button');
    display.setAttribute('tabindex', '0');
    display.title = '查看标记评论';
    display.innerHTML = '<span class="hl-comment-icon" aria-hidden="true">💬</span><span class="hl-comment-text">' + escHtml(comment) + '</span>';
    mark.after(display);
  }

  function unwrapOwn(mark) {
    var next = mark.nextElementSibling;
    if (next && next.classList.contains('hl-comment-display')) next.remove();
    unwrapElement(mark);
  }

  function unwrapCommunity(el) {
    unwrapElement(el);
  }

  function unwrapElement(el) {
    var parent = el.parentNode;
    if (!parent) return;
    while (el.firstChild) parent.insertBefore(el.firstChild, el);
    parent.removeChild(el);
    if (parent.normalize) parent.normalize();
  }

  function removeCommunityByText(scope, text) {
    if (!scope || !text) return;
    scope.querySelectorAll('.hl-community-mark').forEach(function (el) {
      if ((el.textContent || '').trim() === text) unwrapCommunity(el);
    });
  }

  function applyMark(mark, h) {
    if (h.id) mark.dataset.hlid = h.id;
    if (h.comment) {
      mark.dataset.hlcomment = h.comment;
      mark.classList.add('has-hl-comment');
      updateCommentDisplay(mark, h.comment);
    }
  }

  function isInsideHlNode(node, container) {
    var p = node.parentElement;
    while (p && p !== container) {
      if (p.classList && (
        p.classList.contains('paper-highlight') ||
        p.classList.contains('hl-community-mark')
      )) return true;
      p = p.parentElement;
    }
    return false;
  }

  function restoreOwn(container, opts, done) {
    fetch('/api/highlights/' + opts.date + '/' + encodeURIComponent(opts.paperId), { credentials: 'same-origin' })
      .then(function (r) { return r.json(); })
      .then(function (d) {
        var ownTexts = new Set();
        if (d.ok && d.highlights) {
          d.highlights.forEach(function (h) {
            var t = (h.text || '').trim();
            if (t) ownTexts.add(t);
            highlightOwnText(container, h);
          });
        }
        if (done) done(ownTexts);
      })
      .catch(function () { if (done) done(new Set()); });
  }

  function restoreCommunity(container, opts, skipTexts) {
    if (!showCommunityEnabled) return;
    fetch('/api/highlights-community/' + opts.date + '/' + encodeURIComponent(opts.paperId), { credentials: 'same-origin' })
      .then(function (r) { return r.json(); })
      .then(function (d) {
        if (!d.ok || !d.marks) return;
        d.marks.forEach(function (m) {
          var text = (m.text || '').trim();
          if (!text) return;
          if (skipTexts && skipTexts.has(text)) return;
          underlineText(container, text);
        });
      })
      .catch(function () {});
  }

  function highlightOwnText(container, h) {
    var text = h.text;
    var color = h.color;
    var tw = document.createTreeWalker(container, NodeFilter.SHOW_TEXT);
    var node;
    while ((node = tw.nextNode())) {
      if (isInsideHlNode(node, container)) continue;
      var idx = node.textContent.indexOf(text);
      if (idx >= 0) {
        try {
          var range = document.createRange();
          range.setStart(node, idx);
          range.setEnd(node, idx + text.length);
          removeCommunityByText(container, text);
          var mark = document.createElement('mark');
          mark.className = 'paper-highlight';
          mark.style.background = color;
          range.surroundContents(mark);
          applyMark(mark, h);
        } catch (e) { /* ignore */ }
        return;
      }
    }
  }

  function underlineText(container, text) {
    var tw = document.createTreeWalker(container, NodeFilter.SHOW_TEXT);
    var node;
    while ((node = tw.nextNode())) {
      if (isInsideHlNode(node, container)) continue;
      var idx = node.textContent.indexOf(text);
      if (idx >= 0) {
        try {
          var range = document.createRange();
          range.setStart(node, idx);
          range.setEnd(node, idx + text.length);
          var span = document.createElement('span');
          span.className = 'hl-community-mark';
          span.setAttribute('title', '此处曾被标记');
          range.surroundContents(span);
        } catch (e) { /* ignore */ }
        return;
      }
    }
  }

  return { attach: attach, setShowCommunity: setShowCommunity, COLORS: ZOTERO_COLORS };
})();
