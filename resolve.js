/* Resolve mechanism (add-on). When a status dropdown in the queue is set to "Resolved",
   ask for actual cost / downtime / note and POST to /api/workorders/{id}/resolve.
   Does not edit features.js or index.html logic; it intercepts the change event first. */
(function () {
  'use strict';

  const toNum = s => {
    if (s === null || s.trim() === '') return null;
    const n = Number(s.replace(/[^\d.\-]/g, ''));
    return Number.isFinite(n) ? n : null;
  };

  const refresh = () => { if (window.fetchQueue) window.fetchQueue(); };

  document.addEventListener('change', async e => {
    const sel = e.target;
    if (!sel.matches || !sel.matches('#workorder-table-body select')) return;
    if (sel.value !== 'Resolved') return;

    e.stopImmediatePropagation();                 // stop features.js's plain status update
    const tr = sel.closest('tr');
    const id = tr && tr.dataset.id;
    if (!id) return;

    const cost = prompt(`Resolving ${id}\n\nActual repair cost in \u20b9 (leave blank if unknown):`, '');
    if (cost === null) { refresh(); return; }     // cancelled -> rebuild rows from server state
    const hrs = prompt('Downtime in hours (leave blank if unknown):', '');
    if (hrs === null) { refresh(); return; }
    const note = prompt('Resolution note (optional):', '');
    if (note === null) { refresh(); return; }

    try {
      const res = await fetch(`/api/workorders/${encodeURIComponent(id)}/resolve`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ repair_cost: toNum(cost), downtime_hours: toNum(hrs), note: note.trim() })
      });
      if (!res.ok) {
        const err = await res.json().catch(() => ({}));
        throw new Error(err.detail || `HTTP ${res.status}`);
      }
    } catch (err) {
      console.error('Resolve failed:', err);
      alert('Could not resolve ' + id + ': ' + err.message);
    }
    refresh();                                    // re-read status, KPIs and rows from the server
  }, true);                                       // capture phase: runs before the dropdown's own handler
})();