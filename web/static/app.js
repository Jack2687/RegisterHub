(() => {
  let currentView = 'overview';
  let selectedAdapter = '';
  let adaptersCache = [];
  let activeJobId = '';
  let logStream = null;
  let logEntries = [];
  let logFilter = 'all';
  let runHeadlessMode = 'auto';
  let revealSecrets = false;
  let accFilter = 'all';
  let accountsCache = [];

  // Tools state
  let idCountry = 'US';
  let cardBrand = 'visa';
  let cardsCache = [];
  let tmSessionId = '';
  let tmEmail = '';
  let totpTimer = null;
  let totpCountdown = 30;
  let lastTotpCode = '';
  let activeCodeName = '';
  let activeCodeText = '';
  const TOTP_KEY = 'rh_lite_totp_v1';

  const $ = (sel) => document.querySelector(sel);
  const $$ = (sel) => Array.from(document.querySelectorAll(sel));

  const VIEW_TITLES = {
    overview: '总览仪表盘',
    agent: 'AI 直跑工作台 (单线程自主代理)',
    runner: '单线程适配器试跑',
    accounts: '账号与 API Key 台账',
    tools: '开源实用工具箱',
    snapshots: '失败原因与现场快照',
    scaffold: '站点工坊 (源码 / 嗅探 / 脚手架)',
    doctor: '环境真执行诊断',
    layout: '视窗标定台 (单 / 多窗口)',
    config: '运行配置 (config.json / IMAP)',
    llm: 'LLM 模型与 API 配置',
  };

  const VIEW_GROUPS = {
    overview: '核心与资产',
    agent: '核心与资产',
    runner: '核心与资产',
    accounts: '核心与资产',
    snapshots: '核心与资产',
    scaffold: '开发与工具',
    layout: '开发与工具',
    tools: '开发与工具',
    doctor: '运维与配置',
    config: '运维与配置',
    llm: '运维与配置',
  };

  const TIER_META = {
    verified: { label: '实测可跑', cls: 'emerald' },
    oauth: { label: '需 OAuth', cls: 'amber' },
    demo: { label: '本地演示', cls: 'sky' },
    template: { label: '示例骨架', cls: 'neutral' },
  };

  function toast(msg) {
    const el = $('#toast');
    el.textContent = msg;
    el.classList.remove('hidden');
    clearTimeout(toast._t);
    toast._t = setTimeout(() => el.classList.add('hidden'), 3200);
  }

  function escapeHtml(s) {
    return String(s || '')
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;');
  }

  function maskSecret(val) {
    const s = String(val || '').trim();
    if (!s) return '—';
    if (s.length <= 10) return '••••••••';
    return `${s.slice(0, 6)}...${s.slice(-4)}`;
  }

  async function copyText(val, label) {
    try {
      await navigator.clipboard.writeText(val);
      toast(`已复制${label || ''}`);
    } catch {
      toast('剪贴板写入失败');
    }
  }

  // ==================== Theme & Sidebar ====================
  function syncThemeIcons() {
    const isDark = document.documentElement.classList.contains('dark');
    $('#icon-sun').classList.toggle('hidden', !isDark);
    $('#icon-moon').classList.toggle('hidden', isDark);
  }
  syncThemeIcons();

  $('#btn-theme-toggle').addEventListener('click', () => {
    const next = document.documentElement.classList.contains('dark') ? 'light' : 'dark';
    document.documentElement.classList.remove('dark', 'light');
    document.documentElement.classList.add(next);
    try { localStorage.setItem('rh_lite_theme', next); } catch {}
    syncThemeIcons();
  });

  if (localStorage.getItem('rh_lite_sidebar') === '1') {
    $('#app-shell').classList.add('collapsed');
  }
  $('#btn-toggle-sidebar').addEventListener('click', () => {
    const collapsed = $('#app-shell').classList.toggle('collapsed');
    try { localStorage.setItem('rh_lite_sidebar', collapsed ? '1' : '0'); } catch {}
  });

  // ==================== Navigation ====================
  function switchView(target) {
    if (!VIEW_TITLES[target]) target = 'overview';
    currentView = target;
    $('#topbar-title').textContent = VIEW_TITLES[target];
    const grpEl = $('#topbar-group');
    if (grpEl) grpEl.textContent = VIEW_GROUPS[target] || '核心与资产';
    $$('.nav-item').forEach((b) => b.classList.toggle('active', b.dataset.view === target));
    $$('.view').forEach((v) => v.classList.toggle('active', v.id === `view-${target}`));
    if (target === 'overview') loadOverview();
    if (target === 'agent') {
      loadLlmConfig(false);
      loadAgentSessions(false);
    }
    if (target === 'llm') loadLlmConfig(true);
    if (target === 'config') loadConfig();
    if (target === 'accounts') loadAccounts();
    if (target === 'snapshots') loadFailuresAndSnapshots();
    if (target === 'layout') loadLayout();
    if (target === 'runner') {
      loadAdapters();
      loadJobs(false);
    }
    if (target === 'scaffold') {
      loadAdapters().then(() => {
        if (!activeCodeName && adaptersCache.length) loadAdapterCode(adaptersCache[0].name);
      });
    }
    if (target === 'tools') {
      if (!$('#identity-grid').innerHTML) genIdentities();
      if (!cardsCache.length) genCards();
    }
  }

  $$('.nav-item').forEach((btn) => btn.addEventListener('click', () => switchView(btn.dataset.view)));
  $$('[data-jump]').forEach((btn) => btn.addEventListener('click', () => switchView(btn.dataset.jump)));
  $('#heartbeat-card').addEventListener('click', () => switchView('doctor'));

  function activateToolTab(toolName) {
    switchView('tools');
    const targetBtn = $(`#tools-tab-group .seg-btn[data-tool="${toolName}"]`);
    if (targetBtn) targetBtn.click();
  }
  $('#btn-jump-ipscore')?.addEventListener('click', () => activateToolTab('ipscore'));
  $('#btn-jump-har')?.addEventListener('click', () => activateToolTab('har'));

  $$('#scaffold-tab-group .seg-btn').forEach((btn) => {
    btn.addEventListener('click', () => {
      const t = btn.dataset.scTab;
      $$('#scaffold-tab-group .seg-btn').forEach((b) => b.classList.toggle('active', b === btn));
      $$('.scaffold-panel').forEach((p) => p.classList.toggle('active', p.id === `scaffold-panel-${t}`));
    });
  });

  $('#btn-global-refresh').addEventListener('click', async () => {
    const ico = $('#icon-refresh');
    ico.classList.add('spin');
    try {
      if (currentView === 'overview') await Promise.all([loadOverview(), loadDoctor()]);
      else if (currentView === 'agent') await Promise.all([loadLlmConfig(false), loadAgentSessions(false)]);
      else if (currentView === 'llm') await loadLlmConfig(true);
      else if (currentView === 'doctor') await loadDoctor();
      else if (currentView === 'config') await loadConfig();
      else if (currentView === 'runner') await Promise.all([loadAdapters(), loadJobs(false)]);
      else if (currentView === 'accounts') await loadAccounts();
      else if (currentView === 'snapshots') await loadFailuresAndSnapshots();
      else if (currentView === 'layout') await loadLayout();
      else if (currentView === 'scaffold') await loadAdapters();
      toast('已刷新当前面板数据');
    } finally {
      setTimeout(() => ico.classList.remove('spin'), 320);
    }
  });

  // ==================== 0. Overview Dashboard ====================
  async function loadOverview() {
    try {
      const res = await fetch('/api/overview');
      const data = await res.json();
      const k = data.kpi || {};
      $('#ov-accounts').textContent = String(k.accounts_total ?? 0);
      $('#ov-keys-sub').textContent = `其中 ${k.accounts_with_key ?? 0} 条含有效 Key`;
      $('#ov-adapters').textContent = String(k.adapters_total ?? 0);
      $('#ov-running-sub').textContent = k.running ? '当前有 1 个任务运行中' : '单线程空闲就绪';
      $('#ov-failures').textContent = String(k.failures_total ?? 0);
      $('#ov-snaps-sub').textContent = `留存 ${k.snapshots_total ?? 0} 张失败截图`;

      const badge = $('#nav-badge-accounts');
      if ((k.accounts_total ?? 0) > 0) {
        badge.textContent = String(k.accounts_total);
        badge.classList.remove('hidden');
      } else {
        badge.classList.add('hidden');
      }

      const snapBadge = $('#nav-badge-snapshots');
      if (snapBadge) {
        if ((k.failures_total ?? 0) > 0) {
          snapBadge.textContent = String(k.failures_total);
          snapBadge.classList.remove('hidden');
        } else {
          snapBadge.classList.add('hidden');
        }
      }

      // 7-day bar chart
      const trend = data.daily_trend || [];
      const maxVal = Math.max(1, ...trend.map((d) => (d.done || 0) + (d.failed || 0)));
      $('#ov-chart').innerHTML = trend
        .map((d) => {
          const okH = d.done ? Math.max(8, Math.round((d.done / maxVal) * 95)) : 0;
          const failH = d.failed ? Math.max(8, Math.round((d.failed / maxVal) * 95)) : 0;
          const total = (d.done || 0) + (d.failed || 0);
          return `
            <div class="chart-col" title="${escapeHtml(d.full_date)}: 成功 ${d.done}, 失败 ${d.failed}">
              <div class="chart-cnt">${total || ''}</div>
              <div class="chart-bars">
                ${failH ? `<div class="bar-fail" style="height:${failH}px"></div>` : ''}
                ${okH ? `<div class="bar-ok" style="height:${okH}px"></div>` : ''}
                ${!okH && !failH ? `<div class="bar-zero"></div>` : ''}
              </div>
              <div class="chart-lbl">${escapeHtml(d.date)}</div>
            </div>`;
        })
        .join('');

      // Recent jobs
      const jobs = data.recent_jobs || [];
      const jbox = $('#ov-recent-jobs');
      if (!jobs.length) {
        jbox.innerHTML = `<div class="muted-small" style="padding:18px;text-align:center">暂无近期任务，点上方「启动单站试跑」开始</div>`;
      } else {
        jbox.innerHTML = jobs
          .map(
            (j) => `
            <button type="button" class="history-item" data-ov-job="${escapeHtml(j.id)}">
              <span class="mono">${escapeHtml(j.adapter)} ×${j.count} <span style="color:var(--text-muted)">(${j.duration_s}s)</span></span>
              <span class="pill ${j.status === 'done' ? 'pass' : j.status === 'running' ? 'warn' : 'fail'}">${escapeHtml(j.status)}</span>
            </button>`
          )
          .join('');
        jbox.querySelectorAll('[data-ov-job]').forEach((b) => {
          b.addEventListener('click', () => {
            switchView('runner');
            replayJobLog(b.dataset.ovJob);
          });
        });
      }

      // Quick adapters grid
      const ads = data.adapters || [];
      $('#ov-adapter-grid').innerHTML = ads
        .map((a) => {
          const tm = TIER_META[a.tier] || TIER_META.template;
          return `
            <div class="adapter-card-mini">
              <div class="adapter-card-top">
                <span class="adapter-name">${escapeHtml(a.name)}</span>
                <span class="badge ${tm.cls}">${tm.label}</span>
              </div>
              <div class="adapter-url" title="${escapeHtml(a.target_url)}">${escapeHtml(a.target_url || 'embedded / local')}</div>
              <div class="adapter-card-foot">
                <span class="muted-small mono">${a.lines || 0} 行</span>
                <div class="actions">
                  <button type="button" class="btn btn-ghost btn-sm" data-ov-code="${escapeHtml(a.name)}">源码</button>
                  <button type="button" class="btn btn-primary btn-sm" data-ov-run="${escapeHtml(a.name)}">试跑</button>
                </div>
              </div>
            </div>`;
        })
        .join('');

      $('#ov-adapter-grid').querySelectorAll('[data-ov-run]').forEach((b) => {
        b.addEventListener('click', () => {
          selectedAdapter = b.dataset.ovRun;
          switchView('runner');
        });
      });
      $('#ov-adapter-grid').querySelectorAll('[data-ov-code]').forEach((b) => {
        b.addEventListener('click', () => {
          switchView('scaffold');
          loadAdapterCode(b.dataset.ovCode);
        });
      });

      // Recent accounts live feed
      const racc = data.recent_accounts || [];
      const abox = $('#ov-recent-accounts');
      if (abox) {
        if (!racc.length) {
          abox.innerHTML = `<div class="muted-small" style="padding:18px;text-align:center">暂无成号产出，跑通任意适配器后实时汇聚于此</div>`;
        } else {
          abox.innerHTML = racc
            .map(
              (a) => `
              <div class="history-item" style="cursor:default">
                <div style="min-width:0;flex:1">
                  <div class="mono" style="font-size:12px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${escapeHtml(a.email)}</div>
                  <div class="muted-small mono" style="font-size:10.5px">${a.has_key ? `Key: ${escapeHtml(a.masked_key)}` : escapeHtml(a.username || '标准账号')}</div>
                </div>
                <div class="actions" style="flex-shrink:0">
                  <button type="button" class="btn btn-ghost btn-sm" data-ov-cp="${escapeHtml(a.has_key ? a.api_key : a.email)}">复制</button>
                </div>
              </div>`
            )
            .join('');
          abox.querySelectorAll('[data-ov-cp]').forEach((b) => {
            b.addEventListener('click', () => copyText(b.dataset.ovCp, ''));
          });
        }
      }
    } catch {}
  }

  // ==================== 1. Doctor ====================
  async function loadDoctor() {
    const banner = $('#doctor-banner');
    const grid = $('#doctor-grid');
    banner.className = 'banner muted';
    banner.textContent = '正在执行环境真执行体检…';
    try {
      const res = await fetch('/api/doctor');
      const data = await res.json();
      const checks = data.checks || [];
      const passCount = checks.filter((c) => c.state === 'pass').length;

      banner.className = `banner ${data.ready ? 'ok' : 'fail'}`;
      banner.textContent = data.ready
        ? `环境就绪 (${data.checked_at}) · 0 阻塞项，${data.warnings} 条建议项`
        : `检测到 ${data.blockers} 个阻塞项，请按卡片提示修复后再试跑 (${data.checked_at})`;

      $('#ov-doctor').textContent = `${passCount}/${checks.length}`;
      $('#ov-doctor-sub').textContent = data.ready ? '核心依赖全部通过' : `${data.blockers} 项待修复`;
      $('#hb-summary').textContent = data.ready ? `${passCount}/${checks.length} 在线` : `${data.blockers} 阻塞`;
      $('#hb-dot').className = `dot ${data.ready ? 'emerald' : 'rose'}`;

      const badge = $('#nav-badge-doctor');
      if (data.blockers > 0) {
        badge.textContent = String(data.blockers);
        badge.classList.remove('hidden');
      } else {
        badge.classList.add('hidden');
      }

      grid.innerHTML = checks
        .map(
          (c) => `
          <div class="check-item">
            <div class="check-top">
              <span class="check-name">${escapeHtml(c.name)}</span>
              <span class="pill ${c.state}">${c.state === 'pass' ? '通过' : c.state === 'warn' ? '提醒' : '缺失'}</span>
            </div>
            <div class="check-detail">${escapeHtml(c.detail)}</div>
            ${c.hint ? `<div class="check-hint">→ ${escapeHtml(c.hint)}</div>` : ''}
          </div>`
        )
        .join('');
    } catch (e) {
      banner.className = 'banner fail';
      banner.textContent = '体检接口调用失败: ' + e.message;
    }
  }
  $('#btn-run-doctor').addEventListener('click', loadDoctor);

  // ==================== 2. Runner ====================
  function renderAdapters() {
    const box = $('#adapter-list');
    const q = ($('#adapter-search')?.value || '').trim().toLowerCase();
    const list = q
      ? adaptersCache.filter((a) => `${a.name} ${a.target_url || ''}`.toLowerCase().includes(q))
      : adaptersCache;
    if (!list.length) {
      box.innerHTML = `<div class="empty-box" style="padding:20px">未匹配到站点适配器</div>`;
    } else {
      box.innerHTML = list
        .map((a) => {
          const tm = TIER_META[a.tier] || TIER_META.template;
          return `
            <button type="button" class="adapter-btn ${a.name === selectedAdapter ? 'selected' : ''}" data-name="${escapeHtml(a.name)}">
              <div class="adapter-card-top">
                <span class="adapter-name">${escapeHtml(a.name)}</span>
                <span class="badge ${tm.cls}">${tm.label}</span>
              </div>
              <div class="adapter-url">${escapeHtml(a.target_url || 'embedded / local')}</div>
            </button>`;
        })
        .join('');
      box.querySelectorAll('.adapter-btn').forEach((b) => {
        b.addEventListener('click', () => {
          selectedAdapter = b.dataset.name;
          box.querySelectorAll('.adapter-btn').forEach((x) => x.classList.toggle('selected', x === b));
        });
      });
    }

    // Also render Studio adapter list
    const sbox = $('#studio-adapter-list');
    if (sbox) {
      sbox.innerHTML = adaptersCache
        .map((a) => {
          const tm = TIER_META[a.tier] || TIER_META.template;
          return `
            <button type="button" class="adapter-btn ${a.name === activeCodeName ? 'selected' : ''}" data-code-name="${escapeHtml(a.name)}">
              <div class="adapter-card-top">
                <span class="adapter-name">${escapeHtml(a.name)}</span>
                <span class="badge ${tm.cls}">${tm.label}</span>
              </div>
              <div class="adapter-url">${escapeHtml(a.file || '')} · ${a.lines || 0} 行</div>
            </button>`;
        })
        .join('');
      sbox.querySelectorAll('[data-code-name]').forEach((b) => {
        b.addEventListener('click', () => loadAdapterCode(b.dataset.codeName));
      });
    }
  }

  async function loadAdapters() {
    try {
      const res = await fetch('/api/adapters');
      const data = await res.json();
      adaptersCache = data.adapters || [];
      if (!selectedAdapter && adaptersCache.length) selectedAdapter = adaptersCache[0].name;
      renderAdapters();
    } catch {}
  }
  $('#adapter-search').addEventListener('input', renderAdapters);

  $$('#run-headless-group .seg-btn').forEach((btn) => {
    btn.addEventListener('click', () => {
      runHeadlessMode = btn.dataset.hl;
      $$('#run-headless-group .seg-btn').forEach((b) => b.classList.toggle('active', b === btn));
    });
  });

  function renderLogs() {
    const box = $('#console-log');
    const filtered = logFilter === 'all' ? logEntries : logEntries.filter((x) => x.level === logFilter);
    if (!filtered.length) {
      box.textContent = logEntries.length ? '当前筛选级别下无日志。' : '尚未运行任务。点击左侧适配器开始试跑。';
      return;
    }
    box.innerHTML = '';
    const frag = document.createDocumentFragment();
    for (const item of filtered.slice(-1500)) {
      const div = document.createElement('div');
      if (item.level === 'ok') div.className = 'log-ok';
      if (item.level === 'warn') div.className = 'log-warn';
      if (item.level === 'error') div.className = 'log-err';
      div.textContent = item.text;
      frag.appendChild(div);
    }
    box.appendChild(frag);
    box.scrollTop = box.scrollHeight;
  }

  function appendLog(line, level) {
    logEntries.push({ text: line, level: level || 'info' });
    if (logEntries.length > 2000) logEntries = logEntries.slice(-1500);
    if (logFilter !== 'all' && (level || 'info') !== logFilter) return;
    const box = $('#console-log');
    if (logEntries.length === 1) box.innerHTML = '';
    const div = document.createElement('div');
    if (level === 'ok') div.className = 'log-ok';
    if (level === 'warn') div.className = 'log-warn';
    if (level === 'error') div.className = 'log-err';
    div.textContent = line;
    box.appendChild(div);
    box.scrollTop = box.scrollHeight;
  }

  $$('#log-filter-group .seg-btn').forEach((btn) => {
    btn.addEventListener('click', () => {
      logFilter = btn.dataset.lvl;
      $$('#log-filter-group .seg-btn').forEach((b) => b.classList.toggle('active', b === btn));
      renderLogs();
    });
  });

  function setRunningState(running) {
    $('#btn-start-run').disabled = running;
    $('#btn-stop-run').disabled = !running;
    $('#nav-badge-runner').classList.toggle('hidden', !running);
    $('#console-dot').className = `dot ${running ? 'blue' : 'neutral'}`;
  }

  async function replayJobLog(jid) {
    const lr = await fetch(`/api/jobs/${encodeURIComponent(jid)}/log`);
    if (!lr.ok) return toast('未找到该任务日志');
    const ld = await lr.json();
    $('#console-status').textContent = `历史回放: ${jid}`;
    $('#console-dot').className = 'dot neutral';
    logEntries = String(ld.log || '')
      .split(/\r?\n/)
      .filter(Boolean)
      .map((line) => {
        const low = line.toLowerCase();
        const level =
          low.includes('[error]') || low.includes('traceback')
            ? 'error'
            : low.includes('[warn]')
              ? 'warn'
              : low.includes('[ok]') || low.includes('成功')
                ? 'ok'
                : 'info';
        return { text: line, level };
      });
    renderLogs();
  }

  function attachJobStream(job) {
    if (logStream) logStream.close();
    activeJobId = job.id;
    logEntries = [];
    $('#console-log').innerHTML = '';
    $('#console-status').textContent = `运行中: ${job.adapter} (PID=${job.pid || '-'})`;
    setRunningState(true);

    logStream = new EventSource(`/api/jobs/${encodeURIComponent(activeJobId)}/stream`);
    logStream.onmessage = (ev) => {
      const msg = JSON.parse(ev.data);
      if (msg.type === 'log') {
        appendLog(`[${msg.ts}] ${msg.text}`, msg.level);
      } else if (msg.type === 'status') {
        $('#console-status').textContent = `已结束: ${msg.status} (code=${msg.returncode})`;
        setRunningState(false);
        $('#console-dot').className = `dot ${msg.status === 'done' ? 'emerald' : 'rose'}`;
        logStream.close();
        logStream = null;
        loadJobs(false);
        loadOverview();
        toast(`单站试跑结束 (${msg.status})`);
      }
    };
    logStream.onerror = () => {
      if (logStream) { logStream.close(); logStream = null; }
      setRunningState(false);
      loadJobs(false);
    };
  }

  async function loadJobs(autoReconnect) {
    try {
      const res = await fetch('/api/jobs');
      const data = await res.json();
      const jobs = data.jobs || [];
      const box = $('#job-history');
      if (!jobs.length) {
        box.innerHTML = `<div class="muted-small">暂无历史运行记录</div>`;
      } else {
        box.innerHTML = jobs
          .slice(0, 8)
          .map(
            (j) => `
            <button type="button" class="history-item" data-jid="${escapeHtml(j.id)}">
              <span class="mono">${escapeHtml(j.adapter)} ×${j.count}</span>
              <span class="pill ${j.status === 'done' ? 'pass' : j.status === 'running' ? 'warn' : 'fail'}">${escapeHtml(j.status)}</span>
            </button>`
          )
          .join('');
        box.querySelectorAll('.history-item').forEach((btn) => {
          btn.addEventListener('click', () => replayJobLog(btn.dataset.jid));
        });
      }
      if (autoReconnect && data.active_job && data.active_job.status === 'running') {
        attachJobStream(data.active_job);
      }
    } catch {}
  }

  $('#btn-start-run').addEventListener('click', async () => {
    if (!selectedAdapter) return toast('请先选择站点适配器');
    const count = Math.max(1, Math.min(20, Number($('#run-count').value) || 1));
    const custom_email = $('#run-custom-email').value.trim();
    const headless = runHeadlessMode === 'auto' ? null : runHeadlessMode === 'true';
    setRunningState(true);
    try {
      const res = await fetch('/api/run', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ adapter: selectedAdapter, count, headless, custom_email }),
      });
      const data = await res.json();
      if (!data.ok) {
        setRunningState(false);
        return toast(data.error || '启动失败');
      }
      attachJobStream(data.job);
    } catch (e) {
      setRunningState(false);
      toast('启动异常: ' + e.message);
    }
  });

  $('#btn-stop-run').addEventListener('click', async () => {
    if (!activeJobId) return;
    await fetch(`/api/jobs/${encodeURIComponent(activeJobId)}/stop`, { method: 'POST' });
    setRunningState(false);
    toast('已发送进程树停止指令');
  });

  $('#btn-clear-log').addEventListener('click', () => {
    logEntries = [];
    $('#console-log').textContent = '';
  });

  // ==================== 3. Accounts & Key Ledger ====================
  function setAccFilter(val) {
    accFilter = val;
    $$('#acc-filter-group .seg-btn').forEach((b) => b.classList.toggle('active', b.dataset.af === val));
    renderAccounts();
  }
  $$('#acc-filter-group .seg-btn').forEach((b) => b.addEventListener('click', () => setAccFilter(b.dataset.af)));
  $$('[data-acc-filter]').forEach((b) => b.addEventListener('click', () => setAccFilter(b.dataset.accFilter)));

  function renderAccounts() {
    const q = ($('#accounts-search')?.value || '').trim().toLowerCase();
    const rows = accountsCache.filter((r) => {
      if (accFilter === 'with_key' && !r.api_key) return false;
      if (accFilter === 'plain' && r.api_key) return false;
      if (q && !`${r.email} ${r.username} ${r.api_key || ''}`.toLowerCase().includes(q)) return false;
      return true;
    });
    const empty = $('#accounts-empty');
    const tbl = $('#accounts-table');
    if (!rows.length) {
      empty.classList.remove('hidden');
      tbl.classList.add('hidden');
      return;
    }
    empty.classList.add('hidden');
    tbl.classList.remove('hidden');
    $('#accounts-tbody').innerHTML = rows
      .slice(0, 300)
      .map(
        (r, idx) => `
        <tr>
          <td class="mono">${escapeHtml(r.email)}</td>
          <td class="mono">${escapeHtml(revealSecrets ? r.password || '—' : maskSecret(r.password))}</td>
          <td class="mono">${escapeHtml(r.username || '—')}</td>
          <td class="mono">${
            r.api_key
              ? `<span class="badge emerald">${escapeHtml(revealSecrets ? r.api_key : maskSecret(r.api_key))}</span>`
              : '<span style="color:var(--text-muted)">—</span>'
          }</td>
          <td style="text-align:right; white-space:nowrap;">
            ${r.api_key ? `<button type="button" class="btn btn-secondary btn-sm" data-test-key="${idx}">测活</button>` : ''}
            ${r.api_key ? `<button type="button" class="btn btn-secondary btn-sm" data-copy-key="${idx}">Key</button>` : ''}
            <button type="button" class="btn btn-ghost btn-sm" data-copy-row="${idx}">整行</button>
            <button type="button" class="btn btn-ghost btn-sm" data-del-row="${idx}" style="color:var(--rose)">删</button>
          </td>
        </tr>`
      )
      .join('');

    $('#accounts-tbody').querySelectorAll('[data-copy-key]').forEach((b) => {
      b.addEventListener('click', () => {
        const row = rows[Number(b.dataset.copyKey)];
        if (row?.api_key) copyText(row.api_key, ' API Key');
      });
    });
    $('#accounts-tbody').querySelectorAll('[data-copy-row]').forEach((b) => {
      b.addEventListener('click', () => {
        const r = rows[Number(b.dataset.copyRow)];
        if (r) {
          const line = r.api_key
            ? `${r.email}----${r.password}----${r.username}----${r.api_key}`
            : `${r.email}----${r.password}----${r.username}`;
          copyText(line, ' 整行凭据');
        }
      });
    });
    $('#accounts-tbody').querySelectorAll('[data-test-key]').forEach((b) => {
      b.addEventListener('click', async () => {
        const r = rows[Number(b.dataset.testKey)];
        if (!r?.api_key) return;
        b.disabled = true;
        try {
          const resp = await fetch('/api/accounts/test-key', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ key: r.api_key, base_url: $('#acc-test-base').value.trim() }),
          });
          const d = await resp.json();
          toast(d.detail || (d.ok ? '存活' : '失效'));
        } finally {
          b.disabled = false;
        }
      });
    });
    $('#accounts-tbody').querySelectorAll('[data-del-row]').forEach((b) => {
      b.addEventListener('click', async () => {
        const r = rows[Number(b.dataset.delRow)];
        if (!r || !window.confirm(`确认删除账号 ${r.email}？（自动创建 .bak 备份）`)) return;
        try {
          const resp = await fetch('/api/accounts/delete', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ email: r.email, api_key: r.api_key || '' }),
          });
          const d = await resp.json().catch(() => ({}));
          if (!resp.ok || !d.ok || !(Number(d.removed || 0) > 0)) {
            toast(d.error || '删除失败：未匹配到该台账记录');
            return;
          }
          toast(`已移除 ${r.email}`);
          loadAccounts();
          loadOverview();
        } catch (err) {
          toast('删除请求异常: ' + (err?.message || err));
        }
      });
    });
  }

  async function loadAccounts() {
    try {
      const res = await fetch('/api/accounts');
      const data = await res.json();
      accountsCache = data.accounts || [];
      const withKey = accountsCache.filter((r) => Boolean(r.api_key)).length;
      $('#acc-kpi-total').textContent = String(accountsCache.length);
      $('#acc-kpi-keys').textContent = String(withKey);
      $('#acc-kpi-plain').textContent = String(accountsCache.length - withKey);
      renderAccounts();
    } catch {}
  }

  $('#accounts-search').addEventListener('input', renderAccounts);
  $('#btn-toggle-secrets').addEventListener('click', () => {
    revealSecrets = !revealSecrets;
    $('#btn-toggle-secrets').textContent = revealSecrets ? '隐藏明文' : '显示明文';
    renderAccounts();
  });
  $('#btn-clear-accounts').addEventListener('click', async () => {
    if (!accountsCache.length) return;
    if (!window.confirm('确认清空全部本地账号台账？（将自动保存 accounts.txt.bak 备份）')) return;
    await fetch('/api/accounts/clear', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' });
    toast('已清空账号台账（原文件已备份至 .bak）');
    loadAccounts();
    loadOverview();
  });

  $$('[data-export]').forEach((btn) => {
    btn.addEventListener('click', async () => {
      const fmt = btn.dataset.export;
      const res = await fetch(`/api/export/${fmt}`, { method: 'POST' });
      const data = await res.json();
      if (!data.ok) return toast(data.error || '导出失败（可能暂无账号）');
      const blob = new Blob([data.content], { type: 'text/plain;charset=utf-8' });
      const a = document.createElement('a');
      a.href = URL.createObjectURL(blob);
      a.download = data.filename;
      a.click();
      URL.revokeObjectURL(a.href);
      toast(`已导出 ${data.filename}`);
    });
  });

  // ==================== 4. Tools Lite ====================
  let ipScoreAutoLoaded = false;
  let lastHarSkeleton = '';

  function renderIpScoreBox(d) {
    const box = $('#ips-result');
    const gCls = d.grade === 'S' ? 'emerald' : d.grade === 'A' ? 'sky' : d.grade === 'B' ? 'amber' : 'rose';
    const stroke = d.grade === 'S' ? '#10b981' : d.grade === 'A' ? '#0ea5e9' : d.grade === 'B' ? '#f59e0b' : '#f43f5e';
    const sc = Math.max(0, Math.min(100, Number(d.score || 0)));
    const off = (125.66 * (1 - sc / 100)).toFixed(2);
    const factorsHtml = (d.factors || [])
      .map((f) => `<span class="badge ${f.tone === 'emerald' ? 'emerald' : f.tone === 'rose' ? 'rose' : f.tone === 'amber' ? 'amber' : 'neutral'}">${escapeHtml(f.label)} (${f.delta === 0 ? '±0' : f.delta})</span>`)
      .join('');
    box.innerHTML = `
      <div class="totp-box" style="margin-top:0">
        <div class="totp-left">
          <div class="totp-ring">
            <svg viewBox="0 0 48 48"><circle cx="24" cy="24" r="20" class="ring-bg"/><circle cx="24" cy="24" r="20" class="ring-fg" style="stroke:${stroke};stroke-dashoffset:${off}"/></svg>
            <span class="mono">${sc}</span>
          </div>
          <div>
            <div style="display:flex;flex-wrap:wrap;align-items:center;gap:8px">
              <span class="mono" style="font-size:16px;font-weight:700">${escapeHtml(d.ip || '探测失败')}</span>
              <span class="badge ${gCls}">${escapeHtml(d.grade)} 级</span>
              ${d.ip_type ? `<span class="badge ${d.residential ? 'emerald' : 'amber'}">${escapeHtml(d.ip_type)}</span>` : ''}
              ${d.asn ? `<span class="badge neutral mono">${escapeHtml(d.asn)}</span>` : ''}
              ${d.cached ? `<span class="badge neutral">5m 缓存</span>` : ''}
            </div>
            <div class="card-sub" style="margin-top:4px;color:var(--text-primary)">${escapeHtml(d.verdict || d.error || '')}</div>
            <div class="card-sub mono" style="margin-top:4px">
              位置: ${escapeHtml([d.country, d.region, d.city].filter(Boolean).join(' · ') || '—')} · ISP: ${escapeHtml(d.isp || '—')} · 滥用分: ${d.abuser_score ?? 0} · ${d.latency_ms ?? 0}ms
            </div>
          </div>
        </div>
      </div>
      ${factorsHtml ? `<div class="actions" style="margin-top:10px">${factorsHtml}</div>` : ''}`;
  }

  async function autoLoadCurrentIpScore() {
    if (ipScoreAutoLoaded) return;
    ipScoreAutoLoaded = true;
    $('#ips-result').innerHTML = `<div class="empty-box">正在静默探测当前出口 IP 纯净度…</div>`;
    try {
      const res = await fetch('/api/tools/ip-score/current');
      const d = await res.json();
      renderIpScoreBox(d);
    } catch {
      ipScoreAutoLoaded = false;
    }
  }

  $$('#tools-tab-group .seg-btn').forEach((btn) => {
    btn.addEventListener('click', () => {
      const t = btn.dataset.tool;
      $$('#tools-tab-group .seg-btn').forEach((b) => b.classList.toggle('active', b === btn));
      $$('.tool-panel').forEach((p) => p.classList.toggle('active', p.id === `tool-panel-${t}`));
      if (t === 'totp') startTotpLoop();
      else stopTotpLoop();
      if (t === 'ipscore') autoLoadCurrentIpScore();
    });
  });

  // 4.1 Identity
  $$('#id-country-group .seg-btn').forEach((btn) => {
    btn.addEventListener('click', () => {
      idCountry = btn.dataset.cc;
      $$('#id-country-group .seg-btn').forEach((b) => b.classList.toggle('active', b === btn));
      genIdentities();
    });
  });

  async function genIdentities() {
    try {
      const res = await fetch('/api/tools/identity', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          country: idCountry,
          count: 2,
          domain: $('#id-domain').value.trim(),
          password_length: Number($('#id-pwlen').value) || 14,
        }),
      });
      const data = await res.json();
      const list = data.identities || [];
      $('#identity-grid').innerHTML = list
        .map((p) => {
          const rows = [
            ['邮箱', p.email],
            ['强密码', p.password],
            ['用户名', p.username],
            ['城市/州/邮编', `${p.city}, ${p.state} ${p.postal_code || p.zip_code || ''}`],
            ['电话', p.phone],
            ['生日', p.birth_date || `${p.birth_year || ''}`],
          ];
          return `
            <div class="card">
              <div class="card-head compact">
                <div>
                  <div class="card-title">${escapeHtml(p.full_name)}</div>
                  <div class="card-sub">${escapeHtml(p.country || idCountry)}</div>
                </div>
                <button type="button" class="btn btn-secondary btn-sm" data-copy-raw="${escapeHtml(JSON.stringify(p))}">复制 JSON</button>
              </div>
              <div class="id-fields">
                ${rows
                  .map(
                    ([k, v]) => `
                    <div class="id-field">
                      <div style="min-width:0"><div class="id-k">${k}</div><div class="id-v mono">${escapeHtml(v)}</div></div>
                      <button type="button" class="btn btn-ghost btn-sm" data-copy-raw="${escapeHtml(v)}">复制</button>
                    </div>`
                  )
                  .join('')}
              </div>
            </div>`;
        })
        .join('');
      $('#identity-grid').querySelectorAll('[data-copy-raw]').forEach((b) => {
        b.addEventListener('click', () => copyText(b.dataset.copyRaw, ''));
      });
    } catch {}
  }
  $('#btn-gen-identity').addEventListener('click', genIdentities);

  // 4.15 IP Purity & Risk Scorer
  $('#btn-ip-score').addEventListener('click', async () => {
    const btn = $('#btn-ip-score');
    btn.disabled = true;
    btn.textContent = '多源情报打分中…';
    try {
      const res = await fetch('/api/tools/ip-score', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          ip: $('#ips-ip').value.trim(),
          proxy_url: $('#ips-proxy').value.trim(),
          target_url: $('#ips-target').value.trim(),
        }),
      });
      const d = await res.json();
      renderIpScoreBox(d);
      const sc = Math.max(0, Math.min(100, Number(d.score || 0)));
      toast(d.ok ? `IP ${d.ip} 综合纯净度 ${sc} 分 (${d.grade} 级)` : d.error || '探测失败');
    } catch (e) {
      toast('IP 打分异常: ' + e.message);
    } finally {
      btn.disabled = false;
      btn.textContent = '立即检测并打分';
    }
  });

  // 4.18 Offline HAR Protocol Extractor
  $('#har-file')?.addEventListener('change', (ev) => {
    const f = ev.target.files?.[0];
    if (!f) return;
    const reader = new FileReader();
    reader.onload = () => {
      $('#har-text').value = String(reader.result || '');
      toast(`已载入 ${f.name} (${Math.round(f.size / 1024)} KB)`);
    };
    reader.readAsText(f);
  });

  $('#btn-parse-har')?.addEventListener('click', async () => {
    const har_text = $('#har-text').value.trim();
    if (!har_text) return toast('请先上传 .har 文件或粘贴 HAR JSON');
    const btn = $('#btn-parse-har');
    btn.disabled = true;
    btn.textContent = '提炼中…';
    try {
      const res = await fetch('/api/tools/har', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ har_text, site_name: $('#har-site').value.trim() }),
      });
      const d = await res.json();
      if (!d.ok) return toast(d.error || 'HAR 解析失败');
      const sigs = d.signals || [];
      $('#har-signals').innerHTML = sigs.length
        ? sigs.map((s) => `<span class="badge sky">${escapeHtml(s)}</span>`).join('')
        : `<span class="badge emerald">纯 REST 协议链（无显著人机特征）</span>`;
      const steps = d.api_steps || [];
      $('#har-steps-count').textContent = String(steps.length);
      $('#har-steps-list').innerHTML = steps.length
        ? steps
            .map(
              (st) => `
              <div class="history-item" style="cursor:default;flex-direction:column;align-items:flex-start;gap:4px">
                <div style="display:flex;width:100%;justify-content:space-between;gap:6px">
                  <span class="mono" style="font-weight:600;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${escapeHtml(st.method)} ${escapeHtml(st.path)}</span>
                  <span class="badge ${st.status >= 200 && st.status < 300 ? 'emerald' : 'amber'} mono">${st.status}</span>
                </div>
                ${st.req_keys?.length ? `<div class="muted-small mono" style="font-size:10.5px">Req: ${escapeHtml(st.req_keys.join(', '))}</div>` : ''}
              </div>`
            )
            .join('')
        : `<div class="empty-box" style="padding:18px">未提炼到业务接口，请确认 HAR 含 POST/JSON 请求</div>`;
      lastHarSkeleton = d.skeleton || '';
      $('#har-skeleton').textContent = lastHarSkeleton;
      $('#har-result').classList.remove('hidden');
      toast(`HAR 解析完成：共提炼 ${steps.length} 条业务请求`);
    } catch (e) {
      toast('HAR 解析异常: ' + e.message);
    } finally {
      btn.disabled = false;
      btn.textContent = '提炼 API 链与骨架';
    }
  });
  $('#btn-copy-har-code')?.addEventListener('click', () => lastHarSkeleton && copyText(lastHarSkeleton, '协议骨架代码'));

  // 4.2 TempMail Web Inbox
  $('#btn-tm-new').addEventListener('click', async () => {
    const btn = $('#btn-tm-new');
    btn.disabled = true;
    btn.textContent = '申请中…';
    try {
      const res = await fetch('/api/tempmail/new', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' });
      const d = await res.json();
      if (!d.ok) return toast(d.error || '申请临时邮箱失败');
      tmSessionId = d.session_id;
      tmEmail = d.email;
      $('#tm-provider').textContent = d.provider;
      $('#tm-email').textContent = d.email;
      $('#tm-addr-bar').classList.remove('hidden');
      $('#btn-tm-refresh').disabled = false;
      $('#tm-messages').innerHTML = `<div class="empty-box">收件箱已就绪，发送验证邮件后点右上角「刷新收件箱」查看验证码。</div>`;
      toast(`已申请临时邮箱: ${d.email}`);
    } finally {
      btn.disabled = false;
      btn.textContent = '申请新临时邮箱';
    }
  });

  $('#btn-tm-copy').addEventListener('click', () => tmEmail && copyText(tmEmail, '临时邮箱'));
  $('#btn-tm-use').addEventListener('click', () => {
    if (!tmEmail) return;
    $('#run-custom-email').value = tmEmail;
    switchView('runner');
    toast('已将临时邮箱带入单站试跑');
  });

  $('#btn-tm-refresh').addEventListener('click', async () => {
    if (!tmSessionId) return;
    const btn = $('#btn-tm-refresh');
    btn.disabled = true;
    try {
      const res = await fetch(`/api/tempmail/messages?session_id=${encodeURIComponent(tmSessionId)}`);
      const d = await res.json();
      if (!d.ok) return toast(d.error || '拉取收件箱失败');
      const msgs = d.messages || [];
      if (!msgs.length) {
        $('#tm-messages').innerHTML = `<div class="empty-box">暂无新邮件（刚发出请等待 3–5 秒后再点刷新）。</div>`;
        return;
      }
      $('#tm-messages').innerHTML = msgs
        .map(
          (m) => `
          <div class="tm-msg-item">
            <div class="tm-msg-head">
              <span>${escapeHtml(m.subject || '(无主题)')} <span class="muted-small mono">· ${escapeHtml(m.from)}</span></span>
              ${m.code ? `<button type="button" class="btn btn-primary btn-sm" data-copy-otp="${escapeHtml(m.code)}">复制验证码 ${escapeHtml(m.code)}</button>` : ''}
            </div>
            <div class="tm-msg-body">${escapeHtml(m.body)}</div>
          </div>`
        )
        .join('');
      $('#tm-messages').querySelectorAll('[data-copy-otp]').forEach((b) => {
        b.addEventListener('click', () => copyText(b.dataset.copyOtp, '验证码'));
      });
    } finally {
      btn.disabled = false;
    }
  });

  // 4.3 TOTP
  function loadSavedTotps() {
    try { return JSON.parse(localStorage.getItem(TOTP_KEY) || '[]') || []; } catch { return []; }
  }
  function renderSavedTotps() {
    const list = loadSavedTotps();
    const box = $('#totp-saved-list');
    if (!list.length) {
      box.innerHTML = `<div class="muted-small" style="padding:18px;text-align:center">暂无收藏，右侧点「收藏密钥」</div>`;
      return;
    }
    box.innerHTML = list
      .map(
        (it) => `
        <div class="history-item">
          <button type="button" class="btn btn-ghost btn-sm" style="flex:1;justify-content:flex-start" data-pick-totp="${escapeHtml(it.secret)}">${escapeHtml(it.label)}</button>
          <button type="button" class="btn btn-ghost btn-sm" style="color:var(--rose)" data-del-totp="${escapeHtml(it.id)}">删</button>
        </div>`
      )
      .join('');
    box.querySelectorAll('[data-pick-totp]').forEach((b) => {
      b.addEventListener('click', () => {
        $('#totp-secret').value = b.dataset.pickTotp;
        refreshTotp();
      });
    });
    box.querySelectorAll('[data-del-totp]').forEach((b) => {
      b.addEventListener('click', () => {
        const next = loadSavedTotps().filter((x) => x.id !== b.dataset.delTotp);
        localStorage.setItem(TOTP_KEY, JSON.stringify(next));
        renderSavedTotps();
      });
    });
  }
  renderSavedTotps();

  async function refreshTotp() {
    const sec = $('#totp-secret').value.trim();
    if (!sec) return;
    const res = await fetch('/api/tools/totp', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ secret: sec }),
    });
    const d = await res.json();
    if (!d.ok) {
      lastTotpCode = '';
      $('#totp-code').textContent = '--- ---';
      $('#totp-err-badge').classList.remove('hidden');
      return;
    }
    $('#totp-err-badge').classList.add('hidden');
    lastTotpCode = d.code;
    totpCountdown = d.remaining;
    $('#totp-code').textContent = `${d.code.slice(0, 3)} ${d.code.slice(3)}`;
    updateTotpRing();
  }

  function updateTotpRing() {
    $('#totp-sec').textContent = `${totpCountdown}s`;
    const arc = $('#totp-arc');
    arc.style.strokeDashoffset = String(125.66 * (1 - totpCountdown / 30));
    arc.style.stroke = totpCountdown <= 5 ? '#f43f5e' : '#10b981';
  }

  function startTotpLoop() {
    stopTotpLoop();
    refreshTotp();
    totpTimer = setInterval(() => {
      totpCountdown -= 1;
      if (totpCountdown <= 0) {
        refreshTotp();
      } else {
        updateTotpRing();
      }
    }, 1000);
  }
  function stopTotpLoop() {
    if (totpTimer) { clearInterval(totpTimer); totpTimer = null; }
  }

  $('#totp-secret').addEventListener('input', refreshTotp);
  $('#btn-totp-copy').addEventListener('click', () => lastTotpCode && copyText(lastTotpCode, ' 2FA 验证码'));
  $('#btn-totp-save').addEventListener('click', () => {
    const secret = $('#totp-secret').value.trim();
    if (!secret) return;
    const list = loadSavedTotps();
    const label = $('#totp-label').value.trim() || `密钥 ${list.length + 1}`;
    const next = [{ id: String(Date.now()), label, secret }, ...list.filter((x) => x.secret !== secret)].slice(0, 20);
    localStorage.setItem(TOTP_KEY, JSON.stringify(next));
    $('#totp-label').value = '';
    renderSavedTotps();
    toast(`已收藏「${label}」`);
  });

  // 4.4 Luhn Cards
  $$('#card-brand-group .seg-btn').forEach((btn) => {
    btn.addEventListener('click', () => {
      cardBrand = btn.dataset.brand;
      $$('#card-brand-group .seg-btn').forEach((b) => b.classList.toggle('active', b === btn));
      genCards();
    });
  });
  async function genCards() {
    const res = await fetch('/api/tools/card', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ brand: cardBrand, bin_prefix: $('#card-bin').value.trim(), count: 6 }),
    });
    const d = await res.json();
    cardsCache = d.cards || [];
    $('#cards-grid').innerHTML = cardsCache
      .map(
        (c) => `
        <div class="vcard">
          <div class="vcard-top"><span>${escapeHtml(c.brand)}</span><span class="badge emerald">Luhn Valid</span></div>
          <div class="vcard-num mono"><span>${escapeHtml(c.formatted)}</span><button type="button" class="btn btn-ghost btn-sm" style="color:#fff" data-copy-c="${escapeHtml(c.card_number)}">复制</button></div>
          <div class="vcard-bot mono"><span>EXP ${escapeHtml(c.exp)}</span><span>CVV ${escapeHtml(c.cvv)}</span><button type="button" class="btn btn-secondary btn-sm" data-copy-c="${escapeHtml(`${c.card_number}|${c.exp_month}|${c.exp_year}|${c.cvv}`)}">全套</button></div>
        </div>`
      )
      .join('');
    $('#cards-grid').querySelectorAll('[data-copy-c]').forEach((b) => {
      b.addEventListener('click', () => copyText(b.dataset.copyC, '卡号'));
    });
  }
  $('#btn-gen-cards').addEventListener('click', genCards);
  $('#btn-copy-all-cards').addEventListener('click', () => {
    if (!cardsCache.length) return;
    copyText(cardsCache.map((c) => `${c.card_number}|${c.exp_month}|${c.exp_year}|${c.cvv}`).join('\n'), '全部卡号');
  });

  // ==================== 5. Failures & Snapshots ====================
  async function loadFailuresAndSnapshots() {
    try {
      const [fr, sr] = await Promise.all([fetch('/api/failures'), fetch('/api/snapshots')]);
      const fd = await fr.json();
      const sd = await sr.json();

      const fails = fd.failures || [];
      if (!fails.length) {
        $('#failures-empty').classList.remove('hidden');
        $('#failures-table').classList.add('hidden');
      } else {
        $('#failures-empty').classList.add('hidden');
        $('#failures-table').classList.remove('hidden');
        const CAT_BADGE = {
          soft: { label: '软失败', cls: 'amber' },
          captcha: { label: '人机验证', cls: 'sky' },
          dead: { label: '死号', cls: 'rose' },
          unknown: { label: '待复核', cls: 'neutral' },
        };
        $('#failures-tbody').innerHTML = fails
          .map((f) => {
            const cm = CAT_BADGE[f.category] || CAT_BADGE.unknown;
            return `
            <tr>
              <td class="mono" style="white-space:nowrap;font-size:11.5px">${escapeHtml(f.time)}</td>
              <td style="max-width:340px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${escapeHtml(f.reason)}"><span class="badge ${cm.cls}" style="margin-right:6px">${cm.label}</span>${escapeHtml(f.reason)}</td>
              <td class="mono muted-small" style="max-width:210px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${escapeHtml(f.account)}">${escapeHtml(f.account || '—')}</td>
              <td style="text-align:right;white-space:nowrap">
                ${f.snapshot_url ? `<button type="button" class="btn btn-secondary btn-sm" data-open-snap="${escapeHtml(f.snapshot_url)}" data-open-name="${escapeHtml(f.snapshot)}">查看截图</button>` : '<span class="muted-small">—</span>'}
              </td>
            </tr>`;
          })
          .join('');
        $('#failures-tbody').querySelectorAll('[data-open-snap]').forEach((b) => {
          b.addEventListener('click', () => openLightbox(b.dataset.openName, b.dataset.openSnap));
        });
      }

      const snaps = sd.snapshots || [];
      if (!snaps.length) {
        $('#snapshots-empty').classList.remove('hidden');
        $('#snapshots-grid').innerHTML = '';
      } else {
        $('#snapshots-empty').classList.add('hidden');
        $('#snapshots-grid').innerHTML = snaps
          .map(
            (s) => `
            <button type="button" class="snap-card" data-url="${escapeHtml(s.url)}" data-name="${escapeHtml(s.name)}">
              <img src="${escapeHtml(s.url)}" alt="${escapeHtml(s.name)}" loading="lazy" />
              <div class="snap-meta">
                <div class="snap-name mono">${escapeHtml(s.name)}</div>
                <div class="snap-sub">${escapeHtml(s.mtime)} · ${s.size_kb} KB</div>
              </div>
            </button>`
          )
          .join('');
        $('#snapshots-grid').querySelectorAll('.snap-card').forEach((card) => {
          card.addEventListener('click', () => openLightbox(card.dataset.name, card.dataset.url));
        });
      }
    } catch {}
  }

  function openLightbox(name, url) {
    $('#lightbox-title').textContent = name;
    $('#lightbox-img').src = url;
    $('#lightbox').classList.remove('hidden');
  }
  $('#btn-refresh-snaps').addEventListener('click', loadFailuresAndSnapshots);
  $('#btn-clear-fails').addEventListener('click', async () => {
    if (!window.confirm('确认清空 failed.txt 失败记录？（保留现场截图）')) return;
    await fetch('/api/failures/clear', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ clear_snapshots: false }),
    });
    toast('已清空失败日志');
    loadFailuresAndSnapshots();
    loadOverview();
  });
  $('#lightbox-close').addEventListener('click', () => $('#lightbox').classList.add('hidden'));
  $('#lightbox').addEventListener('click', (e) => {
    if (e.target.id === 'lightbox') $('#lightbox').classList.add('hidden');
  });
  window.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') $('#lightbox').classList.add('hidden');
  });

  // ==================== 6. Studio (Scaffold + Inspector + Code) ====================
  async function loadAdapterCode(name) {
    activeCodeName = name;
    renderAdapters();
    $('#studio-code-title').textContent = `源码预览 · sites/${name}.py`;
    $('#studio-code-view').textContent = '# 正在读取源码…';
    try {
      const res = await fetch(`/api/adapters/${encodeURIComponent(name)}/code`);
      const d = await res.json();
      if (!res.ok) {
        $('#studio-code-view').textContent = `# 读取失败: ${d.error || res.status}`;
        return;
      }
      activeCodeText = d.code || '';
      $('#studio-code-title').textContent = `源码审查 · ${d.file}`;
      $('#studio-code-sub').textContent = `层级: ${(TIER_META[d.tier] || TIER_META.template).label} · 只读参考`;
      $('#studio-code-view').textContent = activeCodeText;
    } catch (e) {
      $('#studio-code-view').textContent = `# 读取异常: ${e.message}`;
    }
  }
  $('#btn-copy-code').addEventListener('click', () => activeCodeText && copyText(activeCodeText, '适配器源码'));
  $('#btn-code-run').addEventListener('click', () => {
    if (activeCodeName) selectedAdapter = activeCodeName;
    switchView('runner');
  });

  $('#btn-probe-url').addEventListener('click', async () => {
    const url = $('#probe-url').value.trim();
    if (!url) return toast('请输入目标注册页 URL');
    const btn = $('#btn-probe-url');
    btn.disabled = true;
    btn.textContent = '嗅探中…';
    try {
      const res = await fetch('/api/inspector/probe', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ url }),
      });
      const d = await res.json();
      const box = $('#probe-out');
      box.classList.remove('hidden');
      box.textContent = JSON.stringify(d, null, 2);
      if (d.ok) {
        $('#btn-probe-to-sc').classList.remove('hidden');
        toast(`嗅探完成：发现 ${d.detected_count || 0} 项表单字段`);
      } else {
        toast(d.error || '嗅探失败');
      }
    } finally {
      btn.disabled = false;
      btn.textContent = '开始嗅探';
    }
  });
  $('#btn-probe-to-sc').addEventListener('click', () => {
    const url = $('#probe-url').value.trim();
    $('#sc-url').value = url;
    try {
      const host = new URL(url).hostname.replace(/^www\./, '').split('.')[0];
      if (host && !$('#sc-name').value.trim()) $('#sc-name').value = host;
    } catch {}
    toast('已同步填入左侧脚手架');
  });

  $('#btn-scaffold').addEventListener('click', async () => {
    const site_name = $('#sc-name').value.trim();
    const url = $('#sc-url').value.trim();
    const probe = $('#sc-probe').checked;
    const overwrite = $('#sc-overwrite').checked;
    const box = $('#scaffold-msg');
    if (!site_name) return toast('请输入适配器名称');
    $('#btn-scaffold').disabled = true;
    try {
      const res = await fetch('/api/scaffold', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ site_name, url, probe, overwrite }),
      });
      const data = await res.json();
      box.classList.remove('hidden', 'err');
      if (data.ok) {
        box.textContent = data.message || `生成成功: ${data.path}`;
        toast(`已生成适配器 sites/${data.site}.py`);
        await loadAdapters();
        if (data.site) loadAdapterCode(data.site);
      } else {
        box.classList.add('err');
        box.textContent = data.error || '生成失败';
      }
    } finally {
      $('#btn-scaffold').disabled = false;
    }
  });

  // ==================== 7. Layout (Full 6 Presets + Zoom + Real Calibrate & Demo) ====================
  let layoutData = null;
  let layoutPresetKey = 'single';
  let layoutHoveredSlotIdx = null;
  let layoutFocusedSlotIdx = null;

  const PRESET_META = {
    single: { label: '1窗 · 全屏', concurrency: 1, getBlock: (g) => g?.single_concurrency },
    dual: { label: '2窗 · 左右分屏', concurrency: 2, getBlock: (g) => g?.dual_concurrency },
    quad_edge: { label: '4窗 · 贴边四宫格', concurrency: 4, applyOpts: { quad_mode: 'edge_2x2' }, getBlock: (g) => g?.quad_presets?.edge_2x2 },
    quad_compact: { label: '4窗 · 居中紧凑', concurrency: 4, applyOpts: { quad_mode: 'centered_compact' }, getBlock: (g) => g?.quad_presets?.centered_compact },
    hex_edge: { label: '6窗 · 贴边六宫格', concurrency: 6, applyOpts: { hex_mode: 'edge_3x2' }, getBlock: (g) => g?.hex_presets?.edge_3x2 },
    hex_wing: { label: '6窗 · 八字展翼', concurrency: 6, applyOpts: { hex_mode: 'wing_cascade' }, getBlock: (g) => g?.hex_presets?.wing_cascade },
  };

  function syncLayoutRangeFill(val) {
    const slider = $('#layout-zoom-range');
    if (!slider) return;
    const min = Number(slider.min || 45);
    const max = Number(slider.max || 125);
    const cur = Math.max(min, Math.min(max, Number(val ?? slider.value) || 100));
    const pct = max > min ? ((cur - min) / (max - min)) * 100 : 0;
    slider.style.setProperty('--range-pct', `${pct.toFixed(2)}%`);
  }

  function syncLayoutSlotFocus() {
    const activeIdx = layoutHoveredSlotIdx !== null ? layoutHoveredSlotIdx : layoutFocusedSlotIdx;
    $$('#layout-canvas .slot-rect[data-slot-idx]').forEach((el) => {
      el.classList.toggle('is-focused', activeIdx !== null && Number(el.dataset.slotIdx) === activeIdx);
    });
    $$('#layout-slot-list .layout-slot-row.active-slot[data-slot-idx]').forEach((el) => {
      el.classList.toggle('is-focused', activeIdx !== null && Number(el.dataset.slotIdx) === activeIdx);
    });
  }

  const layoutSlotExitTimers = new Map();

  function bindSlotInteractionOnce(el) {
    if (!el || el.dataset.boundSlot === '1') return;
    el.dataset.boundSlot = '1';
    el.addEventListener('mouseenter', () => {
      const idx = Number(el.dataset.slotIdx);
      layoutHoveredSlotIdx = idx;
      syncLayoutSlotFocus();
    });
    el.addEventListener('mouseleave', () => {
      const idx = Number(el.dataset.slotIdx);
      if (layoutHoveredSlotIdx === idx) {
        layoutHoveredSlotIdx = null;
        syncLayoutSlotFocus();
      }
    });
    el.addEventListener('click', (ev) => {
      ev.stopPropagation();
      const idx = Number(el.dataset.slotIdx);
      layoutFocusedSlotIdx = layoutFocusedSlotIdx === idx ? null : idx;
      syncLayoutSlotFocus();
    });
  }

  function bindLayoutSlotInteractions() {
    $$('#layout-canvas .slot-rect[data-slot-idx]').forEach(bindSlotInteractionOnce);
    $$('#layout-slot-list .layout-slot-row.active-slot[data-slot-idx]').forEach(bindSlotInteractionOnce);
  }

  function renderLayoutCanvas() {
    const g = layoutData || {};
    const meta = PRESET_META[layoutPresetKey] || PRESET_META.single;
    const block = meta.getBlock(g);
    const slots = block?.slots || [];
    const activeQuad = String(g.quad_mode || 'centered_compact');
    const activeHex = String(g.hex_mode || 'wing_cascade');

    if (layoutFocusedSlotIdx !== null && layoutFocusedSlotIdx >= slots.length) {
      layoutFocusedSlotIdx = null;
    }
    if (layoutHoveredSlotIdx !== null && layoutHoveredSlotIdx >= slots.length) {
      layoutHoveredSlotIdx = null;
    }

    $$('#layout-preset-tabs [data-preset]').forEach((btn) => {
      const k = btn.dataset.preset;
      const active = k === layoutPresetKey;
      btn.classList.toggle('btn-primary', active);
      btn.classList.toggle('btn-secondary', !active);
      const isDef =
        (k === 'quad_edge' && activeQuad === 'edge_2x2') ||
        (k === 'quad_compact' && activeQuad === 'centered_compact') ||
        (k === 'hex_edge' && activeHex === 'edge_3x2') ||
        (k === 'hex_wing' && activeHex === 'wing_cascade');
      btn.textContent = PRESET_META[k].label + (isDef ? ' ·默认' : '');
    });

    const singleSlot = g.single_concurrency?.slots?.[0];
    const waW = Math.max(640, Number(g.work_area?.width || (singleSlot ? singleSlot.width - 12 : 1920)));
    const waH = Math.max(480, Number(g.work_area?.height || (singleSlot ? singleSlot.height - 7 : 1040)));
    const waX = Number(g.work_area?.left || 0);
    const waY = Number(g.work_area?.top || 0);

    $('#layout-screen-label').textContent = g.screen_resolution || '待自动生成';
    $('#layout-desc').textContent = block?.description || meta.label;
    $('#layout-wa-label').textContent = `工作区 ${waW} × ${waH}`;
    $('#layout-slot-count').textContent = `${slots.length} 窗`;
    $('#btn-launch-calibrate').textContent = `启动真机交互标定 (-c ${meta.concurrency})`;

    const canvas = $('#layout-canvas');
    canvas.style.aspectRatio = `${waW} / ${waH}`;
    if (!slots.length) {
      layoutSlotExitTimers.forEach((t) => clearTimeout(t));
      layoutSlotExitTimers.clear();
      canvas.innerHTML = `<div class="empty-box" style="height:100%;display:flex;align-items:center;justify-content:center">尚未生成视窗配置，点击右上角「一键重算 6 套布局」。</div>`;
    } else {
      const emptyEl = canvas.querySelector('.empty-box');
      if (emptyEl) emptyEl.remove();

      const activeKeys = new Set(slots.map((_, i) => String(i)));
      // Smooth exit for removed slots
      canvas.querySelectorAll('.slot-rect').forEach((el) => {
        const key = el.dataset.slotIdx || '';
        if (!activeKeys.has(key)) {
          el.classList.remove('is-focused');
          el.classList.add('slot-exit');
          if (!layoutSlotExitTimers.has(key)) {
            const timer = setTimeout(() => {
              layoutSlotExitTimers.delete(key);
              el.remove();
            }, 300);
            layoutSlotExitTimers.set(key, timer);
          }
        }
      });

      // Reuse DOM nodes in place so CSS transitions glide smoothly across presets
      slots.forEach((s, i) => {
        const key = String(i);
        if (layoutSlotExitTimers.has(key)) {
          clearTimeout(layoutSlotExitTimers.get(key));
          layoutSlotExitTimers.delete(key);
        }
        const leftPct = Math.max(0, Math.min(96, ((s.x - waX) / waW) * 100));
        const topPct = Math.max(0, Math.min(96, ((s.y - waY) / waH) * 100));
        const wPct = Math.max(8, Math.min(100 - leftPct, (s.width / waW) * 100));
        const hPct = Math.max(12, Math.min(100 - topPct, (s.height / waH) * 100));
        const zPct = Math.round((s.zoom || 1) * 100);
        const colorCls = `c${i % 6}`;
        const titleText = s.name || `Slot #${i + 1}`;
        const dimText = `${s.width}×${s.height} @ (${s.x},${s.y})`;

        let el = canvas.querySelector(`.slot-rect[data-slot-idx="${key}"]`);
        const isNew = !el;
        if (isNew) {
          el = document.createElement('div');
          el.dataset.slotIdx = key;
          el.className = `slot-rect ${colorCls} slot-enter`;
          el.innerHTML = `<div class="slot-head"><span class="slot-title-text"></span><span class="slot-zoom mono"></span></div><div class="slot-dim mono"></div>`;
          bindSlotInteractionOnce(el);
          canvas.appendChild(el);
        } else {
          el.classList.remove('slot-exit');
        }

        el.style.left = `${leftPct}%`;
        el.style.top = `${topPct}%`;
        el.style.width = `${wPct}%`;
        el.style.height = `${hPct}%`;
        el.style.zIndex = String(10 + i);
        const tEl = el.querySelector('.slot-title-text');
        const zEl = el.querySelector('.slot-zoom');
        const dEl = el.querySelector('.slot-dim');
        if (tEl) tEl.textContent = titleText;
        if (zEl) zEl.textContent = `${zPct}%`;
        if (dEl) dEl.textContent = dimText;

        if (isNew) {
          requestAnimationFrame(() => {
            requestAnimationFrame(() => {
              el.classList.remove('slot-enter');
            });
          });
        }
      });
    }

    const curZoomPct = slots.length ? Math.round((slots[0].zoom || 1) * 100) : 100;
    const clampedZoom = Math.max(45, Math.min(125, curZoomPct));
    $('#layout-zoom-range').value = String(clampedZoom);
    $('#layout-zoom-val').textContent = `${curZoomPct}%`;
    syncLayoutRangeFill(clampedZoom);

    $('#layout-slot-list').innerHTML = slots
      .map(
        (s, i) => `
          <button type="button" class="layout-slot-row active-slot" data-slot-idx="${i}">
            <span class="slot-row-name">${escapeHtml(s.name || `Slot #${i + 1}`)}</span>
            <span class="slot-row-meta mono">${s.width}×${s.height} · ${Math.round((s.zoom || 1) * 100)}%</span>
          </button>`
      )
      .join('');
    bindLayoutSlotInteractions();
    syncLayoutSlotFocus();

    const defBtn = $('#btn-set-default-mode');
    defBtn.classList.remove('hidden');
    if (meta.applyOpts) {
      defBtn.style.visibility = 'visible';
      defBtn.disabled = false;
      defBtn.textContent = `将「${meta.label}」设为默认形态`;
    } else {
      defBtn.style.visibility = 'hidden';
      defBtn.disabled = true;
    }

    const grid = $('#layout-summary');
    grid.innerHTML = [
      { name: '屏幕逻辑工作区', val: g.screen_resolution || '已检测' },
      { name: '当前预览预设', val: `${meta.label} (${slots.length} 窗口)` },
      { name: '4/6 并发默认形态', val: `${activeQuad} / ${activeHex}` },
      { name: '最近校准时间', val: g.calibrated_at || '—' },
    ]
      .map((m) => `<div class="check-item"><div class="check-name">${escapeHtml(m.name)}</div><div class="check-detail mono">${escapeHtml(m.val)}</div></div>`)
      .join('');
  }

  $('#layout-canvas')?.addEventListener('click', () => {
    if (layoutFocusedSlotIdx !== null) {
      layoutFocusedSlotIdx = null;
      syncLayoutSlotFocus();
    }
  });

  async function loadLayout() {
    try {
      const res = await fetch('/api/layout');
      const data = await res.json();
      layoutData = data.geometry || {};
      renderLayoutCanvas();
    } catch {}
  }

  $$('#layout-preset-tabs [data-preset]').forEach((btn) => {
    btn.addEventListener('click', () => {
      layoutPresetKey = btn.dataset.preset;
      layoutHoveredSlotIdx = null;
      layoutFocusedSlotIdx = null;
      renderLayoutCanvas();
    });
  });

  $('#layout-zoom-range')?.addEventListener('input', (e) => {
    $('#layout-zoom-val').textContent = `${e.target.value}%`;
    syncLayoutRangeFill(e.target.value);
  });

  $('#btn-save-zoom')?.addEventListener('click', async () => {
    const zoom = Number($('#layout-zoom-range').value || 100) / 100;
    const res = await fetch('/api/layout/update', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ preset_key: layoutPresetKey, zoom }),
    });
    const d = await res.json();
    if (d.ok) {
      layoutData = d.geometry;
      renderLayoutCanvas();
      toast(`已保存「${PRESET_META[layoutPresetKey].label}」防切割缩放为 ${Math.round(zoom * 100)}%`);
    } else {
      toast(d.error || '保存缩放失败');
    }
  });

  $('#btn-set-default-mode')?.addEventListener('click', async () => {
    const opts = PRESET_META[layoutPresetKey]?.applyOpts;
    if (!opts) return;
    const res = await fetch('/api/layout/auto', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(opts),
    });
    const d = await res.json();
    if (d.ok) {
      layoutData = d.geometry;
      renderLayoutCanvas();
      toast(`已设为默认并发形态并写入 config/window_geometry.json`);
    } else {
      toast(d.error || '切换失败');
    }
  });

  $('#btn-auto-layout').addEventListener('click', async () => {
    const res = await fetch('/api/layout/auto', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' });
    const data = await res.json();
    if (data.ok) {
      layoutData = data.geometry;
      renderLayoutCanvas();
      toast('已按本机工作区重新生成 6 套单/多窗口无溢出预设');
    } else {
      toast(data.error || '计算失败');
    }
  });

  $('#btn-launch-calibrate')?.addEventListener('click', async () => {
    const meta = PRESET_META[layoutPresetKey] || PRESET_META.single;
    const res = await fetch('/api/layout/calibrate', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ concurrency: meta.concurrency, url: $('#cal-test-url')?.value.trim() || '' }),
    });
    const d = await res.json();
    toast(d.ok ? d.message : d.error || '启动标定失败');
  });

  $('#btn-demo-layouts')?.addEventListener('click', async () => {
    const res = await fetch('/api/layout/demo', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ hold_sec: 5 }),
    });
    const d = await res.json();
    toast(d.ok ? d.message : d.error || '启动巡演失败');
  });

  // ==================== 8. Config + Proxy/IMAP Self Tests ====================
  async function loadConfig() {
    try {
      const res = await fetch('/api/config');
      const cfg = await res.json();
      const b = cfg.browser || {};
      const d = cfg.defaults || {};
      const m = cfg.mail || {};
      $('#cfg-proxy').value = b.proxy || '';
      $('#cfg-cdp').value = b.cdp_endpoint || '';
      $('#cfg-slowmo').value = b.slow_mo ?? 50;
      $('#cfg-timeout').value = b.timeout ?? 30000;
      $('#cfg-country').value = d.country || 'US';
      $('#cfg-pwlen').value = d.password_length ?? 14;
      $('#cfg-headless').checked = Boolean(b.headless);
      $('#cfg-stealth').checked = b.enable_stealth !== false;
      $('#cfg-chrome').checked = b.use_chrome_channel !== false;
      $('#cfg-disposable').checked = d.use_disposable_mail !== false;
      $('#cfg-imap-server').value = m.imap_server || '';
      $('#cfg-imap-port').value = m.imap_port ?? 993;
      $('#cfg-imap-user').value = m.imap_user || '';
      $('#cfg-imap-pass').value = m.imap_password || '';
    } catch (e) {
      toast('读取配置失败: ' + e.message);
    }
  }

  $('#btn-save-config').addEventListener('click', async () => {
    const payload = {
      proxy: $('#cfg-proxy').value.trim(),
      cdp_endpoint: $('#cfg-cdp').value.trim(),
      slow_mo: Number($('#cfg-slowmo').value) || 0,
      timeout: Number($('#cfg-timeout').value) || 30000,
      country: ($('#cfg-country').value.trim() || 'US').toUpperCase(),
      password_length: Number($('#cfg-pwlen').value) || 14,
      headless: $('#cfg-headless').checked,
      enable_stealth: $('#cfg-stealth').checked,
      use_chrome_channel: $('#cfg-chrome').checked,
      use_disposable_mail: $('#cfg-disposable').checked,
      imap_server: $('#cfg-imap-server').value.trim(),
      imap_port: Number($('#cfg-imap-port').value) || 993,
      imap_user: $('#cfg-imap-user').value.trim(),
      imap_password: $('#cfg-imap-pass').value,
    };
    const res = await fetch('/api/config', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    const data = await res.json();
    if (data.ok) toast('运行配置与 IMAP 设置已原子保存至 config.json');
    else toast(data.error || '保存失败');
  });

  $('#btn-test-proxy').addEventListener('click', async () => {
    const btn = $('#btn-test-proxy');
    btn.disabled = true;
    try {
      const res = await fetch('/api/config/test-proxy', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ proxy: $('#cfg-proxy').value.trim() }),
      });
      const d = await res.json();
      const box = $('#proxy-test-out');
      box.classList.remove('hidden');
      box.textContent = JSON.stringify(d, null, 2);
      toast(d.ok ? `代理连通正常 (${d.latency_ms}ms)` : `代理测试失败: ${d.error || ''}`);
    } finally {
      btn.disabled = false;
    }
  });

  $('#btn-test-imap').addEventListener('click', async () => {
    const btn = $('#btn-test-imap');
    btn.disabled = true;
    try {
      const res = await fetch('/api/config/test-imap', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          imap_server: $('#cfg-imap-server').value.trim(),
          imap_port: Number($('#cfg-imap-port').value) || 993,
          imap_user: $('#cfg-imap-user').value.trim(),
          imap_password: $('#cfg-imap-pass').value,
        }),
      });
      const d = await res.json();
      const box = $('#imap-test-out');
      box.classList.remove('hidden');
      box.textContent = JSON.stringify(d, null, 2);
      toast(d.ok ? `IMAP 登录成功（收件箱 ${d.inbox_messages} 封 · ${d.latency_ms}ms）` : `IMAP 失败: ${d.error || ''}`);
    } finally {
      btn.disabled = false;
    }
  });

  // ==================== 9. LLM Configuration ====================
  let llmMaxTokensField = 'max_tokens';
  let discoveredModelsCache = [];

  function syncLlmMtfButtons() {
    $$('#llm-mtf-group .seg-btn').forEach((b) => {
      b.classList.toggle('active', b.dataset.mtf === llmMaxTokensField);
    });
  }

  $$('#llm-mtf-group .seg-btn').forEach((btn) => {
    btn.addEventListener('click', () => {
      llmMaxTokensField = btn.dataset.mtf || 'max_tokens';
      syncLlmMtfButtons();
    });
  });

  function isLocalLlmUrl(url) {
    const raw = String(url || '').trim();
    if (!raw) return false;
    try {
      const u = new URL(raw);
      return ['127.0.0.1', 'localhost', '::1', '[::1]'].includes((u.hostname || '').toLowerCase());
    } catch {
      return /^https?:\/\/(127\.0\.0\.1|localhost|\[::1\])(?::|\/|$)/i.test(raw);
    }
  }

  function updateAgentLlmPill(cfg) {
    const pill = $('#agent-llm-pill');
    if (!pill) return;
    if (cfg && cfg.has_key) {
      pill.className = 'badge emerald mono';
      pill.textContent = `LLM: ${cfg.model || 'ready'}`;
    } else if (cfg && isLocalLlmUrl(cfg.base_url)) {
      pill.className = 'badge emerald mono';
      pill.textContent = `本地模型就绪: ${cfg.model || 'local'}`;
    } else {
      pill.className = 'badge amber';
      pill.textContent = 'LLM: 未配置 Key · 点击配置';
    }
  }

  function renderDiscoveredModels() {
    const box = $('#llm-models-box');
    const chips = $('#llm-models-chips');
    const meta = $('#llm-models-meta');
    if (!box || !chips) return;
    if (!discoveredModelsCache.length) {
      box.classList.add('hidden');
      return;
    }
    box.classList.remove('hidden');
    const q = ($('#llm-models-filter')?.value || '').trim().toLowerCase();
    const currentModel = ($('#llm-model')?.value || '').trim();
    const filtered = q
      ? discoveredModelsCache.filter((m) => m.toLowerCase().includes(q))
      : discoveredModelsCache;
    const shown = filtered.slice(0, 80);
    if (meta) {
      meta.textContent =
        filtered.length > shown.length
          ? `共 ${discoveredModelsCache.length} 个模型（显示前 ${shown.length} 个，输入关键词可筛选）`
          : `已发现 ${filtered.length} / ${discoveredModelsCache.length} 个模型（点击填入）`;
    }
    if (!shown.length) {
      chips.innerHTML = `<span class="muted-small">未匹配到包含 "${escapeHtml(q)}" 的模型</span>`;
      return;
    }
    chips.innerHTML = shown
      .map(
        (m) =>
          `<button type="button" class="model-chip mono ${m === currentModel ? 'active' : ''}" data-pick-model="${escapeHtml(m)}">${escapeHtml(m)}</button>`
      )
      .join('');
    chips.querySelectorAll('[data-pick-model]').forEach((btn) => {
      btn.addEventListener('click', () => {
        const picked = btn.dataset.pickModel || '';
        if ($('#llm-model')) $('#llm-model').value = picked;
        renderDiscoveredModels();
        toast(`已选择模型: ${picked}`);
      });
    });
  }

  $('#llm-models-filter')?.addEventListener('input', renderDiscoveredModels);
  $('#llm-model')?.addEventListener('input', () => {
    if (discoveredModelsCache.length) renderDiscoveredModels();
  });

  async function loadLlmConfig(fillForm = true) {
    try {
      const res = await fetch('/api/llm/config');
      const cfg = await res.json();
      updateAgentLlmPill(cfg);
      if (!fillForm) return cfg;
      if ($('#llm-base-url')) $('#llm-base-url').value = cfg.base_url || 'https://api.openai.com/v1';
      if ($('#llm-model')) $('#llm-model').value = cfg.model || 'gpt-4o-mini';
      if ($('#llm-api-key')) $('#llm-api-key').value = '';
      if ($('#llm-proxy-url')) $('#llm-proxy-url').value = cfg.proxy_url || '';
      if ($('#llm-max-tokens')) $('#llm-max-tokens').value = cfg.max_tokens ?? 4096;
      if ($('#llm-temperature')) $('#llm-temperature').value = cfg.temperature ?? 0.2;
      llmMaxTokensField = cfg.max_tokens_field || 'max_tokens';
      syncLlmMtfButtons();
      const hint = $('#llm-key-hint');
      if (hint) {
        if (cfg.has_key) {
          hint.className = 'badge emerald mono';
          hint.textContent = `已保存: ${cfg.api_key_hint || '***'}`;
        } else if (isLocalLlmUrl(cfg.base_url)) {
          hint.className = 'badge sky';
          hint.textContent = '本地免 Key';
        } else {
          hint.className = 'badge amber';
          hint.textContent = '未配置 Key';
        }
      }
      return cfg;
    } catch (e) {
      if (fillForm) toast('读取 LLM 配置失败: ' + e.message);
      return null;
    }
  }

  function collectLlmFormPayload() {
    const rawTemp = ($('#llm-temperature')?.value ?? '').trim();
    const parsedTemp = rawTemp === '' ? 0.2 : Number(rawTemp);
    const rawMaxTok = ($('#llm-max-tokens')?.value ?? '').trim();
    const parsedMaxTok = rawMaxTok === '' ? 4096 : Number(rawMaxTok);
    return {
      base_url: $('#llm-base-url')?.value.trim() || 'https://api.openai.com/v1',
      model: $('#llm-model')?.value.trim() || 'gpt-4o-mini',
      api_key: $('#llm-api-key')?.value.trim() || '',
      proxy_url: $('#llm-proxy-url')?.value.trim() || '',
      max_tokens: Number.isFinite(parsedMaxTok) && parsedMaxTok > 0 ? parsedMaxTok : 4096,
      max_tokens_field: llmMaxTokensField,
      temperature: Number.isFinite(parsedTemp) ? parsedTemp : 0.2,
    };
  }

  $('#btn-llm-models')?.addEventListener('click', async () => {
    const btn = $('#btn-llm-models');
    const banner = $('#llm-test-banner');
    btn.disabled = true;
    btn.textContent = '获取模型中…';
    try {
      const res = await fetch('/api/llm/models', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(collectLlmFormPayload()),
      });
      const d = await res.json();
      if (d.ok && Array.isArray(d.models) && d.models.length) {
        discoveredModelsCache = d.models;
        if ($('#llm-models-filter')) $('#llm-models-filter').value = '';
        renderDiscoveredModels();
        if (banner) {
          banner.className = 'banner ok';
          banner.textContent = `已从 ${d.source_url || '/models'} 获取 ${d.count} 个模型，点击下方标签可直接填入`;
          banner.classList.remove('hidden');
        }
        toast(`已获取 ${d.count} 个可用模型`);
      } else {
        discoveredModelsCache = [];
        renderDiscoveredModels();
        if (banner) {
          banner.className = 'banner fail';
          banner.textContent = `获取模型失败: ${d.error || '未返回可用模型'}`;
          banner.classList.remove('hidden');
        }
        toast(d.error || '获取模型列表失败');
      }
    } catch (e) {
      toast('获取模型异常: ' + e.message);
    } finally {
      btn.disabled = false;
      btn.textContent = '获取模型';
    }
  });

  $('#btn-llm-save')?.addEventListener('click', async () => {
    const btn = $('#btn-llm-save');
    btn.disabled = true;
    try {
      const res = await fetch('/api/llm/config', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(collectLlmFormPayload()),
      });
      const d = await res.json();
      if (!d.ok) return toast(d.error || '保存 LLM 配置失败');
      await loadLlmConfig(true);
      toast('LLM 配置已原子保存至 results/llm_config.json');
    } catch (e) {
      toast('保存异常: ' + e.message);
    } finally {
      btn.disabled = false;
    }
  });

  $('#btn-llm-test')?.addEventListener('click', async () => {
    const btn = $('#btn-llm-test');
    const banner = $('#llm-test-banner');
    const out = $('#llm-test-out');
    btn.disabled = true;
    btn.textContent = '连通性测试中…';
    banner.className = 'banner muted';
    banner.textContent = '正在发送最小 1 轮探针请求（Reply with OK）…';
    banner.classList.remove('hidden');
    try {
      const res = await fetch('/api/llm/test', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(collectLlmFormPayload()),
      });
      const d = await res.json();
      banner.className = `banner ${d.ok ? 'ok' : 'fail'}`;
      banner.textContent = d.ok
        ? `连通成功 · 模型 ${d.model} · 延迟 ${d.latency_ms}ms · 回复: ${d.reply || 'OK'}`
        : `连通失败 (${d.latency_ms ?? 0}ms): ${d.error || '未知错误'}`;
      out.classList.remove('hidden');
      out.textContent = JSON.stringify(d, null, 2);
      toast(d.ok ? `LLM 连通正常 (${d.latency_ms}ms)` : `LLM 测试失败: ${d.error || ''}`);
    } catch (e) {
      banner.className = 'banner fail';
      banner.textContent = '测试请求异常: ' + e.message;
    } finally {
      btn.disabled = false;
      btn.textContent = '一键测试连通';
    }
  });

  // ==================== 10. AI Direct-Run Workbench ====================
  let agentSessionsCache = [];
  let activeAgentSessionId = '';
  let editingAgentSessionId = '';
  let agentEventSource = null;
  let agentRunning = false;
  let agentEvents = [];
  let agentPlan = [];
  let agentUsage = { prompt_tokens: 0, completion_tokens: 0, total_tokens: 0 };

  function syncAgentExportButton() {
    const expBtn = $('#btn-agent-export');
    if (expBtn) expBtn.disabled = !activeAgentSessionId;
  }

  function setAgentRunningState(running, statusLabel = '') {
    agentRunning = Boolean(running);
    const badge = $('#nav-badge-agent');
    if (badge) badge.classList.toggle('hidden', !agentRunning);
    if ($('#btn-agent-stop')) $('#btn-agent-stop').disabled = !agentRunning;
    if ($('#btn-agent-quick-run')) $('#btn-agent-quick-run').disabled = agentRunning;

    const sendBtn = $('#btn-agent-send');
    if (sendBtn) {
      sendBtn.disabled = false;
      sendBtn.textContent = agentRunning ? '发送纠偏' : '发送';
    }
    const followInp = $('#agent-followup-input');
    if (followInp) {
      followInp.placeholder = agentRunning
        ? '运行中可输入实时纠偏指令（Enter 注入，当前工具完成后生效）…'
        : '输入追问或下一步指令（Enter 发送，如：修改选择器后再试跑 1 次）…';
    }

    const st = statusLabel || (agentRunning ? 'running' : 'idle');
    const dot = $('#agent-status-dot');
    const stBadge = $('#agent-status-badge');
    if (dot) {
      dot.className = `dot ${st === 'running' ? 'blue' : st === 'done' ? 'emerald' : st === 'error' ? 'rose' : st === 'stopped' ? 'amber' : 'neutral'}`;
    }
    if (stBadge) {
      stBadge.className = `badge ${st === 'running' ? 'sky' : st === 'done' ? 'emerald' : st === 'error' ? 'rose' : st === 'stopped' ? 'amber' : 'neutral'}`;
      stBadge.textContent = st;
    }
    syncAgentExportButton();
  }

  function renderAgentPlan(steps) {
    agentPlan = Array.isArray(steps) ? steps : [];
    const box = $('#agent-plan-list');
    const prog = $('#agent-plan-progress');
    if (!box) return;
    const doneCnt = agentPlan.filter((s) => s.status === 'done').length;
    if (prog) prog.textContent = `${doneCnt}/${agentPlan.length}`;
    if (!agentPlan.length) {
      box.innerHTML = `<div class="muted-small">等待 AI 规划执行步骤…</div>`;
      return;
    }
    const icoMap = { pending: '○', in_progress: '◐', done: '✓', failed: '✕' };
    box.innerHTML = agentPlan
      .map(
        (s) => `
        <div class="plan-step ${escapeHtml(s.status || 'pending')}">
          <span class="plan-ico">${icoMap[s.status] || '○'}</span>
          <span class="plan-title">${escapeHtml(s.title)}</span>
        </div>`
      )
      .join('');
  }

  function renderAgentUsage(usage) {
    agentUsage = {
      prompt_tokens: Number(usage?.prompt_tokens || 0),
      completion_tokens: Number(usage?.completion_tokens || 0),
      total_tokens: Number(usage?.total_tokens || 0),
    };
    if ($('#agent-tok-prompt')) $('#agent-tok-prompt').textContent = String(agentUsage.prompt_tokens);
    if ($('#agent-tok-completion')) $('#agent-tok-completion').textContent = String(agentUsage.completion_tokens);
    if ($('#agent-tok-total')) $('#agent-tok-total').textContent = String(agentUsage.total_tokens);
  }

  function startAgentSessionRename(sid) {
    if (!sid) return;
    editingAgentSessionId = sid;
    renderAgentSessions();
    const inp = $(`#agent-session-list [data-ag-rename-input="${CSS.escape(sid)}"]`);
    if (inp) {
      inp.scrollIntoView({ block: 'nearest' });
      inp.focus();
      inp.select();
    }
  }

  async function commitAgentSessionRename(sid, rawTitle) {
    const clean = String(rawTitle || '').replace(/\s+/g, ' ').trim().slice(0, 80);
    const existing = agentSessionsCache.find((x) => x.id === sid);
    editingAgentSessionId = '';
    if (!clean) {
      toast('会话标题不能为空');
      renderAgentSessions();
      return;
    }
    if (existing && existing.title === clean) {
      renderAgentSessions();
      return;
    }
    try {
      const res = await fetch(`/api/agent/sessions/${encodeURIComponent(sid)}`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ title: clean }),
      });
      const d = await res.json();
      if (!res.ok || !d.ok) {
        toast(d.error || '重命名失败');
        renderAgentSessions();
        return;
      }
      if (d.session) {
        const idx = agentSessionsCache.findIndex((x) => x.id === sid);
        if (idx >= 0) agentSessionsCache[idx] = { ...agentSessionsCache[idx], ...d.session };
        if (activeAgentSessionId === sid) {
          $('#agent-chat-title').textContent = d.session.title || sid;
        }
      }
      renderAgentSessions();
      toast('会话已重命名');
    } catch (e) {
      toast('重命名异常: ' + e.message);
      renderAgentSessions();
    }
  }

  function renderAgentSessions() {
    const box = $('#agent-session-list');
    if (!box) return;
    if (!agentSessionsCache.length) {
      box.innerHTML = `<div class="muted-small" style="padding:12px;text-align:center">暂无历史会话</div>`;
      return;
    }
    const q = ($('#agent-session-search')?.value || '').trim().toLowerCase();
    const filtered = q
      ? agentSessionsCache.filter((s) => `${s.title || ''} ${s.target_url || ''} ${s.id || ''}`.toLowerCase().includes(q))
      : agentSessionsCache;
    if (!filtered.length) {
      box.innerHTML = `<div class="muted-small" style="padding:12px;text-align:center">未匹配到包含 “${escapeHtml(q)}” 的会话</div>`;
      return;
    }
    box.innerHTML = filtered
      .map((s) => {
        const activeCls = s.id === activeAgentSessionId ? 'active' : '';
        const pillCls = s.status === 'done' ? 'pass' : s.status === 'running' ? 'warn' : s.status === 'error' ? 'fail' : 'neutral';
        const isEditing = editingAgentSessionId === s.id;
        const titleHtml = isEditing
          ? `<input type="text" class="agent-rename-input" maxlength="80" value="${escapeHtml(s.title)}" data-ag-rename-input="${escapeHtml(s.id)}" />`
          : `<div class="agent-session-title" data-ag-title="${escapeHtml(s.id)}" title="${escapeHtml(s.title)}（双击重命名）">${escapeHtml(s.title)}</div>`;
        return `
          <div class="agent-session-row ${activeCls}" data-ag-select="${escapeHtml(s.id)}">
            <div class="agent-session-main">
              ${titleHtml}
              <div class="agent-session-meta">
                <span class="pill ${pillCls}" style="padding:0 6px;font-size:10px">${escapeHtml(s.status)}</span>
                <span class="mono">${escapeHtml((s.updated_at || s.created_at || '').slice(5, 16))}</span>
              </div>
            </div>
            <div class="agent-session-actions">
              <button type="button" class="agent-session-btn" title="重命名会话" data-ag-rename="${escapeHtml(s.id)}"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 20h9"/><path d="M16.5 3.5a2.12 2.12 0 1 1 3 3L7 19l-4 1 1-4Z"/></svg></button>
              <button type="button" class="agent-session-del" title="删除会话" data-ag-del="${escapeHtml(s.id)}">×</button>
            </div>
          </div>`;
      })
      .join('');

    box.querySelectorAll('[data-ag-select]').forEach((row) => {
      row.addEventListener('click', (ev) => {
        if (ev.target.closest('[data-ag-del], [data-ag-rename], [data-ag-rename-input]')) return;
        selectAgentSession(row.dataset.agSelect);
      });
    });
    box.querySelectorAll('[data-ag-title]').forEach((el) => {
      el.addEventListener('dblclick', (ev) => {
        ev.stopPropagation();
        startAgentSessionRename(el.dataset.agTitle);
      });
    });
    box.querySelectorAll('[data-ag-rename]').forEach((btn) => {
      btn.addEventListener('click', (ev) => {
        ev.stopPropagation();
        startAgentSessionRename(btn.dataset.agRename);
      });
    });
    box.querySelectorAll('[data-ag-rename-input]').forEach((inp) => {
      let settled = false;
      const sid = inp.dataset.agRenameInput;
      inp.addEventListener('click', (ev) => ev.stopPropagation());
      inp.addEventListener('keydown', (ev) => {
        if (ev.key === 'Escape') {
          ev.preventDefault();
          settled = true;
          editingAgentSessionId = '';
          renderAgentSessions();
        } else if (ev.key === 'Enter' && !ev.isComposing && ev.keyCode !== 229) {
          ev.preventDefault();
          settled = true;
          commitAgentSessionRename(sid, inp.value);
        }
      });
      inp.addEventListener('blur', () => {
        if (settled) return;
        settled = true;
        commitAgentSessionRename(sid, inp.value);
      });
    });
    box.querySelectorAll('[data-ag-del]').forEach((btn) => {
      btn.addEventListener('click', async (ev) => {
        ev.stopPropagation();
        const sid = btn.dataset.agDel;
        if (!window.confirm('确认删除该 AI 会话记录？')) return;
        if (editingAgentSessionId === sid) editingAgentSessionId = '';
        await fetch(`/api/agent/sessions/${encodeURIComponent(sid)}`, { method: 'DELETE' });
        if (activeAgentSessionId === sid) {
          closeAgentStream();
          activeAgentSessionId = '';
          agentEvents = [];
          renderAgentPlan([]);
          renderAgentUsage({});
          $('#agent-chat-title').textContent = '未选择会话';
          $('#agent-chat-url').textContent = '';
          setAgentRunningState(false, 'idle');
          renderAgentFeed();
        }
        toast('已删除会话');
        await loadAgentSessions(false);
      });
    });
  }

  $('#agent-session-search')?.addEventListener('input', renderAgentSessions);
  $('#agent-chat-title')?.addEventListener('dblclick', () => {
    if (!activeAgentSessionId) return;
    const searchEl = $('#agent-session-search');
    const rowVisible = $(`#agent-session-list [data-ag-select="${CSS.escape(activeAgentSessionId)}"]`);
    if (!rowVisible && searchEl && searchEl.value) {
      searchEl.value = '';
    }
    startAgentSessionRename(activeAgentSessionId);
  });

  function formatToolPreview(args) {
    if (!args || typeof args !== 'object') return '';
    const keys = Object.keys(args);
    if (!keys.length) return '';
    const firstVal = args[keys[0]];
    const preview = typeof firstVal === 'string' ? firstVal : JSON.stringify(firstVal);
    return `${keys[0]}=${String(preview || '').slice(0, 60)}`;
  }

  function renderAgentFeed() {
    const feed = $('#agent-chat-feed');
    if (!feed) return;
    // Preserve open state of user-expanded <details> tool cards
    const openKeys = new Set(
      Array.from(feed.querySelectorAll('details.tool-card[open]')).map((el) => el.dataset.callKey)
    );

    if (!agentEvents.length) {
      feed.innerHTML = `<div class="empty-box">在上方填写目标 URL 并点击「一键 AI 直跑」，或在下方直接向注册工程师 AI 提问。</div>`;
      return;
    }

    // Group tool_start + tool_end pairs by call_id while preserving timeline order
    const items = [];
    const toolByCallId = new Map();
    for (const ev of agentEvents) {
      if (ev.type === 'user_msg' || ev.type === 'assistant_msg') {
        items.push(ev);
      } else if (ev.type === 'tool_start') {
        const entry = {
          type: 'tool',
          call_id: ev.call_id || `t_${items.length}`,
          name: ev.name || 'tool',
          args: ev.args || {},
          ts: ev.ts || '',
          done: false,
          ok: true,
          duration_ms: null,
          result: null,
        };
        toolByCallId.set(entry.call_id, entry);
        items.push(entry);
      } else if (ev.type === 'tool_end') {
        const existing = toolByCallId.get(ev.call_id);
        if (existing) {
          existing.done = true;
          existing.ok = ev.ok !== false;
          existing.duration_ms = ev.duration_ms;
          existing.result = ev.result;
        } else {
          items.push({
            type: 'tool',
            call_id: ev.call_id || `t_${items.length}`,
            name: ev.name || 'tool',
            args: {},
            ts: ev.ts || '',
            done: true,
            ok: ev.ok !== false,
            duration_ms: ev.duration_ms,
            result: ev.result,
          });
        }
      } else if (ev.type === 'status' && ev.status === 'error' && ev.error) {
        items.push({ type: 'error_msg', ts: ev.ts || '', error: ev.error });
      } else if (ev.type === 'status' && ev.status === 'stopped' && ev.error) {
        items.push({ type: 'stopped_msg', ts: ev.ts || '', error: ev.error });
      }
    }

    if (!items.length) {
      feed.innerHTML = `<div class="empty-box">会话已就绪，等待执行事件…</div>`;
      return;
    }

    feed.innerHTML = items
      .map((it, idx) => {
        if (it.type === 'user_msg') {
          return `
            <div class="chat-msg user">
              <div class="chat-meta"><span>你</span>${it.steered ? '<span class="badge amber">实时纠偏</span>' : ''}<span class="mono">${escapeHtml(it.ts)}</span></div>
              <div class="chat-bubble">${escapeHtml(it.content)}${it.target_url ? `\n\nURL: ${escapeHtml(it.target_url)}` : ''}</div>
            </div>`;
        }
        if (it.type === 'assistant_msg') {
          return `
            <div class="chat-msg assistant">
              <div class="chat-meta"><span class="badge sky">AI Agent</span>${it.step ? `<span class="mono">Step ${it.step}</span>` : ''}${it.wrap_up ? '<span class="badge neutral">收尾总结</span>' : ''}<span class="mono">${escapeHtml(it.ts)}</span></div>
              <div class="chat-bubble">${escapeHtml(it.content)}</div>
            </div>`;
        }
        if (it.type === 'error_msg') {
          return `
            <div class="chat-msg error">
              <div class="chat-meta"><span class="badge rose">运行异常</span><span class="mono">${escapeHtml(it.ts)}</span></div>
              <div class="chat-bubble">${escapeHtml(it.error)}</div>
            </div>`;
        }
        if (it.type === 'stopped_msg') {
          return `
            <div class="chat-msg stopped">
              <div class="chat-meta"><span class="badge amber">已手动停止</span><span class="mono">${escapeHtml(it.ts)}</span></div>
              <div class="chat-bubble">${escapeHtml(it.error)}</div>
            </div>`;
        }
        if (it.type === 'tool') {
          const key = `${it.call_id}_${idx}`;
          const isOpen = openKeys.has(key) ? ' open' : '';
          const statePill = !it.done
            ? (agentRunning ? `<span class="badge sky">执行中…</span>` : `<span class="badge amber">已中止</span>`)
            : it.ok
              ? `<span class="badge emerald">完成</span>`
              : `<span class="badge rose">失败</span>`;
          const durTxt = it.duration_ms != null ? `<span class="mono muted-small">${it.duration_ms}ms</span>` : '';
          const argsJson = JSON.stringify(it.args || {}, null, 2);
          const resJson = it.done ? JSON.stringify(it.result ?? {}, null, 2) : (agentRunning ? '等待工具返回…' : '工具执行已被中止');
          return `
            <details class="tool-card" data-call-key="${escapeHtml(key)}"${isOpen}>
              <summary>
                <span class="tool-sum-left">
                  ${statePill}
                  <span class="tool-name mono">${escapeHtml(it.name)}</span>
                  <span class="tool-preview mono">${escapeHtml(formatToolPreview(it.args))}</span>
                </span>
                <span class="tool-sum-right">
                  ${durTxt}
                  <span class="mono muted-small">${escapeHtml(it.ts)}</span>
                </span>
              </summary>
              <div class="tool-body">
                <div class="tool-sec-label">Arguments</div>
                <pre class="tool-pre mono">${escapeHtml(argsJson)}</pre>
                <div class="tool-sec-label">Output</div>
                <pre class="tool-pre mono">${escapeHtml(resJson)}</pre>
              </div>
            </details>`;
        }
        return '';
      })
      .join('');

    feed.scrollTop = feed.scrollHeight;
  }

  let agentStreamSeq = 0;
  let agentReconnectTimer = null;
  let agentReconnectAttempts = 0;

  function closeAgentStream() {
    if (agentReconnectTimer) {
      clearTimeout(agentReconnectTimer);
      agentReconnectTimer = null;
    }
    if (agentEventSource) {
      agentEventSource.close();
      agentEventSource = null;
    }
  }

  function handleAgentEvent(ev, isLive) {
    if (typeof ev.seq === 'number' && ev.seq + 1 > agentStreamSeq) {
      agentStreamSeq = ev.seq + 1;
    }
    if (!ev.eof) {
      agentEvents.push(ev);
    }
    if (ev.type === 'plan') {
      renderAgentPlan(ev.steps || []);
    } else if (ev.type === 'usage') {
      renderAgentUsage(ev.session_usage || ev);
    } else if (ev.type === 'status') {
      const st = ev.status || 'idle';
      setAgentRunningState(st === 'running', st);
      if (isLive && !ev.replay && (st === 'done' || st === 'error' || st === 'stopped')) {
        agentReconnectAttempts = 0;
        closeAgentStream();
        loadAgentSessions(false);
        loadAdapters();
        loadOverview();
        if (st === 'done') toast('AI 直跑本轮已完成');
        else if (st === 'stopped') toast(ev.error || 'AI 直跑已停止');
        else if (st === 'error') toast(`AI 直跑报错: ${ev.error || ''}`);
      }
    }
    renderAgentFeed();
  }

  function attachAgentStream(sessionId, sinceSeq = 0) {
    closeAgentStream();
    if (!sinceSeq) {
      agentEvents = [];
      agentStreamSeq = 0;
      agentReconnectAttempts = 0;
    }
    setAgentRunningState(true, 'running');
    const qs = sinceSeq > 0 ? `?since=${encodeURIComponent(String(sinceSeq))}` : '';
    const es = new EventSource(`/api/agent/sessions/${encodeURIComponent(sessionId)}/stream${qs}`);
    agentEventSource = es;
    es.onopen = () => {
      agentReconnectAttempts = 0;
    };
    es.onmessage = (raw) => {
      try {
        const ev = JSON.parse(raw.data);
        if (typeof ev.seq === 'number' && ev.seq < agentStreamSeq) {
          return;
        }
        handleAgentEvent(ev, true);
      } catch {}
    };
    es.onerror = () => {
      if (agentEventSource !== es) return;
      es.close();
      agentEventSource = null;
      if (activeAgentSessionId === sessionId && agentReconnectAttempts < 4) {
        const attempt = ++agentReconnectAttempts;
        const delayMs = Math.min(5000, 800 * Math.pow(2, attempt - 1));
        agentReconnectTimer = setTimeout(async () => {
          agentReconnectTimer = null;
          if (activeAgentSessionId !== sessionId) return;
          try {
            const res = await fetch(`/api/agent/sessions/${encodeURIComponent(sessionId)}`);
            const d = await res.json();
            if (d?.ok && d.session?.status === 'running') {
              attachAgentStream(sessionId, agentStreamSeq);
              return;
            }
            if (d?.ok && d.session) {
              agentEvents = d.session.events || agentEvents;
              setAgentRunningState(false, d.session.status || 'idle');
              renderAgentFeed();
            }
          } catch {}
          setAgentRunningState(false);
          loadAgentSessions(false);
        }, delayMs);
        return;
      }
      setAgentRunningState(false);
      loadAgentSessions(false);
    };
  }

  async function selectAgentSession(sessionId) {
    closeAgentStream();
    activeAgentSessionId = sessionId;
    syncAgentExportButton();
    renderAgentSessions();
    try {
      const res = await fetch(`/api/agent/sessions/${encodeURIComponent(sessionId)}`);
      const d = await res.json();
      if (!d.ok || !d.session) return toast(d.error || '加载会话失败');
      const s = d.session;
      $('#agent-chat-title').textContent = s.title || s.id;
      $('#agent-chat-url').textContent = s.target_url || '';
      renderAgentPlan(s.plan || []);
      renderAgentUsage(s.usage || {});
      agentEvents = s.events || [];
      setAgentRunningState(s.status === 'running', s.status || 'idle');
      renderAgentFeed();
      if (s.status === 'running') {
        attachAgentStream(s.id);
      }
    } catch (e) {
      toast('读取会话异常: ' + e.message);
    }
  }

  async function loadAgentSessions(autoReconnect = false) {
    try {
      const res = await fetch('/api/agent/sessions');
      const d = await res.json();
      agentSessionsCache = d.sessions || [];
      const activeId = d.active_session_id || '';
      if ($('#nav-badge-agent')) {
        $('#nav-badge-agent').classList.toggle('hidden', !activeId);
      }
      if (!editingAgentSessionId || !document.activeElement?.matches('.agent-rename-input')) {
        renderAgentSessions();
      }
      if (autoReconnect && activeId) {
        await selectAgentSession(activeId);
      } else if (!activeAgentSessionId && agentSessionsCache.length && currentView === 'agent') {
        await selectAgentSession(agentSessionsCache[0].id);
      }
      syncAgentExportButton();
    } catch {}
  }

  async function createNewAgentSession(title = '', targetUrl = '') {
    const res = await fetch('/api/agent/sessions', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ title, target_url: targetUrl }),
    });
    const d = await res.json();
    if (!d.ok || !d.session) throw new Error(d.error || '创建会话失败');
    activeAgentSessionId = d.session.id;
    syncAgentExportButton();
    $('#agent-chat-title').textContent = d.session.title;
    $('#agent-chat-url').textContent = d.session.target_url || '';
    agentEvents = [];
    renderAgentPlan([]);
    renderAgentUsage({});
    renderAgentFeed();
    await loadAgentSessions(false);
    return d.session;
  }

  async function dispatchAgentTurn(sessionId, message, targetUrl = '', isSteer = false) {
    if (!isSteer) setAgentRunningState(true, 'running');
    try {
      const res = await fetch(`/api/agent/sessions/${encodeURIComponent(sessionId)}/message`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ message, target_url: targetUrl }),
      });
      const d = await res.json();
      if (!d.ok) {
        if (!isSteer) setAgentRunningState(false, 'idle');
        return toast(d.error || '发送指令失败');
      }
      if (d.session) {
        $('#agent-chat-title').textContent = d.session.title || sessionId;
        $('#agent-chat-url').textContent = d.session.target_url || '';
      }
      if (d.steered) {
        if (!agentEventSource) attachAgentStream(sessionId);
        toast('已注入实时纠偏，将在当前步骤完成后生效');
        return;
      }
      attachAgentStream(sessionId);
      loadAgentSessions(false);
    } catch (e) {
      if (!isSteer) setAgentRunningState(false, 'error');
      toast('AI 直跑启动异常: ' + e.message);
    }
  }

  $('#btn-agent-new-session')?.addEventListener('click', async () => {
    try {
      closeAgentStream();
      await createNewAgentSession('', $('#agent-target-url')?.value.trim() || '');
      setAgentRunningState(false, 'idle');
      toast('已创建新 AI 会话');
    } catch (e) {
      toast(e.message);
    }
  });

  $('#btn-agent-export')?.addEventListener('click', async () => {
    if (!activeAgentSessionId) return toast('请先选择要导出的会话');
    const btn = $('#btn-agent-export');
    btn.disabled = true;
    try {
      const res = await fetch(`/api/agent/sessions/${encodeURIComponent(activeAgentSessionId)}/export`);
      const d = await res.json();
      if (!d.ok) return toast(d.error || '导出会话失败');
      const blob = new Blob([d.markdown || ''], { type: 'text/markdown;charset=utf-8' });
      const a = document.createElement('a');
      a.href = URL.createObjectURL(blob);
      a.download = d.filename || `${activeAgentSessionId}.md`;
      a.click();
      URL.revokeObjectURL(a.href);
      toast(`已导出 ${a.download}`);
    } catch (e) {
      toast('导出异常: ' + e.message);
    } finally {
      syncAgentExportButton();
    }
  });

  $$('[data-agent-chip]').forEach((chip) => {
    chip.addEventListener('click', () => {
      const inp = $('#agent-instruction');
      if (inp) {
        inp.value = chip.dataset.agentChip || '';
        inp.focus();
      }
      toast('已填入快捷指令，点击「一键 AI 直跑」开始');
    });
  });

  $('#btn-agent-quick-run')?.addEventListener('click', async () => {
    const url = $('#agent-target-url')?.value.trim() || '';
    let instr = $('#agent-instruction')?.value.trim() || '';
    if (!instr && url) {
      instr = '探测该目标注册页的表单与风控特征，生成符合 BaseSiteAdapter 规范的适配器代码并单线程试跑 1 个号验证结果';
    }
    if (!instr) return toast('请输入目标 URL 或一句话指令');
    try {
      const sess = await createNewAgentSession('', url);
      await dispatchAgentTurn(sess.id, instr, url, false);
    } catch (e) {
      toast(e.message || '启动失败');
    }
  });

  function handleAgentLaunchEnter(e) {
    if (e.key === 'Enter' && !e.shiftKey && !e.isComposing && e.keyCode !== 229) {
      e.preventDefault();
      const btn = $('#btn-agent-quick-run');
      if (btn && !btn.disabled) btn.click();
    }
  }
  $('#agent-target-url')?.addEventListener('keydown', handleAgentLaunchEnter);
  $('#agent-instruction')?.addEventListener('keydown', handleAgentLaunchEnter);

  async function sendAgentFollowup() {
    const inp = $('#agent-followup-input');
    const text = inp?.value.trim() || '';
    if (!text) return;
    inp.value = '';
    const sendBtn = $('#btn-agent-send');
    if (sendBtn) sendBtn.disabled = true;
    try {
      let sid = activeAgentSessionId;
      const isSteer = agentRunning && Boolean(sid);
      if (!sid) {
        if (agentRunning) {
          return toast('当前已有运行中的会话，请先选中该会话再发送纠偏');
        }
        const sess = await createNewAgentSession('', $('#agent-target-url')?.value.trim() || '');
        sid = sess.id;
      }
      await dispatchAgentTurn(sid, text, '', isSteer);
    } catch (e) {
      toast(e.message || '发送失败');
    } finally {
      if (sendBtn) sendBtn.disabled = false;
    }
  }

  $('#btn-agent-send')?.addEventListener('click', sendAgentFollowup);
  $('#agent-followup-input')?.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey && !e.isComposing && e.keyCode !== 229) {
      e.preventDefault();
      sendAgentFollowup();
    }
  });

  $('#btn-agent-stop')?.addEventListener('click', async () => {
    if (!activeAgentSessionId) return;
    const sid = activeAgentSessionId;
    try {
      const res = await fetch(`/api/agent/sessions/${encodeURIComponent(sid)}/stop`, { method: 'POST' });
      const d = await res.json();
      setAgentRunningState(false, d.status || 'stopped');
      closeAgentStream();
      toast(d.error || '已发送 AI 直跑停止指令');
      await selectAgentSession(sid);
      loadAgentSessions(false);
    } catch (e) {
      toast('停止异常: ' + e.message);
    }
  });

  // Initial boot
  loadOverview();
  loadDoctor();
  loadAdapters();
  loadJobs(true);
  loadLlmConfig(false);
  loadAgentSessions(true);
})();
