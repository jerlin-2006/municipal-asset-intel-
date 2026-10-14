/* Resolution recommendations + status timeline (add-on).
   Shows a detail panel under the work-order queue for the selected complaint.
   Read-only: it only calls GET /api/workorders/{id}/detail. */
(function () {
  'use strict';
  const $ = (s, r = document) => r.querySelector(s);
  const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const money = n => (n == null ? '--' : '\u20b9' + Math.round(n).toLocaleString('en-IN'));
  const fmt = t => {
    if (!t) return '';
    const d = new Date(t);
    return isNaN(d) ? esc(t) : d.toLocaleString('en-IN', { day: '2-digit', month: 'short', year: 'numeric', hour: '2-digit', minute: '2-digit' });
  };
  const LEVEL = { High: 'text-rose-400', Medium: 'text-amber-400', Low: 'text-emerald-400', Unassigned: 'text-slate-400' };

  let lastId = null, seq = 0, timer = null;

  function currentId() {
    try { if (typeof selectedId !== 'undefined' && selectedId) return selectedId; } catch (_) { /* ignore */ }
    return lastId;
  }

  function inject() {
    if ($('#fx-detail')) return true;
    const tbody = $('#workorder-table-body');
    const sec = tbody && tbody.closest('section');
    if (!sec) return false;
    const panel = document.createElement('section');
    panel.id = 'fx-detail';
    panel.className = 'bg-slate-900/90 border border-slate-800 rounded-2xl p-6 shadow-xl';
    panel.innerHTML = `
      <h2 class="text-lg font-bold text-white flex items-center gap-2 mb-1"><span>\u{1f6e0}\ufe0f</span> Recommended Resolution & Status Timeline</h2>
      <p class="text-xs text-slate-400 mb-4">Click a work order above. Steps are standard field practice chosen from the defect type, asset type, priority and asset condition. Have your engineer review them before dispatch.</p>
      <div id="fx-detail-body" class="text-sm text-slate-300"><p class="text-xs text-slate-500">No work order selected.</p></div>`;
    sec.after(panel);
    return true;
  }

  const list = (items, ordered) => {
    const tag = ordered ? 'ol' : 'ul', cls = ordered ? 'list-decimal' : 'list-disc';
    return `<${tag} class="${cls} ml-5 space-y-1 text-xs text-slate-300">${(items || []).map(i => `<li>${esc(i)}</li>`).join('')}</${tag}>`;
  };
  const card = (title, inner, extra = '') =>
    `<div class="bg-slate-950/80 border border-slate-800/80 rounded-xl p-4 ${extra}"><h3 class="text-xs font-bold text-slate-300 uppercase tracking-wider mb-2">${title}</h3>${inner}</div>`;

  function stepper(stages) {
    return `<div class="flex items-start">${stages.map((s, i) => {
      const circle = s.state === 'done' ? 'bg-emerald-500 text-white'
        : s.state === 'current' ? 'bg-cyan-500 text-white ring-4 ring-cyan-500/30' : 'bg-slate-800 text-slate-500';
      const label = s.state === 'pending' ? 'text-slate-500' : 'text-slate-200';
      const line = i < stages.length - 1
        ? `<div class="flex-1 h-0.5 mt-3.5 ${s.state === 'done' ? 'bg-emerald-500' : 'bg-slate-700'}"></div>` : '';
      return `<div class="flex flex-col items-center text-center w-24 shrink-0">
          <div class="w-7 h-7 rounded-full flex items-center justify-center text-xs font-bold ${circle}">${s.state === 'done' ? '\u2713' : i + 1}</div>
          <span class="mt-1 text-xs font-semibold ${label}">${esc(s.name)}</span>
          <span class="text-[10px] text-slate-500">${fmt(s.time)}</span>
        </div>${line}`;
    }).join('')}</div>`;
  }

  function eventLog(events) {
    return `<ol class="relative border-l border-slate-700 ml-2 space-y-4">${events.map((e, i) => `
      <li class="ml-4">
        <span class="absolute -left-1.5 w-3 h-3 rounded-full mt-1 ${i === events.length - 1 ? 'bg-cyan-400' : 'bg-slate-600'}"></span>
        <p class="text-xs font-semibold text-slate-200">${esc(e.title)}</p>
        <p class="text-[10px] text-slate-500">${fmt(e.time)}</p>
        ${e.note ? `<p class="text-xs text-slate-400 mt-0.5">${esc(e.note)}</p>` : ''}
      </li>`).join('')}</ol>`;
  }

  function render(d) {
    const s = d.solution, body = $('#fx-detail-body');
    if (!body) return;
    const est = s.estimate
      ? `Typical cost ${money(s.estimate.cost)}${s.estimate.downtime != null ? `, downtime ${s.estimate.downtime} h` : ''} (from ${s.estimate.based_on} past repairs)`
      : 'No cost estimate available for this defect.';
    const due = s.due_at ? `Due by ${fmt(s.due_at)}` : '';
    const overdue = s.overdue ? '<span class="ml-2 px-2 py-0.5 rounded-full text-[10px] font-bold bg-rose-950 text-rose-300 border border-rose-800">OVERDUE</span>' : '';
    const notes = (s.notes || []).map(n => `<p class="text-xs text-amber-300 mt-1">\u2022 ${esc(n)}</p>`).join('');

    body.innerHTML = `
      <div class="flex flex-wrap items-center gap-x-3 gap-y-1 mb-4">
        <span class="font-mono font-bold text-cyan-400">${esc(d.asset_id || 'Unmatched')}</span>
        <span class="text-xs text-slate-400">${esc(d.id)} \u2022 ${esc(d.location)} \u2022 ${esc(s.category)}</span>
        <span class="text-xs font-semibold ${LEVEL[d.priority_level] || LEVEL.Unassigned}">${esc(d.priority_level)} ${d.asset_id ? '(' + esc(d.priority_score) + ')' : ''}</span>
        ${overdue}
      </div>
      <div class="mb-5 overflow-x-auto">${stepper(d.stages)}</div>
      <div class="grid grid-cols-1 lg:grid-cols-2 gap-4">
        ${card('1. Immediate actions', list(s.immediate, true))}
        ${card('2. Repair steps', list(s.repair, true))}
        ${card('Materials & tools', list(s.materials, false))}
        ${card('Safety', list(s.safety, false))}
        ${card('Verify & close', list(s.verify, true))}
        ${card('Prevent a repeat', list(s.prevent, false))}
      </div>
      <div class="mt-4 text-xs text-slate-400">
        <p><b class="text-slate-300">Crew:</b> ${esc(s.crew || '--')} &nbsp;\u2022&nbsp; <b class="text-slate-300">Target:</b> ${esc(s.target)} ${due ? '&nbsp;\u2022&nbsp; ' + esc(due) : ''}</p>
        <p class="mt-1"><b class="text-slate-300">Estimate:</b> ${esc(est)}</p>
        ${notes}
      </div>
      <h3 class="text-xs font-bold text-slate-300 uppercase tracking-wider mt-6 mb-3">Status timeline</h3>
      ${eventLog(d.events)}`;
  }

  async function load(id) {
    const my = ++seq, body = $('#fx-detail-body');
    try {
      const res = await fetch('/api/workorders/' + encodeURIComponent(id) + '/detail');
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const d = await res.json();
      if (my !== seq) return;                       // a newer click already replaced this request
      render(d);
    } catch (e) {
      console.error('Detail panel failed:', e);
      if (body) body.innerHTML = `<p class="text-xs text-rose-400">Could not load details for ${esc(id)}: ${esc(e.message)}</p>`;
    }
  }

  function refresh() {
    const id = currentId();
    if (id) { lastId = id; load(id); }
  }

  function boot() {
    if (!inject()) return;
    const tbody = $('#workorder-table-body');
    // row click: core code sets selectedId first, then this runs
    tbody.addEventListener('click', e => {
      if (e.target.closest('select, option, a, button')) return;
      setTimeout(refresh, 0);
    });
    // rows are rebuilt after every queue refresh or status change
    new MutationObserver(() => { clearTimeout(timer); timer = setTimeout(refresh, 250); }).observe(tbody, { childList: true });
    refresh();
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', boot); else boot();
})();