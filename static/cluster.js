(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  const el = (tag, text, cls) => { const node = document.createElement(tag); if (text !== undefined && text !== null) node.textContent = text; if (cls) node.className = cls; return node; };
  const fmt = value => value === null || value === undefined ? '—' : new Intl.NumberFormat(undefined, {maximumFractionDigits: 1}).format(value);
  const when = value => { if (!value) return 'unknown'; const date = typeof value === 'number' ? new Date(value * 1000) : new Date(value); return Number.isNaN(date.valueOf()) ? String(value) : date.toLocaleString([], {month:'short', day:'numeric', hour:'2-digit', minute:'2-digit'}); };
  const activeStates = new Set(['queued', 'researching', 'awaiting_title', 'running', 'awaiting_approval', 'dispatched', 'uploading', 'retry_wait', 'error']);
  let data = null, loading = false;

  async function request(url, options = {}) {
    const response = await fetch(url, {cache: 'no-store', ...options});
    const body = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(body.error || `Request failed (${response.status})`);
    return body;
  }

  function meter(label, percent, detail) {
    const row = el('div', null, 'metric-row'), track = el('div', null, 'meter'), fill = el('i');
    const bounded = typeof percent === 'number' ? Math.max(0, Math.min(100, percent)) : 0;
    fill.style.width = `${bounded}%`; track.append(fill);
    row.append(el('span', label), track, el('b', detail)); return row;
  }

  async function setPolicy(workerId, card) {
    const button = card.querySelector('[data-action=policy]');
    button.disabled = true;
    try {
      await request(`/api/workers/${encodeURIComponent(workerId)}/policy`, {
        method:'POST', headers:{'Content-Type':'application/json'},
        body:JSON.stringify({mode:card.querySelector('[data-field=mode]').value,
          max_jobs:Number(card.querySelector('[data-field=slots]').value)})
      });
      await refresh();
    } catch (error) { $('clusterStatus').dataset.error = 'true'; $('clusterStatus').textContent = error.message; }
    finally { button.disabled = false; }
  }

  async function assign(workerId, card) {
    const jobId = card.querySelector('[data-field=job]').value;
    if (!jobId) return;
    const button = card.querySelector('[data-action=assign]'); button.disabled = true;
    try {
      await request(`/api/dispatch/${encodeURIComponent(jobId)}`, {
        method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({worker_id:workerId})
      });
      await refresh();
    } catch (error) { $('clusterStatus').dataset.error = 'true'; $('clusterStatus').textContent = error.message; }
    finally { button.disabled = false; }
  }

  function nodeCard(node, queued) {
    const card = el('article', null, 'node-card'); card.dataset.health = node.health || 'offline';
    const head = el('div', null, 'node-head'), title = el('div', null, 'node-title');
    title.append(el('strong', node.name || node.worker_id), el('small', `${node.role || 'worker'} · ${node.worker_id}`));
    head.append(el('i', null, 'node-dot'), title, el('span', node.health || 'unknown', 'health-pill')); card.append(head);
    const report = node.node || {}, cpu = report.cpu || {}, memory = report.memory || {};
    const cpuDetail = cpu.percent == null ? (cpu.load_1m == null ? 'waiting' : `load ${fmt(cpu.load_1m)}`) : `${fmt(cpu.percent)}%`;
    card.append(meter('CPU', cpu.percent, cpuDetail));
    const memoryPercent = memory.used_percent;
    const memoryDetail = memoryPercent == null ? ((node.metrics || {}).render?.available_memory_gb != null ? `${fmt(node.metrics.render.available_memory_gb)} GB free` : 'waiting') : `${fmt(memoryPercent)}%`;
    card.append(meter('RAM', memoryPercent, memoryDetail));
    const gpu = report.gpu || {}, device = (gpu.devices || [])[0];
    card.append(meter('GPU', device?.utilization_percent, device ? `${fmt(device.utilization_percent)}%` : (node.hardware?.hw_encoder || 'unknown')));
    const cap = node.capacity || {}, facts = el('div', null, 'node-facts');
    facts.append(el('div', `Slots: ${fmt(cap.active || 0)} active · ${fmt(cap.effective_free || 0)} free · ${fmt(cap.effective_max_jobs || cap.max_jobs || 0)} total`));
    facts.append(el('div', `Hardware: ${fmt(node.hardware?.cpu_cores)} CPU · ${fmt(node.hardware?.ram_gb)} GB · ${node.hardware?.hw_encoder || 'software H.264'}`));
    facts.append(el('div', node.online ? `Heartbeat ${fmt(node.age_seconds)}s ago` : `Last seen ${when(node.last_seen)}`));
    facts.append(el('div', `${node.agent?.name || 'Reporter'}: ${node.agent?.state || 'unknown'}`, 'node-agent')); card.append(facts);
    (node.jobs || []).slice(0, 3).forEach(job => {
      const row = el('div', null, 'node-job'); row.append(el('strong', job.title || job.job_id), el('span', `${String(job.status || '').replaceAll('_',' ')} · ${fmt(job.progress_pct)}% · ${job.step || 'waiting'}`)); card.append(row);
    });
    if (!(node.jobs || []).length) card.append(el('div', node.online ? 'No active jobs' : 'Worker is unreachable', 'node-job'));
    if (node.role === 'worker') {
      const actions = el('div', null, 'node-actions');
      const mode = el('select'); mode.dataset.field = 'mode'; ['active','draining','paused'].forEach(value => { const option = new Option(value, value); option.selected = value === (node.policy?.mode || 'active'); mode.append(option); });
      const slots = el('select'); slots.dataset.field = 'slots'; for (let value = 1; value <= Math.max(1, Number(cap.max_jobs || 1)); value++) { const option = new Option(`${value} slot${value === 1 ? '' : 's'}`, value); option.selected = value === Number(cap.effective_max_jobs || cap.max_jobs || 1); slots.append(option); }
      const apply = el('button', 'Apply'); apply.type = 'button'; apply.dataset.action = 'policy'; apply.addEventListener('click', () => setPolicy(node.worker_id, card)); actions.append(mode, slots, apply);
      if (queued.length && node.online && Number(cap.effective_free || 0) > 0 && node.policy?.mode === 'active') {
        const assignRow = el('div', null, 'assign-row'), jobs = el('select'); jobs.dataset.field = 'job'; jobs.append(new Option('Choose queued job…', '')); queued.forEach(job => jobs.append(new Option(job.title || job.job_id, job.job_id)));
        const send = el('button', 'Send'); send.type = 'button'; send.dataset.action = 'assign'; send.addEventListener('click', () => assign(node.worker_id, card)); assignRow.append(jobs, send); actions.append(assignRow);
      }
      card.append(actions);
    }
    return card;
  }

  function drawJobs() {
    if (!data) return;
    const filter = $('jobFilter').value;
    const jobs = (data.job_history || []).filter(job => filter === 'all' || (filter === 'error' ? job.status === 'error' : activeStates.has(job.status)));
    $('jobRows').replaceChildren();
    jobs.slice(0, 80).forEach(job => {
      const row = el('tr'), title = el('td', null, 'job-title'); title.append(el('strong', job.title || job.job_id), el('small', `${job.profile || 'profile'} · ${job.job_id}`));
      if (job.error) title.append(el('span', job.error, 'job-error'));
      row.append(title, el('td', job.worker_id || 'main'), el('td', String(job.status || 'unknown').replaceAll('_',' '), 'status-label'), el('td', `${fmt(job.progress_pct || 0)}%`)); $('jobRows').append(row);
    });
    if (!jobs.length) { const row = el('tr'), cell = el('td', 'No jobs match this view.', 'empty-row'); cell.colSpan = 4; row.append(cell); $('jobRows').append(row); }
  }

  function draw(value) {
    data = value; const summary = value.summary || {}, observer = value.observer || {}, update = value.update || {};
    $('sumOnline').textContent = fmt(summary.online); $('sumTotal').textContent = `of ${fmt(summary.total)} known nodes`;
    $('sumSlots').textContent = fmt(summary.free_slots); $('sumJobs').textContent = fmt(summary.active_jobs);
    $('sumObserver').textContent = observer.running ? 'Watching' : 'Starting'; $('sumModel').textContent = observer.model || 'SalCube';
    $('sumUpdate').textContent = update.auto_update ? `Git: ${String(update.state || 'checking').replaceAll('_', ' ')}` : 'Git: manual updates';
    $('sumUpdate').dataset.state = update.state || 'manual';
    $('agentState').textContent = observer.running ? `Watching every ${fmt((observer.interval_seconds || 300) / 60)} minutes · Git ${String(update.state || 'checking').replaceAll('_', ' ')}` : 'Starting';
    const queued = (value.job_history || []).filter(job => job.status === 'queued');
    $('nodeGrid').replaceChildren(...(value.nodes || []).map(node => nodeCard(node, queued)));
    drawJobs();
    $('eventRows').replaceChildren();
    (value.events || []).slice(0, 24).forEach(event => { const row = el('div', null, 'event-row'); row.dataset.severity = event.severity || 'info'; row.append(el('time', when(event.at)), el('span', event.worker_id || 'cluster'), el('p', event.message || event.kind)); $('eventRows').append(row); });
    if (!(value.events || []).length) $('eventRows').append(el('div', 'No cluster transitions recorded yet.', 'empty-row'));
    $('processRows').replaceChildren();
    (value.nodes || []).forEach(node => ((node.node || {}).processes || []).forEach(process => { const row = el('div', null, 'compact-row'); row.append(el('strong', `${node.name}: ${process.name || 'process'}`), el('span', `PID ${fmt(process.pid)} · ${fmt(process.rss_mb)} MB · ${process.status || 'unknown'}`)); $('processRows').append(row); }));
    if (!$('processRows').children.length) $('processRows').append(el('div', 'Awaiting detailed worker telemetry.', 'empty-row'));
    $('changeRows').replaceChildren();
    (value.events || []).filter(event => event.kind === 'file_change').slice(0, 20).forEach(event => { const row = el('div', null, 'compact-row'); row.append(el('strong', event.worker_id || 'node'), el('span', `${event.message} · ${when(event.at)}`)); $('changeRows').append(row); });
    if (!$('changeRows').children.length) $('changeRows').append(el('div', 'No recent monitored changes.', 'empty-row'));
    $('clusterNotes').replaceChildren(...(value.notes || []).map(note => el('li', note)));
  }

  async function refresh() {
    if (loading) return; loading = true; $('clusterRefresh').disabled = true;
    try {
      draw(await request('/api/cluster'));
      $('clusterStatus').dataset.error = 'false'; $('clusterStatus').textContent = `Live snapshot · updated ${new Date().toLocaleTimeString([], {hour:'2-digit', minute:'2-digit', second:'2-digit'})} · refreshes every 10 seconds`;
    } catch (error) { $('clusterStatus').dataset.error = 'true'; $('clusterStatus').textContent = `${error.message}${data ? ' · Last good snapshot remains visible.' : ''}`; }
    finally { loading = false; $('clusterRefresh').disabled = false; }
  }

  $('clusterRefresh').addEventListener('click', refresh); $('jobFilter').addEventListener('change', drawJobs);
  $('clusterAsk').addEventListener('click', async () => {
    const question = $('clusterQuestion').value.trim(); if (!question) { $('clusterQuestion').focus(); return; }
    const button = $('clusterAsk'); button.disabled = true; $('clusterAnswer').textContent = 'SalCube is reading the cluster…';
    try { const result = await request('/api/ops/chat', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({question})}); $('clusterAnswer').textContent = result.answer; }
    catch (error) { $('clusterAnswer').textContent = error.message; } finally { button.disabled = false; }
  });
  setInterval(() => { if (!document.hidden) refresh(); }, 10000);
  document.addEventListener('visibilitychange', () => { if (!document.hidden) refresh(); });
  refresh();
})();
