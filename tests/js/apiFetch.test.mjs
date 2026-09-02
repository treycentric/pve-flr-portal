import test from 'node:test';
import assert from 'node:assert/strict';
import { loadApp } from './helpers.mjs';

// Lets the microtask queue drain so an un-awaited async call settles.
const flush = () => new Promise((r) => setImmediate(r));

test('apiFetch returns the response untouched when status is not 401', async () => {
  const { apiFetch } = loadApp({ window: { location: {} } });
  const original = globalThis.fetch;
  const body = { ok: true, status: 200, json: async () => ({ hi: true }) };
  globalThis.fetch = async (url, init) => {
    assert.equal(url, '/api/thing');
    assert.deepEqual(init, { method: 'POST' });
    return body;
  };
  try {
    const resp = await apiFetch('/api/thing', { method: 'POST' });
    assert.equal(resp, body);
  } finally {
    globalThis.fetch = original;
  }
});

test('apiFetch redirects to /login?reason=expired on 401 and never resolves', async () => {
  const { apiFetch, window } = loadApp({ window: { location: {} } });
  const original = globalThis.fetch;
  globalThis.fetch = async () => ({ ok: false, status: 401, json: async () => ({}) });
  try {
    let settled = false;
    apiFetch('/api/thing').then(() => {
      settled = true;
    });
    await flush();
    assert.equal(window.location.href, '/login?reason=expired');
    assert.equal(settled, false);
  } finally {
    globalThis.fetch = original;
  }
});

test('a burst of 401s triggers only one redirect', async () => {
  const { apiFetch, window } = loadApp({ window: { location: {} } });
  const original = globalThis.fetch;
  let navigations = 0;
  const loc = {};
  Object.defineProperty(loc, 'href', {
    set() {
      navigations += 1;
    },
  });
  window.location = loc;
  globalThis.fetch = async () => ({ status: 401 });
  try {
    apiFetch('/api/a');
    apiFetch('/api/b');
    apiFetch('/api/c');
    await flush();
    assert.equal(navigations, 1);
  } finally {
    globalThis.fetch = original;
  }
});
