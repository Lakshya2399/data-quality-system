'use strict';
const fs = require('node:fs');
const path = require('node:path');
const { JSDOM } = require('jsdom');

const PUBLIC = path.join(__dirname, '..', 'public');

/** Loads the real index.html and runs the real app.js in jsdom, with fetch mocked by `backend`. */
function loadUi(backend, { hash = '', pollMs = 15 } = {}) {
  const html = fs.readFileSync(path.join(PUBLIC, 'index.html'), 'utf8');
  const dom = new JSDOM(html, { url: `http://localhost:3000/${hash}`, runScripts: 'outside-only', pretendToBeVisual: true });
  const { window } = dom;
  let inflight = 0;                                       // lets teardown wait for the page to go quiet
  window.fetch = async (...args) => {
    inflight += 1;
    try { return await backend.fetch(...args); } finally { inflight -= 1; }
  };
  window.DQ_POLL_MS = pollMs;
  const errors = [];
  window.addEventListener('error', (e) => errors.push(e.error || e.message));
  window.eval(fs.readFileSync(path.join(PUBLIC, 'app.js'), 'utf8'));
  const { document } = window;
  return {
    window, document, errors,
    $: (sel) => document.querySelector(sel),
    $$: (sel) => [...document.querySelectorAll(sel)],
    text: (sel) => (document.querySelector(sel)?.textContent ?? ''),
    /** Resolves once no request is in flight (checked twice, a tick apart, so follow-up requests are caught). */
    async idle(timeout = 1500) {
      const start = Date.now();
      let quiet = 0;
      while (quiet < 2 && Date.now() - start < timeout) {
        await new Promise((r) => setImmediate(r));
        quiet = inflight === 0 ? quiet + 1 : 0;
      }
    },
    close: () => window.close(),
    change(el, value) {
      if (value !== undefined) el.value = value;
      el.dispatchEvent(new window.Event('change', { bubbles: true }));
    },
    click: (el) => el.dispatchEvent(new window.MouseEvent('click', { bubbles: true, cancelable: true })),
    submit: (form) => form.dispatchEvent(new window.Event('submit', { bubbles: true, cancelable: true })),
    pickFile(input, name, content = 'a,b\n1,2\n') {
      const file = new window.File([content], name, { type: 'text/csv' });
      Object.defineProperty(input, 'files', { value: [file], configurable: true });
      return file;
    },
  };
}

async function waitFor(fn, { timeout = 3000, interval = 3, message = 'condition' } = {}) {
  const start = Date.now();
  let lastError;
  for (;;) {
    try {
      const value = fn();
      if (value) return value;
    } catch (err) { lastError = err; }
    if (Date.now() - start > timeout) throw new Error(`timed out waiting for ${message}${lastError ? `: ${lastError.message}` : ''}`);
    await new Promise((r) => setTimeout(r, interval));
  }
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

module.exports = { loadUi, waitFor, sleep };
