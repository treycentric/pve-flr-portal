import test from 'node:test';
import assert from 'node:assert/strict';
import { loadApp } from './helpers.mjs';

const snaps = [
  { volume: 'v1', time: '2026-01-01T00:00:00' },
  { volume: 'v2', time: '2026-02-01T00:00:00' },
  { volume: 'v3', time: '2026-03-01T00:00:00' },
];

test('constructor sorts snapshots ascending by date and attaches a Date', () => {
  const { portalApp } = loadApp();
  const a = portalApp([snaps[2], snaps[0], snaps[1]]);
  assert.deepEqual(a.snapshots.map((s) => s.volume), ['v1', 'v2', 'v3']);
  assert.ok(a.snapshots[0].date instanceof Date);
});

test('selectedSnapshotTime returns the matching snapshot time, else empty string', () => {
  const { portalApp } = loadApp();
  const a = portalApp(snaps);
  assert.equal(a.selectedSnapshotTime, '');
  a.selectedVolume = 'v2';
  assert.equal(a.selectedSnapshotTime, '2026-02-01T00:00:00');
});

test('_loadRestoreCapabilities is a no-op without a guest vmid', async () => {
  const { portalApp } = loadApp();
  const a = portalApp(snaps, { type: 'qemu', vmid: null });
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async () => {
    throw new Error('fetch should not have been called');
  };
  try {
    await a._loadRestoreCapabilities();
    assert.equal(a.restoreCaps, null);
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test('_loadRestoreCapabilities stores the parsed response on success', async () => {
  const { portalApp } = loadApp();
  const a = portalApp(snaps, { type: 'qemu', vmid: '133' });
  const originalFetch = globalThis.fetch;
  let requestedUrl = null;
  globalThis.fetch = async (url) => {
    requestedUrl = url;
    return { ok: true, json: async () => ({ design_a: { available: true, reason: null } }) };
  };
  try {
    await a._loadRestoreCapabilities();
    assert.ok(requestedUrl.startsWith('/api/restore-capabilities?'));
    assert.ok(requestedUrl.includes('type=qemu'));
    assert.ok(requestedUrl.includes('vmid=133'));
    assert.deepEqual(a.restoreCaps, { design_a: { available: true, reason: null } });
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test('_loadRestoreCapabilities leaves restoreCaps null on a non-ok response', async () => {
  const { portalApp } = loadApp();
  const a = portalApp(snaps, { type: 'qemu', vmid: '133' });
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async () => ({ ok: false });
  try {
    await a._loadRestoreCapabilities();
    assert.equal(a.restoreCaps, null);
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test('_loadRestoreCapabilities swallows a network error and leaves restoreCaps null', async () => {
  const { portalApp } = loadApp();
  const a = portalApp(snaps, { type: 'qemu', vmid: '133' });
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async () => {
    throw new Error('network down');
  };
  try {
    await a._loadRestoreCapabilities();
    assert.equal(a.restoreCaps, null);
  } finally {
    globalThis.fetch = originalFetch;
  }
});

// _w is the measured pixel width of the SVG (renderTimeline sets it from
// getBoundingClientRect, so one SVG unit is one pixel). Set it explicitly
// here rather than leaning on the unmeasured default.
test('xFor maps viewStart to 0, viewEnd to _w, midpoint to half _w', () => {
  const { portalApp } = loadApp();
  const a = portalApp(snaps);
  a._w = 800;
  a.viewStart = new Date(2026, 0, 1);
  a.viewEnd = new Date(2026, 0, 31);
  assert.equal(a.xFor(new Date(2026, 0, 1)), 0);
  assert.equal(a.xFor(new Date(2026, 0, 31)), 800);
  assert.equal(a.xFor(new Date(2026, 0, 16)), 400);
});

test('xFor returns 0 for a non-positive view range', () => {
  const { portalApp } = loadApp();
  const a = portalApp(snaps);
  a.viewStart = new Date(2026, 0, 1);
  a.viewEnd = new Date(2026, 0, 1);
  assert.equal(a.xFor(new Date(2026, 0, 5)), 0);
});

test('groupsInView groups by calendar day and excludes out-of-window snapshots', () => {
  const { portalApp } = loadApp();
  const a = portalApp([
    { volume: 'a', time: '2026-02-10T08:00:00' },
    { volume: 'b', time: '2026-02-10T09:30:00' },
    { volume: 'c', time: '2026-06-01T00:00:00' },
  ]);
  a.viewStart = new Date(2026, 1, 1);
  a.viewEnd = new Date(2026, 2, 1);
  const groups = a.groupsInView();
  assert.equal(groups.length, 1);
  assert.equal(groups[0].items.length, 2);
  assert.match(groups[0].key, /^\d{4}-\d{2}-\d{2}$/);
});

test('ticksInView returns 7 evenly spaced points when zoomed past 120 days', () => {
  const { portalApp } = loadApp();
  const a = portalApp(snaps);
  a._w = 800;
  a.viewStart = new Date(2026, 0, 1);
  a.viewEnd = new Date(2026, 11, 31);
  const ticks = a.ticksInView();
  assert.equal(ticks.length, 7);
  assert.equal(ticks[0].x, 0);
  assert.equal(ticks[6].x, 800);
});

test('ticksInView marks month starts inside a day-resolution window', () => {
  const { portalApp } = loadApp();
  const a = portalApp(snaps);
  a.viewStart = new Date(2026, 0, 20);
  a.viewEnd = new Date(2026, 2, 5);
  assert.ok(a.ticksInView().some((t) => t.isMonth));
});

test('selectedIndex / hasOlder / hasNewer follow selectedVolume', () => {
  const { portalApp } = loadApp();
  const a = portalApp(snaps);
  a.selectedVolume = 'v2';
  assert.equal(a.selectedIndex, 1);
  assert.equal(a.hasOlder, true);
  assert.equal(a.hasNewer, true);
  a.selectedVolume = 'v1';
  assert.equal(a.hasOlder, false);
  a.selectedVolume = 'v3';
  assert.equal(a.hasNewer, false);
});

test('_formatTimestamp zero-pads every component', () => {
  const { portalApp } = loadApp();
  const a = portalApp(snaps);
  const iso = '2026-03-04T05:06:07';
  const d = new Date(iso);
  const pad = (n) => String(n).padStart(2, '0');
  const expected =
    `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ` +
    `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
  assert.equal(a._formatTimestamp(iso), expected);
});
