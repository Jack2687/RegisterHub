(() => {
  let selectedAdapter = '';
  let activeJobId = '';
  let logStream = null;
  let revealSecrets = false;
  let accountsCache = [];

  const $ = (sel) => document.querySelector(sel);
  const $$ = (sel) => Array.from(document.querySelectorAll(sel));

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
    if (!s) return '-';
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

  // Tab navigation
  $$('.tab').forEach((btn) => {
    btn.addEventListener('click', () => {
      const target = btn.dataset.view;
      $$('.tab').forEach((b) => b.classList.toggle('active', b === btn));
      $$('.view').forEach((v) => v.classList.toggle('active', v.id === `view-${target}`));
      if (target === 'config') loadConfig();
      if (target === 'accounts') loadAccounts();
      if (target === 'snapshots') loadSnapshots();
      if (target === 'layout') loadLayout();
      if (target === 'runner') {
        loadAdapters();
        loadJobs(false);
      }
    });
  });

  // 1. Doctor
  async function loadDoctor() {
    const banner = $('#doctor-banner');
    const grid = $('#doctor-grid');
    banner.className = 'banner muted';
    banner.textContent = '正在执行环境真执行体检…';
    try {
      const res = await fetch('/api/doctor');
      const data = await res.json();
      banner.className = `banner ${data.ready ? 'ok' : 'fail'}`;
      banner.textContent = data.ready
        ? `环境就绪 (${data.checked_at}) · 0 阻塞项，${data.warnings} 条建议项`
        : `检测到 ${data.blockers} 个阻塞项，请按卡片提示修复后再试跑 (${data.checked_at})`;

      grid.innerHTML = (data.checks || [])
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

  // 2. Runtime Config
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
    if (data.ok) {
      toast('配置已原子保存至 config.json');
    } else {
      toast(data.error || '保存失败');
    }
  });

  // 3. Adapters & Single-thread Runner
  async function loadAdapters() {
    const box = $('#adapter-list');
    try {
      const res = await fetch('/api/adapters');
      const data = await res.json();
      const list = data.adapters || [];
      if (!selectedAdapter && list.length) selectedAdapter = list[0].name;
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
    } catch (e) {
      box.innerHTML = `<div class="empty-box">加载适配器失败: ${escapeHtml(e.message)}</div>`;
    }
  }

  function appendLog(line, level) {
    const box = $('#console-log');
    const div = document.createElement('div');
    if (level === 'ok') div.className = 'log-ok';
    if (level === 'warn') div.className = 'log-warn';
    if (level === 'error') div.className = 'log-err';
    div.textContent = line;
    box.appendChild(div);
    box.scrollTop = box.scrollHeight;
  }

  function setRunningState(running) {
    $('#btn-start-run').disabled = running;
    $('#btn-stop-run').disabled = !running;
  }

  function attachJobStream(job) {
    if (logStream) logStream.close();
    activeJobId = job.id;
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
        logStream.close();
        logStream = null;
        loadJobs(false);
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
            $('#console-log').textContent = ld.log || '(空日志)';
          });
        });
      }

      if (autoReconnect && data.active_job && data.active_job.status === 'running') {
        attachJobStream(data.active_job);
      }
    } catch {
      // ignore
    }
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
    $('#console-log').innerHTML = '';
  });

  // 4. Accounts & Export (with API Key + reveal + copy)
  function renderAccounts() {
    const rows = accountsCache;
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
          <td class="mono">${escapeHtml(revealSecrets ? r.password || '-' : maskSecret(r.password))}</td>
          <td class="mono">${escapeHtml(r.username || '-')}</td>
          <td class="mono">${escapeHtml(r.api_key ? (revealSecrets ? r.api_key : maskSecret(r.api_key)) : '-')}</td>
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
    const res = await fetch('/api/accounts');
    const data = await res.json();
    accountsCache = data.accounts || [];
    renderAccounts();
  }

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

  // 5. Failure Snapshots
  async function loadSnapshots() {
    const empty = $('#snapshots-empty');
    const grid = $('#snapshots-grid');
    try {
      const res = await fetch('/api/snapshots');
      const data = await res.json();
      const list = data.snapshots || [];
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

  // 6. Scaffold
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

  // 7. Layout
  async function loadLayout() {
    const res = await fetch('/api/layout');
    const data = await res.json();
    const g = data.geometry || {};
    const single = g.single_concurrency?.slots?.[0];
    const grid = $('#layout-summary');
    if (!single) {
      grid.innerHTML = `<div class="check-item">尚未生成 config/window_geometry.json，请点击右上角自动适配。</div>`;
      return;
    }
    grid.innerHTML = [
      { name: '适配分辨率', val: g.screen_resolution || '已检测' },
      { name: '单窗口尺寸', val: `${single.width} × ${single.height}` },
      { name: '防切割缩放比 (Zoom)', val: `${Math.round((single.zoom || 1) * 100)}%` },
      { name: '校准时间', val: g.calibrated_at || '-' },
    ]
      .map(
        (m) => `
        <div class="check-item">
          <div class="check-name">${escapeHtml(m.name)}</div>
          <div class="check-detail mono">${escapeHtml(m.val)}</div>
        </div>`
      )
      .join('');
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

  // Initial load (including auto-reconnect if a job is already running!)
  loadDoctor();
  loadAdapters();
  loadJobs(true);
})();
