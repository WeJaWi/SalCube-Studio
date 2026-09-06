/* Read-only, scoped analytics. No third-party chart scripts or billing estimates. */
(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  const fmt = n => n == null ? '—' : new Intl.NumberFormat(undefined, {maximumFractionDigits: 1}).format(n);
  const duration = n => n == null ? '—' : n >= 3600 ? `${fmt(n / 3600)} hr` : n >= 60 ? `${fmt(n / 60)} min` : `${fmt(n)} sec`;
  const element = (tag, text, cls) => { const e = document.createElement(tag); if (text != null) e.textContent = text; if (cls) e.className = cls; return e; };
  let data = null, controller = null;
  function chart() {
    if (!data) return;
    const metric = $('chartMetric').value, rows = data.daily, maximum = Math.max(1, ...rows.map(r => r[metric]));
    const noun = metric === 'jobs' ? 'jobs' : 'characters';
    $('activityChart').replaceChildren();
    rows.forEach(row => {
      const label = `${row.date}: ${fmt(row[metric])} ${noun}`;
      const slot = element('div', null, 'stats-bar-slot'); slot.tabIndex = 0; slot.title = label; slot.setAttribute('aria-label', label);
      const bar = element('div', null, 'stats-bar'); bar.style.height = `${row[metric] / maximum * 100}%`;
      slot.append(bar); slot.addEventListener('focus', () => $('chartSummary').textContent = label); slot.addEventListener('mouseenter', () => $('chartSummary').textContent = label);
      $('activityChart').append(slot);
    });
    $('chartFrom').textContent = data.period.from; $('chartTo').textContent = data.period.to;
    $('chartSummary').textContent = `${fmt(rows.reduce((sum, r) => sum + r[metric], 0))} ${noun} across ${data.period.days} days. Select a bar for its daily total.`;
  }
  function draw(value) {
    data = value;
    const jobs = data.jobs, ai = data.ai33, kie = data.kie;
    $('metricJobs').textContent = fmt(jobs.total);
    $('detailJobs').textContent = `${fmt(jobs.statuses.done || 0)} done · ${fmt(jobs.statuses.error || 0)} failed · ${fmt(jobs.statuses.retry_wait || 0)} retrying`;
    $('metricChars').textContent = fmt(ai.daily_average_characters);
    $('detailChars').textContent = `${fmt(ai.submitted_characters)} submitted characters in this period`;
    $('metricKie').textContent = fmt(kie.reported_credits);
    $('detailKie').textContent = kie.tasks_with_reported_credits ? `Credits reported for ${fmt(kie.tasks_with_reported_credits)} of ${fmt(kie.tasks)} tasks` : `No credit amounts reported · ${fmt(kie.tasks)} recorded tasks`;
    $('metricRender').textContent = duration(jobs.median_render_seconds);
    $('detailRender').textContent = jobs.render_samples ? `${fmt(jobs.render_samples)} measured completed renders` : 'New completions will build this history';
    const chosen = $('statsProfile').value;
    $('statsProfile').replaceChildren(new Option('All accessible profiles', ''), ...data.filters.profiles.map(p => new Option(p, p)));
    $('statsProfile').value = chosen;
    $('profileRows').replaceChildren();
    data.profiles.forEach(p => {
      const row = element('tr');
      [p.profile, fmt(p.jobs), fmt(p.done), fmt(p.error), fmt(p.retrying), fmt(p.daily_average_characters), duration(p.median_render_seconds)].forEach((v, i) => row.append(element('td', v, i === 0 ? 'stats-profile-name' : '')));
      $('profileRows').append(row);
    });
    if (!data.profiles.length) { const row = element('tr'), cell = element('td', 'No recorded activity in this period.', 'stats-empty'); cell.colSpan = 7; row.append(cell); $('profileRows').append(row); }
    $('statusRows').replaceChildren();
    Object.entries(jobs.current).sort((a, b) => b[1] - a[1]).forEach(([name, count]) => {
      const row = element('div', null, 'stats-state'); row.append(element('span', name.replaceAll('_', ' ')), element('strong', fmt(count))); $('statusRows').append(row);
    });
    if (!Object.keys(jobs.current).length) $('statusRows').append(element('div', 'No jobs in this scope.', 'stats-empty'));
    $('stageRows').replaceChildren();
    const maxStage = Math.max(1, ...data.stages.map(s => s.seconds));
    data.stages.slice(0, 8).forEach(s => {
      const row = element('div', null, 'stats-stage'), heading = element('div', null, 'stats-state'), track = element('div', null, 'stats-track'), fill = element('i');
      heading.append(element('span', s.stage.replaceAll('_', ' ')), element('strong', duration(s.seconds))); fill.style.width = `${s.seconds / maxStage * 100}%`; track.append(fill); row.append(heading, track); $('stageRows').append(row);
    });
    if (!data.stages.length) $('stageRows').append(element('div', 'No measured stage timings for this selection yet.', 'stats-empty'));
    $('statsNotes').replaceChildren(...data.notes.map(note => element('li', note)));
    chart();
  }
  async function refresh() {
    if (controller) controller.abort();
    const current = new AbortController(); controller = current;
    $('statsStatus').textContent = 'Updating statistics…'; $('statsRefresh').disabled = true;
    try {
      const query = new URLSearchParams({days: $('statsDays').value, profile: $('statsProfile').value});
      const response = await fetch(`/api/stats?${query}`, {signal: current.signal, cache: 'no-store'});
      if (!response.ok) throw new Error(response.status === 401 ? 'Your session expired. Sign in again.' : 'Statistics could not be loaded.');
      draw(await response.json());
      $('statsStatus').dataset.error = 'false';
      $('statsStatus').textContent = `Updated ${new Date().toLocaleTimeString([], {hour: '2-digit', minute: '2-digit'})} · Refreshes every 30 seconds · Locally recorded usage`;
    } catch (error) {
      if (error.name !== 'AbortError') { $('statsStatus').dataset.error = 'true'; $('statsStatus').textContent = `${error.message}${data ? ' Previously loaded figures are still shown.' : ' Try Refresh.'}`; }
    } finally { if (controller === current) { controller = null; $('statsRefresh').disabled = false; } }
  }
  $('statsDays').addEventListener('change', refresh); $('statsProfile').addEventListener('change', refresh);
  $('statsRefresh').addEventListener('click', refresh); $('chartMetric').addEventListener('change', chart);
  const ask = $('statsAsk');
  if (ask) ask.addEventListener('click', async () => {
    const question = $('statsQuestion').value.trim(); if (!question) { $('statsQuestion').focus(); return; }
    ask.disabled = true; $('statsAnswer').textContent = 'SalCube is checking the statistics…';
    try {
      const response = await fetch('/api/ops/chat', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({question, days: Number($('statsDays').value), profile: $('statsProfile').value})});
      const result = await response.json(); if (!response.ok) throw new Error(result.error || 'Could not reach SalCube.');
      $('statsAnswer').textContent = result.answer;
    } catch (error) { $('statsAnswer').textContent = error.message; }
    finally { ask.disabled = false; }
  });
  setInterval(() => { if (!document.hidden && !controller) refresh(); }, 30000);
  document.addEventListener('visibilitychange', () => { if (!document.hidden) refresh(); });
  refresh();
})();
