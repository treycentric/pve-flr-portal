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

test('startRestore is a no-op without any selection, dest dir, and confirmed overwrite', async () => {
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

    // Unlike single-file-only restore before multi-file restore existed
    // (docs/plan.md §7.7, issue #24), more than one selection is now
    // valid, not a no-op - only a genuinely empty selection is.
    s.$refs.tbody.querySelectorAll = () => [];
    s.restoreDestDir = '/etc';
    s.restoreOverwrite = true;
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

test('startRestore posts item[] params for a multi-selection bundle restore, not filepath/name', async () => {
  const { fileGridState } = loadApp();
  const s = fileGridState();
  const checked = [
    checkbox({ filepath: 'L2V0Yw==', name: 'etc', leaf: false }),
    checkbox({ filepath: 'L2hvbWUvZmlsZQ==', name: 'file', leaf: true }),
  ];
  s.$refs = { tbody: { querySelectorAll: () => checked }, form: { dataset: { volume: 'pbs:backup/vm/133/x' } } };
  s.openRestore('qemu', '133', 'web (133)', '2026-08-30T14:48:06Z');
  s.restoreDestDir = '/home/user/restore';
  s.restoreOverwrite = true;
  s.restoreMetadata = true; // should be ignored - not sent for a bundle restore
  s.restoreVerify = true; // same

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
  assert.equal(qs.has('filepath'), false);
  assert.equal(qs.has('name'), false);
  assert.equal(qs.has('restore_metadata'), false);
  assert.equal(qs.has('verify'), false);
  assert.equal(qs.get('dest_dir'), '/home/user/restore');
  const items = qs.getAll('item').map((raw) => JSON.parse(raw));
  assert.deepEqual(items, [
    { filepath: 'L2V0Yw==', name: 'etc', leaf: false },
    { filepath: 'L2hvbWUvZmlsZQ==', name: 'file', leaf: true },
  ]);
  assert.equal(s.restoreSubmitted, true);
});

test('startRestore treats a single selected directory as a bundle restore too', async () => {
  const { fileGridState } = loadApp();
  const s = fileGridState();
  const checked = [checkbox({ filepath: 'L2V0Yw==', name: 'etc', leaf: false })];
  s.$refs = { tbody: { querySelectorAll: () => checked }, form: { dataset: { volume: 'vol' } } };
  s.openRestore('qemu', '133', 'web (133)', '2026-08-30T14:48:06Z');
  s.restoreDestDir = '/restore';
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
  assert.equal(qs.has('filepath'), false);
  assert.equal(qs.getAll('item').length, 1);
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

test('openRestore kicks off an initial browseInto(null) and checks original-location when browsing is available', async () => {
  const { fileGridState } = loadApp();
  const s = fileGridState();
  s.$refs = { form: { dataset: { volume: 'vol1' } } };
  const requestedUrls = [];
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async (url) => {
    requestedUrls.push(url);
    if (url.startsWith('/api/restore-original-path')) {
      // Unavailable for this item - openRestore should step the mode
      // down to 'browse' once this resolves (issue #68).
      return { ok: true, json: async () => ({ available: false, directory: null, reason: 'no drive letter' }) };
    }
    return { ok: true, json: async () => ({ path: null, parent: null, entries: [{ name: 'C:', path: 'C:\\' }] }) };
  };
  try {
    s.openRestore('qemu', '133', 'web (133)', '2026-08-30T14:48:06Z', true, [{ label: 'Root', filepath: '/' }]);
    await new Promise((r) => setTimeout(r, 0)); // let the fire-and-forget checks settle
  } finally {
    globalThis.fetch = originalFetch;
  }
  assert.equal(s.restoreDestMode, 'browse');
  assert.ok(requestedUrls.some((u) => u.startsWith('/api/restore-original-path?')));
  const browseUrl = requestedUrls.find((u) => u.startsWith('/api/restore-browse?'));
  assert.ok(browseUrl);
  assert.ok(!browseUrl.includes('path='));
  assert.deepEqual(s.restoreBrowseEntries, [{ name: 'C:', path: 'C:\\' }]);
});

test('openRestore does not browse or check original-location when browsing is unavailable', () => {
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
  assert.equal(s.restoreDestMode, 'manual');
  assert.equal(s.restoreOriginalChecking, false);
});

test('checkOriginalLocation populates the resolved directory and keeps original mode active on success', async () => {
  const { fileGridState } = loadApp();
  const s = fileGridState();
  s.$refs = { form: { dataset: { volume: 'vol1' } } };
  s._guestType = 'qemu';
  s._guestVmid = '133';
  s._crumbs = [{ label: 'Root', filepath: '/' }];
  s.restoreDestMode = 'original';
  let requestedUrl = null;
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async (url) => {
    requestedUrl = url;
    return { ok: true, json: async () => ({ available: true, directory: '/home/alice', reason: null }) };
  };
  try {
    await s.checkOriginalLocation();
  } finally {
    globalThis.fetch = originalFetch;
  }
  assert.ok(requestedUrl.startsWith('/api/restore-original-path?'));
  assert.equal(s.restoreOriginalAvailable, true);
  assert.equal(s.restoreOriginalDirectory, '/home/alice');
  assert.equal(s.restoreDestDir, '/home/alice');
  assert.equal(s.restoreDestMode, 'original');
  assert.equal(s.restoreOriginalChecking, false);
});

test('checkOriginalLocation steps down to browse and surfaces the reason when unavailable', async () => {
  const { fileGridState } = loadApp();
  const s = fileGridState();
  s.$refs = { form: { dataset: { volume: 'vol1' } } };
  s.restoreBrowseAvailable = true;
  s.restoreDestMode = 'original';
  s.restoreBrowsePath = null;
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async (url) => {
    if (url.startsWith('/api/restore-original-path')) {
      return { ok: true, json: async () => ({ available: false, directory: null, reason: 'not mounted' }) };
    }
    return { ok: true, json: async () => ({ path: '/', parent: null, entries: [] }) };
  };
  try {
    await s.checkOriginalLocation();
  } finally {
    globalThis.fetch = originalFetch;
  }
  assert.equal(s.restoreOriginalAvailable, false);
  assert.equal(s.restoreOriginalReason, 'not mounted');
  assert.equal(s.restoreDestMode, 'browse');
});

test('checkOriginalLocation surfaces a message and steps down when the fetch itself throws', async () => {
  const { fileGridState } = loadApp();
  const s = fileGridState();
  s.$refs = { form: { dataset: { volume: 'vol1' } } };
  s.restoreBrowseAvailable = false;
  s.restoreDestMode = 'original';
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async () => {
    throw new Error('offline');
  };
  try {
    await s.checkOriginalLocation();
  } finally {
    globalThis.fetch = originalFetch;
  }
  assert.equal(s.restoreOriginalAvailable, false);
  assert.ok(s.restoreOriginalReason.includes('offline'));
  assert.equal(s.restoreDestMode, 'manual'); // no browse available either, so falls all the way back
});

test('browseInto updates path/parent/entries and mirrors destDir', async () => {
  const { fileGridState } = loadApp();
  const s = fileGridState();
  s._guestType = 'qemu';
  s._guestVmid = '133';
  s.restoreDestMode = 'browse'; // browseInto only steers restoreDestDir while this is the active mode
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

test('restoreBrowseStatusText prioritizes loading, then error, then empty, else null', () => {
  // 2026-09-02: status messages used to render inside the folder-list
  // box; moved into the toolbar's path display instead - this getter
  // is what decides what (if anything) overrides the real path there.
  const { fileGridState } = loadApp();
  const s = fileGridState();

  s.restoreBrowseLoading = true;
  s.restoreBrowseError = 'should be hidden by loading';
  s.restoreBrowseEntries = [];
  assert.equal(s.restoreBrowseStatusText, 'Loading…');

  s.restoreBrowseLoading = false;
  assert.equal(s.restoreBrowseStatusText, 'should be hidden by loading');

  s.restoreBrowseError = null;
  s.restoreBrowseEntries = [];
  assert.equal(s.restoreBrowseStatusText, 'No subfolders here.');

  s.restoreBrowseEntries = [{ name: 'etc', path: '/etc' }];
  assert.equal(s.restoreBrowseStatusText, null);
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
  s.restoreDestMode = 'browse';
  s.restoreBrowsePath = '/etc';
  s.setDestMode('manual');
  assert.equal(s.restoreDestMode, 'manual');

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
  assert.equal(s.restoreDestMode, 'browse');
  assert.ok(requestedUrl.includes('path=%2Fetc'));
});

test('setDestMode is a no-op when the requested mode is already active', async () => {
  const { fileGridState } = loadApp();
  const s = fileGridState();
  s.restoreDestMode = 'browse';
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async () => {
    throw new Error('should not re-fetch when already in browse mode');
  };
  try {
    s.setDestMode('browse');
  } finally {
    globalThis.fetch = originalFetch;
  }
  assert.equal(s.restoreDestMode, 'browse');
});

test('setDestMode switching to original restores the cached resolved directory without re-fetching', () => {
  const { fileGridState } = loadApp();
  const s = fileGridState();
  s.restoreDestMode = 'manual';
  s.restoreOriginalDirectory = '/home/alice';
  s.restoreDestDir = 'something-else';
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async () => {
    throw new Error('should not fetch - the resolved directory is already cached');
  };
  try {
    s.setDestMode('original');
  } finally {
    globalThis.fetch = originalFetch;
  }
  assert.equal(s.restoreDestMode, 'original');
  assert.equal(s.restoreDestDir, '/home/alice');
});
