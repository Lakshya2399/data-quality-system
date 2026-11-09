'use strict';
/** An in-memory stand-in for the FastAPI backend, built from payloads captured from the REAL API
 *  (test/fixtures, regenerate with backend/scripts/export_ui_fixtures.py). It also simulates the
 *  created -> running -> completed lifecycle, one step per GET of the run/execution. */
const fs = require('node:fs');
const path = require('node:path');

const NAMES = ['health', 'runs', 'run', 'model', 'rules', 'executions', 'execution',
  'run_low_scores', 'rules_low_scores', 'execution_failed', 'run_failed'];
const load = (n) => JSON.parse(fs.readFileSync(path.join(__dirname, 'fixtures', `${n}.json`), 'utf8'));
const clone = (x) => JSON.parse(JSON.stringify(x));

class MockBackend {
  constructor({ empty = false, down = false, geminiConfigured = true } = {}) {
    this.fx = Object.fromEntries(NAMES.map((n) => [n, load(n)]));
    this.fx.health.gemini_configured = geminiConfigured;      // the fixture reflects the exporting machine's env
    this.down = down;
    this.calls = [];
    this.failures = new Map();
    this.runs = new Map();            // run_id -> { run, model, rules, executions: [list items], pending: [] }
    this.execs = new Map();           // exec_id -> { execution, pending: [] }
    this.order = [];                  // newest first

    if (!empty) {
      this.addRun({ run: this.fx.run, model: this.fx.model, rules: this.fx.rules, executions: this.fx.executions });
      this.execs.set(this.fx.execution.exec_id, { execution: this.fx.execution, pending: [] });
      // the fixtures were captured from two separate backends, so both executions are '#1': give this one its own id
      this.failedExec = { ...clone(this.fx.execution_failed), exec_id: 7, run_id: this.fx.run_low_scores.run_id };
      this.addRun({ run: this.fx.run_low_scores, model: this.fx.model, rules: this.fx.rules_low_scores,
        executions: [this.listItem(this.failedExec)] });
      this.execs.set(7, { execution: this.failedExec, pending: [] });
      this.addRun({ run: this.fx.run_failed });
    }
  }

  get goodRunId() { return this.fx.run.run_id; }
  get lowRunId() { return this.fx.run_low_scores.run_id; }
  get failedRunId() { return this.fx.run_failed.run_id; }

  listItem(ex) {
    const { exec_id, run_id, target_file, total_rows, status, error, started_at, finished_at } = ex;
    return { exec_id, run_id, target_file, total_rows, status, error, started_at, finished_at };
  }

  addRun(entry) {
    this.runs.set(entry.run.run_id, { executions: [], pending: [], ...clone(entry) });
    this.order.push(entry.run.run_id);
  }

  /** Make the next call matching "METHOD /path" fail once. */
  fail(key, status, detail) { this.failures.set(key, { status, detail }); }

  summary(id) {
    const e = this.runs.get(id);
    return { ...e.run, rule_count: e.rules ? e.rules.rules.length : 0, execution_count: e.executions.length };
  }

  respond(status, body) {
    return new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } });
  }

  fetch = async (url, options = {}) => {
    if (this.down) throw new TypeError('fetch failed');
    const u = new URL(url, 'http://ui.test');
    const method = (options.method || 'GET').toUpperCase();
    const p = u.pathname.replace(/^\/api/, '');
    this.calls.push({ method, path: p, query: Object.fromEntries(u.searchParams), body: options.body });

    const injected = this.failures.get(`${method} ${p}`) || this.failures.get(`${method} *`);
    if (injected) {
      this.failures.delete(`${method} ${p}`); this.failures.delete(`${method} *`);
      return this.respond(injected.status, { detail: injected.detail });
    }
    let m;
    if (method === 'GET' && p === '/health') return this.respond(200, this.fx.health);
    if (method === 'GET' && p === '/runs') return this.respond(200, this.order.map((id) => this.summary(id)));

    if (method === 'POST' && p === '/pipeline/run') {
      const id = 'newrun000001';
      const run = { ...clone(this.fx.run), run_id: id, status: 'created', rule_count: 0, execution_count: 0, warnings: [] };
      this.runs.set(id, {
        run, model: clone(this.fx.model), rules: clone(this.fx.rules), executions: [],
        pending: [{ status: 'created' }, { status: 'running' }, { status: 'completed' }],
      });
      this.order.unshift(id);
      return this.respond(202, { run_id: id, status: 'created', poll_url: `/runs/${id}` });
    }

    if ((m = p.match(/^\/runs\/([^/]+)$/)) && method === 'GET') {
      const e = this.runs.get(m[1]);
      if (!e) return this.respond(404, { detail: `unknown run '${m[1]}'` });
      if (e.pending.length) e.run = { ...e.run, ...e.pending.shift() };
      return this.respond(200, this.summary(m[1]));
    }
    if ((m = p.match(/^\/runs\/([^/]+)\/(model|rules|executions)$/)) && method === 'GET') {
      const e = this.runs.get(m[1]);
      if (!e) return this.respond(404, { detail: `unknown run '${m[1]}'` });
      if (m[2] !== 'executions' && (e.run.status !== 'completed' || !e[m[2]])) {
        return this.respond(409, { detail: `run is '${e.run.status}', so the ${m[2]} is not available` });
      }
      return this.respond(200, m[2] === 'executions' ? e.executions : e[m[2]]);
    }

    if ((m = p.match(/^\/runs\/([^/]+)\/execute$/)) && method === 'POST') {
      const e = this.runs.get(m[1]);
      if (!e) return this.respond(404, { detail: `unknown run '${m[1]}'` });
      const execId = 99 + e.executions.length;
      const done = { ...clone(this.fx.execution), exec_id: execId, run_id: m[1] };
      const running = { ...done, status: 'running', finished_at: null, results: [], summary: { rules_executed: 0, rules_with_failures: 0, rules_errored: 0 } };
      this.execs.set(execId, { execution: running, pending: [done] });
      e.executions.unshift(this.listItem(running));
      return this.respond(202, { exec_id: execId, run_id: m[1], status: 'running', poll_url: `/executions/${execId}` });
    }
    if ((m = p.match(/^\/executions\/(\d+)$/)) && method === 'GET') {
      const e = this.execs.get(Number(m[1]));
      if (!e) return this.respond(404, { detail: `unknown execution ${m[1]}` });
      const current = e.execution;
      if (e.pending.length) {
        e.execution = e.pending.shift();
        for (const run of this.runs.values()) {
          run.executions = run.executions.map((x) => (x.exec_id === e.execution.exec_id ? this.listItem(e.execution) : x));
        }
      }
      return this.respond(200, current);
    }
    return this.respond(404, { detail: `mock: no route for ${method} ${p}` });
  };
}

module.exports = { MockBackend, clone };
