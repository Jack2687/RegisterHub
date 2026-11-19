(() => {
  let currentView = 'doctor';
  let selectedAdapter = '';
  let adaptersCache = [];
  let activeJobId = '';
  let logStream = null;
  let logEntries = [];
  let logFilter = 'all';
  let revealSecrets = false;
  let accountsCache = [];

  const $ = (sel) => document.querySelector(sel);
  const $$ = (sel) => Array.from(document.querySelectorAll(sel));

  const VIEW_TITLES = {
    doctor: '环境真执行诊断',
    runner: '单线程适配器试跑',
    accounts: '账号与 API Key 台账',
    snapshots: '本地失败现场快照',
    scaffold: '新建站点适配器脚手架',
    layout: '单窗口视窗标定',
    config: '运行配置 (config.json)',
  };

  function toast(msg) {
    const el = $('#toast');
    el.textContent = msg;
    el.classList.remove('hidden');
    clearTimeout(toast._t);
    toast._t = setTimeout(() => el.classList.add('hidden'), 3000);
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
    const isDark = document.documentElement.classList.contains('dark');
    const next = isDark ? 'light' : 'dark';
    document.documentElement.classList.remove('dark', 'light');
    document.documentElement.classList.add(next);
    try {
      localStorage.setItem('rh_lite_theme', next);
    } catch {}
    syncThemeIcons();
  });

  if (localStorage.getItem('rh_lite_sidebar') === '1') {
    $('#app-shell').classList.add('collapsed');
  }
  $('#btn-toggle-sidebar').addEventListener('click', () => {
    const collapsed = $('#app-shell').classList.toggle('collapsed');
    try {
      localStorage.setItem('rh_lite_sidebar', collapsed ? '1' : '0');
    } catch {}
  });

  // ==================== Navigation ====================
  function switchView(target) {
    if (!VIEW_TITLES[target]) target = 'doctor';
    currentView = target;
    $('#topbar-title').textContent = VIEW_TITLES[target];
    $$('.nav-item').forEach((b) => b.classList.toggle('active', b.dataset.view === target));
    $$('.view').forEach((v) => v.classList.toggle('active', v.id === `view-${target}`));
    if (target === 'config') loadConfig();
    if (target === 'accounts') loadAccounts();
    if (target === 'snapshots') loadSnapshots();
    if (target === 'layout') loadLayout();
    if (target === 'runner') {
      loadAdapters();
      loadJobs(false);
    }
  }

  $$('.nav-item').forEach((btn) => {
    btn.addEventListener('click', () => switchView(btn.dataset.view));
  });
  $$('[data-jump]').forEach((btn) => {
    btn.addEventListener('click', () => switchView(btn.dataset.jump));
  });
  $('#heartbeat-card').addEventListener('click', () => switchView('doctor'));

  $('#btn-global-refresh').addEventListener('click', async () => {
    const ico = $('#icon-refresh');
    ico.classList.add('spin');
    try {
      if (currentView === 'doctor') await Promise.all([loadDoctor(), loadAdapters(), loadAccounts(), loadSnapshots()]);
      else if (currentView === 'config') await loadConfig();
      else if (currentView === 'runner') await Promise.all([loadAdapters(), loadJobs(false)]);
      else if (currentView === 'accounts') await loadAccounts();
      else if (currentView === 'snapshots') await loadSnapshots();
      else if (currentView === 'layout') await loadLayout();
      toast('已刷新当前面板数据');
    } finally {
      setTimeout(() => ico.classList.remove('spin'), 320);
    }
  });

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

      $('#kpi-doctor').textContent = `${passCount}/${checks.length}`;
      $('#kpi-doctor-sub').textContent = data.ready ? '核心依赖全部通过' : `${data.blockers} 项待修复`;
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

  // ==================== 2. Runtime Config ====================
  async function loadConfig() {
    try {
      const res = await fetch('/api/config');
      const cfg = await res.json();
      const b = cfg.browser || {};
      const d = cfg.defaults || {};
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
    };
    const res = await fetch('/api/config', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    const data = await res.json();
    if (data.ok) toast('运行配置已原子保存至 config.json');
    else toast(data.error || '保存失败');
  });

  // ==================== 3. Adapters & Single-thread Runner ====================
  function renderAdapters() {
    const box = $('#adapter-list');
    const q = ($('#adapter-search')?.value || '').trim().toLowerCase();
    const list = q
      ? adaptersCache.filter((a) => `${a.name} ${a.target_url || ''}`.toLowerCase().includes(q))
      : adaptersCache;
    if (!list.length) {
      box.innerHTML = `<div class="empty-box" style="padding:20px">未匹配到站点适配器</div>`;
      return;
    }
    box.innerHTML = list
      .map(
        (a) => `
        <button type="button" class="adapter-btn ${a.name === selectedAdapter ? 'selected' : ''}" data-name="${escapeHtml(a.name)}">
          <div class="adapter-name">${escapeHtml(a.name)}</div>
          <div class="adapter-url">${escapeHtml(a.target_url || 'embedded / local')}</div>
        </button>`
      )
      .join('');
    box.querySelectorAll('.adapter-btn').forEach((b) => {
      b.addEventListener('click', () => {
        selectedAdapter = b.dataset.name;
        box.querySelectorAll('.adapter-btn').forEach((x) => x.classList.toggle('selected', x === b));
      });
    });
  }

  async function loadAdapters() {
    try {
      const res = await fetch('/api/adapters');
      const data = await res.json();
      adaptersCache = data.adapters || [];
      $('#kpi-adapters').textContent = String(adaptersCache.length);
      if (!selectedAdapter && adaptersCache.length) selectedAdapter = adaptersCache[0].name;
      renderAdapters();
    } catch (e) {
      $('#adapter-list').innerHTML = `<div class="empty-box">加载适配器失败: ${escapeHtml(e.message)}</div>`;
    }
  }
  $('#adapter-search').addEventListener('input', renderAdapters);

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
        loadAccounts();
        loadSnapshots();
        toast(`单站试跑结束 (${msg.status})`);
      }
    };
    logStream.onerror = () => {
      if (logStream) {
        logStream.close();
        logStream = null;
      }
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
          btn.addEventListener('click', async () => {
            const jid = btn.dataset.jid;
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
          });
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
    setRunningState(true);
    try {
      const res = await fetch('/api/run', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ adapter: selectedAdapter, count }),
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

  // ==================== 4. Accounts & Export ====================
  function renderAccounts() {
    const q = ($('#accounts-search')?.value || '').trim().toLowerCase();
    const rows = q
      ? accountsCache.filter((r) => `${r.email} ${r.username} ${r.api_key || ''}`.toLowerCase().includes(q))
      : accountsCache;
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
            ${r.api_key ? `<button type="button" class="btn btn-secondary btn-sm" data-copy-key="${idx}">Key</button>` : ''}
            <button type="button" class="btn btn-ghost btn-sm" data-copy-row="${idx}">整行</button>
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
  }

  async function loadAccounts() {
    try {
      const res = await fetch('/api/accounts');
      const data = await res.json();
      accountsCache = data.accounts || [];
      const withKey = accountsCache.filter((r) => Boolean(r.api_key)).length;
      $('#kpi-accounts').textContent = String(accountsCache.length);
      $('#kpi-accounts-sub').textContent = `其中 ${withKey} 条含有效 Key`;
      $('#acc-kpi-total').textContent = String(accountsCache.length);
      $('#acc-kpi-keys').textContent = String(withKey);
      $('#acc-kpi-plain').textContent = String(accountsCache.length - withKey);

      const badge = $('#nav-badge-accounts');
      if (accountsCache.length > 0) {
        badge.textContent = String(accountsCache.length);
        badge.classList.remove('hidden');
      } else {
        badge.classList.add('hidden');
      }
      renderAccounts();
    } catch {}
  }

  $('#accounts-search').addEventListener('input', renderAccounts);
  $('#btn-toggle-secrets').addEventListener('click', () => {
    revealSecrets = !revealSecrets;
    $('#btn-toggle-secrets').textContent = revealSecrets ? '隐藏明文' : '显示明文';
    renderAccounts();
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

  // ==================== 5. Failure Snapshots ====================
  async function loadSnapshots() {
    const empty = $('#snapshots-empty');
    const grid = $('#snapshots-grid');
    try {
      const res = await fetch('/api/snapshots');
      const data = await res.json();
      const list = data.snapshots || [];
      $('#kpi-snaps').textContent = String(list.length);
      if (!list.length) {
        empty.classList.remove('hidden');
        grid.innerHTML = '';
        return;
      }
      empty.classList.add('hidden');
      grid.innerHTML = list
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
      grid.querySelectorAll('.snap-card').forEach((card) => {
        card.addEventListener('click', () => {
          $('#lightbox-title').textContent = card.dataset.name;
          $('#lightbox-img').src = card.dataset.url;
          $('#lightbox').classList.remove('hidden');
        });
      });
    } catch (e) {
      toast('加载快照失败: ' + e.message);
    }
  }
  $('#btn-refresh-snaps').addEventListener('click', loadSnapshots);
  $('#lightbox-close').addEventListener('click', () => $('#lightbox').classList.add('hidden'));
  $('#lightbox').addEventListener('click', (e) => {
    if (e.target.id === 'lightbox') $('#lightbox').classList.add('hidden');
  });
  window.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') $('#lightbox').classList.add('hidden');
  });

  // ==================== 6. Scaffold ====================
  $$('[data-copy-cmd]').forEach((btn) => {
    btn.addEventListener('click', () => copyText(btn.dataset.copyCmd, '命令'));
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
        loadAdapters();
      } else {
        box.classList.add('err');
        box.textContent = data.error || '生成失败';
      }
    } finally {
      $('#btn-scaffold').disabled = false;
    }
  });

  // ==================== 7. Layout Preview ====================
  async function loadLayout() {
    try {
      const res = await fetch('/api/layout');
      const data = await res.json();
      const g = data.geometry || {};
      const single = g.single_concurrency?.slots?.[0];
      const grid = $('#layout-summary');
      if (!single) {
        grid.innerHTML = `<div class="check-item">尚未生成 config/window_geometry.json，请点击右上角自动适配。</div>`;
        return;
      }
      const zoomPct = Math.round((single.zoom || 1) * 100);
      $('#layout-screen-label').textContent = `逻辑屏幕工作区 · ${g.screen_resolution || '已检测'}`;
      $('#mw-title').textContent = `Single Window · X=${single.x ?? 0}, Y=${single.y ?? 0}`;
      $('#mw-dim').textContent = `${single.width} × ${single.height}`;
      $('#mw-zoom').textContent = `防切割缩放 ${zoomPct}%`;

      grid.innerHTML = [
        { name: '适配分辨率', val: g.screen_resolution || '已检测' },
        { name: '单窗口尺寸', val: `${single.width} × ${single.height}` },
        { name: '防切割缩放比 (Zoom)', val: `${zoomPct}%` },
        { name: '校准时间', val: g.calibrated_at || '—' },
      ]
        .map(
          (m) => `
          <div class="check-item">
            <div class="check-name">${escapeHtml(m.name)}</div>
            <div class="check-detail mono">${escapeHtml(m.val)}</div>
          </div>`
        )
        .join('');
    } catch {}
  }

  $('#btn-auto-layout').addEventListener('click', async () => {
    const res = await fetch('/api/layout/auto', { method: 'POST' });
    const data = await res.json();
    if (data.ok) {
      toast('已重新计算本机单窗口标定参数');
      loadLayout();
    } else {
      toast(data.error || '计算失败');
    }
  });

  // Initial boot
  loadDoctor();
  loadAdapters();
  loadAccounts();
  loadSnapshots();
  loadJobs(true);
})();
