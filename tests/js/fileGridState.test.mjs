import test from 'node:test';
import assert from 'node:assert/strict';
import { loadApp, fakeTbody } from './helpers.mjs';

function checkbox(spec) {
  return { value: JSON.stringify(spec) };
}

test('applySort orders rows alphabetically by name then toggles direction', () => {
  const { fileGridState } = loadApp();
  const s = fileGridState();
  const rows = [
    { dataset: { name: 'banana', size: '10', type: 'file', modified: '5' } },
    { dataset: { name: 'apple', size: '30', type: 'file', modified: '1' } },
  ];
  const tb = fakeTbody(rows);
  s.$refs = { tbody: tb };

  s.applySort();
  assert.deepEqual(tb.order.map((r) => r.dataset.name), ['apple', 'banana']);

  s.setSort('name'); // same key -> flip to desc
  assert.equal(s.sortDir, 'desc');
  assert.deepEqual(tb.order.slice(-2).map((r) => r.dataset.name), ['banana', 'apple']);
});

test('applySort compares size and modified numerically', () => {
  const { fileGridState } = loadApp();
  const s = fileGridState();
  const rows = [
    { dataset: { name: 'a', size: '100', type: 'f', modified: '2' } },
    { dataset: { name: 'b', size: '9', type: 'f', modified: '1' } },
  ];
  const tb = fakeTbody(rows);
  s.$refs = { tbody: tb };

  s.setSort('size');
  assert.deepEqual(tb.order.slice(-2).map((r) => r.dataset.size), ['9', '100']);
});

test('isSingleFile is true only for exactly one leaf selection', () => {
  const { fileGridState } = loadApp();
  const s = fileGridState();
  let checked = [checkbox({ filepath: 'a', name: 'a', leaf: true })];
  s.$refs = { tbody: { querySelectorAll: () => checked } };
  assert.equal(s.isSingleFile, true);

  checked = [checkbox({ leaf: false })];
  assert.equal(s.isSingleFile, false);

  checked = [checkbox({ leaf: true }), checkbox({ leaf: true })];
  assert.equal(s.isSingleFile, false);
});

test('singleDownloadHref builds the query from the first selected item', () => {
  const { fileGridState } = loadApp();
  const s = fileGridState();
  const checked = [checkbox({ filepath: 'abc', name: 'f.txt', leaf: true })];
  s.$refs = {
    tbody: { querySelectorAll: () => checked },
    form: { dataset: { volume: 'vol1' } },
  };
  s.count = 1;

  const href = s.singleDownloadHref();
  assert.ok(href.startsWith('/api/download?'));
  const params = new URLSearchParams(href.split('?')[1]);
  assert.equal(params.get('volume'), 'vol1');
  assert.equal(params.get('filepath'), 'abc');
  assert.equal(params.get('name'), 'f.txt');
});

test('singleDownloadHref returns # when nothing is selected', () => {
  const { fileGridState } = loadApp();
  const s = fileGridState();
  s.$refs = { tbody: { querySelectorAll: () => [] }, form: { dataset: { volume: 'v' } } };
  assert.equal(s.singleDownloadHref(), '#');
});

test('bundleHref carries volume, name, format and one item param per selection', () => {
  const { fileGridState } = loadApp();
  const s = fileGridState();
  const checked = [
    checkbox({ filepath: 'a', name: 'a', leaf: true }),
    checkbox({ filepath: 'b', name: 'b', leaf: true }),
  ];
  s.$refs = {
    tbody: { querySelectorAll: () => checked },
    form: { dataset: { volume: 'vol9' } },
  };
  s.crumbs = [{ label: 'etc', filepath: '/etc' }];

  const qs = new URLSearchParams(s.bundleHref('targz').split('?')[1]);
  assert.equal(qs.get('volume'), 'vol9');
  assert.equal(qs.get('name'), 'etc');
  assert.equal(qs.get('format'), 'targz');
  assert.equal(qs.getAll('item').length, 2);
});

test('archiveBaseName uses a single selected folder name, else the last crumb', () => {
  const { fileGridState } = loadApp();
  const s = fileGridState();
  let checked = [checkbox({ filepath: 'd', name: 'myfolder', leaf: false })];
  s.$refs = { tbody: { querySelectorAll: () => checked } };
  assert.equal(s.archiveBaseName, 'myfolder');

  checked = [checkbox({ leaf: true }), checkbox({ leaf: true })];
  s.crumbs = [{ label: 'Root' }, { label: 'var' }];
  assert.equal(s.archiveBaseName, 'var');
});

test('openRestore resets the modal and stashes the caller-supplied guest/snapshot context', () => {
  const { fileGridState } = loadApp();
  const s = fileGridState();
  s.restoreError = 'stale error';
  s.restoreSubmitted = true;
  s.restoreMetadata = true;
  s.restoreVerify = true;
  s.openRestore('qemu', '133', 'web (133)', '2026-08-30T14:48:06Z');
  assert.equal(s.restoreOpen, true);
  assert.equal(s.restoreDestDir, '');
  assert.equal(s.restoreOverwrite, false);
  assert.equal(s.restoreMetadata, false);
  assert.equal(s.restoreVerify, false);
  assert.equal(s.restoreError, null);
  assert.equal(s.restoreSubmitted, false);
  assert.equal(s._guestType, 'qemu');
  assert.equal(s._guestVmid, '133');
  assert.equal(s._guestLabel, 'web (133)');
  assert.equal(s._snapshotTime, '2026-08-30T14:48:06Z');
});

test('startRestore is a no-op without a single selection, dest dir, and confirmed overwrite', async () => {
  const { fileGridState } = loadApp();
  const s = fileGridState();
  const checked = [checkbox({ filepath: 'a', name: 'hosts', leaf: true })];
  s.$refs = { tbody: { querySelectorAll: () => checked }, form: { dataset: { volume: 'vol' } } };
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async () => {
    throw new Error('fetch should not have been called');
  };
  try {
    s.restoreDestDir = '';
    s.restoreOverwrite = true;
    await s.startRestore();
    assert.equal(s.restoreSubmitted, false);

    s.restoreDestDir = '/etc';
    s.restoreOverwrite = false;
    await s.startRestore();
    assert.equal(s.restoreSubmitted, false);
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test('startRestore posts the expected fields and marks submitted on success', async () => {
  const { fileGridState } = loadApp();
  const s = fileGridState();
  const checked = [checkbox({ filepath: 'L2V0Yy9ob3N0cw==', name: 'hosts', leaf: true })];
  s.$refs = { tbody: { querySelectorAll: () => checked }, form: { dataset: { volume: 'pbs:backup/vm/133/x' } } };
  s.openRestore('qemu', '133', 'web (133)', '2026-08-30T14:48:06Z');
  s.restoreDestDir = 'C:\\Windows\\Temp';
  s.restoreOverwrite = true;

  let posted = null;
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async (url, opts) => {
    posted = { url, body: opts.body };
    return { ok: true, json: async () => ({ id: 'job-1', status: 'queued' }) };
  };
  try {
    await s.startRestore();
  } finally {
    globalThis.fetch = originalFetch;
  }

  assert.equal(posted.url, '/api/restore');
  const qs = new URLSearchParams(posted.body);
  assert.equal(qs.get('volume'), 'pbs:backup/vm/133/x');
  assert.equal(qs.get('filepath'), 'L2V0Yy9ob3N0cw==');
  assert.equal(qs.get('name'), 'hosts');
  assert.equal(qs.get('guest_type'), 'qemu');
  assert.equal(qs.get('vmid'), '133');
  assert.equal(qs.get('guest_label'), 'web (133)');
  assert.equal(qs.get('snapshot_time'), '2026-08-30T14:48:06Z');
  assert.equal(qs.get('dest_dir'), 'C:\\Windows\\Temp');
  assert.equal(qs.get('overwrite'), 'true');
  assert.equal(qs.get('restore_metadata'), 'false');
  assert.equal(qs.get('verify'), 'false');
  assert.equal(qs.has('source_mtime'), false);
  assert.equal(s.restoreSubmitted, true);
  assert.equal(s.restoreSubmitting, false);
  assert.equal(s.restoreError, null);
});

test('startRestore includes restore_metadata/verify/source_mtime when set', async () => {
  const { fileGridState } = loadApp();
  const s = fileGridState();
  const checked = [checkbox({ filepath: 'a', name: 'hosts', leaf: true, mtime: 1700000000 })];
  s.$refs = { tbody: { querySelectorAll: () => checked }, form: { dataset: { volume: 'vol' } } };
  s.openRestore('qemu', '133', 'web (133)', '2026-08-30T14:48:06Z');
  s.restoreDestDir = '/etc';
  s.restoreOverwrite = true;
  s.restoreMetadata = true;
  s.restoreVerify = true;

  let posted = null;
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async (url, opts) => {
    posted = opts.body;
    return { ok: true, json: async () => ({ id: 'job-1', status: 'queued' }) };
  };
  try {
    await s.startRestore();
  } finally {
    globalThis.fetch = originalFetch;
  }

  const qs = new URLSearchParams(posted);
  assert.equal(qs.get('restore_metadata'), 'true');
  assert.equal(qs.get('verify'), 'true');
  assert.equal(qs.get('source_mtime'), '1700000000');
});

test('startRestore omits source_mtime when the selected item has none', async () => {
  const { fileGridState } = loadApp();
  const s = fileGridState();
  const checked = [checkbox({ filepath: 'a', name: 'hosts', leaf: true, mtime: null })];
  s.$refs = { tbody: { querySelectorAll: () => checked }, form: { dataset: { volume: 'vol' } } };
  s.openRestore('qemu', '133', 'web (133)', '2026-08-30T14:48:06Z');
  s.restoreDestDir = '/etc';
  s.restoreOverwrite = true;

  let posted = null;
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async (url, opts) => {
    posted = opts.body;
    return { ok: true, json: async () => ({ id: 'job-1', status: 'queued' }) };
  };
  try {
    await s.startRestore();
  } finally {
    globalThis.fetch = originalFetch;
  }

  const qs = new URLSearchParams(posted);
  assert.equal(qs.has('source_mtime'), false);
});

test('startRestore surfaces the server-provided detail message on failure', async () => {
  const { fileGridState } = loadApp();
  const s = fileGridState();
  const checked = [checkbox({ filepath: 'a', name: 'hosts', leaf: true })];
  s.$refs = { tbody: { querySelectorAll: () => checked }, form: { dataset: { volume: 'vol' } } };
  s.openRestore('qemu', '133', 'web (133)', '2026-08-30T14:48:06Z');
  s.restoreDestDir = '/etc';
  s.restoreOverwrite = true;

  const originalFetch = globalThis.fetch;
  globalThis.fetch = async () => ({ ok: false, json: async () => ({ detail: 'guest agent unavailable' }) });
  try {
    await s.startRestore();
  } finally {
    globalThis.fetch = originalFetch;
  }

  assert.equal(s.restoreError, 'guest agent unavailable');
  assert.equal(s.restoreSubmitted, false);
});

test('startRestore surfaces a message when the fetch itself throws', async () => {
  const { fileGridState } = loadApp();
  const s = fileGridState();
  const checked = [checkbox({ filepath: 'a', name: 'hosts', leaf: true })];
  s.$refs = { tbody: { querySelectorAll: () => checked }, form: { dataset: { volume: 'vol' } } };
  s.openRestore('qemu', '133', 'web (133)', '2026-08-30T14:48:06Z');
  s.restoreDestDir = '/etc';
  s.restoreOverwrite = true;

  const originalFetch = globalThis.fetch;
  globalThis.fetch = async () => {
    throw new Error('offline');
  };
  try {
    await s.startRestore();
  } finally {
    globalThis.fetch = originalFetch;
  }

  assert.ok(s.restoreError.includes('offline'));
  assert.equal(s.restoreSubmitted, false);
});

test('openRestore kicks off an initial browseInto(null) when browsing is available', async () => {
  const { fileGridState } = loadApp();
  const s = fileGridState();
  let requestedUrl = null;
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async (url) => {
    requestedUrl = url;
    return { ok: true, json: async () => ({ path: null, parent: null, entries: [{ name: 'C:', path: 'C:\\' }] }) };
  };
  try {
    s.openRestore('qemu', '133', 'web (133)', '2026-08-30T14:48:06Z', true);
    await new Promise((r) => setTimeout(r, 0)); // let the fire-and-forget browseInto() settle
  } finally {
    globalThis.fetch = originalFetch;
  }
  assert.equal(s.restoreBrowsing, true);
  assert.ok(requestedUrl.startsWith('/api/restore-browse?'));
  assert.ok(!requestedUrl.includes('path='));
  assert.deepEqual(s.restoreBrowseEntries, [{ name: 'C:', path: 'C:\\' }]);
});

test('openRestore does not browse when browsing is unavailable', () => {
  const { fileGridState } = loadApp();
  const s = fileGridState();
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async () => {
    throw new Error('should not be called');
  };
  try {
    s.openRestore('qemu', '133', 'web (133)', '2026-08-30T14:48:06Z', false);
  } finally {
    globalThis.fetch = originalFetch;
  }
  assert.equal(s.restoreBrowsing, false);
});

test('browseInto updates path/parent/entries and mirrors destDir', async () => {
  const { fileGridState } = loadApp();
  const s = fileGridState();
  s._guestType = 'qemu';
  s._guestVmid = '133';
  const originalFetch = globalThis.fetch;
  let requestedUrl = null;
  globalThis.fetch = async (url) => {
    requestedUrl = url;
    return {
      ok: true,
      json: async () => ({
        path: '/etc',
        parent: '/',
        separator: '/',
        entries: [{ name: 'nginx', path: '/etc/nginx' }],
      }),
    };
  };
  try {
    await s.browseInto('/etc');
  } finally {
    globalThis.fetch = originalFetch;
  }
  assert.ok(requestedUrl.includes('path=%2Fetc'));
  assert.equal(s.restoreBrowsePath, '/etc');
  assert.equal(s.restoreBrowseParent, '/');
  assert.deepEqual(s.restoreBrowseEntries, [{ name: 'nginx', path: '/etc/nginx' }]);
  assert.equal(s.restoreDestDir, '/etc');
  assert.equal(s.restoreBrowseLoading, false);
});

test('browseInto(null) does not overwrite restoreDestDir (drive-list has no path)', async () => {
  const { fileGridState } = loadApp();
  const s = fileGridState();
  s.restoreDestDir = 'should-stay';
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async () => ({
    ok: true,
    json: async () => ({ path: null, parent: null, entries: [] }),
  });
  try {
    await s.browseInto(null);
  } finally {
    globalThis.fetch = originalFetch;
  }
  assert.equal(s.restoreDestDir, 'should-stay');
  assert.equal(s.restoreBrowsePath, null);
});

test('browseInto surfaces the server error detail', async () => {
  const { fileGridState } = loadApp();
  const s = fileGridState();
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async () => ({ ok: false, json: async () => ({ detail: 'guest-exec disabled' }) });
  try {
    await s.browseInto('/etc');
  } finally {
    globalThis.fetch = originalFetch;
  }
  assert.equal(s.restoreBrowseError, 'guest-exec disabled');
});

test('browseUp browses into the current parent, including null (top level)', async () => {
  const { fileGridState } = loadApp();
  const s = fileGridState();
  s.restoreBrowseParent = '/etc';
  let requestedUrl = null;
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async (url) => {
    requestedUrl = url;
    return { ok: true, json: async () => ({ path: '/etc', parent: '/', entries: [] }) };
  };
  try {
    await s.browseUp();
  } finally {
    globalThis.fetch = originalFetch;
  }
  assert.ok(requestedUrl.includes('path=%2Fetc'));
});

test('setDestMode switches to manual, then back re-browses the current path', async () => {
  const { fileGridState } = loadApp();
  const s = fileGridState();
  s.restoreBrowsing = true;
  s.restoreBrowsePath = '/etc';
  s.setDestMode('manual');
  assert.equal(s.restoreBrowsing, false);

  let requestedUrl = null;
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async (url) => {
    requestedUrl = url;
    return { ok: true, json: async () => ({ path: '/etc', parent: '/', entries: [] }) };
  };
  try {
    s.setDestMode('browse');
    await new Promise((r) => setTimeout(r, 0));
  } finally {
    globalThis.fetch = originalFetch;
  }
  assert.equal(s.restoreBrowsing, true);
  assert.ok(requestedUrl.includes('path=%2Fetc'));
});

test('setDestMode is a no-op when the requested mode is already active', async () => {
  const { fileGridState } = loadApp();
  const s = fileGridState();
  s.restoreBrowsing = true;
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async () => {
    throw new Error('should not re-fetch when already in browse mode');
  };
  try {
    s.setDestMode('browse');
  } finally {
    globalThis.fetch = originalFetch;
  }
  assert.equal(s.restoreBrowsing, true);
});
