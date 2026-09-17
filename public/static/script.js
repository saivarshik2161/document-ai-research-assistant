/**
 * Document AI — Research Assistant Frontend Script
 * Production RAG workspace with exact math (KaTeX), exact tables (marked.js),
 * exact graphs (Chart.js), external web sources, and persistent chat history.
 */

(function () {
  'use strict';

  // --- State ---
  const state = {
    documents: [],
    workspaceSummary: { total_documents: 0, total_pages: 0, total_chunks: 0 },
    currentSessionId: localStorage.getItem('doc_ai_active_session_id') || ('sess_' + Date.now()),
    currentMessages: [],
    isQuerying: false,
    viewerDoc: null,
    viewerPages: [],
    viewerPageIndex: 0,
    theme: localStorage.getItem('document_ai_theme') || 'dark',
    chartInstances: {},
  };

  // --- Client-side Session / User Isolation ---
  function getUserId() {
    let uid = localStorage.getItem('doc_ai_user_id');
    if (!uid) {
      uid = 'usr_' + Math.random().toString(36).substring(2, 9) + '_' + Date.now().toString(36);
      localStorage.setItem('doc_ai_user_id', uid);
    }
    return uid;
  }

  // --- API Endpoint Resolver for Localhost & Netlify Cross-Origin ---
  function getBackendBase() {
    return localStorage.getItem('doc_ai_backend_url') || '';
  }

  function apiUrl(path) {
    const base = getBackendBase();
    const cleanPath = path.startsWith('/') ? path : '/' + path;
    const separator = cleanPath.includes('?') ? '&' : '?';
    const pathWithSession = cleanPath + separator + 'session_id=' + encodeURIComponent(getUserId());
    if (!base) return pathWithSession;
    return base.replace(/\/+$/, '') + pathWithSession;
  }

  // Intercept fetch calls to always attach X-Session-ID header
  const _origFetch = window.fetch;
  window.fetch = function (resource, init) {
    init = init || {};
    const headers = new Headers(init.headers || {});
    if (!headers.has('X-Session-ID')) {
      headers.set('X-Session-ID', getUserId());
    }
    init.headers = headers;
    return _origFetch.call(this, resource, init);
  };

  // --- DOM Elements ---
  const $ = (id) => document.getElementById(id);
  const qsa = (sel) => document.querySelectorAll(sel);

  const el = {
    html: document.documentElement,
    themeToggleBtn: $('themeToggleBtn'),
    clearAllBtn: $('clearAllBtn'),
    newChatBtn: $('newChatBtn'),
    historyBtn: $('historyBtn'),
    historyCountBadge: $('historyCountBadge'),
    toastContainer: $('toastContainer'),

    // Sources & Stats
    statDocs: $('statDocs'),
    statPages: $('statPages'),
    statChunks: $('statChunks'),
    sourcesList: $('sourcesList'),
    sourcesCountPill: $('sourcesCountPill'),
    sourcesEmptyState: $('sourcesEmptyState'),
    triggerUploadBtn: $('triggerUploadBtn'),
    dropzone: $('dropzone'),
    fileInput: $('fileInput'),
    uploadStatus: $('uploadStatus'),
    uploadStatusText: $('uploadStatusText'),
    progressFill: $('progressFill'),

    // Chat
    chatThread: $('chatThread'),
    chatWelcome: $('chatWelcome'),
    messagesContainer: $('messagesContainer'),
    composerInput: $('composerInput'),
    sendMessageBtn: $('sendMessageBtn'),

    // Citation Modal
    citationModal: $('citationModal'),
    citationDocName: $('citationDocName'),
    citationPageBadge: $('citationPageBadge'),
    citationExcerptText: $('citationExcerptText'),
    openDocumentPageBtn: $('openDocumentPageBtn'),

    // Document Viewer Modal
    docViewerModal: $('docViewerModal'),
    viewerDocTitle: $('viewerDocTitle'),
    viewerPageStats: $('viewerPageStats'),
    viewerDownloadLink: $('viewerDownloadLink'),
    viewerPrevBtn: $('viewerPrevBtn'),
    viewerCurPage: $('viewerCurPage'),
    viewerTotalPages: $('viewerTotalPages'),
    viewerNextBtn: $('viewerNextBtn'),
    viewerPageText: $('viewerPageText'),

    // Chat History Modal
    historyModal: $('historyModal'),
    historyList: $('historyList'),
    historyEmptyState: $('historyEmptyState'),
    saveCurrentChatBtn: $('saveCurrentChatBtn'),
    exportChatBtn: $('exportChatBtn'),
    exportMdBtn: $('exportMdBtn'),
  };

  let activeCitationData = null;

  // Configure marked.js if available
  if (typeof marked !== 'undefined') {
    marked.setOptions({
      gfm: true,
      breaks: true,
    });
  }

  // --- Theme Management ---
  function applyTheme(theme) {
    state.theme = theme;
    el.html.setAttribute('data-theme', theme);
    localStorage.setItem('document_ai_theme', theme);
    if (el.themeToggleBtn) {
      const icon = el.themeToggleBtn.querySelector('.theme-icon');
      if (icon) icon.textContent = theme === 'dark' ? '☀️' : '🌙';
    }
    updateChartsTheme();
  }

  function updateChartsTheme() {
    const isDark = state.theme === 'dark';
    const textColor = isDark ? '#94a3b8' : '#475569';
    const gridColor = isDark ? 'rgba(255,255,255,0.08)' : 'rgba(0,0,0,0.06)';

    Object.values(state.chartInstances).forEach((chart) => {
      if (chart && chart.options && chart.options.scales) {
        if (chart.options.scales.x) {
          chart.options.scales.x.ticks.color = textColor;
          chart.options.scales.x.grid.color = gridColor;
        }
        if (chart.options.scales.y) {
          chart.options.scales.y.ticks.color = textColor;
          chart.options.scales.y.grid.color = gridColor;
        }
        chart.update();
      }
    });
  }

  // --- Toast Notifications ---
  function showToast(message, type = 'info') {
    if (!el.toastContainer) return;
    const toast = document.createElement('div');
    toast.className = `toast ${type === 'error' ? 'toast--error' : type === 'success' ? 'toast--success' : ''}`;
    toast.textContent = message;
    el.toastContainer.appendChild(toast);
    setTimeout(() => {
      toast.style.opacity = '0';
      toast.style.transition = 'opacity 0.3s ease';
      setTimeout(() => toast.remove(), 300);
    }, 4000);
  }

  // --- Safe Escaping & Markdown / Math / Chart Parser ---
  function escapeHtml(text) {
    const div = document.createElement('div');
    div.textContent = text || '';
    return div.innerHTML;
  }

  function formatTimestamp() {
    const now = new Date();
    return now.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
  }

  function renderAssistantContent(rawText, containerElement) {
    if (!rawText) return;

    let text = rawText;

    // 1. Normalize LaTeX math blocks and delimiters before parsing
    // Normalize display math blocks: \[ ... \] or standalone [ ... ] containing math
    text = text.replace(/(?:^|\n)\s*\\?\[\s*([\s\S]*?)\s*\\?\]\s*(?=\n|$)/g, (match, eq) => {
      if (/\\|[=_\^]|frac|theta|omega|alpha|beta|begin|cdot|times/i.test(eq) && !/^\s*\d+\s*$/.test(eq)) {
        return `\n\n$$\n${eq.trim()}\n$$\n\n`;
      }
      return match;
    });

    // Normalize inline \( ... \) into $ ... $
    text = text.replace(/\\\(([\s\S]*?)\\\)/g, (match, eq) => `$${eq.trim()}$`);

    // Normalize variables in parentheses like (K_b), (\omega_m), (J_m), (D_m), (\theta_m(t)) into inline math $K_b$
    text = text.replace(/\(([a-zA-Z\\](?:_[a-zA-Z0-9]+|\\[a-zA-Z]+)(?:\([a-zA-Z0-9]+\))?)\)/g, ' $$$1$ ');

    // Normalize standalone environments like \begin{aligned}...\end{aligned} not wrapped in $$
    text = text.replace(/(?<!\$\$)\s*(\\begin\{(?:aligned|equation|align|gather|bmatrix|pmatrix|cases)\*?\}[\s\S]*?\\end\{(?:aligned|equation|align|gather|bmatrix|pmatrix|cases)\*?\})\s*(?!\$\$)/g, '\n\n$$\n$1\n$$\n\n');

    // 2. Extract and placeholder any ```chart codeblocks
    const chartsData = [];
    text = text.replace(/```(?:chart|plot)\s*([\s\S]*?)```/gi, (match, jsonStr) => {
      try {
        const cleanJson = jsonStr
          .replace(/\/\/.*$/gm, '')
          .replace(/\/\*[\s\S]*?\*\//g, '')
          .replace(/,\s*([\]}])/g, '$1')
          .trim();
        const parsed = JSON.parse(cleanJson);
        const chartId = 'chart_' + Math.random().toString(36).substring(2, 9);
        chartsData.push({ id: chartId, config: parsed });
        return `\n\n<div class="chart-card">
                  <div class="chart-card-head">
                    <h4 class="chart-card-title">📈 ${escapeHtml(parsed.title || 'Data Graph')}</h4>
                  </div>
                  <div class="chart-canvas-wrap">
                    <canvas id="${chartId}"></canvas>
                  </div>
                </div>\n\n`;
      } catch (err) {
        return match;
      }
    });

    // 3. Extract and protect all LaTeX math blocks and inline math before marked.js
    const mathTokens = [];

    // Block math: $$ ... $$
    text = text.replace(/\$\$([\s\S]*?)\$\$/g, (match, mathCode) => {
      const token = `@@KATEXBLOCK${mathTokens.length}@@`;
      mathTokens.push({ token, code: mathCode.trim(), display: true });
      return `\n\n${token}\n\n`;
    });

    // Inline math: $ ... $ (ensuring not escaped \$ and non-empty)
    text = text.replace(/(?<!\\)\$([^\$\n]+?)(?<!\\)\$/g, (match, mathCode) => {
      const token = `@@KATEXINLINE${mathTokens.length}@@`;
      mathTokens.push({ token, code: mathCode.trim(), display: false });
      return token;
    });

    // 4. Parse Markdown safely with marked.js
    let renderedHtml = '';
    if (typeof marked !== 'undefined' && typeof marked.parse === 'function') {
      renderedHtml = marked.parse(text);
      // Wrap any <table> in .table-wrapper for responsive horizontal scrolling
      renderedHtml = renderedHtml.replace(/<table>([\s\S]*?)<\/table>/gi, '<div class="table-wrapper"><table>$1</table></div>');
    } else {
      renderedHtml = text
        .replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>')
        .replace(/\*([^*]+)\*/g, '<em>$1</em>')
        .replace(/^### (.*$)/gim, '<h3>$1</h3>')
        .replace(/^## (.*$)/gim, '<h2>$1</h2>')
        .replace(/^# (.*$)/gim, '<h1>$1</h1>');
    }

    // 5. Render protected math tokens using KaTeX
    mathTokens.forEach(({ token, code, display }) => {
      let mathHtml = '';
      if (typeof katex !== 'undefined' && typeof katex.renderToString === 'function') {
        try {
          mathHtml = katex.renderToString(code, {
            displayMode: display,
            throwOnError: false,
          });
        } catch (mathErr) {
          mathHtml = `<span class="katex-error" title="${escapeHtml(mathErr.message)}">${escapeHtml(code)}</span>`;
        }
      } else {
        mathHtml = display ? `<div class="katex-display">${escapeHtml(code)}</div>` : `<code>${escapeHtml(code)}</code>`;
      }
      renderedHtml = renderedHtml.split(token).join(mathHtml);
    });

    containerElement.innerHTML = renderedHtml;

    // 4. Render exact Chart.js charts
    if (chartsData.length > 0 && typeof Chart !== 'undefined') {
      setTimeout(() => {
        chartsData.forEach(({ id, config }) => {
          const canvas = document.getElementById(id);
          if (!canvas) return;

          const isDark = state.theme === 'dark';
          const textColor = isDark ? '#94a3b8' : '#475569';
          const gridColor = isDark ? 'rgba(255,255,255,0.08)' : 'rgba(0,0,0,0.06)';

          const chartType = config.type || 'line';
          const datasets = (config.datasets || []).map((ds, i) => {
            const colors = ['#4f46e5', '#10b981', '#f59e0b', '#ef4444', '#06b6d4'];
            const color = ds.borderColor || colors[i % colors.length];
            return {
              label: ds.label || `Series ${i + 1}`,
              data: ds.data || [],
              borderColor: color,
              backgroundColor: ds.backgroundColor || (chartType === 'line' ? 'transparent' : color),
              borderWidth: 2.5,
              tension: 0.35,
              pointRadius: 3,
              pointHoverRadius: 6,
              fill: false,
            };
          });

          if (state.chartInstances[id]) {
            state.chartInstances[id].destroy();
          }

          state.chartInstances[id] = new Chart(canvas, {
            type: chartType,
            data: {
              labels: config.labels || [],
              datasets: datasets,
            },
            options: {
              responsive: true,
              maintainAspectRatio: false,
              plugins: {
                legend: {
                  labels: { color: textColor, font: { family: 'Inter', size: 12 } },
                },
                tooltip: {
                  mode: 'index',
                  intersect: false,
                },
              },
              scales: {
                x: {
                  title: { display: !!config.xAxis, text: config.xAxis || '', color: textColor },
                  ticks: { color: textColor },
                  grid: { color: gridColor },
                },
                y: {
                  title: { display: !!config.yAxis, text: config.yAxis || '', color: textColor },
                  ticks: { color: textColor },
                  grid: { color: gridColor },
                },
              },
            },
          });
        });
      }, 50);
    }
  }

  // --- Sources & Documents Management ---
  async function loadSources() {
    try {
      const res = await fetch(apiUrl('/sources'));
      const data = await res.json();
      if (data.success && Array.isArray(data.documents)) {
        state.documents = data.documents;
        if (data.workspace_summary) {
          state.workspaceSummary = data.workspace_summary;
        } else {
          state.workspaceSummary = {
            total_documents: data.documents.length,
            total_pages: data.documents.reduce((acc, d) => acc + (d.pages || 0), 0),
            total_chunks: data.documents.reduce((acc, d) => acc + (d.chunks || 0), 0),
          };
        }
        renderSourcesList();
      }
    } catch (err) {
      console.warn('Could not fetch sources:', err);
    }
  }

  function renderSourcesList() {
    const docs = state.documents;
    const summary = state.workspaceSummary;

    // Header counter badge
    if (el.sourcesCountPill) {
      el.sourcesCountPill.textContent = docs.length;
    }

    // Workspace Aggregate Summary Strip
    if (el.statDocs) el.statDocs.textContent = summary.total_documents || docs.length;
    if (el.statPages) el.statPages.textContent = summary.total_pages || 0;
    if (el.statChunks) el.statChunks.textContent = summary.total_chunks || 0;

    if (docs.length === 0) {
      el.sourcesList.innerHTML = '';
      el.sourcesEmptyState.classList.remove('is-hidden');
      return;
    }

    function getFileIcon(filename) {
      const ext = (filename || '').split('.').pop().toLowerCase();
      if (ext === 'docx' || ext === 'doc') return '📝';
      if (ext === 'txt' || ext === 'md' || ext === 'markdown') return '📑';
      if (ext === 'csv' || ext === 'tsv') return '📊';
      return '📄';
    }

    el.sourcesEmptyState.classList.add('is-hidden');
    el.sourcesList.innerHTML = docs
      .map(
        (doc) => `
        <div class="source-item" data-filename="${escapeHtml(doc.filename)}">
          <div class="source-info">
            <span class="source-name" title="${escapeHtml(doc.filename)}">${getFileIcon(doc.filename)} ${escapeHtml(doc.filename)}</span>
            <div class="doc-badges-row">
              <span class="badge-page" title="Total pages in document">📄 ${doc.pages || 0} Pages</span>
              <span class="badge-chunk" title="Semantic chunks created for vector retrieval">🧩 ${doc.chunks || 0} Chunks</span>
            </div>
          </div>
          <div class="source-actions">
            <button type="button" class="btn btn--ghost btn--sm view-doc-btn" title="Inspect extracted pages">View</button>
            <button type="button" class="btn btn--ghost btn--sm delete-doc-btn" title="Remove document">✕</button>
          </div>
        </div>
      `
      )
      .join('');

    // Attach click listeners to cards
    el.sourcesList.querySelectorAll('.view-doc-btn').forEach((btn) => {
      btn.addEventListener('click', (e) => {
        const item = e.target.closest('.source-item');
        if (item) openDocViewer(item.dataset.filename, 1);
      });
    });

    el.sourcesList.querySelectorAll('.delete-doc-btn').forEach((btn) => {
      btn.addEventListener('click', (e) => {
        const item = e.target.closest('.source-item');
        if (item) deleteSource(item.dataset.filename);
      });
    });
  }

  async function deleteSource(filename) {
    if (!confirm(`Remove "${filename}" from the workspace?`)) return;
    try {
      const res = await fetch(apiUrl(`/sources/${encodeURIComponent(filename)}`), { method: 'DELETE' });
      const data = await res.json();
      if (data.success) {
        showToast(data.message || `Removed ${filename}`, 'success');
        await loadSources();
      } else {
        showToast(data.error || 'Could not delete document', 'error');
      }
    } catch (err) {
      showToast('Error removing document', 'error');
    }
  }

  // --- Upload Handling ---
  async function handleFileUpload(files) {
    if (!files || files.length === 0) return;

    const allowedExts = ['.pdf', '.docx', '.doc', '.txt', '.md', '.markdown', '.csv', '.tsv', '.json'];
    const validFiles = Array.from(files).filter((f) => {
      const name = f.name.toLowerCase();
      return allowedExts.some((ext) => name.endsWith(ext));
    });

    if (validFiles.length === 0) {
      showToast('Please select valid document files (PDF, Word DOCX, TXT, MD, CSV).', 'error');
      return;
    }

    const formData = new FormData();
    validFiles.forEach((file) => formData.append('files', file));

    // Show upload progress
    el.uploadStatus.classList.remove('is-hidden');
    el.uploadStatusText.textContent = `Extracting & indexing ${validFiles.length} document(s)...`;
    el.progressFill.style.width = '60%';

    try {
      const res = await fetch(apiUrl('/upload'), {
        method: 'POST',
        body: formData,
      });

      if (!res.ok) {
        if (window.location.hostname.includes('netlify') && !getBackendBase()) {
          el.uploadStatus.classList.add('is-hidden');
          el.progressFill.style.width = '0%';
          showToast('Netlify only hosts the frontend UI. Click "⚙️ Backend" in the top bar to connect your Python server URL!', 'error');
          return;
        }
      }

      const data = await res.json();

      el.progressFill.style.width = '100%';
      setTimeout(() => {
        el.uploadStatus.classList.add('is-hidden');
        el.progressFill.style.width = '0%';
      }, 500);

      if (data.success) {
        showToast(data.message || 'Documents indexed successfully!', 'success');
        state.documents = data.documents || [];
        if (data.workspace_summary) {
          state.workspaceSummary = data.workspace_summary;
        }
        renderSourcesList();
      } else {
        showToast(data.error || 'Upload failed.', 'error');
      }
    } catch (err) {
      el.uploadStatus.classList.add('is-hidden');
      el.progressFill.style.width = '0%';
      if (window.location.hostname.includes('netlify') && !getBackendBase()) {
        showToast('Netlify only hosts the frontend UI. Click "⚙️ Backend" in the top bar to connect your Python server URL!', 'error');
      } else {
        showToast('Failed to upload document. Please ensure your Python backend server is running.', 'error');
      }
    } finally {
      el.fileInput.value = '';
    }
  }

  // --- Document Viewer Modal ---
  async function openDocViewer(filename, targetPage = 1) {
    try {
      const res = await fetch(apiUrl(`/sources/${encodeURIComponent(filename)}`));
      const data = await res.json();
      if (!data.success) {
        showToast(data.error || 'Failed to load document preview.', 'error');
        return;
      }

      state.viewerDoc = filename;
      state.viewerPages = data.pages || [];
      state.viewerPageIndex = Math.max(0, Math.min(targetPage - 1, state.viewerPages.length - 1));

      el.viewerDocTitle.textContent = filename;
      el.viewerPageStats.textContent = `${state.viewerPages.length} Pages`;
      el.viewerDownloadLink.href = `/sources/${encodeURIComponent(filename)}/download`;

      renderViewerPage();
      el.docViewerModal.classList.remove('is-hidden');
    } catch (err) {
      showToast('Could not load document preview.', 'error');
    }
  }

  function renderViewerPage() {
    if (!state.viewerPages || state.viewerPages.length === 0) {
      el.viewerPageText.textContent = 'No text content available on this page.';
      el.viewerCurPage.textContent = '0';
      el.viewerTotalPages.textContent = '0';
      return;
    }

    const current = state.viewerPages[state.viewerPageIndex];
    el.viewerCurPage.textContent = current.page;
    el.viewerTotalPages.textContent = state.viewerPages.length;
    el.viewerPageText.textContent = current.text || '(Blank page)';

    el.viewerPrevBtn.disabled = state.viewerPageIndex === 0;
    el.viewerNextBtn.disabled = state.viewerPageIndex >= state.viewerPages.length - 1;
  }

  // --- Citation Inspector Modal ---
  function openCitationModal(source) {
    activeCitationData = source;
    el.citationDocName.textContent = source.filename || 'Document';
    el.citationPageBadge.textContent = `Page ${source.page || 1}`;
    el.citationExcerptText.textContent = `"${source.excerpt || source.snippet || source.content || 'No passage available.'}"`;
    el.citationModal.classList.remove('is-hidden');
  }

  function closeModals() {
    el.citationModal.classList.add('is-hidden');
    el.docViewerModal.classList.add('is-hidden');
    el.historyModal.classList.add('is-hidden');
  }

  // --- Chat & Question Answering ---
  function appendUserMessage(text, timestamp = null) {
    if (el.chatWelcome) el.chatWelcome.classList.add('is-hidden');

    const row = document.createElement('div');
    row.className = 'msg-row msg-user';
    row.innerHTML = `
      <div class="msg-bubble-wrap">
        <div class="msg-meta">
          <span>You</span>
          <span>&bull;</span>
          <span>${timestamp || formatTimestamp()}</span>
        </div>
        <div class="msg-bubble">${escapeHtml(text)}</div>
      </div>
      <div class="msg-avatar">U</div>
    `;
    el.messagesContainer.appendChild(row);
    scrollToBottom();
  }

  function appendAssistantPlaceholder() {
    const row = document.createElement('div');
    row.className = 'msg-row msg-assistant';
    row.id = 'activeAssistantBubble';
    row.innerHTML = `
      <div class="msg-avatar">AI</div>
      <div class="msg-bubble-wrap">
        <div class="msg-meta">
          <span>Research Assistant</span>
          <span>&bull;</span>
          <span>Generating answer...</span>
        </div>
        <div class="msg-bubble">
          <div class="typing-indicator">
            <span class="typing-dot"></span>
            <span class="typing-dot"></span>
            <span class="typing-dot"></span>
          </div>
        </div>
      </div>
    `;
    el.messagesContainer.appendChild(row);
    scrollToBottom();
    return row;
  }

  function populateAssistantAnswer(bubbleRow, answerText, sources = [], externalSources = [], timestamp = null) {
    bubbleRow.removeAttribute('id');

    const metaSpan = bubbleRow.querySelector('.msg-meta');
    if (metaSpan) {
      metaSpan.innerHTML = `
        <span>Research Assistant</span>
        <span>&bull;</span>
        <span>${timestamp || formatTimestamp()}</span>
      `;
    }

    const bubble = bubbleRow.querySelector('.msg-bubble');

    // 1. Render Markdown, LaTeX Math, and Charts
    const contentDiv = document.createElement('div');
    contentDiv.className = 'markdown-body';
    renderAssistantContent(answerText, contentDiv);
    bubble.innerHTML = '';
    bubble.appendChild(contentDiv);

    // 2. Document Sources Used Drawer
    if (sources && sources.length > 0) {
      const sourcesDrawer = document.createElement('div');
      sourcesDrawer.className = 'msg-sources-drawer';
      sourcesDrawer.innerHTML = `
        <div class="msg-sources-label">Document Sources Used (${sources.length}):</div>
        <div class="msg-sources-list">
          ${sources
            .map(
              (s, i) => `
              <button type="button" class="source-pill-btn" data-source-index="${i}">
                <span>📄</span>
                <span>${escapeHtml(s.filename)} : p.${s.page}</span>
              </button>
            `
            )
            .join('')}
        </div>
      `;
      bubble.appendChild(sourcesDrawer);

      sourcesDrawer.querySelectorAll('.source-pill-btn').forEach((btn) => {
        btn.addEventListener('click', () => {
          const idx = parseInt(btn.dataset.sourceIndex, 10);
          if (sources[idx]) openCitationModal(sources[idx]);
        });
      });
    }

    // 3. External Web Sources Drawer (Links & Citations)
    if (externalSources && externalSources.length > 0) {
      const extDrawer = document.createElement('div');
      extDrawer.className = 'msg-external-drawer';
      extDrawer.innerHTML = `
        <div class="msg-external-label">
          <span>🌐</span>
          <span>External Sources & Links (${externalSources.length}):</span>
        </div>
        <div class="msg-external-list">
          ${externalSources
            .map(
              (es) => `
              <a href="${escapeHtml(es.url)}" target="_blank" rel="noopener noreferrer" class="external-source-pill" title="${escapeHtml(es.title)}">
                <span>🔗</span>
                <span>${escapeHtml(es.title)}</span>
              </a>
            `
            )
            .join('')}
        </div>
      `;
      bubble.appendChild(extDrawer);
    }

    // 4. Action Bar (Copy Button)
    const actionsBar = document.createElement('div');
    actionsBar.className = 'msg-actions-bar';
    actionsBar.innerHTML = `
      <button type="button" class="msg-action-btn copy-btn" title="Copy answer">📋 Copy</button>
    `;
    bubble.appendChild(actionsBar);

    const copyBtn = actionsBar.querySelector('.copy-btn');
    if (copyBtn) {
      copyBtn.addEventListener('click', () => {
        navigator.clipboard.writeText(answerText).then(() => {
          copyBtn.textContent = '✓ Copied!';
          setTimeout(() => (copyBtn.textContent = '📋 Copy'), 2000);
        });
      });
    }

    scrollToBottom();
  }

  function scrollToBottom() {
    if (el.chatThread) {
      el.chatThread.scrollTop = el.chatThread.scrollHeight;
    }
  }

  async function handleSendQuestion(questionText) {
    const question = (questionText || el.composerInput.value || '').trim();
    if (!question || state.isQuerying) return;

    // Reset composer input
    el.composerInput.value = '';
    el.composerInput.style.height = 'auto';
    el.sendMessageBtn.disabled = true;
    state.isQuerying = true;

    // Render User Message & Assistant Placeholder
    const timeStr = formatTimestamp();
    appendUserMessage(question, timeStr);
    state.currentMessages.push({ role: 'user', content: question, timestamp: timeStr });

    const assistantBubble = appendAssistantPlaceholder();

    try {
      const res = await fetch(apiUrl('/ask'), {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ question }),
      });
      const data = await res.json();

      if (data.success) {
        populateAssistantAnswer(
          assistantBubble,
          data.answer,
          data.sources || [],
          data.external_sources || [],
          timeStr
        );
        state.currentMessages.push({
          role: 'assistant',
          content: data.answer,
          sources: data.sources || [],
          external_sources: data.external_sources || [],
          timestamp: timeStr,
        });
        autoSaveSession();
      } else {
        const errorMsg = data.error || 'Failed to retrieve answer. Please try again.';
        assistantBubble.querySelector('.msg-bubble').innerHTML = `
          <p style="color: var(--danger); font-weight: 500;">⚠️ ${escapeHtml(errorMsg)}</p>
        `;
        showToast(errorMsg, 'error');
      }
    } catch (err) {
      const isNetlifyWithoutBackend = window.location.hostname.includes('netlify') && !getBackendBase();
      const errText = isNetlifyWithoutBackend
        ? '⚠️ Netlify hosts the frontend UI only. The Python AI backend needs to be connected. Click "⚙️ Backend" in the navigation bar to enter your server URL (e.g. from Render.com).'
        : '⚠️ Server connection error. Please ensure your Python backend server is running.';
      assistantBubble.querySelector('.msg-bubble').innerHTML = `
        <p style="color: var(--danger); font-weight: 500;">${errText}</p>
      `;
      showToast(errText, 'error');
    } finally {
      state.isQuerying = false;
      updateSendButtonState();
    }
  }

  function updateSendButtonState() {
    const hasText = el.composerInput.value.trim().length > 0;
    el.sendMessageBtn.disabled = !hasText || state.isQuerying;
  }

  // --- Chat History Management ---
  async function autoSaveSession() {
    if (state.currentMessages.length === 0) return;

    const payload = {
      id: state.currentSessionId,
      messages: state.currentMessages,
    };

    try {
      const res = await fetch(apiUrl('/history/save'), {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });
      const data = await res.json();
      if (data.success) {
        localStorage.setItem('doc_ai_active_session_id', state.currentSessionId);
        updateHistoryCount();
      }
    } catch (err) {
      console.warn('Auto-save error:', err);
    }
  }

  async function updateHistoryCount() {
    try {
      const res = await fetch(apiUrl('/history'));
      const data = await res.json();
      if (data.success && el.historyCountBadge) {
        el.historyCountBadge.textContent = data.sessions.length;
      }
    } catch (err) {
      // ignore
    }
  }

  async function openHistoryModal() {
    el.historyModal.classList.remove('is-hidden');
    try {
      const res = await fetch(apiUrl('/history'));
      const data = await res.json();
      if (!data.success || !data.sessions || data.sessions.length === 0) {
        el.historyList.innerHTML = '';
        el.historyEmptyState.classList.remove('is-hidden');
        return;
      }

      el.historyEmptyState.classList.add('is-hidden');
      el.historyList.innerHTML = data.sessions
        .map(
          (s) => `
          <div class="history-item" data-session-id="${escapeHtml(s.id)}">
            <div class="history-item-info">
              <span class="history-title" title="${escapeHtml(s.title)}">${escapeHtml(s.title)}</span>
              <div class="history-meta">
                <span>💬 ${s.message_count} messages</span>
                <span>&bull;</span>
                <span>${escapeHtml(s.updated_at || '')}</span>
              </div>
            </div>
            <div class="history-item-actions">
              <button type="button" class="btn btn--primary btn--sm load-session-btn">Load</button>
              <button type="button" class="btn btn--ghost btn--sm delete-session-btn" title="Delete conversation">🗑️</button>
            </div>
          </div>
        `
        )
        .join('');

      el.historyList.querySelectorAll('.load-session-btn').forEach((btn) => {
        btn.addEventListener('click', (e) => {
          const item = e.target.closest('.history-item');
          if (item) loadSession(item.dataset.sessionId);
        });
      });

      el.historyList.querySelectorAll('.delete-session-btn').forEach((btn) => {
        btn.addEventListener('click', (e) => {
          const item = e.target.closest('.history-item');
          if (item) deleteSession(item.dataset.sessionId);
        });
      });
    } catch (err) {
      showToast('Could not load chat history.', 'error');
    }
  }

  async function loadSession(sessionId) {
    try {
      const res = await fetch(apiUrl(`/history/${encodeURIComponent(sessionId)}`));
      const data = await res.json();
      if (!data.success || !data.session) {
        showToast('Session not found.', 'error');
        return;
      }

      state.currentSessionId = data.session.id;
      state.currentMessages = data.session.messages || [];
      localStorage.setItem('doc_ai_active_session_id', state.currentSessionId);

      // Render messages to chat thread
      el.messagesContainer.innerHTML = '';
      if (state.currentMessages.length > 0 && el.chatWelcome) {
        el.chatWelcome.classList.add('is-hidden');
      }

      state.currentMessages.forEach((msg) => {
        if (msg.role === 'user') {
          appendUserMessage(msg.content, msg.timestamp);
        } else if (msg.role === 'assistant') {
          const row = document.createElement('div');
          row.className = 'msg-row msg-assistant';
          row.innerHTML = `
            <div class="msg-avatar">AI</div>
            <div class="msg-bubble-wrap">
              <div class="msg-meta">
                <span>Research Assistant</span>
                <span>&bull;</span>
                <span>${msg.timestamp || ''}</span>
              </div>
              <div class="msg-bubble"></div>
            </div>
          `;
          el.messagesContainer.appendChild(row);
          populateAssistantAnswer(
            row,
            msg.content,
            msg.sources || [],
            msg.external_sources || [],
            msg.timestamp
          );
        }
      });

      closeModals();
      showToast(`Loaded "${data.session.title}"`, 'success');
    } catch (err) {
      showToast('Failed to load session.', 'error');
    }
  }

  async function deleteSession(sessionId) {
    if (!confirm('Are you sure you want to delete this saved conversation?')) return;
    try {
      const res = await fetch(apiUrl(`/history/${encodeURIComponent(sessionId)}`), { method: 'DELETE' });
      const data = await res.json();
      if (data.success) {
        showToast('Session deleted.', 'success');
        openHistoryModal();
        updateHistoryCount();
        if (state.currentSessionId === sessionId) {
          startNewChat();
        }
      }
    } catch (err) {
      showToast('Error deleting session.', 'error');
    }
  }

  function startNewChat() {
    state.currentSessionId = 'sess_' + Date.now();
    state.currentMessages = [];
    localStorage.removeItem('doc_ai_active_session_id');
    el.messagesContainer.innerHTML = '';
    if (el.chatWelcome) el.chatWelcome.classList.remove('is-hidden');
    showToast('Started new chat conversation.', 'info');
  }

  function exportChat(format = 'json') {
    if (state.currentMessages.length === 0) {
      showToast('No messages in current conversation to export.', 'error');
      return;
    }

    let blob, filename;
    if (format === 'json') {
      const jsonStr = JSON.stringify(
        {
          session_id: state.currentSessionId,
          exported_at: new Date().toISOString(),
          messages: state.currentMessages,
        },
        null,
        2
      );
      blob = new Blob([jsonStr], { type: 'application/json' });
      filename = `chat_export_${Date.now()}.json`;
    } else {
      let md = `# Research Assistant Conversation Export\n*Exported on ${new Date().toLocaleString()}*\n\n---\n\n`;
      state.currentMessages.forEach((m) => {
        md += `### ${m.role === 'user' ? '👤 User' : '🤖 Assistant'} (${m.timestamp || ''})\n\n${m.content}\n\n---\n\n`;
      });
      blob = new Blob([md], { type: 'text/markdown' });
      filename = `chat_export_${Date.now()}.md`;
    }

    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = filename;
    a.click();
    URL.revokeObjectURL(a.href);
    showToast(`Exported as ${format.toUpperCase()}`, 'success');
  }

  // --- Clear Workspace ---
  async function handleClearWorkspace() {
    if (!confirm('Are you sure you want to clear all uploaded documents and reset the workspace?')) {
      return;
    }

    try {
      const res = await fetch(apiUrl('/clear'), { method: 'POST' });
      const data = await res.json();
      if (data.success) {
        state.documents = [];
        state.workspaceSummary = { total_documents: 0, total_pages: 0, total_chunks: 0 };
        renderSourcesList();
        startNewChat();
        showToast('Workspace reset successfully.', 'success');
      } else {
        showToast('Failed to clear workspace.', 'error');
      }
    } catch (err) {
      showToast('Error clearing workspace.', 'error');
    }
  }

  // --- Event Listeners Setup ---
  function initEventListeners() {
    // Theme toggle
    if (el.themeToggleBtn) {
      el.themeToggleBtn.addEventListener('click', () => {
        const next = state.theme === 'dark' ? 'light' : 'dark';
        applyTheme(next);
      });
    }

    // New Chat
    if (el.newChatBtn) {
      el.newChatBtn.addEventListener('click', startNewChat);
    }

    // Chat History
    if (el.historyBtn) {
      el.historyBtn.addEventListener('click', openHistoryModal);
    }

    if (el.saveCurrentChatBtn) {
      el.saveCurrentChatBtn.addEventListener('click', async () => {
        await autoSaveSession();
        showToast('Session saved to history!', 'success');
        openHistoryModal();
      });
    }

    if (el.exportChatBtn) {
      el.exportChatBtn.addEventListener('click', () => exportChat('json'));
    }

    if (el.exportMdBtn) {
      el.exportMdBtn.addEventListener('click', () => exportChat('md'));
    }

    // Clear Workspace
    if (el.clearAllBtn) {
      el.clearAllBtn.addEventListener('click', handleClearWorkspace);
    }

    // Upload triggers
    if (el.triggerUploadBtn) {
      el.triggerUploadBtn.addEventListener('click', () => el.fileInput.click());
    }

    if (el.dropzone) {
      el.dropzone.addEventListener('click', () => el.fileInput.click());

      el.dropzone.addEventListener('dragover', (e) => {
        e.preventDefault();
        el.dropzone.classList.add('drag-over');
      });

      el.dropzone.addEventListener('dragleave', () => {
        el.dropzone.classList.remove('drag-over');
      });

      el.dropzone.addEventListener('drop', (e) => {
        e.preventDefault();
        el.dropzone.classList.remove('drag-over');
        if (e.dataTransfer && e.dataTransfer.files) {
          handleFileUpload(e.dataTransfer.files);
        }
      });
    }

    if (el.fileInput) {
      el.fileInput.addEventListener('change', (e) => {
        if (e.target.files) {
          handleFileUpload(e.target.files);
        }
      });
    }

    // Suggestion chips
    qsa('.suggestion-chip').forEach((chip) => {
      chip.addEventListener('click', () => {
        const prompt = chip.dataset.prompt;
        if (prompt) {
          el.composerInput.value = prompt;
          handleSendQuestion(prompt);
        }
      });
    });

    // Composer input handling
    if (el.composerInput) {
      el.composerInput.addEventListener('input', () => {
        el.composerInput.style.height = 'auto';
        el.composerInput.style.height = `${Math.min(el.composerInput.scrollHeight, 160)}px`;
        updateSendButtonState();
      });

      el.composerInput.addEventListener('keydown', (e) => {
        if (e.key === 'Enter' && !e.shiftKey) {
          e.preventDefault();
          handleSendQuestion();
        }
      });
    }

    if (el.sendMessageBtn) {
      el.sendMessageBtn.addEventListener('click', () => handleSendQuestion());
    }

    // Modal close buttons
    qsa('[data-close]').forEach((btn) => {
      btn.addEventListener('click', closeModals);
    });

    // Modal backdrop clicks
    [el.citationModal, el.docViewerModal, el.historyModal].forEach((modal) => {
      if (modal) {
        modal.addEventListener('click', (e) => {
          if (e.target === modal) closeModals();
        });
      }
    });

    // Viewer Navigation
    if (el.viewerPrevBtn) {
      el.viewerPrevBtn.addEventListener('click', () => {
        if (state.viewerPageIndex > 0) {
          state.viewerPageIndex--;
          renderViewerPage();
        }
      });
    }

    if (el.viewerNextBtn) {
      el.viewerNextBtn.addEventListener('click', () => {
        if (state.viewerPageIndex < state.viewerPages.length - 1) {
          state.viewerPageIndex++;
          renderViewerPage();
        }
      });
    }

    // Open full page from Citation Inspector
    if (el.openDocumentPageBtn) {
      el.openDocumentPageBtn.addEventListener('click', () => {
        if (activeCitationData) {
          const docName = activeCitationData.filename;
          const pageNum = activeCitationData.page || 1;
          closeModals();
          openDocViewer(docName, pageNum);
        }
      });
    }

    // Escape key closes modals
    window.addEventListener('keydown', (e) => {
      if (e.key === 'Escape') closeModals();
    });
  }

  // --- Bootstrap ---
  function init() {
    applyTheme(state.theme);
    initEventListeners();
    loadSources();
    updateHistoryCount();
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
