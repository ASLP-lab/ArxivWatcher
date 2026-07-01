/* ArxivAskAI — 纯本地「问 AI」模块
 *
 * 特点：
 *   - 用户自己配置 API（OpenAI 兼容协议 / Anthropic 协议），Key 只存在浏览器 localStorage
 *   - 对话全部在浏览器发起，直接打到大模型厂商；本服务器只负责提供 PDF 全文文本
 *   - 对话历史按 论文 ID 存到 localStorage，不写服务器
 */
var ArxivAskAI = (function () {
  'use strict';

  var CFG_KEY = 'arxiv_askai_config';
  var CHAT_PREFIX = 'arxiv_askai_chat_';

  // ── DOM ──
  var overlay = null;
  var bodyEl = null;
  var inputEl = null;
  var sendBtn = null;
  var settingsPanel = null;
  var currentPid = null;
  var currentDate = null;
  var currentPaper = null;
  var busy = false;
  // 缓存的上下文（PDF 全文 + 解读 + 属性），打开一次论文只取一次
  var ctxCache = null;
  var ctxLoading = false;

  function defaultConfig() {
    return {
      provider: 'openai',
      openai_base_url: 'https://api.openai.com/v1',
      openai_api_key: '',
      openai_model: 'gpt-4o',
      anthropic_api_key: '',
      anthropic_model: 'claude-sonnet-4-20250514',
      anthropic_base_url: 'https://api.anthropic.com',
      system_prompt: '你是一位学术论文阅读助手。请基于提供的论文全文、系统已有的解读和元数据，回答用户的问题。回答用中文，简洁清晰。',
      include_fulltext: true,
      include_analysis: true,
      include_meta: true,
    };
  }

  function loadConfig() {
    try {
      var raw = localStorage.getItem(CFG_KEY);
      if (!raw) return defaultConfig();
      var c = JSON.parse(raw);
      var d = defaultConfig();
      // 合并，保证新字段有默认值
      for (var k in d) if (!(k in c)) c[k] = d[k];
      return c;
    } catch (e) { return defaultConfig(); }
  }

  function saveConfig(cfg) {
    try { localStorage.setItem(CFG_KEY, JSON.stringify(cfg)); } catch (e) {}
  }

  function loadChat(pid) {
    try {
      var raw = localStorage.getItem(CHAT_PREFIX + pid);
      if (!raw) return [];
      var arr = JSON.parse(raw);
      return Array.isArray(arr) ? arr : [];
    } catch (e) { return []; }
  }

  function saveChat(pid, msgs) {
    try { localStorage.setItem(CHAT_PREFIX + pid, JSON.stringify(msgs)); } catch (e) {}
  }

  function clearChat(pid) {
    try { localStorage.removeItem(CHAT_PREFIX + pid); } catch (e) {}
  }

  // ── 构建 DOM（首次调用时）──
  function ensureDOM() {
    if (overlay) return;
    overlay = document.createElement('div');
    overlay.className = 'askai-overlay';
    overlay.innerHTML =
      '<div class="askai-card">' +
        '<div class="askai-head">' +
          '<span class="askai-drag-handle" title="按住拖动">⋮⋮</span>' +
          '<span class="askai-head-title">🤖 问 AI <small id="askai-paper-name"></small></span>' +
          '<button class="askai-head-btn" id="askai-toggle-settings" title="API 设置">⚙️ 设置</button>' +
          '<button class="askai-head-btn" id="askai-clear-chat" title="清空当前论文的对话">🗑️ 清空</button>' +
          '<button class="askai-head-close" id="askai-close" title="关闭">&times;</button>' +
        '</div>' +
        '<div class="askai-privacy">🔒 本功能完全在本地运行：你的 API Key 和对话只存在当前浏览器，不经过本服务器。清除浏览器数据后会丢失。</div>' +
        '<div class="askai-settings" id="askai-settings">' +
          '<h4>API 配置（仅存于本浏览器）</h4>' +
          '<div class="askai-provider-tabs">' +
            '<button class="askai-provider-tab active" data-provider="openai">OpenAI 兼容</button>' +
            '<button class="askai-provider-tab" data-provider="anthropic">Anthropic</button>' +
          '</div>' +
          '<div class="askai-field" data-for="openai">' +
            '<label>API Base URL</label>' +
            '<input id="askai-openai-base" type="text" placeholder="https://api.openai.com/v1">' +
            '<div class="askai-hint">OpenAI / DeepSeek / 通义 / Ollama / vLLM 等兼容接口。DeepSeek 用 https://api.deepseek.com/v1</div>' +
          '</div>' +
          '<div class="askai-field" data-for="openai">' +
            '<label>API Key</label>' +
            '<input id="askai-openai-key" type="password" placeholder="sk-...">' +
          '</div>' +
          '<div class="askai-field" data-for="openai">' +
            '<label>模型名</label>' +
            '<input id="askai-openai-model" type="text" placeholder="gpt-4o / deepseek-chat / qwen2.5:72b">' +
          '</div>' +
          '<div class="askai-field" data-for="anthropic" style="display:none">' +
            '<label>Anthropic API Key</label>' +
            '<input id="askai-anthropic-key" type="password" placeholder="sk-ant-...">' +
          '</div>' +
          '<div class="askai-field" data-for="anthropic" style="display:none">' +
            '<label>模型名</label>' +
            '<input id="askai-anthropic-model" type="text" placeholder="claude-sonnet-4-20250514 / MiniMax-M2.7">' +
          '</div>' +
          '<div class="askai-field" data-for="anthropic" style="display:none">' +
            '<label>Base URL（MiniMax: https://api.minimaxi.com/anthropic）</label>' +
            '<input id="askai-anthropic-base" type="text" placeholder="https://api.anthropic.com">' +
          '</div>' +
          '<div class="askai-field">' +
            '<label>系统提示词（可选）</label>' +
            '<textarea id="askai-system-prompt" rows="2" style="width:100%;font-size:12px;padding:6px 10px;border:1px solid var(--border,#e5e7eb);border-radius:6px;box-sizing:border-box;resize:vertical"></textarea>' +
          '</div>' +
          '<label class="askai-ctx-toggle"><input type="checkbox" id="askai-ctx-fulltext" checked> 附带 PDF 全文</label>' +
          '<label class="askai-ctx-toggle"><input type="checkbox" id="askai-ctx-analysis" checked> 附带系统解读</label>' +
          '<label class="askai-ctx-toggle"><input type="checkbox" id="askai-ctx-meta" checked> 附带论文属性（标题/作者/评分等）</label>' +
          '<div class="askai-settings-actions">' +
            '<button class="askai-save-btn" id="askai-save-settings">保存</button>' +
            '<button class="askai-clear-btn" id="askai-reset-settings">恢复默认</button>' +
          '</div>' +
        '</div>' +
        '<div class="askai-body" id="askai-body"></div>' +
        '<div class="askai-foot">' +
          '<div class="askai-input-row">' +
            '<textarea class="askai-input" id="askai-input" rows="1" placeholder="向 AI 提问这篇论文…（Enter 发送，Shift+Enter 换行）"></textarea>' +
            '<button class="askai-send" id="askai-send">发送</button>' +
          '</div>' +
          '<div class="askai-foot-meta">' +
            '<span id="askai-status">就绪</span>' +
            '<button id="askai-reload-ctx">重新加载论文上下文</button>' +
          '</div>' +
        '</div>' +
      '</div>';
    document.body.appendChild(overlay);
    bodyEl = overlay.querySelector('#askai-body');
    inputEl = overlay.querySelector('#askai-input');
    sendBtn = overlay.querySelector('#askai-send');
    settingsPanel = overlay.querySelector('#askai-settings');

    // 自动增高 textarea
    inputEl.addEventListener('input', function () {
      inputEl.style.height = 'auto';
      inputEl.style.height = Math.min(120, inputEl.scrollHeight) + 'px';
    });
    inputEl.addEventListener('keydown', function (e) {
      if (e.key === 'Enter' && !e.shiftKey) {
        e.preventDefault();
        doSend();
      }
    });

    overlay.querySelector('#askai-close').addEventListener('click', close);
    overlay.querySelector('#askai-send').addEventListener('click', doSend);
    overlay.querySelector('#askai-clear-chat').addEventListener('click', function () {
      if (currentPid && confirm('确定清空当前论文的对话记录？此操作仅影响本地存储。')) {
        clearChat(currentPid);
        renderChat([]);
      }
    });
    overlay.querySelector('#askai-toggle-settings').addEventListener('click', function () {
      settingsPanel.classList.toggle('open');
      this.classList.toggle('active', settingsPanel.classList.contains('open'));
      if (settingsPanel.classList.contains('open')) fillSettingsForm();
    });
    overlay.querySelector('#askai-save-settings').addEventListener('click', function () {
      var cfg = readSettingsForm();
      saveConfig(cfg);
      settingsPanel.classList.remove('open');
      overlay.querySelector('#askai-toggle-settings').classList.remove('active');
      setStatus('已保存配置');
    });
    overlay.querySelector('#askai-reset-settings').addEventListener('click', function () {
      if (confirm('恢复全部设置为默认值？（会清除已保存的配置）')) {
        saveConfig(defaultConfig());
        fillSettingsForm();
      }
    });
    overlay.querySelectorAll('.askai-provider-tab').forEach(function (tab) {
      tab.addEventListener('click', function () {
        var p = tab.dataset.provider;
        overlay.querySelectorAll('.askai-provider-tab').forEach(function (t) { t.classList.remove('active'); });
        tab.classList.add('active');
        overlay.querySelectorAll('.askai-field[data-for]').forEach(function (f) {
          f.style.display = (f.dataset.for === p) ? '' : 'none';
        });
      });
    });
    overlay.querySelector('#askai-reload-ctx').addEventListener('click', function () {
      ctxCache = null;
      if (currentPid) {
        loadContext(currentDate, currentPaper, function () { renderChat(loadChat(currentPid)); });
      }
    });
    // 拖动逻辑（标题栏为手柄）
    enableDrag();
    // ESC 关闭
    document.addEventListener('keydown', escCloseHandler);
  }

  // ESC 关闭弹窗
  function escCloseHandler(e) {
    if (e.key === 'Escape' && overlay && overlay.classList.contains('open')) {
      // 输入框聚焦时不触发，避免打字时误关
      if (document.activeElement && document.activeElement.tagName === 'TEXTAREA') return;
      close();
    }
  }

  // ── 拖动（标题栏拖动整个卡片）──
  function enableDrag() {
    var head = overlay.querySelector('.askai-head');
    var card = overlay.querySelector('.askai-card');
    if (!head || !card) return;
    var dragging = false;
    var startX = 0, startY = 0;
    var offsetX = 0, offsetY = 0; // 当前相对居中位置的位移

    head.addEventListener('mousedown', function (e) {
      // 点到按钮时不触发拖动
      if (e.target.closest('button')) return;
      dragging = true;
      startX = e.clientX;
      startY = e.clientY;
      card.classList.add('dragging');
      e.preventDefault();
    });

    document.addEventListener('mousemove', function (e) {
      if (!dragging) return;
      var dx = e.clientX - startX;
      var dy = e.clientY - startY;
      offsetX += dx;
      offsetY += dy;
      startX = e.clientX;
      startY = e.clientY;
      // 限制在视口内（粗略）
      var rect = card.getBoundingClientRect();
      var cx = rect.left + rect.width / 2;
      var cy = rect.top + rect.height / 2;
      var maxOffX = (window.innerWidth / 2) - 40;
      var maxOffY = (window.innerHeight / 2) - 40;
      offsetX = Math.max(-maxOffX, Math.min(maxOffX, offsetX));
      offsetY = Math.max(-maxOffY, Math.min(maxOffY, offsetY));
      card.style.transform = 'translate(' + offsetX + 'px,' + offsetY + 'px)';
    });

    document.addEventListener('mouseup', function () {
      if (!dragging) return;
      dragging = false;
      card.classList.remove('dragging');
    });

    // 触摸支持
    head.addEventListener('touchstart', function (e) {
      if (e.target.closest('button')) return;
      if (e.touches.length !== 1) return;
      dragging = true;
      startX = e.touches[0].clientX;
      startY = e.touches[0].clientY;
      card.classList.add('dragging');
    }, { passive: true });
    document.addEventListener('touchmove', function (e) {
      if (!dragging || e.touches.length !== 1) return;
      var dx = e.touches[0].clientX - startX;
      var dy = e.touches[0].clientY - startY;
      offsetX += dx;
      offsetY += dy;
      startX = e.touches[0].clientX;
      startY = e.touches[0].clientY;
      var rect = card.getBoundingClientRect();
      var maxOffX = (window.innerWidth / 2) - 40;
      var maxOffY = (window.innerHeight / 2) - 40;
      offsetX = Math.max(-maxOffX, Math.min(maxOffX, offsetX));
      offsetY = Math.max(-maxOffY, Math.min(maxOffY, offsetY));
      card.style.transform = 'translate(' + offsetX + 'px,' + offsetY + 'px)';
    }, { passive: true });
    document.addEventListener('touchend', function () {
      if (!dragging) return;
      dragging = false;
      card.classList.remove('dragging');
    });
  }

  function fillSettingsForm() {
    var c = loadConfig();
    overlay.querySelector('#askai-openai-base').value = c.openai_base_url || '';
    overlay.querySelector('#askai-openai-key').value = c.openai_api_key || '';
    overlay.querySelector('#askai-openai-model').value = c.openai_model || '';
    overlay.querySelector('#askai-anthropic-key').value = c.anthropic_api_key || '';
    overlay.querySelector('#askai-anthropic-model').value = c.anthropic_model || '';
    overlay.querySelector('#askai-anthropic-base').value = c.anthropic_base_url || 'https://api.anthropic.com';
    overlay.querySelector('#askai-system-prompt').value = c.system_prompt || '';
    overlay.querySelector('#askai-ctx-fulltext').checked = !!c.include_fulltext;
    overlay.querySelector('#askai-ctx-analysis').checked = !!c.include_analysis;
    overlay.querySelector('#askai-ctx-meta').checked = !!c.include_meta;
    // 切换 tab 显示
    var p = c.provider || 'openai';
    overlay.querySelectorAll('.askai-provider-tab').forEach(function (t) {
      t.classList.toggle('active', t.dataset.provider === p);
    });
    overlay.querySelectorAll('.askai-field[data-for]').forEach(function (f) {
      f.style.display = (f.dataset.for === p) ? '' : 'none';
    });
  }

  function readSettingsForm() {
    var provider = 'openai';
    overlay.querySelectorAll('.askai-provider-tab').forEach(function (t) {
      if (t.classList.contains('active')) provider = t.dataset.provider;
    });
    return {
      provider: provider,
      openai_base_url: (overlay.querySelector('#askai-openai-base').value || '').trim(),
      openai_api_key: (overlay.querySelector('#askai-openai-key').value || '').trim(),
      openai_model: (overlay.querySelector('#askai-openai-model').value || '').trim(),
      anthropic_api_key: (overlay.querySelector('#askai-anthropic-key').value || '').trim(),
      anthropic_model: (overlay.querySelector('#askai-anthropic-model').value || '').trim(),
      anthropic_base_url: (overlay.querySelector('#askai-anthropic-base').value || 'https://api.anthropic.com').trim(),
      system_prompt: overlay.querySelector('#askai-system-prompt').value || '',
      include_fulltext: overlay.querySelector('#askai-ctx-fulltext').checked,
      include_analysis: overlay.querySelector('#askai-ctx-analysis').checked,
      include_meta: overlay.querySelector('#askai-ctx-meta').checked,
    };
  }

  // ── 上下文构建：PDF 全文 + 解读 + 属性 ──
  function loadContext(date, paper, cb) {
    var cfg = loadConfig();
    var parts = [];
    // 1) 属性（直接来自 paper 对象）
    if (cfg.include_meta) {
      parts.push('【论文元数据】\n' + buildMetaBlock(paper));
    }
    // 2) 系统解读（paper.analysis markdown，可能随列表已带，也可能需要懒加载）
    var analysisStep = function (next) {
      if (!cfg.include_analysis) { next(); return; }
      var md = paper.analysis || '';
      if (md && md.indexOf('[LLM') !== 0) {
        parts.push('【系统已有的解读】\n' + md);
        next();
      } else {
        // 懒加载
        setStatus('正在加载系统解读…');
        var digest = paper.analysis_digest;
        var url = digest
          ? '/api/h/' + digest + '/analysis/' + date + '/' + encodeURIComponent(paper.paper_id)
          : '/api/analysis/' + date + '/' + encodeURIComponent(paper.paper_id);
        fetch(url, { credentials: 'same-origin' })
          .then(function (r) { return r.json(); })
          .then(function (d) {
            // analysis 端点返回的是 html；提取纯文本作为参考
            if (d && d.ok) {
              var tmp = document.createElement('div');
              tmp.innerHTML = d.analysis_html || '';
              var txt = (tmp.textContent || '').trim();
              if (txt) parts.push('【系统已有的解读】\n' + txt);
            }
            next();
          })
          .catch(function () { next(); });
      }
    };
    // 3) PDF 全文
    var fulltextStep = function (next) {
      if (!cfg.include_fulltext) { next(); return; }
      setStatus('正在提取 PDF 全文（首次较慢，已提取的会缓存）…');
      ctxLoading = true;
      fetch('/api/paper-text/' + date + '/' + encodeURIComponent(paper.paper_id), { credentials: 'same-origin' })
        .then(function (r) { return r.json(); })
        .then(function (d) {
          ctxLoading = false;
          if (d && d.ok && d.text) {
            // 截断保护：太长的全文截断
            var t = d.text;
            if (t.length > 60000) t = t.slice(0, 60000) + '\n\n[... 全文过长，已截断 ...]';
            parts.push('【论文 PDF 全文】\n' + t);
            setStatus(d.cached ? '上下文就绪（PDF 文本已缓存）' : '上下文就绪（本次新提取了 PDF）');
          } else {
            setStatus('PDF 全文获取失败：' + (d && d.msg ? d.msg : '未知错误') + '（仍可基于元数据/解读对话）');
          }
          next();
        })
        .catch(function (err) {
          ctxLoading = false;
          setStatus('PDF 全文获取失败：网络错误。仍可基于元数据/解读对话。');
          next();
        });
    };

    analysisStep(function () {
      fulltextStep(function () {
        ctxCache = parts.join('\n\n');
        cb();
      });
    });
  }

  function buildMetaBlock(paper) {
    var lines = [];
    if (paper.title) lines.push('标题: ' + paper.title);
    if (paper.authors && paper.authors.length) lines.push('作者: ' + paper.authors.join(', '));
    if (paper.subjects) lines.push('分类: ' + paper.subjects);
    if (paper.comments) lines.push('Comments: ' + paper.comments);
    if (paper.abs_url) lines.push('链接: ' + paper.abs_url);
    if (paper.score) lines.push('LLM 评分: ' + paper.score);
    if (paper.domain_tags && paper.domain_tags.length) lines.push('领域: ' + paper.domain_tags.join(' | '));
    if (paper.innovation_method) lines.push('创新方法: ' + paper.innovation_method);
    if (paper.related_org_titles && paper.related_org_titles.length) lines.push('相关单位: ' + paper.related_org_titles.join(', '));
    if (paper.blacklisted) lines.push('命中黑名单: ' + (paper.blacklist_reason || '是'));
    return lines.join('\n');
  }

  // ── 渲染对话 ──
  function escapeHtml(s) {
    return (s || '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
  }

  function renderMarkdownLite(text) {
    // 极简 markdown：代码块、行内代码、加粗、换行。避免引入额外依赖。
    var html = escapeHtml(text);
    // 代码块
    html = html.replace(/```([\s\S]*?)```/g, function (_, code) {
      return '<pre><code>' + code.replace(/^\n/, '') + '</code></pre>';
    });
    // 行内代码
    html = html.replace(/`([^`\n]+)`/g, '<code>$1</code>');
    // 加粗
    html = html.replace(/\*\*([^*\n]+)\*\*/g, '<strong>$1</strong>');
    // 换行
    html = html.replace(/\n/g, '<br>');
    return html;
  }

  function appendMsg(role, content) {
    var div = document.createElement('div');
    div.className = 'askai-msg ' + role;
    if (role === 'user') {
      div.textContent = content;
    } else {
      div.innerHTML = renderMarkdownLite(content);
    }
    bodyEl.appendChild(div);
    bodyEl.scrollTop = bodyEl.scrollHeight;
    return div;
  }

  function renderChat(msgs) {
    bodyEl.innerHTML = '';
    if (!msgs.length) {
      var empty = document.createElement('div');
      empty.className = 'askai-empty';
      empty.innerHTML = '向 AI 提问这篇论文，例如：<br>「这篇论文的核心创新是什么？」<br>「实验设置有哪些局限？」<br>「和 XXX 方法相比有何优劣？」';
      bodyEl.appendChild(empty);
      return;
    }
    msgs.forEach(function (m) {
      if (m.role === 'system') return; // 系统消息不直接展示
      appendMsg(m.role === 'user' ? 'user' : 'assistant', m.content);
    });
  }

  function setStatus(t) {
    var el = overlay.querySelector('#askai-status');
    if (el) el.textContent = t;
  }

  // ── 发送 ──
  function doSend() {
    if (busy) return;
    var cfg = loadConfig();
    if (!isConfigured(cfg)) {
      settingsPanel.classList.add('open');
      overlay.querySelector('#askai-toggle-settings').classList.add('active');
      fillSettingsForm();
      setStatus('请先完成 API 设置');
      return;
    }
    var text = (inputEl.value || '').trim();
    if (!text) return;
    if (!ctxCache) {
      setStatus('上下文还在加载，请稍候…');
      return;
    }

    // 组装消息
    var msgs = loadChat(currentPid);
    // 首次提问时注入上下文 system 消息
    var sysContent = (cfg.system_prompt ? cfg.system_prompt + '\n\n' : '') +
      '以下是本次提问的论文背景资料，请主要基于这些内容回答：\n\n' + ctxCache;
    var hasSys = msgs.some(function (m) { return m.role === 'system'; });
    if (!hasSys) {
      msgs.unshift({ role: 'system', content: sysContent });
    } else {
      // 更新 system（论文上下文可能因重新加载变化）
      msgs[0].content = sysContent;
    }
    msgs.push({ role: 'user', content: text });
    saveChat(currentPid, msgs);

    // UI
    inputEl.value = '';
    inputEl.style.height = 'auto';
    // 移除空提示
    var empty = bodyEl.querySelector('.askai-empty');
    if (empty) empty.remove();
    appendMsg('user', text);
    // 创建 AI 消息占位（流式填充）
    var aiEl = document.createElement('div');
    aiEl.className = 'askai-msg assistant askai-streaming';
    aiEl.innerHTML = '<span class="askai-typing-inline">AI 思考中…</span>';
    bodyEl.appendChild(aiEl);
    bodyEl.scrollTop = bodyEl.scrollHeight;
    setBusy(true);
    setStatus('生成中…');

    var acc = '';
    streamLLM(cfg, msgs, {
      onToken: function (piece) {
        if (!acc) {
          aiEl.innerHTML = ''; // 清掉"思考中"
        }
        acc += piece;
        aiEl.innerHTML = renderMarkdownLite(acc);
        bodyEl.scrollTop = bodyEl.scrollHeight;
      },
      onDone: function () {
        aiEl.classList.remove('askai-streaming');
        if (!acc.trim()) {
          aiEl.innerHTML = '<em>（AI 返回为空）</em>';
          setStatus('AI 返回为空');
          setBusy(false);
          return;
        }
        msgs.push({ role: 'assistant', content: acc });
        saveChat(currentPid, msgs);
        setStatus('完成（对话已存到本地）');
        setBusy(false);
      },
      onError: function (err) {
        aiEl.classList.remove('askai-streaming');
        if (acc) {
          // 已有部分输出：保留并提示中断
          aiEl.innerHTML = renderMarkdownLite(acc) +
            '<div class="askai-msg-error">⚠️ 生成中断：' + escapeHtml(err) + '</div>';
        } else {
          aiEl.remove();
          var eDiv = document.createElement('div');
          eDiv.className = 'askai-msg-error';
          eDiv.textContent = '⚠️ ' + err;
          bodyEl.appendChild(eDiv);
        }
        bodyEl.scrollTop = bodyEl.scrollHeight;
        setStatus('失败：' + err);
        setBusy(false);
      },
    });
  }

  function setBusy(b) {
    busy = b;
    if (sendBtn) sendBtn.disabled = b;
    if (inputEl) inputEl.disabled = b;
  }

  function isConfigured(cfg) {
    if (cfg.provider === 'anthropic') {
      return !!(cfg.anthropic_api_key && cfg.anthropic_model);
    }
    return !!(cfg.openai_api_key && cfg.openai_model && cfg.openai_base_url);
  }

  // ── LLM 调用：OpenAI 协议（流式 SSE）──
  function callOpenAI(cfg, messages, handlers) {
    var base = (cfg.openai_base_url || '').replace(/\/+$/, '');
    var url = base.endsWith('/chat/completions') ? base : base + '/chat/completions';
    fetch(url, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'Authorization': 'Bearer ' + cfg.openai_api_key,
      },
      body: JSON.stringify({
        model: cfg.openai_model,
        messages: messages,
        temperature: 0.3,
        max_tokens: 2048,
        stream: true,
      }),
    }).then(function (resp) {
      if (!resp.ok || !resp.body) {
        // 非 2xx：尝试解析错误 JSON
        return resp.json().then(function (d) {
          var msg = (d && d.error && d.error.message) || ('HTTP ' + resp.status);
          throw new Error(msg);
        }, function () { throw new Error('HTTP ' + resp.status); });
      }
      return readSSE(resp.body, function (dataStr) {
        // OpenAI stream chunk: {"choices":[{"delta":{"content":"xxx"}}]}
        if (dataStr === '[DONE]') return;
        try {
          var obj = JSON.parse(dataStr);
          var delta = obj.choices && obj.choices[0] && obj.choices[0].delta;
          if (delta && delta.content) handlers.onToken(delta.content);
        } catch (e) { /* 忽略解析失败的行 */ }
      });
    }).then(function (full) {
      handlers.onDone(full || '');
    }).catch(function (err) {
      handlers.onError(err.message || String(err));
    });
  }

  // ── LLM 调用：Anthropic 协议（流式 SSE）──
  function isOfficialAnthropicHost(base) {
    try {
      var u = new URL(base.indexOf('://') >= 0 ? base : 'https://' + base);
      return u.hostname === 'api.anthropic.com';
    } catch (e) {
      return false;
    }
  }

  /** 第三方兼容端点（MiniMax 等）CORS 通常不允许 anthropic-version，改用 Bearer 鉴权。 */
  function buildAnthropicHeaders(cfg, base) {
    var key = cfg.anthropic_api_key || '';
    var headers = { 'Content-Type': 'application/json' };
    if (isOfficialAnthropicHost(base)) {
      headers['x-api-key'] = key;
      headers['anthropic-version'] = '2023-06-01';
      headers['anthropic-dangerous-direct-browser-access'] = 'true';
    } else {
      headers['Authorization'] = 'Bearer ' + key;
    }
    return headers;
  }

  function callAnthropic(cfg, messages, handlers) {
    var base = (cfg.anthropic_base_url || 'https://api.anthropic.com').replace(/\/+$/, '');
    var url = base + '/v1/messages';
    var systemText = '';
    var conv = [];
    messages.forEach(function (m) {
      if (m.role === 'system') {
        systemText += (systemText ? '\n\n' : '') + m.content;
      } else {
        conv.push({ role: m.role === 'assistant' ? 'assistant' : 'user', content: m.content });
      }
    });
    if (conv.length === 0) { handlers.onError('没有对话内容'); return; }
    fetch(url, {
      method: 'POST',
      headers: buildAnthropicHeaders(cfg, base),
      body: JSON.stringify({
        model: cfg.anthropic_model,
        max_tokens: 2048,
        system: systemText,
        messages: conv,
        stream: true,
      }),
    }).then(function (resp) {
      if (!resp.ok || !resp.body) {
        return resp.json().then(function (d) {
          var msg = (d && d.error && d.error.message) || ('HTTP ' + resp.status);
          throw new Error(msg);
        }, function () { throw new Error('HTTP ' + resp.status); });
      }
      return readSSE(resp.body, function (dataStr) {
        // Anthropic stream events: content_block_delta { delta: { text } }
        try {
          var obj = JSON.parse(dataStr);
          if (obj.type === 'content_block_delta' && obj.delta && obj.delta.text) {
            handlers.onToken(obj.delta.text);
          }
        } catch (e) { /* 忽略 */ }
      });
    }).then(function (full) {
      handlers.onDone(full || '');
    }).catch(function (err) {
      var msg = err.message || String(err);
      if (msg === 'Failed to fetch' || /NetworkError|Load failed/i.test(msg)) {
        msg = '网络请求失败。若控制台有 CORS 报错，说明该 API 不允许浏览器直连——可换 OpenAI 兼容端点，或改用支持 CORS 的代理 Base URL。';
      }
      handlers.onError(msg);
    });
  }

  // 通用 SSE 读取：按 \n\n 分块解析 data: 行，拼接文本并回调
  // 返回 Promise<完整文本>
  function readSSE(stream, onData) {
    var reader = stream.getReader();
    var decoder = new TextDecoder('utf-8');
    var buffer = '';
    var full = '';
    function pump() {
      return reader.read().then(function (result) {
        if (result.done) {
          // 处理残留
          if (buffer.trim()) parseSSEChunk(buffer, onData);
          return full;
        }
        buffer += decoder.decode(result.value, { stream: true });
        // SSE 事件以空行分隔
        var idx;
        while ((idx = buffer.indexOf('\n\n')) >= 0) {
          var chunk = buffer.slice(0, idx);
          buffer = buffer.slice(idx + 2);
          var piece = parseSSEChunk(chunk, onData);
          if (piece) full += piece;
        }
        return pump();
      });
    }
    return pump();
  }

  // 解析单个 SSE 事件块，提取所有 data: 行的内容，调用 onData(data)，返回拼接的增量文本（若有）
  function parseSSEChunk(chunk, onData) {
    var lines = chunk.split('\n');
    var dataLines = [];
    lines.forEach(function (ln) {
      if (ln.indexOf('data:') === 0) {
        dataLines.push(ln.slice(5).replace(/^\s/, ''));
      }
    });
    if (!dataLines.length) return '';
    var dataStr = dataLines.join('\n');
    var before = '';
    onData(dataStr);
    // 对 OpenAI/Anthropic 的 delta，无法在这里精确拿到增量文本（需解析 JSON），
    // 但为了累积 full 文本，在 callOpenAI/callAnthropic 的 onData 回调里已经处理 token。
    // 这里返回空即可，完整文本在 onToken 时由调用方累积。
    return '';
  }

  function streamLLM(cfg, messages, handlers) {
    if (cfg.provider === 'anthropic') callAnthropic(cfg, messages, handlers);
    else callOpenAI(cfg, messages, handlers);
  }

  // ── 对外入口 ──
  function open(pid, date, paper) {
    ensureDOM();
    currentPid = pid;
    currentDate = date;
    currentPaper = paper;
    ctxCache = null;
    var nameEl = overlay.querySelector('#askai-paper-name');
    if (nameEl) nameEl.textContent = (paper && paper.title) ? '· ' + paper.title.slice(0, 50) : '';
    overlay.classList.add('open');
    // 不锁定 body 滚动 —— 弹窗可拖动，主界面保持可交互
    var card = overlay.querySelector('.askai-card');
    if (card) card.style.transform = ''; // 重置上次拖动位置

    var cfg = loadConfig();
    if (!isConfigured(cfg)) {
      settingsPanel.classList.add('open');
      overlay.querySelector('#askai-toggle-settings').classList.add('active');
      fillSettingsForm();
      setStatus('请先配置 API（仅存于本地浏览器）');
    } else {
      setStatus('就绪');
    }
    renderChat(loadChat(pid));
    // 后台加载上下文
    loadContext(date, paper, function () {
      if (overlay.classList.contains('open') && isConfigured(loadConfig())) {
        setStatus('上下文就绪，可以提问了');
      }
    });
    setTimeout(function () { inputEl && inputEl.focus(); }, 50);
  }

  function close() {
    if (!overlay) return;
    overlay.classList.remove('open');
    document.removeEventListener('keydown', escCloseHandler);
  }

  return { open: open, close: close };
})();
