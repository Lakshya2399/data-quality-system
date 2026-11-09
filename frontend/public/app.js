/* Data Quality System UI - vanilla JS, no build step.
 *
 * SECURITY: rule descriptions and justifications come from an LLM and failed-row values come from
 * uploaded CSVs, so NOTHING dynamic is ever put into the page as HTML. Every node is created with
 * h() below, which only produces text nodes. Do not introduce innerHTML / insertAdjacentHTML.
 */
(() => {
  'use strict';

  const POLL_MS = Number(window.DQ_POLL_MS) || 1500;
  const MAX_POLL_FAILURES = 3;
  const MAX_REASONS = 10;
  const TERMINAL = new Set(['completed', 'failed']);
  const SCORE_LABEL = { 1: 'poor', 2: 'marginal', 3: 'useful', 4: 'important', 5: 'critical' };
  const LABELS = { pipeline: 'Run sample pipeline', execute: 'Execute rules' };

  const $ = (id) => document.getElementById(id);

  const state = {
    runId: null, run: null, model: null, rules: null, runs: [],
    executions: [], execId: null, execution: null,
    minScore: 4, elapsed: 0, busy: { pipeline: false, execute: false },
    runToken: 0, execToken: 0,
  };

  // ------------------------------------------------------------------ DOM helpers (text only)
  function h(tag, props = {}, ...children) {
    const el = document.createElement(tag);
    for (const [key, value] of Object.entries(props)) {
      if (value === null || value === undefined || value === false) continue;
      if (key === 'class') el.className = value;
      else if (key === 'text') el.textContent = value;
      else if (key.startsWith('on') && typeof value === 'function') el.addEventListener(key.slice(2), value);
      else el.setAttribute(key, value === true ? '' : String(value));
    }
    for (const child of children.flat()) {
      if (child === null || child === undefined || child === false) continue;
      el.append(child.nodeType ? child : document.createTextNode(String(child)));
    }
    return el;
  }

  const safeClass = (s) => (/^[a-z_-]+$/.test(String(s)) ? String(s) : 'unknown');
  const plural = (n, word) => `${n} ${word}${n === 1 ? '' : 's'}`;
  const pct = (part, whole) => (whole ? Math.round((1000 * part) / whole) / 10 : 0);

  function fmtTime(iso) {
    if (!iso) return '';
    const d = new Date(iso);
    return Number.isNaN(d.getTime()) ? String(iso) : d.toLocaleString();
  }

  function badge(score) {
    if (!Number.isInteger(score) || score < 1 || score > 5) {
      return h('span', { class: 'score-badge score-none', title: 'not rated', text: '–' });
    }
    return h('span', { class: `score-badge score-${score}`, title: `${score} – ${SCORE_LABEL[score]}`, text: String(score) });
  }

  // ------------------------------------------------------------------ API
  class ApiError extends Error {
    constructor(message, status) { super(message); this.status = status; }
  }

  function describeError(body, text, res) {
    const d = body && body.detail;
    if (typeof d === 'string') return d;
    if (Array.isArray(d)) {
      return d.map((e) => `${(e.loc || []).filter((p) => p !== 'query' && p !== 'body').join('.')}: ${e.msg}`).join('; ');
    }
    return `${res.status} ${res.statusText || 'error'}${text && !body ? `: ${text.slice(0, 200)}` : ''}`;
  }

  async function api(path, options) {
    let res;
    try {
      res = await fetch(`/api${path}`, options);
    } catch (err) {
      throw new ApiError('Cannot reach the UI server. Is it still running?', 0);
    }
    const text = await res.text();
    let body = null;
    try { body = text ? JSON.parse(text) : null; } catch (err) { body = null; }
    if (!res.ok) throw new ApiError(describeError(body, text, res), res.status);
    return body;
  }

  // ------------------------------------------------------------------ banner / buttons / hash
  function showBanner(message, kind = 'error') {
    const el = $('banner');
    el.textContent = message;
    el.className = kind === 'info' ? 'banner info' : 'banner';
    el.hidden = false;
  }

  function clearBanner() {
    const el = $('banner');
    el.hidden = true;
    el.textContent = '';
  }

  function updateButtons() {
    const completed = Boolean(state.run && state.run.status === 'completed');
    $('run-pipeline').disabled = state.busy.pipeline;
    $('run-pipeline').textContent = state.busy.pipeline ? 'Starting…' : LABELS.pipeline;
    $('run-execute').disabled = !completed || state.busy.execute;
    $('run-execute').textContent = state.busy.execute ? 'Starting…' : LABELS.execute;
    updateExecuteHint();
  }

  function updateExecuteHint() {
    const el = $('execute-hint');
    if (!state.run || state.run.status !== 'completed' || !state.rules) {
      el.textContent = 'Run (or select) a completed sample pipeline first.';
      return;
    }
    const n = state.rules.rules.filter((r) => r.score !== null && r.score >= state.minScore).length;
    el.textContent = n
      ? `${plural(n, 'rule')} rated ${state.minScore} or higher will run.`
      : `No rule is rated ${state.minScore} or higher, so nothing would run. Lower the minimum.`;
  }

  function syncModes() {
    $('pipeline-uploads').hidden = !$('pipeline-mode-upload').checked;
    $('exec-uploads').hidden = !$('exec-mode-upload').checked;
  }

  function readHash() {
    const p = new URLSearchParams(window.location.hash.slice(1));
    return { run: p.get('run'), exec: p.get('exec') };
  }

  function writeHash() {
    const p = new URLSearchParams();
    if (state.runId) p.set('run', state.runId);
    if (state.execId) p.set('exec', String(state.execId));
    const next = p.toString();
    window.history.replaceState(null, '', next ? `#${next}` : window.location.pathname + window.location.search);
  }

  // ------------------------------------------------------------------ rendering: run bar
  function renderRunSelect() {
    const sel = $('run-select');
    const options = state.runs.length
      ? state.runs.map((r) => h('option', { value: r.run_id, text: `${r.run_id} · ${r.status} · ${fmtTime(r.created_at)}` }))
      : [h('option', { value: '', text: '— no runs yet —' })];
    sel.replaceChildren(...options);
    sel.value = state.runId || '';
  }

  function renderExecSelect() {
    const sel = $('exec-select');
    const options = state.executions.length
      ? state.executions.map((e) => h('option', {
        value: String(e.exec_id), text: `#${e.exec_id} · ${e.status} · ${e.target_file || ''} · ${fmtTime(e.started_at)}`,
      }))
      : [h('option', { value: '', text: '— no executions yet —' })];
    sel.replaceChildren(...options);
    sel.value = state.execId ? String(state.execId) : '';
  }

  function renderRunStatus() {
    const el = $('run-status');
    const run = state.run;
    if (!run) {
      el.className = 'status';
      el.textContent = state.runId ? 'Loading…' : 'No run selected.';
      return;
    }
    el.className = `status ${safeClass(run.status)}`;
    if (run.status === 'completed') el.textContent = `Run ${run.run_id} completed · ${plural(run.rule_count ?? 0, 'rule')}`;
    else if (run.status === 'failed') el.textContent = `Run ${run.run_id} failed: ${run.error || 'unknown error'}`;
    else el.textContent = `Run ${run.run_id} ${run.status}…${state.elapsed ? ` (${state.elapsed}s)` : ''}`;
  }

  function waitingText(what) {
    if (!state.runId) return `Run the sample pipeline to see the ${what}.`;
    if (!state.run) return 'Loading…';
    if (state.run.status === 'failed') return `The ${what} is not available because the run failed (see the status above).`;
    return `Waiting for the pipeline to finish before the ${what} is available…`;
  }

  // ------------------------------------------------------------------ rendering: data model
  function observedText(o = {}) {
    const parts = [`${o.null_pct ?? 0}% null`, `${o.distinct_count ?? '?'} distinct`];
    if (o.min !== undefined && o.min !== null) parts.push(`range ${o.min} – ${o.max}`);
    if (o.date_formats) {
      const formats = Object.keys(o.date_formats).join(', ');
      parts.push(`date format${o.mixed_date_formats ? 's (MIXED)' : ''}: ${formats}`);
    }
    return parts.join(' · ');
  }

  function renderModelTable(table) {
    const head = h('tr', {}, ['Column', 'Type', 'Meaning', 'Expected constraints', 'Observed in sample'].map((t) => h('th', { text: t })));
    const rows = table.columns.map((c) => h('tr', {},
      h('td', {}, h('code', { text: c.name }), table.primary_key.includes(c.name) ? h('span', { class: 'tag', text: 'PK' }) : null),
      h('td', { text: c.data_type }),
      h('td', {}, h('span', { class: 'tag', text: c.semantic_type }), ' ', c.description),
      h('td', {}, c.expected_constraints.length ? h('ul', { class: 'plain' }, c.expected_constraints.map((x) => h('li', { text: x }))) : h('span', { class: 'sub', text: '–' })),
      h('td', { class: 'sub', text: observedText(c.observed) }),
    ));
    const pk = table.primary_key.length ? ` · primary key: ${table.primary_key.join(', ')}` : '';
    return h('div', { class: 'table-block' },
      h('div', { class: 'table-title' }, h('h3', { text: table.name }), h('span', { class: 'tag', text: table.role }),
        h('span', { class: 'sub', text: `${table.row_count} sample rows${pk}` })),
      h('p', { class: 'sub', text: table.description }),
      h('div', { class: 'table-wrap' }, h('table', {}, h('thead', {}, head), h('tbody', {}, rows))));
  }

  function renderModel() {
    const body = $('model-body');
    if (!state.model) {
      body.replaceChildren(h('p', { class: 'empty', text: waitingText('data model') }));
      return;
    }
    const m = state.model.model;
    const nodes = m.tables.map(renderModelTable);
    if (m.relationships.length) {
      nodes.push(h('h3', { text: 'Relationships' }), h('ul', { class: 'plain', id: 'relationships' }, m.relationships.map((r) => h('li', {},
        h('code', { text: `${r.child_table}.${r.child_column} → ${r.parent_table}.${r.parent_column}` }),
        ` ${r.cardinality.replace('_', ' ')}, ${Math.round(r.overlap_ratio * 100)}% overlap, ${r.confidence} confidence. ${r.description}`))));
    }
    if (m.cross_column_constraints.length) {
      nodes.push(h('h3', { text: 'Cross-column constraints' }), h('ul', { class: 'plain', id: 'constraints' }, m.cross_column_constraints.map((c) => h('li', {},
        h('code', { text: `${c.table}: ${c.columns.join(', ')}` }), ` ${c.description} `, h('span', { class: 'tag', text: `from ${c.source}` })))));
    }
    nodes.push(h('details', {}, h('summary', { text: 'Raw JSON' }), h('pre', { text: JSON.stringify(state.model.model, null, 2) })));
    body.replaceChildren(...nodes);
  }

  // ------------------------------------------------------------------ rendering: rules
  function willExecute(rule) {
    return rule.score !== null && rule.score !== undefined && rule.score >= state.minScore;
  }

  function renderRules() {
    const body = $('rules-body');
    if (!state.rules) {
      body.replaceChildren(h('p', { class: 'empty', text: waitingText('rules') }));
      return;
    }
    const { rules, warnings, score_distribution: dist } = state.rules;
    const n = rules.filter(willExecute).length;
    const nodes = [];
    if (warnings.length) {
      nodes.push(h('div', { class: 'warnings', role: 'note' }, h('strong', { text: 'Heads up' }),
        h('ul', {}, warnings.map((w) => h('li', { text: w })))));
    }
    nodes.push(h('p', { id: 'rules-summary' }, h('strong', { text: `${n} of ${rules.length} rules` }),
      ` will execute at a minimum score of ${state.minScore}.`,
      n === 0 ? ' Nothing would run: lower the minimum score or re-run the pipeline.' : ''));
    nodes.push(h('div', { class: 'dist', 'aria-label': 'score distribution' },
      [1, 2, 3, 4, 5].map((s) => h('span', { class: 'item' }, badge(s), `× ${dist[String(s)] ?? 0}`))));

    const head = h('tr', {}, ['ID', 'Rule', 'Type', 'Columns', 'Severity', 'Sample fails', 'Score', 'Why this score', 'Runs?']
      .map((t) => h('th', { text: t })));
    const rows = rules.map((r) => h('tr', { class: willExecute(r) ? 'executes' : '' },
      h('td', { text: r.rule_key }),
      h('td', {}, h('strong', { text: r.name }), h('div', { class: 'sub', text: r.description || '' }),
        h('details', {}, h('summary', { text: 'definition' }), h('pre', { text: JSON.stringify(r.definition, null, 2) }))),
      h('td', { text: r.type }),
      h('td', {}, r.definition.columns.map((c) => h('code', { text: c })).flatMap((el, i) => (i ? [' ', el] : [el]))),
      h('td', { text: r.severity || '' }),
      h('td', { class: 'num', text: r.sample_fail_count === null ? '–' : String(r.sample_fail_count) }),
      h('td', {}, badge(r.score)),
      h('td', { class: 'sub', text: r.justification || '' }),
      h('td', {}, willExecute(r) ? h('span', { class: 'yes', text: 'yes' }) : h('span', { class: 'no', text: 'no' }))));
    nodes.push(h('div', { class: 'table-wrap' }, h('table', { id: 'rules-table' }, h('thead', {}, head), h('tbody', {}, rows))));
    body.replaceChildren(...nodes);
  }

  // ------------------------------------------------------------------ rendering: report
  function renderResult(r) {
    const failed = r.fail_count || 0;
    const kind = r.status === 'error' ? 'errored' : failed > 0 ? 'has-failures' : 'clean';
    const nodes = [
      h('div', { class: 'result-head' }, h('h3', { text: r.rule_name }), badge(r.score),
        h('span', { class: 'tag', text: r.rule_key }), r.severity ? h('span', { class: 'tag', text: r.severity }) : null),
      h('div', { class: 'sub', text: r.description || '' }),
    ];
    if (r.status === 'error') {
      nodes.push(h('p', { class: 'error-box', text: `Could not run: ${r.error}` }));
    } else {
      const bar = h('div', { class: 'bar', role: 'img', 'aria-label': `${failed} of ${r.total_rows} rows failed` });
      const fill = h('span', { class: 'bar-fail' });
      fill.style.width = `${failed > 0 ? Math.max(pct(failed, r.total_rows), 1) : 0}%`;
      bar.append(fill);
      nodes.push(bar, h('div', { class: 'fail-count' },
        h('strong', { text: `${failed} failed` }), ` / ${r.total_rows} rows (${pct(failed, r.total_rows)}%)`,
        r.skipped_count ? ` · ${plural(r.skipped_count, 'null row')} skipped` : ''));
      if (failed > 0) {
        const shown = r.failed_row_ids.length;
        nodes.push(h('div', { class: 'sub', text: `Failed row IDs${shown < failed ? ` (first ${shown} of ${failed})` : ''}` }),
          h('ul', { class: 'chips' }, r.failed_row_ids.map((id) => h('li', { class: 'chip', text: id }))));
        if (r.sample_reasons.length) {
          nodes.push(h('details', {}, h('summary', { text: `Why rows failed (${Math.min(r.sample_reasons.length, MAX_REASONS)} examples)` }),
            h('ul', { class: 'reasons' }, r.sample_reasons.slice(0, MAX_REASONS).map((x) => h('li', {},
              h('code', { text: x.row_id }), `: ${x.reason}`)))));
        }
      }
    }
    return h('article', { class: `result ${kind}` }, nodes);
  }

  function renderReport() {
    const body = $('report-body');
    const ex = state.execution;
    if (!ex) {
      const ready = state.run && state.run.status === 'completed';
      body.replaceChildren(h('p', { class: 'empty', text: ready
        ? 'Execute the rules on a production file to see the report.'
        : 'Run the sample pipeline first, then execute its rules on a production file.' }));
      return;
    }
    const nodes = [h('p', { id: 'exec-header' },
      h('strong', { text: `Execution #${ex.exec_id}` }), ` on ${ex.target_file || 'file'} · `,
      h('span', { class: `status ${safeClass(ex.status)}`, text: ex.status }),
      ` · ${ex.total_rows ?? '?'} rows · started ${fmtTime(ex.started_at)}`)];
    if (ex.error) nodes.push(h('p', { class: 'error-box', text: ex.error }));
    if (ex.status === 'running') nodes.push(h('p', { class: 'empty', text: 'Running the rules…' }));
    if (ex.status !== 'running' && !ex.error) {
      const s = ex.summary;
      nodes.push(h('div', { class: 'summary' },
        [['rules executed', s.rules_executed], ['rules with failures', s.rules_with_failures], ['rules that could not run', s.rules_errored]]
          .map(([label, value]) => h('div', { class: 'stat' }, h('b', { text: String(value) }), label))));
    }
    nodes.push(...ex.results.map(renderResult));
    body.replaceChildren(...nodes);
  }

  function renderAll() {
    renderRunSelect();
    renderRunStatus();
    renderModel();
    renderRules();
    renderExecSelect();
    renderReport();
    updateButtons();
  }

  // ------------------------------------------------------------------ data loading
  async function loadHealth() {
    const el = $('backend-status');
    try {
      const health = await api('/health');
      const keyOk = health.gemini_configured;
      el.className = keyOk ? 'pill ok' : 'pill bad';
      el.textContent = keyOk
        ? `Backend OK · ${health.model}`
        : 'Backend OK · Gemini API key missing (set GEMINI_API_KEY in .env and restart)';
    } catch (err) {
      el.className = 'pill bad';
      el.textContent = 'Backend unreachable';
      showBanner(`${err.message}\nStart the API with:  cd backend && uvicorn app.main:app`);
      throw err;
    }
  }

  async function loadRuns() {
    state.runs = await api('/runs');
    renderRunSelect();
    return state.runs;
  }

  async function refreshExecutions() {
    state.executions = await api(`/runs/${state.runId}/executions`);
    renderExecSelect();
  }

  async function loadDetails(token, execId) {
    const id = state.runId;
    const [model, rules, executions] = await Promise.all([
      api(`/runs/${id}/model`), api(`/runs/${id}/rules`), api(`/runs/${id}/executions`)]);
    if (token !== state.runToken) return;
    Object.assign(state, { model, rules, executions });
    renderAll();
    const pick = execId || (executions[0] && executions[0].exec_id);
    if (pick) await selectExecution(pick);
  }

  async function selectRun(runId, { execId = null } = {}) {
    const token = ++state.runToken;
    state.execToken += 1;
    Object.assign(state, { runId: runId || null, run: null, model: null, rules: null, executions: [], execId: null, execution: null, elapsed: 0 });
    writeHash();
    renderAll();
    if (!runId) return;
    try {
      const run = await api(`/runs/${runId}`);
      if (token !== state.runToken) return;
      state.run = run;
      renderAll();
      if (run.status === 'completed') await loadDetails(token, execId);
      else if (!TERMINAL.has(run.status)) pollRun(token);
    } catch (err) {
      if (token === state.runToken) showBanner(err.message);
    }
  }

  function pollRun(token) {
    const started = Date.now();
    let failures = 0;
    const tick = async () => {
      if (token !== state.runToken) return;
      try {
        const run = await api(`/runs/${state.runId}`);
        if (token !== state.runToken) return;
        failures = 0;
        state.run = run;
        state.elapsed = Math.round((Date.now() - started) / 1000);
        renderRunStatus();
        if (TERMINAL.has(run.status)) {
          await loadRuns().catch(() => {});
          if (token !== state.runToken) return;
          if (run.status === 'completed') await loadDetails(token, null);
          else renderAll();
          return;
        }
      } catch (err) {
        failures += 1;
        if (failures >= MAX_POLL_FAILURES) {
          if (token === state.runToken) showBanner(`Lost contact with the backend while waiting for the run: ${err.message}\nUse Refresh to resume.`);
          return;
        }
      }
      setTimeout(tick, POLL_MS);
    };
    setTimeout(tick, POLL_MS);
  }

  async function selectExecution(execId) {
    const token = ++state.execToken;
    state.execId = execId || null;
    state.execution = null;
    writeHash();
    renderExecSelect();
    renderReport();
    if (!execId) return;
    try {
      const ex = await api(`/executions/${execId}`);
      if (token !== state.execToken) return;
      state.execution = ex;
      renderReport();
      if (ex.status === 'running') pollExecution(token, execId);
    } catch (err) {
      if (token === state.execToken) showBanner(err.message);
    }
  }

  function pollExecution(token, execId) {
    let failures = 0;
    const tick = async () => {
      if (token !== state.execToken) return;
      try {
        const ex = await api(`/executions/${execId}`);
        if (token !== state.execToken) return;
        failures = 0;
        state.execution = ex;
        renderReport();
        if (TERMINAL.has(ex.status)) {
          await refreshExecutions().catch(() => {});
          return;
        }
      } catch (err) {
        failures += 1;
        if (failures >= MAX_POLL_FAILURES) {
          if (token === state.execToken) showBanner(`Lost contact with the backend while waiting for the execution: ${err.message}`);
          return;
        }
      }
      setTimeout(tick, POLL_MS);
    };
    setTimeout(tick, POLL_MS);
  }

  // ------------------------------------------------------------------ user actions
  function setBusy(key, busy) {
    state.busy[key] = busy;
    updateButtons();
  }

  async function submitPipeline(event) {
    event.preventDefault();
    const upload = $('pipeline-mode-upload').checked;
    let body;
    if (upload) {
      const sample = $('sample-file').files[0];
      const lookup = $('lookup-file').files[0];
      if (!sample || !lookup) {
        showBanner('Choose both a sample CSV and a lookup CSV, or switch to "Use the default files".');
        return;
      }
      body = new FormData();
      body.append('sample_file', sample);
      body.append('lookup_file', lookup);
    }
    clearBanner();
    setBusy('pipeline', true);
    try {
      const started = await api('/pipeline/run', { method: 'POST', body });
      await loadRuns();
      await selectRun(started.run_id);
    } catch (err) {
      showBanner(err.message);
    } finally {
      setBusy('pipeline', false);
    }
  }

  async function submitExecute(event) {
    event.preventDefault();
    if (!state.run || state.run.status !== 'completed') return;
    const upload = $('exec-mode-upload').checked;
    let body;
    if (upload) {
      const target = $('target-file').files[0];
      if (!target) {
        showBanner('Choose a production CSV, or switch to "Use the default file".');
        return;
      }
      body = new FormData();
      body.append('target_file', target);
    }
    clearBanner();
    setBusy('execute', true);
    try {
      const started = await api(`/runs/${state.runId}/execute?min_score=${state.minScore}`, { method: 'POST', body });
      await refreshExecutions();
      await selectExecution(started.exec_id);
    } catch (err) {
      showBanner(err.message);
    } finally {
      setBusy('execute', false);
    }
  }

  async function refresh() {
    clearBanner();
    try {
      await loadHealth();
      await loadRuns();
      if (state.runId) await selectRun(state.runId, { execId: state.execId });
    } catch (err) {
      /* the banner is already showing the reason */
    }
  }

  function onHashChange() {
    const { run, exec } = readHash();
    if ((run || null) !== state.runId) selectRun(run, { execId: exec ? Number(exec) : null });
    else if (exec && Number(exec) !== state.execId) selectExecution(Number(exec));
  }

  // ------------------------------------------------------------------ start-up
  async function init() {
    $('pipeline-form').addEventListener('submit', submitPipeline);
    $('execute-form').addEventListener('submit', submitExecute);
    for (const id of ['pipeline-mode-default', 'pipeline-mode-upload', 'exec-mode-default', 'exec-mode-upload']) {
      $(id).addEventListener('change', syncModes);
    }
    $('min-score').addEventListener('change', () => {
      state.minScore = Number($('min-score').value);
      renderRules();
      updateExecuteHint();
    });
    $('run-select').addEventListener('change', () => selectRun($('run-select').value || null));
    $('exec-select').addEventListener('change', () => selectExecution(Number($('exec-select').value) || null));
    $('refresh').addEventListener('click', refresh);
    window.addEventListener('hashchange', onHashChange);

    syncModes();
    renderAll();
    try {
      await loadHealth();
      await loadRuns();
    } catch (err) {
      return;
    }
    const { run, exec } = readHash();
    const first = run || (state.runs[0] && state.runs[0].run_id);
    if (first) await selectRun(first, { execId: exec ? Number(exec) : null });
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
  else init();
})();
