import test from 'node:test';
import assert from 'node:assert/strict';
import { loadApp } from './helpers.mjs';

function job(overrides = {}) {
  return {
    id: 'job-1',
    device: 'web (133)',
    task_name: 'Restore hosts -> /etc',
    restore_version: '2026-08-30T14:48:06Z',
    source: 'hosts',
    destination: '/etc/hosts',
    status: 'running',
    elapsed_seconds: 5,
    error: null,
    cancellable: true,
    ...overrides,
  };
}

test('activeCount counts only queued/running/verifying jobs', () => {
  const { restoreJobsWidget } = loadApp();
  const w = restoreJobsWidget();
  w.jobs = [
    job({ id: '1', status: 'queued' }),
    job({ id: '2', status: 'running' }),
    job({ id: '3', status: 'verifying' }),
    job({ id: '4', status: 'done' }),
    job({ id: '5', status: 'failed' }),
    job({ id: '6', status: 'cancelled' }),
  ];
  assert.equal(w.activeCount, 3);
});

test('selectedCancellable reflects the selected job, false when none selected', () => {
  const { restoreJobsWidget } = loadApp();
  const w = restoreJobsWidget();
  w.jobs = [job({ id: '1', cancellable: true }), job({ id: '2', cancellable: false })];

  w.selectedId = null;
  assert.equal(w.selectedCancellable, false);

  w.selectedId = '1';
  assert.equal(w.selectedCancellable, true);

  w.selectedId = '2';
  assert.equal(w.selectedCancellable, false);
});

test('refresh replaces the job list on success', async () => {
  const { restoreJobsWidget } = loadApp();
  const w = restoreJobsWidget();
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async (url) => {
    assert.equal(url, '/api/restore-jobs');
    return { ok: true, json: async () => [job()] };
  };
  try {
    await w.refresh();
  } finally {
    globalThis.fetch = originalFetch;
  }
  assert.equal(w.jobs.length, 1);
  assert.equal(w.jobs[0].id, 'job-1');
});

test('refresh keeps the last known list on a non-ok response', async () => {
  const { restoreJobsWidget } = loadApp();
  const w = restoreJobsWidget();
  w.jobs = [job()];
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async () => ({ ok: false });
  try {
    await w.refresh();
  } finally {
    globalThis.fetch = originalFetch;
  }
  assert.equal(w.jobs.length, 1);
});

test('refresh keeps the last known list on a network error', async () => {
  const { restoreJobsWidget } = loadApp();
  const w = restoreJobsWidget();
  w.jobs = [job()];
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async () => {
    throw new Error('offline');
  };
  try {
    await w.refresh();
  } finally {
    globalThis.fetch = originalFetch;
  }
  assert.equal(w.jobs.length, 1);
});

test('cancelSelected posts to the cancel endpoint then refreshes', async () => {
  const { restoreJobsWidget } = loadApp();
  const w = restoreJobsWidget();
  w.jobs = [job({ id: 'abc', cancellable: true })];
  w.selectedId = 'abc';

  const calls = [];
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async (url, opts) => {
    calls.push({ url, method: opts && opts.method });
    if (url === '/api/restore-jobs') return { ok: true, json: async () => [job({ id: 'abc', status: 'cancelled', cancellable: false })] };
    return { ok: true, json: async () => job({ id: 'abc', status: 'cancelled' }) };
  };
  try {
    await w.cancelSelected();
  } finally {
    globalThis.fetch = originalFetch;
  }
  assert.deepEqual(calls[0], { url: '/api/restore-jobs/abc/cancel', method: 'POST' });
  assert.equal(calls[1].url, '/api/restore-jobs');
  assert.equal(w.jobs[0].status, 'cancelled');
});

test('cancelSelected is a no-op without a cancellable selection', async () => {
  const { restoreJobsWidget } = loadApp();
  const w = restoreJobsWidget();
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async () => {
    throw new Error('should not be called');
  };
  try {
    w.selectedId = null;
    await w.cancelSelected();

    w.jobs = [job({ id: 'x', cancellable: false })];
    w.selectedId = 'x';
    await w.cancelSelected();
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test('formatElapsed renders m:ss under an hour, h:mm:ss at or past one hour', () => {
  const { restoreJobsWidget } = loadApp();
  const w = restoreJobsWidget();
  assert.equal(w.formatElapsed(5), '0:05');
  assert.equal(w.formatElapsed(65), '1:05');
  assert.equal(w.formatElapsed(3661), '1:01:01');
  assert.equal(w.formatElapsed(0), '0:00');
  assert.equal(w.formatElapsed(undefined), '0:00');
});
