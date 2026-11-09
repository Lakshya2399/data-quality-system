'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const http = require('node:http');
const crypto = require('node:crypto');
const { createServer } = require('../server');

const listen = (server) => new Promise((resolve) => server.listen(0, '127.0.0.1', () => resolve(server.address().port)));
const close = (server) => new Promise((resolve) => { server.closeAllConnections?.(); server.close(() => resolve()); });

/** A fake FastAPI: records every request and answers via handler(record, res). */
async function startUpstream(handler) {
  const seen = [];
  const server = http.createServer((req, res) => {
    const chunks = [];
    req.on('data', (c) => chunks.push(c));
    req.on('end', () => {
      const record = { method: req.method, url: req.url, headers: req.headers, body: Buffer.concat(chunks), req, res };
      seen.push(record);
      handler(record, res);
    });
  });
  const port = await listen(server);
  return { server, port, seen, url: `http://127.0.0.1:${port}` };
}

/** Starts upstream + UI server; returns base URL and a cleanup. */
async function setup(t, handler, serverOptions = {}) {
  const upstream = await startUpstream(handler);
  const ui = createServer({ apiUrl: upstream.url, ...serverOptions });
  const port = await listen(ui);
  t.after(async () => { await close(ui); await close(upstream.server); });
  return { base: `http://127.0.0.1:${port}`, upstream };
}

const json = (res, status, body, headers = {}) => {
  res.writeHead(status, { 'content-type': 'application/json', ...headers });
  res.end(JSON.stringify(body));
};

test('serves the page with a strict CSP and no framework banner', async (t) => {
  const { base } = await setup(t, (rec, res) => json(res, 200, {}));
  const res = await fetch(`${base}/`);
  assert.equal(res.status, 200);
  assert.match(res.headers.get('content-type'), /text\/html/);
  assert.match(res.headers.get('content-security-policy'), /default-src 'self'/);
  assert.match(res.headers.get('content-security-policy'), /script-src 'self'/);
  assert.equal(res.headers.get('x-content-type-options'), 'nosniff');
  assert.equal(res.headers.get('x-powered-by'), null);
  assert.match(await res.text(), /<title>Data Quality System<\/title>/);
  for (const file of ['app.js', 'style.css']) assert.equal((await fetch(`${base}/${file}`)).status, 200, file);
});

test('unknown paths are 404 and path traversal never leaks source files', async (t) => {
  const { base } = await setup(t, (rec, res) => json(res, 200, {}));
  assert.equal((await fetch(`${base}/nope.html`)).status, 404);
  for (const p of ['/..%2fserver.js', '/%2e%2e/server.js', '/..%2f..%2fpackage.json']) {
    const body = await new Promise((resolve) => {
      http.get(`${base}${p}`, (res) => { let d = ''; res.on('data', (c) => (d += c)); res.on('end', () => resolve({ status: res.statusCode, d })); });
    });
    assert.notEqual(body.status, 200, p);
    assert.doesNotMatch(body.d, /createProxy|"name": "dq-ui"/, p);
  }
});

test('/api is stripped, query strings survive, status and body pass through', async (t) => {
  const { base, upstream } = await setup(t, (rec, res) => json(res, 200, [{ run_id: 'abc' }], { 'x-from-api': 'yes' }));
  const res = await fetch(`${base}/api/runs?limit=5&x=a%20b`);
  assert.equal(res.status, 200);
  assert.deepEqual(await res.json(), [{ run_id: 'abc' }]);
  assert.equal(res.headers.get('x-from-api'), 'yes');
  assert.equal(upstream.seen[0].url, '/runs?limit=5&x=a%20b');
  assert.equal(upstream.seen[0].headers.host, new URL(upstream.url).host);   // Host is the backend's, not the proxy's
});

test('error responses keep their status and JSON detail', async (t) => {
  const { base } = await setup(t, (rec, res) => json(res, 409, { detail: "run is 'running'" }));
  const res = await fetch(`${base}/api/runs/x/rules`);
  assert.equal(res.status, 409);
  assert.deepEqual(await res.json(), { detail: "run is 'running'" });
});

test('methods and bodies are forwarded unchanged', async (t) => {
  const { base, upstream } = await setup(t, (rec, res) => json(res, 202, { ok: true }));
  for (const method of ['POST', 'PUT', 'DELETE']) {
    const res = await fetch(`${base}/api/things`, { method, body: JSON.stringify({ m: method }), headers: { 'content-type': 'application/json' } });
    assert.equal(res.status, 202);
  }
  assert.deepEqual(upstream.seen.map((r) => r.method), ['POST', 'PUT', 'DELETE']);
  assert.deepEqual(upstream.seen.map((r) => JSON.parse(r.body).m), ['POST', 'PUT', 'DELETE']);
});

test('multipart uploads are streamed through byte for byte', async (t) => {
  const { base, upstream } = await setup(t, (rec, res) => json(res, 202, { received: rec.body.length }));
  const payload = crypto.randomBytes(3 * 1024 * 1024);                   // 3 MB of incompressible data
  const form = new FormData();
  form.append('sample_file', new Blob([payload], { type: 'text/csv' }), 'sample_orders.csv');
  const res = await fetch(`${base}/api/pipeline/run?wait=false`, { method: 'POST', body: form });
  assert.equal(res.status, 202);
  const seen = upstream.seen[0];
  assert.match(seen.headers['content-type'], /^multipart\/form-data; boundary=/);
  assert.ok(seen.body.includes(payload), 'the exact file bytes reached the backend');
  assert.match(seen.body.toString('latin1', 0, 300), /name="sample_file"; filename="sample_orders.csv"/);
  assert.equal((await res.json()).received, seen.body.length);
});

test('redirects are rewritten so the backend address never reaches the browser', async (t) => {
  let upstreamUrl;
  const { base, upstream } = await setup(t, (rec, res) => {
    const location = rec.url === '/runs/' ? `${upstreamUrl}/runs` : '/executions/1';
    res.writeHead(307, { location });
    res.end();
  });
  upstreamUrl = upstream.url;
  const absolute = await fetch(`${base}/api/runs/`, { redirect: 'manual' });
  assert.equal(absolute.status, 307);
  assert.equal(absolute.headers.get('location'), '/api/runs');
  const relative = await fetch(`${base}/api/anything`, { redirect: 'manual' });
  assert.equal(relative.headers.get('location'), '/api/executions/1');
});

test('only /api is proxied', async (t) => {
  const { base, upstream } = await setup(t, (rec, res) => json(res, 200, {}));
  assert.equal((await fetch(`${base}/apiary`)).status, 404);
  assert.equal((await fetch(`${base}/runs`)).status, 404);
  assert.equal(upstream.seen.length, 0);
});

test('a stopped backend becomes a JSON 502 the UI can show', async (t) => {
  const dead = http.createServer();
  const deadPort = await listen(dead);
  await close(dead);                                                      // port is now closed
  const ui = createServer({ apiUrl: `http://127.0.0.1:${deadPort}` });
  const port = await listen(ui);
  t.after(() => close(ui));
  const res = await fetch(`http://127.0.0.1:${port}/api/health`);
  assert.equal(res.status, 502);
  const body = await res.json();
  assert.match(body.detail, /not reachable/);
  assert.match(body.detail, /uvicorn/);
});

test('a hung backend becomes a JSON 504', async (t) => {
  const { base } = await setup(t, () => { /* never answers */ }, { timeoutMs: 80 });
  const res = await fetch(`${base}/api/runs`);
  assert.equal(res.status, 504);
  assert.match((await res.json()).detail, /did not answer in time/);
});

test('when the browser disconnects the backend request is cancelled', async (t) => {
  const { base, upstream } = await setup(t, () => { /* hold the response open */ });
  const controller = new AbortController();
  const pending = fetch(`${base}/api/runs`, { signal: controller.signal }).catch(() => 'aborted');
  while (!upstream.seen.length) await new Promise((r) => setTimeout(r, 5));
  controller.abort();
  assert.equal(await pending, 'aborted');
  // The backend-side signal is its RESPONSE closing before it was finished (req 'close' fires earlier,
  // as soon as the request body has been read).
  const { res } = upstream.seen[0];
  const cancelled = await Promise.race([
    new Promise((resolve) => (res.destroyed ? resolve(true) : res.on('close', () => resolve(!res.writableFinished)))),
    new Promise((resolve) => setTimeout(() => resolve(false), 1500)),
  ]);
  assert.equal(cancelled, true, 'the proxy must cancel its backend request when the browser goes away');
});
