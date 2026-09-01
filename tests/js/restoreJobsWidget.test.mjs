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

test('formatStatus appends the percentage only when progress_percent is present', () => {
  const { restoreJobsWidget } = loadApp();
  const w = restoreJobsWidget();
  assert.equal(w.formatStatus(job({ status: 'running', progress_percent: 42 })), 'running (42%)');
  assert.equal(w.formatStatus(job({ status: 'done', progress_percent: null })), 'done');
  assert.equal(w.formatStatus(job({ status: 'queued', progress_percent: undefined })), 'queued');
  assert.equal(w.formatStatus(job({ status: 'running', progress_percent: 0 })), 'running (0%)');
});

// A minimal fake `window` for testing startDrag()'s window-level
// pointermove/pointerup listeners, since app.js's top-level `window`
// reference resolves to whatever stub loadApp() was given (see
// helpers.mjs) - the real global `window` in this file is never used by
// the code under test.
function fakeWindow() {
  const listeners = {};
  return {
    location: {},
    Alpine: undefined,
    addEventListener(type, fn) {
      (listeners[type] ||= []).push(fn);
    },
    removeEventListener(type, fn) {
      listeners[type] = (listeners[type] || []).filter((f) => f !== fn);
    },
    dispatch(type, event) {
      for (const fn of listeners[type] || []) fn(event);
    },
    listenerCount(type) {
      return (listeners[type] || []).length;
    },
  };
}

test('startDrag tracks pointer movement as a dragX/dragY offset', () => {
  const win = fakeWindow();
  const { restoreJobsWidget } = loadApp({ window: win });
  const w = restoreJobsWidget();
  w.dragX = 10;
  w.dragY = 20;

  w.startDrag({ clientX: 100, clientY: 100, target: { closest: () => null } });
  win.dispatch('pointermove', { clientX: 130, clientY: 90 });

  assert.equal(w.dragX, 40); // 10 + (130 - 100)
  assert.equal(w.dragY, 10); // 20 + (90 - 100)
});

test('startDrag tracks an independent offset when given xProp/yProp (the log modal)', () => {
  const win = fakeWindow();
  const { restoreJobsWidget } = loadApp({ window: win });
  const w = restoreJobsWidget();
  w.dragX = 999; // untouched - proves the two offsets don't cross-talk
  w.dragY = 999;
  w.logDragX = 10;
  w.logDragY = 20;

  w.startDrag({ clientX: 100, clientY: 100, target: { closest: () => null } }, 'logDragX', 'logDragY');
  win.dispatch('pointermove', { clientX: 130, clientY: 90 });

  assert.equal(w.logDragX, 40); // 10 + (130 - 100)
  assert.equal(w.logDragY, 10); // 20 + (90 - 100)
  assert.equal(w.dragX, 999);
  assert.equal(w.dragY, 999);
});

test('startDrag stops updating after pointerup removes the listeners', () => {
  const win = fakeWindow();
  const { restoreJobsWidget } = loadApp({ window: win });
  const w = restoreJobsWidget();

  w.startDrag({ clientX: 0, clientY: 0, target: { closest: () => null } });
  win.dispatch('pointermove', { clientX: 10, clientY: 10 });
  assert.equal(w.dragX, 10);

  win.dispatch('pointerup', {});
  assert.equal(win.listenerCount('pointermove'), 0);

  win.dispatch('pointermove', { clientX: 999, clientY: 999 });
  assert.equal(w.dragX, 10); // unchanged - listener was removed
});

test('startDrag ignores pointerdown on the close button', () => {
  const win = fakeWindow();
  const { restoreJobsWidget } = loadApp({ window: win });
  const w = restoreJobsWidget();

  w.startDrag({ clientX: 0, clientY: 0, target: { closest: (sel) => (sel === '.modal-close' ? {} : null) } });
  assert.equal(win.listenerCount('pointermove'), 0);
});

// --- log viewer ------------------------------------------------------

function withAlpineStubs(w) {
  w.$nextTick = (fn) => fn();
  w.$refs = { logBody: { scrollTop: 0, scrollHeight: 500 } };
  return w;
}

test('openLog is a no-op without a selection', () => {
  const { restoreJobsWidget } = loadApp();
  const w = withAlpineStubs(restoreJobsWidget());
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async () => {
    throw new Error('should not fetch without a selected job');
  };
  try {
    w.openLog();
  } finally {
    globalThis.fetch = originalFetch;
  }
  assert.equal(w.logOpen, false);
});

test('openLog fetches the selected job’s detail and opens the viewer', async () => {
  const { restoreJobsWidget } = loadApp();
  const w = withAlpineStubs(restoreJobsWidget());
  w.selectedId = 'job-1';
  let requestedUrl = null;
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async (url) => {
    requestedUrl = url;
    return { ok: true, json: async () => ({ id: 'job-1', status: 'running', log: ['line one', 'line two'] }) };
  };
  try {
    w.openLog();
    await new Promise((r) => setTimeout(r, 0));
  } finally {
    globalThis.fetch = originalFetch;
  }
  assert.equal(requestedUrl, '/api/restore-jobs/job-1');
  assert.equal(w.logOpen, true);
  assert.deepEqual(w.logDetail.log, ['line one', 'line two']);
  // Auto-scrolled to the bottom.
  assert.equal(w.$refs.logBody.scrollTop, w.$refs.logBody.scrollHeight);
});

test('refreshLog surfaces the server error detail on failure', async () => {
  const { restoreJobsWidget } = loadApp();
  const w = withAlpineStubs(restoreJobsWidget());
  w.logJobId = 'job-1';
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async () => ({ ok: false, json: async () => ({ detail: 'No such restore job' }) });
  try {
    await w.refreshLog();
  } finally {
    globalThis.fetch = originalFetch;
  }
  assert.equal(w.logError, 'No such restore job');
});

test('refresh() piggybacks refreshLog() onto the same poll tick while the log viewer is open', async () => {
  const { restoreJobsWidget } = loadApp();
  const w = withAlpineStubs(restoreJobsWidget());
  w.logOpen = true;
  w.logJobId = 'job-1';
  const calledUrls = [];
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async (url) => {
    calledUrls.push(url);
    if (url === '/api/restore-jobs') return { ok: true, json: async () => [] };
    return { ok: true, json: async () => ({ id: 'job-1', status: 'running', log: ['still going'] }) };
  };
  try {
    await w.refresh();
  } finally {
    globalThis.fetch = originalFetch;
  }
  assert.deepEqual(calledUrls, ['/api/restore-jobs', '/api/restore-jobs/job-1']);
  assert.deepEqual(w.logDetail.log, ['still going']);
});

test('refresh() does not fetch the log when the viewer is closed', async () => {
  const { restoreJobsWidget } = loadApp();
  const w = withAlpineStubs(restoreJobsWidget());
  w.logOpen = false;
  const calledUrls = [];
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async (url) => {
    calledUrls.push(url);
    return { ok: true, json: async () => [] };
  };
  try {
    await w.refresh();
  } finally {
    globalThis.fetch = originalFetch;
  }
  assert.deepEqual(calledUrls, ['/api/restore-jobs']);
});
