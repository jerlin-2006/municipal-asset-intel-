/* Predictive Insights (add-on): proactive risk ranking, budget optimizer, map / road hotspots.
   Calls /api/insights/risk, /api/insights/budget, /api/insights/map and /api/insights/version.
   Part 1 builds the panel. Part 2 (bottom) keeps it live. */
(function () {
  'use strict';
  const $ = (s, r = document) => r.querySelector(s);
  const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const money = n => (n == null ? '--' : '\u20b9' + Math.round(n).toLocaleString('en-IN'));
  const FIELD = 'bg-slate-950 border border-slate-800 rounded-lg px-2 py-1.5 text-xs text-slate-200 focus:outline-none focus:border-cyan-500';
  const LV = {
    High: 'bg-red-950 text-red-400 border-red-800',
    Medium: 'bg-amber-950 text-amber-400 border-amber-800',
    Low: 'bg-emerald-950 text-emerald-400 border-emerald-800'
  };
  const COL = { High: '#ef4444', Medium: '#f59e0b', Low: '#22c55e' };
  const badge = l => `<span class="px-2 py-0.5 border rounded-full text-[10px] font-semibold ${LV[l] || 'bg-slate-800 text-slate-300 border-slate-700'}">${esc(l)}</span>`;
  const TH = 'py-2 px-3';
  const TD = 'py-2 px-3';

  let tab = 'risk', map = null, markers = null, leafletReady = null;
  const loaded = {};

  async function getJSON(url) {
    const res = await fetch(url);
    if (!res.ok) throw new Error(`${url} returned HTTP ${res.status}`);
    return res.json();
  }

  function msg(lines, kind) {
    const el = $('#fx-ins-msg'); if (!el) return;
    if (!lines || !lines.length) { el.classList.add('hidden'); return; }
    el.className = 'mb-4 p-3 rounded-xl text-xs border ' + (kind === 'error'
      ? 'bg-rose-950/70 border-rose-800 text-rose-300' : 'bg-amber-950/70 border-amber-800 text-amber-300');
    el.innerHTML = lines.map(l => '&bull; ' + esc(l)).join('<br>');
  }

  function stat(label, value, sub) {
    return `<div class="bg-slate-950/80 border border-slate-800/80 rounded-xl p-3">
      <span class="text-[10px] uppercase font-bold text-slate-500 block">${label}</span>
      <span class="text-lg font-extrabold text-white font-mono">${value}</span>
      ${sub ? `<span class="text-[10px] text-slate-500 block">${sub}</span>` : ''}</div>`;
  }

  // ---------- inject ----------
  function inject() {
    if ($('#fx-insights')) return true;
    const root = $('.max-w-7xl') || document.body;
    const sec = document.createElement('section');
    sec.id = 'fx-insights';
    sec.className = 'bg-slate-900/90 border border-slate-800 rounded-2xl p-6 shadow-xl';
    sec.innerHTML = `
      <div class="flex flex-wrap items-center justify-between gap-2 mb-1">
        <h2 class="text-lg font-bold text-white flex items-center gap-2"><span>\u{1F52E}</span> Predictive Insights</h2>
        <button id="fx-ins-refresh" class="text-xs text-cyan-400 hover:text-cyan-300 font-semibold px-3 py-1 bg-cyan-950/50 rounded-lg border border-cyan-800/60">\u21bb Refresh</button>
      </div>
      <p class="text-xs text-slate-400 mb-3">Looks at every asset, not only the ones with complaints: who fails next, where to spend a budget, and where the problems cluster.</p>
      <div class="flex gap-2 mb-4" id="fx-ins-tabs">
        ${[['risk', 'Failure risk ranking'], ['budget', 'Budget optimizer'], ['map', 'Map & hotspots']].map(([k, l]) =>
          `<button data-tab="${k}" class="fx-ins-tab text-xs font-semibold px-3 py-1.5 rounded-lg border">${l}</button>`).join('')}
      </div>
      <div id="fx-ins-msg" class="hidden mb-4 p-3 rounded-xl text-xs border"></div>

      <div id="fx-ins-risk">
        <div class="flex items-center gap-2 mb-3">
          <label class="text-xs text-slate-400">Show top</label>
          <select id="fx-ins-top" class="${FIELD}"><option>10</option><option selected>20</option><option>50</option><option>200</option></select>
          <span id="fx-ins-riskcount" class="text-xs text-slate-500 ml-auto"></span>
        </div>
        <div id="fx-ins-risktable" class="overflow-x-auto rounded-xl border border-slate-800 bg-slate-950/70"></div>
      </div>

      <div id="fx-ins-budget" class="hidden">
        <div class="flex flex-wrap items-end gap-3 mb-4">
          <div><label class="block text-[10px] uppercase font-bold text-slate-500 mb-1">Budget (\u20b9)</label>
            <input id="fx-b-amt" type="number" min="1" value="500000" class="${FIELD} w-36"></div>
          <div><label class="block text-[10px] uppercase font-bold text-slate-500 mb-1">Fallback cost per repair (\u20b9)</label>
            <input id="fx-b-fb" type="number" min="1" value="15000" class="${FIELD} w-36"></div>
          <button id="fx-b-run" class="bg-cyan-600 hover:bg-cyan-500 text-white font-bold py-1.5 px-4 rounded-lg text-xs">Optimize</button>
        </div>
        <div id="fx-b-out"><p class="text-xs text-slate-500">Enter a budget and press Optimize.</p></div>
      </div>

      <div id="fx-ins-map" class="hidden">
        <div id="fx-map-box" class="hidden rounded-xl border border-slate-800 mb-4" style="height:380px;width:100%"></div>
        <div id="fx-map-note" class="hidden text-xs text-amber-300 mb-3"></div>
        <h3 class="text-xs font-bold text-slate-300 uppercase tracking-wider mb-2">Road hotspots</h3>
        <div id="fx-map-roads" class="overflow-x-auto rounded-xl border border-slate-800 bg-slate-950/70"></div>
      </div>`;
    root.appendChild(sec);

    sec.querySelectorAll('.fx-ins-tab').forEach(b => b.addEventListener('click', () => showTab(b.dataset.tab)));
    $('#fx-ins-refresh').addEventListener('click', () => { loaded.risk = loaded.map = false; showTab(tab, true); });
    $('#fx-ins-top').addEventListener('change', () => loadRisk());
    $('#fx-b-run').addEventListener('click', runBudget);
    return true;
  }

  function showTab(name, force) {
    tab = name;
    ['risk', 'budget', 'map'].forEach(k => $('#fx-ins-' + k).classList.toggle('hidden', k !== name));
    document.querySelectorAll('.fx-ins-tab').forEach(b => {
      const on = b.dataset.tab === name;
      b.className = 'fx-ins-tab text-xs font-semibold px-3 py-1.5 rounded-lg border ' +
        (on ? 'bg-cyan-950 text-cyan-300 border-cyan-700' : 'bg-slate-950 text-slate-400 border-slate-800 hover:text-slate-200');
    });
    if (name === 'risk' && (force || !loaded.risk)) loadRisk();
    if (name === 'budget' && (force || !loaded.budget)) runBudget();
    if (name === 'map') { if (force || !loaded.map) loadMap(); else if (map) setTimeout(() => map.invalidateSize(), 50); }
  }

  // ---------- 1. risk ranking ----------
  async function loadRisk() {
    const box = $('#fx-ins-risktable');
    box.innerHTML = '<p class="p-4 text-xs text-slate-500">Scoring assets...</p>';
    try {
      const d = await getJSON('/api/insights/risk?top=' + ($('#fx-ins-top').value || 20));
      loaded.risk = true;
      msg(d.warnings, 'warn');
      $('#fx-ins-riskcount').textContent = d.total_assets ? `Showing ${d.assets.length} of ${d.total_assets} assets` : '';
      if (!d.assets.length) { box.innerHTML = '<p class="p-4 text-xs text-slate-500">No assets to rank.</p>'; return; }
      box.innerHTML = `<table class="w-full text-left text-sm text-slate-300">
        <thead class="text-xs uppercase bg-slate-900 text-slate-400"><tr>
          <th class="${TH}">#</th><th class="${TH}">Asset</th><th class="${TH}">Road</th><th class="${TH}">Risk</th>
          <th class="${TH}">12-mo failure</th><th class="${TH}">RUL</th><th class="${TH}">Condition</th>
          <th class="${TH}">Open</th><th class="${TH}">Why</th></tr></thead>
        <tbody>${d.assets.map((a, i) => `<tr class="border-t border-slate-800/60 align-top">
          <td class="${TD} text-slate-500">${i + 1}</td>
          <td class="${TD}"><span class="font-mono font-bold text-cyan-400">${esc(a.asset_id)}</span><span class="block text-[10px] text-slate-500">${esc(a.asset_type)}</span></td>
          <td class="${TD}">${esc(a.location)}</td>
          <td class="${TD}"><div class="flex items-center gap-2">${badge(a.risk_level)}<span class="font-mono text-xs">${esc(a.risk_score)}</span></div>
            <div class="w-24 h-1.5 bg-slate-800 rounded mt-1"><div class="h-1.5 rounded" style="width:${Math.min(100, Number(a.risk_score) || 0)}%;background:${COL[a.risk_level] || '#38bdf8'}"></div></div></td>
          <td class="${TD} font-mono">${esc(a.fail_prob_12m)}%</td>
          <td class="${TD} font-mono">${a.rul != null ? esc(a.rul) + ' y' : '--'}</td>
          <td class="${TD} font-mono">${a.condition != null ? esc(a.condition) + '/100' : '--'}</td>
          <td class="${TD} font-mono">${esc(a.active_complaints)}</td>
          <td class="${TD} text-xs text-slate-400">${a.reasons.map(esc).join('; ')}</td></tr>`).join('')}</tbody></table>`;
    } catch (e) {
      console.error('Risk ranking failed:', e);
      msg(['Risk ranking failed: ' + e.message], 'error');
      box.innerHTML = '';
    }
  }

  // ---------- 2. budget optimizer ----------
  async function runBudget() {
    const out = $('#fx-b-out');
    const b = Number($('#fx-b-amt').value), fb = Number($('#fx-b-fb').value);
    if (!(b > 0)) { out.innerHTML = '<p class="text-xs text-rose-400">Enter a budget above zero.</p>'; return; }
    out.innerHTML = '<p class="text-xs text-slate-500">Optimizing...</p>';
    try {
      const d = await getJSON('/api/insights/budget?budget=' + encodeURIComponent(b) + '&fallback_cost=' + encodeURIComponent(fb > 0 ? fb : 15000));
      loaded.budget = true;
      if (d.error) { out.innerHTML = `<p class="text-xs text-rose-400">${esc(d.error)}</p>`; return; }
      msg(d.warnings, 'warn');
      const imp = d.improvement_pct == null ? '--' : (d.improvement_pct > 0 ? '+' : '') + d.improvement_pct + '%';
      out.innerHTML = `
        <div class="grid grid-cols-2 md:grid-cols-4 gap-3 mb-4">
          ${stat('Repairs funded', `${d.selected_count} of ${d.total_candidates}`, `${d.deferred_count} deferred`)}
          ${stat('Spent', money(d.spent), `of ${money(d.budget)}, ${money(d.remaining)} left`)}
          ${stat('Risk removed', d.benefit_pct + '%', 'of all 12-month risk points')}
          ${stat('vs fix-worst-first', imp, 'risk removed compared with picking top risk first')}
        </div>
        ${d.selected.length ? `<div class="overflow-x-auto rounded-xl border border-slate-800 bg-slate-950/70"><table class="w-full text-left text-sm text-slate-300">
          <thead class="text-xs uppercase bg-slate-900 text-slate-400"><tr>
            <th class="${TH}">Asset</th><th class="${TH}">Road</th><th class="${TH}">Risk</th><th class="${TH}">12-mo failure</th>
            <th class="${TH}">Cost</th><th class="${TH}">Cost based on</th></tr></thead>
          <tbody>${d.selected.map(a => `<tr class="border-t border-slate-800/60">
            <td class="${TD} font-mono font-bold text-cyan-400">${esc(a.asset_id)}</td><td class="${TD}">${esc(a.location)}</td>
            <td class="${TD}">${badge(a.risk_level)} <span class="font-mono text-xs">${esc(a.risk_score)}</span></td>
            <td class="${TD} font-mono">${esc(a.fail_prob_12m)}%</td><td class="${TD} font-mono">${money(a.cost)}</td>
            <td class="${TD} text-xs text-slate-500">${esc(a.cost_source)}</td></tr>`).join('')}</tbody></table></div>` : ''}
        <p class="text-[10px] text-slate-500 mt-3">Risk points = 12-month failure probability x asset criticality. Costs come from each asset's repair history, then the network average;
          ${d.fallback_used} asset(s) had no cost data and use your fallback cost. This is a planning aid, not an engineering sign-off.</p>`;
    } catch (e) {
      console.error('Budget optimizer failed:', e);
      out.innerHTML = `<p class="text-xs text-rose-400">Budget optimizer failed: ${esc(e.message)}</p>`;
    }
  }

  // ---------- 3. map ----------
  function loadLeaflet() {
    if (window.L) return Promise.resolve();
    if (leafletReady) return leafletReady;
    leafletReady = new Promise((resolve, reject) => {
      const css = document.createElement('link');
      css.rel = 'stylesheet'; css.href = 'https://cdn.jsdelivr.net/npm/leaflet@1.9.4/dist/leaflet.css';
      document.head.appendChild(css);
      const s = document.createElement('script');
      s.src = 'https://cdn.jsdelivr.net/npm/leaflet@1.9.4/dist/leaflet.js';
      s.onload = resolve;
      s.onerror = () => { leafletReady = null; reject(new Error('The Leaflet map library could not load (no internet, or the CDN is blocked).')); };
      document.head.appendChild(s);
    });
    return leafletReady;
  }

  async function loadMap() {
    const roads = $('#fx-map-roads'), note = $('#fx-map-note'), box = $('#fx-map-box');
    roads.innerHTML = '<p class="p-4 text-xs text-slate-500">Loading...</p>';
    try {
      const d = await getJSON('/api/insights/map');
      loaded.map = true;
      msg(d.warnings, 'warn');
      roads.innerHTML = d.roads.length ? `<table class="w-full text-left text-sm text-slate-300">
        <thead class="text-xs uppercase bg-slate-900 text-slate-400"><tr>
          <th class="${TH}">Road</th><th class="${TH}">Assets</th><th class="${TH}">Avg risk</th><th class="${TH}">Highest risk</th>
          <th class="${TH}">Open complaints</th><th class="${TH}">Level</th></tr></thead>
        <tbody>${d.roads.map(r => `<tr class="border-t border-slate-800/60">
          <td class="${TD} font-semibold">${esc(r.location)}</td><td class="${TD} font-mono">${esc(r.assets)}</td>
          <td class="${TD} font-mono">${esc(r.avg_risk)}</td>
          <td class="${TD} font-mono">${esc(r.max_risk)} <span class="text-[10px] text-slate-500">${esc(r.top_asset)}</span></td>
          <td class="${TD} font-mono">${esc(r.active_complaints)}</td><td class="${TD}">${badge(r.level)}</td></tr>`).join('')}</tbody></table>`
        : '<p class="p-4 text-xs text-slate-500">No roads to show.</p>';

      if (!d.has_coords) {
        box.classList.add('hidden');
        note.classList.remove('hidden');
        note.textContent = 'No coordinates found, so only the road table is shown. Add latitude and longitude columns to A_assets.csv to plot assets on a map.';
        return;
      }
      note.classList.add('hidden');
      box.classList.remove('hidden');
      await loadLeaflet();
      if (!map) {
        map = L.map('fx-map-box');
        L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png', { maxZoom: 19, attribution: '&copy; OpenStreetMap contributors' }).addTo(map);
        markers = L.layerGroup().addTo(map);
      }
      markers.clearLayers();
      const pts = [];
      d.points.forEach(p => {
        const c = COL[p.risk_level] || '#38bdf8';
        L.circleMarker([p.lat, p.lon], {
          radius: 6 + Math.min(10, (Number(p.risk_score) || 0) / 10) + Math.min(6, p.active_complaints * 2),
          color: c, fillColor: c, fillOpacity: 0.55, weight: 2
        }).bindPopup(`<b>${esc(p.asset_id)}</b> (${esc(p.asset_type)})<br>${esc(p.location)}<br>Risk ${esc(p.risk_score)} - ${esc(p.risk_level)}<br>12-month failure ${esc(p.fail_prob_12m)}%<br>Open complaints: ${esc(p.active_complaints)}`)
          .addTo(markers);
        pts.push([p.lat, p.lon]);
      });
      map.invalidateSize();
      if (pts.length) map.fitBounds(pts, { padding: [30, 30], maxZoom: 17 });
    } catch (e) {
      console.error('Map failed:', e);
      msg(['Map failed: ' + e.message], 'error');
      if (box) box.classList.add('hidden');
    }
  }

  // ---------- boot ----------
  function boot() {
    try { inject(); showTab('risk'); }
    catch (e) { console.error('insights.js failed to start:', e); }
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', boot); else boot();
})();


/* ===== Part 2: live updates (separate block, so a problem here can't stop Part 1) ===== */
(function () {
  'use strict';
  const POLL_MS = 8000;
  let version = null, busy = false, refreshTimer = null;
  const el = id => document.getElementById(id);
  const stamp = t => { const s = el('fx-live'); if (s) s.textContent = t; };

  function refresh() {
    const b = el('fx-ins-refresh');
    if (b) { b.click(); stamp('\u25CF Live \u00b7 updated ' + new Date().toLocaleTimeString()); }
  }
  function refreshSoon(ms) { clearTimeout(refreshTimer); refreshTimer = setTimeout(refresh, ms); }

  async function check() {
    if (document.hidden || busy) return;
    busy = true;
    try {
      const res = await fetch('/api/insights/version', { cache: 'no-store' });
      if (!res.ok) throw new Error('HTTP ' + res.status);
      const v = (await res.json()).version;
      if (version !== null && v !== version) refresh();
      version = v;
    } catch (e) {
      stamp('\u25CB Live updates offline');
    } finally { busy = false; }
  }

  const origFetch = window.fetch.bind(window);
  window.fetch = async function (input, init) {
    const res = await origFetch(input, init);
    try {
      const url = String(typeof input === 'string' ? input : (input && (input.url || input.href)) || '');
      const method = String((init && init.method) || (input && input.method) || 'GET').toUpperCase();
      if (method !== 'GET' && res.ok && url.includes('/api/') && !url.includes('/api/insights')) refreshSoon(1200);
    } catch (_) { /* never break the page's own requests */ }
    return res;
  };

  function setup() {
    const btn = el('fx-ins-refresh');
    if (!btn) return false;
    if (!el('fx-live')) {
      const tag = document.createElement('span');
      tag.id = 'fx-live';
      tag.className = 'text-[10px] text-emerald-400 ml-auto mr-2';
      tag.textContent = '\u25CF Live';
      btn.parentNode.insertBefore(tag, btn);
    }
    let t;
    ['fx-b-amt', 'fx-b-fb'].forEach(id => {
      const inp = el(id);
      if (inp) inp.addEventListener('input', () => { clearTimeout(t); t = setTimeout(() => el('fx-b-run').click(), 500); });
    });
    return true;
  }

  let tries = 0;
  const wait = setInterval(() => {
    if (setup()) {
      clearInterval(wait);
      check();
      setInterval(check, POLL_MS);
      document.addEventListener('visibilitychange', () => { if (!document.hidden) check(); });
    } else if (++tries > 50) clearInterval(wait);
  }, 200);
})();