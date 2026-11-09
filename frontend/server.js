'use strict';
/**
 * UI server: serves ./public and proxies /api/* to the FastAPI backend.
 *
 *   API_URL  backend base URL            (default http://127.0.0.1:8000)
 *   PORT     port to listen on           (default 3000)
 *   HOST     interface to bind           (default 127.0.0.1 - the API has no authentication and every run
 *                                         spends LLM credits, so do not expose this beyond localhost casually)
 *
 * The proxy is deliberately small and dependency-free: it streams request and response bodies (so
 * multipart uploads are never buffered), maps backend outages to JSON 502/504 errors the UI can show,
 * and rewrites redirect Location headers so the backend address never leaks to the browser.
 */
const http = require('node:http');
const https = require('node:https');
const path = require('node:path');
const express = require('express');

const HOP_BY_HOP = new Set([
  'connection', 'keep-alive', 'proxy-authenticate', 'proxy-authorization',
  'te', 'trailer', 'transfer-encoding', 'upgrade',
]);

const CSP = [
  "default-src 'self'", "script-src 'self'", "style-src 'self'", "img-src 'self' data:",
  "connect-src 'self'", "base-uri 'none'", "form-action 'self'", "frame-ancestors 'none'",
].join('; ');

function withoutHopByHop(headers) {
  const out = {};
  for (const [name, value] of Object.entries(headers)) {
    if (!HOP_BY_HOP.has(name.toLowerCase())) out[name] = value;
  }
  return out;
}

function sendJsonError(res, status, detail) {
  if (res.headersSent) return res.destroy();
  res.writeHead(status, { 'content-type': 'application/json' });
  res.end(JSON.stringify({ detail }));
}

function createProxy(apiUrl, { timeoutMs = 120000 } = {}) {
  const target = new URL(apiUrl);
  const client = target.protocol === 'https:' ? https : http;
  const basePath = target.pathname.replace(/\/+$/, '');

  const rewriteLocation = (location) => {
    if (location.startsWith(target.origin)) return '/api' + location.slice(target.origin.length + basePath.length);
    if (location.startsWith('/')) return '/api' + location;
    return location;
  };

  // Mounted with app.use('/api', proxy): req.url is the path *after* /api, query string included.
  return function proxy(req, res) {
    const upstream = client.request(
      {
        protocol: target.protocol,
        hostname: target.hostname,
        port: target.port,
        method: req.method,
        path: basePath + req.url,
        headers: { ...withoutHopByHop(req.headers), host: target.host },
      },
      (upRes) => {
        const headers = withoutHopByHop(upRes.headers);
        if (headers.location) headers.location = rewriteLocation(headers.location);
        res.writeHead(upRes.statusCode, upRes.statusMessage, headers);
        upRes.pipe(res);
      },
    );
    upstream.setTimeout(timeoutMs, () => upstream.destroy(Object.assign(new Error('timed out'), { code: 'ETIMEDOUT' })));
    upstream.on('error', (err) => {
      if (err.code === 'ETIMEDOUT') return sendJsonError(res, 504, `The backend at ${target.origin} did not answer in time.`);
      sendJsonError(res, 502, `Backend not reachable at ${target.origin} (${err.code || err.message}). Is uvicorn running?`);
    });
    res.on('close', () => { if (!res.writableFinished) upstream.destroy(); });   // browser went away
    req.pipe(upstream);
  };
}

function createServer({
  apiUrl = process.env.API_URL || 'http://127.0.0.1:8000',
  publicDir = path.join(__dirname, 'public'),
  timeoutMs,
} = {}) {
  const app = express();
  app.disable('x-powered-by');
  app.use((req, res, next) => {
    res.setHeader('Content-Security-Policy', CSP);
    res.setHeader('X-Content-Type-Options', 'nosniff');
    res.setHeader('Referrer-Policy', 'no-referrer');
    next();
  });
  app.use('/api', createProxy(apiUrl, { timeoutMs }));
  app.use(express.static(publicDir, { dotfiles: 'ignore', maxAge: 0 }));
  app.use((req, res) => res.status(404).type('text/plain').send('Not found'));
  return http.createServer(app);
}

if (require.main === module) {
  const port = Number(process.env.PORT || 3000);
  const host = process.env.HOST || '127.0.0.1';
  const apiUrl = process.env.API_URL || 'http://127.0.0.1:8000';
  createServer({ apiUrl }).listen(port, host, () => {
    console.log(`DQ UI on http://${host}:${port}  ->  API ${apiUrl}`);
  });
}

module.exports = { createServer, createProxy };
