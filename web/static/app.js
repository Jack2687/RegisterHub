(() => {
  let selectedAdapter = '';
  let activeJobId = '';
  let logStream = null;

  const $ = (sel) => document.querySelector(sel);
  const $$ = (sel) => Array.from(document.querySelectorAll(sel));

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
      .replace(/>/g, '&gt;');
  }

  // Tab navigation
  $$('.tab').forEach((btn) => {
    btn.addEventListener('click', () => {
      const target = btn.dataset.view;
      $$('.tab').forEach((b) => b.classList.toggle('active', b === btn));
      $$('.view').forEach((v) => v.classList.toggle('active', v.id === `view-${target}`));
      if (target === 'accounts') loadAccounts();
      if (target === 'layout') loadLayout();
      if (target === 'runner') loadAdapters();
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

  // 2. Adapters & Single-thread Runner
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
    if (level === 'error') div.className = 'log-err';
    div.textContent = line;
    box.appendChild(div);
    box.scrollTop = box.scrollHeight;
  }

  $('#btn-start-run').addEventListener('click', async () => {
    if (!selectedAdapter) return toast('请先选择站点适配器');
    const count = Math.max(1, Math.min(20, Number($('#run-count').value) || 1));
    if (logStream) logStream.close();
    $('#console-log').innerHTML = '';
    try {
      const res = await fetch('/api/run', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ adapter: selectedAdapter, count }),
      });
      const data = await res.json();
      if (!data.ok) return toast(data.error || '启动失败');

      activeJobId = data.job.id;
      $('#console-status').textContent = `运行中: ${selectedAdapter} (PID=${data.job.pid})`;
      $('#btn-stop-run').disabled = false;

      logStream = new EventSource(`/api/jobs/${encodeURIComponent(activeJobId)}/stream`);
      logStream.onmessage = (ev) => {
        const msg = JSON.parse(ev.data);
        if (msg.type === 'log') {
          appendLog(`[${msg.ts}] ${msg.text}`, msg.level);
        } else if (msg.type === 'status') {
          $('#console-status').textContent = `已结束: ${msg.status} (code=${msg.returncode})`;
          $('#btn-stop-run').disabled = true;
          logStream.close();
          toast(`单站试跑结束 (${msg.status})`);
        }
      };
      logStream.onerror = () => {
        logStream.close();
        $('#btn-stop-run').disabled = true;
      };
    } catch (e) {
      toast('启动异常: ' + e.message);
    }
  });

  $('#btn-stop-run').addEventListener('click', async () => {
    if (!activeJobId) return;
    await fetch(`/api/jobs/${encodeURIComponent(activeJobId)}/stop`, { method: 'POST' });
    $('#btn-stop-run').disabled = true;
    toast('已发送停止指令');
  });

  $('#btn-clear-log').addEventListener('click', () => {
    $('#console-log').innerHTML = '';
  });

  // 3. Scaffold
  $('#btn-scaffold').addEventListener('click', async () => {
    const site_name = $('#sc-name').value.trim();
    const url = $('#sc-url').value.trim();
    const box = $('#scaffold-msg');
    if (!site_name) return toast('请输入适配器名称');
    const res = await fetch('/api/scaffold', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ site_name, url }),
    });
    const data = await res.json();
    box.classList.remove('hidden', 'err');
    if (data.ok) {
      box.textContent = `生成成功: ${data.path}`;
      toast(`已生成适配器 sites/${data.site}.py`);
      loadAdapters();
    } else {
      box.classList.add('err');
      box.textContent = data.error || '生成失败';
    }
  });

  // 4. Accounts & Export
  async function loadAccounts() {
    const res = await fetch('/api/accounts');
    const data = await res.json();
    const rows = data.accounts || [];
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
      .map(
        (r) => `
        <tr>
          <td class="mono">${escapeHtml(r.email)}</td>
          <td class="mono">••••••••</td>
          <td class="mono">${escapeHtml(r.username || '-')}</td>
        </tr>`
      )
      .join('');
  }

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

  // 5. Layout
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

  // Initial load
  loadDoctor();
  loadAdapters();
})();
