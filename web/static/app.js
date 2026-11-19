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
    runner: '单线程适配器试跑',
    accounts: '账号与 API Key 台账',
    tools: '开源实用工具箱',
    snapshots: '失败原因与现场快照',
    scaffold: '站点工坊 (嗅探 / 源码 / 脚手架)',
    doctor: '环境真执行诊断',
    layout: '视窗标定台 (单 / 多窗口)',
    config: '运行配置 (config.json / IMAP)',
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
    $$('.nav-item').forEach((b) => b.classList.toggle('active', b.dataset.view === target));
    $$('.view').forEach((v) => v.classList.toggle('active', v.id === `view-${target}`));
    if (target === 'overview') loadOverview();
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

  $('#btn-global-refresh').addEventListener('click', async () => {
    const ico = $('#icon-refresh');
    ico.classList.add('spin');
    try {
      if (currentView === 'overview') await Promise.all([loadOverview(), loadDoctor()]);
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
        await fetch('/api/accounts/delete', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ email: r.email, api_key: r.api_key || '' }),
        });
        toast(`已移除 ${r.email}`);
        loadAccounts();
        loadOverview();
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

  const PRESET_META = {
    single: { label: '1窗 · 全屏', concurrency: 1, getBlock: (g) => g?.single_concurrency },
    dual: { label: '2窗 · 左右分屏', concurrency: 2, getBlock: (g) => g?.dual_concurrency },
    quad_edge: { label: '4窗 · 贴边四宫格', concurrency: 4, applyOpts: { quad_mode: 'edge_2x2' }, getBlock: (g) => g?.quad_presets?.edge_2x2 },
    quad_compact: { label: '4窗 · 居中紧凑', concurrency: 4, applyOpts: { quad_mode: 'centered_compact' }, getBlock: (g) => g?.quad_presets?.centered_compact },
    hex_edge: { label: '6窗 · 贴边六宫格', concurrency: 6, applyOpts: { hex_mode: 'edge_3x2' }, getBlock: (g) => g?.hex_presets?.edge_3x2 },
    hex_wing: { label: '6窗 · 八字展翼', concurrency: 6, applyOpts: { hex_mode: 'wing_cascade' }, getBlock: (g) => g?.hex_presets?.wing_cascade },
  };

  function renderLayoutCanvas() {
    const g = layoutData || {};
    const meta = PRESET_META[layoutPresetKey] || PRESET_META.single;
    const block = meta.getBlock(g);
    const slots = block?.slots || [];
    const activeQuad = String(g.quad_mode || 'centered_compact');
    const activeHex = String(g.hex_mode || 'wing_cascade');

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
      canvas.innerHTML = `<div class="empty-box" style="height:100%;display:flex;align-items:center;justify-content:center">尚未生成视窗配置，点击右上角「一键重算 6 套布局」。</div>`;
    } else {
      canvas.innerHTML = slots
        .map((s, i) => {
          const leftPct = Math.max(0, Math.min(96, ((s.x - waX) / waW) * 100));
          const topPct = Math.max(0, Math.min(96, ((s.y - waY) / waH) * 100));
          const wPct = Math.max(8, Math.min(100 - leftPct, (s.width / waW) * 100));
          const hPct = Math.max(12, Math.min(100 - topPct, (s.height / waH) * 100));
          const zPct = Math.round((s.zoom || 1) * 100);
          return `
            <div class="slot-rect c${i % 6}" style="left:${leftPct}%;top:${topPct}%;width:${wPct}%;height:${hPct}%;z-index:${10 + i}">
              <div class="slot-head"><span>${escapeHtml(s.name || `Slot #${i + 1}`)}</span><span class="slot-zoom mono">${zPct}%</span></div>
              <div class="slot-dim mono">${s.width}×${s.height} @ (${s.x},${s.y})</div>
            </div>`;
        })
        .join('');
    }

    const curZoomPct = slots.length ? Math.round((slots[0].zoom || 1) * 100) : 100;
    $('#layout-zoom-range').value = String(Math.max(45, Math.min(125, curZoomPct)));
    $('#layout-zoom-val').textContent = `${curZoomPct}%`;

    $('#layout-slot-list').innerHTML = slots
      .map(
        (s) => `
        <div class="layout-slot-row">
          <span>${escapeHtml(s.name)}</span>
          <span class="mono muted-small">${s.width}×${s.height} · ${Math.round((s.zoom || 1) * 100)}%</span>
        </div>`
      )
      .join('');

    const defBtn = $('#btn-set-default-mode');
    if (meta.applyOpts) {
      defBtn.classList.remove('hidden');
      defBtn.textContent = `将「${meta.label}」设为默认形态`;
    } else {
      defBtn.classList.add('hidden');
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
      renderLayoutCanvas();
    });
  });

  $('#layout-zoom-range')?.addEventListener('input', (e) => {
    $('#layout-zoom-val').textContent = `${e.target.value}%`;
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

  // Initial boot
  loadOverview();
  loadDoctor();
  loadAdapters();
  loadJobs(true);
})();
