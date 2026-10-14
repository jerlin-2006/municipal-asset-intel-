/* Add-on UI. Does not modify any core function; it injects new elements and decorates the queue table. */
(function () {
  'use strict';
  const $ = (s, r = document) => r.querySelector(s);
  const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const money = n => (n == null ? '--' : '\u20b9' + Math.round(n).toLocaleString('en-IN'));
  const STATUSES = ['Open', 'Assigned', 'In Progress', 'Resolved'];
  const STATUS_COLOR = { 'Open': 'text-slate-300', 'Assigned': 'text-cyan-300', 'In Progress': 'text-amber-300', 'Resolved': 'text-emerald-400' };

  let meta = {};            // complaint_id -> {status, est_cost, est_downtime}
  let reports = {};         // complaint_id -> raw C row
  const filters = { q: '', level: '', type: '', status: 'Active' };
  let trendChart = null, assetChart = null, autoTimer = null;

  // ---------- inject static UI ----------
  // Each piece is isolated: if one fails (e.g. you customised the page), the others still work.
  const safe = (name, fn) => { try { fn(); } catch (e) { console.error('features.js: ' + name + ' failed', e); } };
  const FIELD = 'bg-slate-950 border border-slate-800 rounded-lg px-2 py-1.5 text-xs text-slate-200 focus:outline-none focus:border-cyan-500';

  function injectKpis(root) {
    const header = $('header', root);
    const kpi = document.createElement('div');
    kpi.innerHTML = `
      <section id="kpi-strip" class="grid grid-cols-2 md:grid-cols-5 gap-3">
        ${[['kpi-open', 'Open work orders'], ['kpi-high', 'High priority open'], ['kpi-rul', 'Avg RUL (open)'],
           ['kpi-unmatched', 'Needs manual review'], ['kpi-resolved', 'Resolved']].map(([id, label]) => `
          <div class="bg-slate-900/90 border border-slate-800 rounded-2xl p-4">
            <span class="text-[10px] uppercase font-bold text-slate-500 block">${label}</span>
            <span id="${id}" class="text-2xl font-extrabold text-white font-mono">--</span>
          </div>`).join('')}
      </section>
      <div id="hotspot-banner" class="hidden mt-3 p-3 rounded-xl text-xs font-medium border bg-amber-950/70 border-amber-800 text-amber-300"></div>`;
    (header || root.firstElementChild).after(kpi);
  }

  function injectFilters() {
    const tableWrap = $('#workorder-table-body').closest('.overflow-x-auto');
    const bar = document.createElement('div');
    bar.className = 'flex flex-wrap items-center gap-2 mb-3';
    bar.innerHTML = `
      <input id="fx-q" placeholder="Search asset, road, defect..." class="${FIELD} w-56">
      <select id="fx-level" class="${FIELD}"><option value="">All priorities</option><option>High</option><option>Medium</option><option>Low</option><option>Unassigned</option></select>
      <select id="fx-type" class="${FIELD}"><option value="">Water + Drainage</option><option>Water</option><option>Drainage</option></select>
      <select id="fx-status" class="${FIELD}"><option value="Active">Active (not resolved)</option><option value="">All statuses</option>${STATUSES.map(s => `<option>${s}</option>`).join('')}</select>
      <label class="text-xs text-slate-400 flex items-center gap-1"><input id="fx-auto" type="checkbox"> Auto-refresh 30s</label>
      <span id="fx-count" class="text-xs text-slate-500 ml-auto"></span>
      <a href="/api/export.csv" class="text-xs text-cyan-400 hover:text-cyan-300 font-semibold px-3 py-1 bg-cyan-950/50 rounded-lg border border-cyan-800/60">\u2b07 Export CSV</a>`;
    tableWrap.before(bar);

    const tr = $('thead tr', tableWrap);
    ['Status', 'Est. Cost / Downtime'].forEach(t => {
      const th = document.createElement('th'); th.className = 'py-3 px-4'; th.textContent = t; tr.appendChild(th);
    });

    const wire = (id, key) => $(id).addEventListener(key === 'q' ? 'input' : 'change', e => { filters[key] = e.target.value; applyFilters(); });
    wire('#fx-q', 'q'); wire('#fx-level', 'level'); wire('#fx-type', 'type'); wire('#fx-status', 'status');
    $('#fx-auto').addEventListener('change', e => {
      clearInterval(autoTimer);
      if (e.target.checked) autoTimer = setInterval(() => window.fetchQueue && window.fetchQueue(), 30000);
    });
  }

  function injectAnalytics(root) {
    const sec = document.createElement('section');
    sec.id = 'fx-analytics';
    sec.className = 'bg-slate-900/90 border border-slate-800 rounded-2xl p-6 shadow-xl';
    sec.innerHTML = `
      <div class="flex flex-wrap items-center justify-between gap-2 mb-1">
        <h2 class="text-lg font-bold text-white flex items-center gap-2"><span>\u{1f4ca}</span> Network Analytics</h2>
        <select id="fx-range" class="${FIELD}">
          <option value="12">Last 12 months</option><option value="24">Last 24 months</option>
          <option value="60" selected>Last 5 years</option><option value="120">Last 10 years</option>
        </select>
      </div>
      <p class="text-xs text-slate-400 mb-3">Complaint trend, load per asset and typical repair cost, from B_history.csv plus live intake.</p>
      <div id="fx-an-msg" class="hidden mb-4 p-3 rounded-xl text-xs border"></div>
      <div class="grid grid-cols-1 lg:grid-cols-2 gap-6">
        <div class="bg-slate-950/80 border border-slate-800/80 rounded-xl p-4">
          <h3 class="text-xs font-bold text-slate-300 uppercase tracking-wider mb-2">Complaints per month</h3>
          <div class="chart-box" style="position:relative;height:300px;width:100%"><canvas id="fxTrend"></canvas></div>
        </div>
        <div class="bg-slate-950/80 border border-slate-800/80 rounded-xl p-4">
          <h3 class="text-xs font-bold text-slate-300 uppercase tracking-wider mb-2">Records & live complaints per asset</h3>
          <div class="chart-box" style="position:relative;height:300px;width:100%"><canvas id="fxAsset"></canvas></div>
        </div>
      </div>
      <div class="mt-6">
        <h3 class="text-xs font-bold text-slate-300 uppercase tracking-wider mb-2">Typical repair cost by defect (Maintenance records)</h3>
        <div id="fx-cost" class="overflow-x-auto rounded-xl border border-slate-800 bg-slate-950/70"></div>
      </div>`;
    root.appendChild(sec);
    $('#fx-range').addEventListener('change', () => loadAnalytics());
  }

  function injectUI() {
    const root = $('.max-w-7xl') || document.body;
    if ($('#kpi-strip') || $('#fx-analytics')) return;       // already injected
    safe('KPI strip', () => injectKpis(root));
    safe('filter bar', injectFilters);
    safe('analytics section', () => injectAnalytics(root));
  }

  // ---------- data ----------
  async function loadMeta() {
    try {
      const [wo, rep] = await Promise.all([fetch('/api/workorders').then(r => r.json()), fetch('/api/reports').then(r => r.json())]);
      meta = wo; reports = {}; rep.forEach(r => { reports[r.complaint_id] = r; });
    } catch (e) { /* leave previous state */ }
  }

  async function loadKpis() {
    try {
      const s = await (await fetch('/api/summary')).json();
      $('#kpi-open').textContent = s.open;
      $('#kpi-high').textContent = s.high_open;
      $('#kpi-high').className = 'text-2xl font-extrabold font-mono ' + (s.high_open > 0 ? 'text-rose-400' : 'text-white');
      $('#kpi-rul').textContent = s.avg_rul != null ? s.avg_rul + ' yrs' : '--';
      $('#kpi-unmatched').textContent = s.unmatched;
      $('#kpi-resolved').textContent = s.resolved;
      const hb = $('#hotspot-banner');
      if (s.hotspots.length) {
        hb.classList.remove('hidden');
        hb.innerHTML = '\u{1f525} <b>Hotspots (3+ complaints in 30 days):</b> ' +
          s.hotspots.map(h => `${esc(h.location)} (${h.count}${h.assets.length ? ': ' + esc(h.assets.join(', ')) : ''})`).join(' \u2022 ');
      } else hb.classList.add('hidden');
    } catch (e) { /* ignore */ }
  }

  // ---------- table decoration ----------
  function decorateRows() {
    document.querySelectorAll('#workorder-table-body tr[data-id]').forEach(tr => {
      if (tr.dataset.fx) return;
      tr.dataset.fx = '1';
      const id = tr.dataset.id, m = meta[id] || { status: 'Open' };

      const tdS = document.createElement('td'); tdS.className = 'py-3 px-4';
      const sel = document.createElement('select');
      sel.className = 'bg-slate-950 border border-slate-800 rounded-md px-1.5 py-1 text-xs focus:outline-none ' + (STATUS_COLOR[m.status] || '');
      STATUSES.forEach(s => { const o = document.createElement('option'); o.textContent = s; o.selected = s === m.status; sel.appendChild(o); });
      sel.onclick = e => e.stopPropagation();                       // don't trigger row selection
      sel.onchange = async e => {
        e.stopPropagation();
        const prev = (meta[id] || {}).status || 'Open';
        try {
          const res = await fetch(`/api/workorders/${encodeURIComponent(id)}/status`, {
            method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ status: sel.value })
          });
          if (!res.ok) throw new Error();
          meta[id] = { ...(meta[id] || {}), status: sel.value };
          sel.className = 'bg-slate-950 border border-slate-800 rounded-md px-1.5 py-1 text-xs focus:outline-none ' + STATUS_COLOR[sel.value];
          tr.classList.toggle('opacity-50', sel.value === 'Resolved');
          applyFilters(); loadKpis();
        } catch (_) { sel.value = prev; alert('Could not update status'); }
      };
      tdS.appendChild(sel);
      tr.classList.toggle('opacity-50', m.status === 'Resolved');

      const tdC = document.createElement('td'); tdC.className = 'py-3 px-4 text-xs text-slate-400';
      tdC.textContent = m.est_cost != null ? `${money(m.est_cost)} / ${m.est_downtime ?? '--'} h` : '--';
      tr.append(tdS, tdC);
    });
    applyFilters();
  }

  function applyFilters() {
    const rows = [...document.querySelectorAll('#workorder-table-body tr[data-id]')];
    let shown = 0;
    rows.forEach(tr => {
      const r = reports[tr.dataset.id] || {}, m = meta[tr.dataset.id] || { status: 'Open' };
      const level = r.priority_level || 'Unassigned';
      const type = r.asset_type || r.extracted_asset_type || '';
      const hay = [r.complaint_id, r.matched_asset_id, r.location, r.issue_type, r.complaint_text].join(' ').toLowerCase();
      const ok = (!filters.q || hay.includes(filters.q.toLowerCase())) &&
        (!filters.level || level === filters.level) &&
        (!filters.type || type === filters.type) &&
        (!filters.status || (filters.status === 'Active' ? m.status !== 'Resolved' : m.status === filters.status));
      tr.classList.toggle('hidden', !ok);
      if (ok) shown++;
    });
    const c = $('#fx-count'); if (c) c.textContent = rows.length ? `Showing ${shown} of ${rows.length}` : '';
  }

  // ---------- analytics ----------
  let anSeq = 0;
  function anMsg(text, kind) {            // kind: 'error' | 'warn' | null
    const el = $('#fx-an-msg'); if (!el) return;
    if (!text) { el.classList.add('hidden'); return; }
    el.className = 'mb-4 p-3 rounded-xl text-xs border ' + (kind === 'error'
      ? 'bg-rose-950/70 border-rose-800 text-rose-300' : 'bg-amber-950/70 border-amber-800 text-amber-300');
    el.innerHTML = text;
  }

  async function loadAnalytics() {
    const my = ++anSeq;                    // ignore results of older, slower requests
    if (typeof Chart === 'undefined') {
      anMsg('Chart.js did not load (no internet or the CDN is blocked), so charts cannot be drawn. Open the browser console (F12) for details.', 'error');
      return;
    }
    try {
      const months = ($('#fx-range') || {}).value || 60;
      const res = await fetch('/api/analytics?months=' + months);
      if (!res.ok) {
        const body = (await res.text()).slice(0, 200);
        throw new Error(`/api/analytics returned HTTP ${res.status}. ${body}`);
      }
      const a = await res.json();
      if (my !== anSeq) return;

      const warns = (a.warnings || []).map(w => '\u2022 ' + esc(w));
      anMsg(warns.length ? warns.join('<br>') : '', 'warn');

      const tick = { color: '#94a3b8', font: { size: 10 } }, grid = { color: '#1e293b' };
      const legend = { labels: { color: '#94a3b8', font: { size: 10 } } };

      if (trendChart) trendChart.destroy();
      trendChart = new Chart($('#fxTrend'), {
        type: 'line',
        data: { labels: a.months, datasets: [
          { label: 'History (B)', data: a.history_monthly, borderColor: '#38bdf8', backgroundColor: '#38bdf826', fill: true, tension: 0.3, pointRadius: 2 },
          { label: 'Live intake (C)', data: a.live_monthly, borderColor: '#f59e0b', tension: 0.3, pointRadius: 3 }] },
        options: { responsive: true, maintainAspectRatio: false, animation: false, plugins: { legend },
          scales: { x: { ticks: { ...tick, maxTicksLimit: 8 }, grid }, y: { beginAtZero: true, ticks: { ...tick, precision: 0 }, grid } } }
      });

      const ids = Object.keys(a.by_asset).sort();
      if (assetChart) assetChart.destroy();
      assetChart = new Chart($('#fxAsset'), {
        type: 'bar',
        data: { labels: ids, datasets: [
          { label: 'History records', data: ids.map(i => a.by_asset[i].history_records || 0), backgroundColor: '#0284c7', borderRadius: 4 },
          { label: 'Live complaints', data: ids.map(i => a.by_asset[i].live_complaints || 0), backgroundColor: '#f59e0b', borderRadius: 4 }] },
        options: { responsive: true, maintainAspectRatio: false, animation: false, plugins: { legend },
          scales: { x: { ticks: tick, grid }, y: { beginAtZero: true, ticks: { ...tick, precision: 0 }, grid } } }
      });

      const types = Object.entries(a.cost_table || {});
      $('#fx-cost').innerHTML = types.length ? `
        <table class="w-full text-left text-sm text-slate-300">
          <thead class="text-xs uppercase bg-slate-900 text-slate-400"><tr>
            <th class="py-2 px-4">Defect</th><th class="py-2 px-4">Avg cost</th><th class="py-2 px-4">Avg downtime</th><th class="py-2 px-4">Based on</th></tr></thead>
          <tbody>${types.map(([t, v]) => `<tr class="border-t border-slate-800/60">
            <td class="py-2 px-4">${esc(t)}</td><td class="py-2 px-4 font-mono">${money(v.avg_cost)}</td>
            <td class="py-2 px-4 font-mono">${v.avg_downtime != null ? v.avg_downtime + ' h' : '--'}</td>
            <td class="py-2 px-4 text-xs text-slate-500">${v.n} repairs</td></tr>`).join('')}</tbody></table>`
        : '<p class="p-4 text-xs text-slate-500">No maintenance cost data found in B_history.csv.</p>';
    } catch (e) {
      console.error('Network analytics failed:', e);
      anMsg('Network analytics failed: ' + esc(e.message), 'error');
    }
  }

  // ---------- boot ----------
  let pending = null;
  function schedule() {
    clearTimeout(pending);
    pending = setTimeout(async () => { await loadMeta(); decorateRows(); loadKpis(); loadAnalytics(); }, 60);
  }

  function boot() {
    injectUI();
    const tbody = $('#workorder-table-body');
    // core code rebuilds <tbody> children on every refresh -> re-decorate each time
    new MutationObserver(schedule).observe(tbody, { childList: true });
    schedule();
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', boot); else boot();
})();

/* Auto-load the other add-ons so index.html only needs the features.js tag.
   Skips any script that index.html already loads. A missing add-on just logs a 404 in the console. */
(function () {
  ['/resolve.js', '/solutions.js', '/insights.js'].forEach(function (src) {
    if (document.querySelector('script[src="' + src + '"]')) return;
    var s = document.createElement('script');
    s.src = src;
    document.body.appendChild(s);
  });
})();