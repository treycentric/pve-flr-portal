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

test('groupsInView groups by the level-3 calendar-day bucket and excludes out-of-window snapshots', () => {
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

test('groupsInView splits same-day snapshots into hourly buckets at level 2, merges them at level 5', () => {
  const { portalApp } = loadApp();
  const a = portalApp([
    { volume: 'a', time: '2026-02-10T08:00:00' },
    { volume: 'b', time: '2026-02-10T09:30:00' },
  ]);
  a.viewStart = new Date(2026, 1, 1);
  a.viewEnd = new Date(2026, 2, 1);
  a.zoomLevel = 2;
  assert.equal(a.groupsInView().length, 2);
  a.zoomLevel = 5;
  assert.equal(a.groupsInView().length, 1);
});

test('ticksInView level 3 marks month starts as two-line major labels', () => {
  const { portalApp } = loadApp();
  const a = portalApp(snaps);
  a.zoomLevel = 3;
  a.viewStart = new Date(2026, 0, 20);
  a.viewEnd = new Date(2026, 2, 5);
  const ticks = a.ticksInView();
  const major = ticks.find((t) => t.major);
  assert.ok(major);
  assert.equal(major.lines.length, 2);
  assert.equal(major.lines[0], '2026');
});

test('ticksInView level 1 makes a major every 10 minutes and labels even minor minutes', () => {
  const { portalApp } = loadApp();
  const a = portalApp(snaps);
  a.zoomLevel = 1;
  a.viewStart = new Date(2026, 0, 1, 11, 0, 0);
  a.viewEnd = new Date(2026, 0, 1, 11, 30, 0);
  const ticks = a.ticksInView();
  assert.deepEqual(
    ticks.filter((t) => t.major).map((t) => t.lines[0]),
    ['11:00', '11:10', '11:20', '11:30'],
  );
  assert.ok(ticks.some((t) => !t.major && t.lines[0] === '04'));
  assert.ok(!ticks.some((t) => !t.major && t.lines.length === 1 && Number(t.lines[0]) % 2 === 1));
});

test('ticksInView level 5 makes January the two-line major and labels even months', () => {
  const { portalApp } = loadApp();
  const a = portalApp(snaps);
  a.zoomLevel = 5;
  a.viewStart = new Date(2025, 10, 1);
  a.viewEnd = new Date(2026, 4, 1);
  const ticks = a.ticksInView();
  const jan = ticks.find((t) => t.major);
  assert.deepEqual(jan.lines, ['2026', 'Jan']);
  assert.ok(ticks.some((t) => !t.major && t.lines[0] === 'Feb'));
  assert.ok(ticks.some((t) => !t.major && t.lines[0] === 'Apr'));
});

test('stepZoom moves between the five levels, clamps at the ends, and resizes the span to match', () => {
  const { portalApp } = loadApp();
  const a = portalApp(snaps);
  a.$refs = {}; // renderTimeline bails without an SVG ref
  a.viewStart = new Date(2026, 0, 1);
  a.viewEnd = new Date(2026, 3, 17); // ~level-3 span, centered mid-Feb
  const mid = (a.viewStart.getTime() + a.viewEnd.getTime()) / 2;

  a.stepZoom(-1);
  assert.equal(a.zoomLevel, 2);
  assert.equal(a.viewEnd - a.viewStart, 60 * 60 * 60 * 1000); // 60 hourly minors
  assert.equal((a.viewStart.getTime() + a.viewEnd.getTime()) / 2, mid);

  a.stepZoom(-1);
  assert.equal(a.zoomLevel, 1);
  a.stepZoom(-1);
  assert.equal(a.zoomLevel, 1); // clamped

  a.zoomLevel = 5;
  a.stepZoom(1);
  assert.equal(a.zoomLevel, 5); // clamped
});

test('the selected callout number is the position within its own tick group, not a count and not a global index', () => {
  const { portalApp } = loadApp();
  const a = portalApp([
    { volume: 'a', time: '2026-02-10T08:00:00' },
    { volume: 'b', time: '2026-02-11T08:00:00' },
    { volume: 'c', time: '2026-02-11T09:30:00' },
    { volume: 'd', time: '2026-02-11T20:00:00' },
  ]);
  a.viewStart = new Date(2026, 1, 1);
  a.viewEnd = new Date(2026, 2, 1);
  const [lone, cluster] = a.groupsInView();

  // A lone snapshot always reads "1", even though it is 1st of 4 overall.
  a.selectedVolume = 'a';
  assert.equal(a._calloutPosition(lone), 1);

  // Picking the 3rd of a 3-snapshot day reads "3", not the count and not
  // its global index (4).
  a.selectedVolume = 'd';
  assert.equal(a._calloutPosition(cluster), 3);
  assert.equal(a.selectedIndex + 1, 4);

  // The first member of the same cluster still reads "1".
  a.selectedVolume = 'b';
  assert.equal(a._calloutPosition(cluster), 1);
});

test('_thinLabels keeps every major label and drops minor labels that would collide', () => {
  const { portalApp } = loadApp();
  const a = portalApp(snaps);
  // Two majors 10px apart with a minor squeezed between them: both majors
  // survive, the minor loses its text but keeps its tick.
  const ticks = [
    { x: 0, major: true, lines: ['2026', 'Feb'] },
    { x: 5, major: false, lines: ['2'] },
    { x: 200, major: false, lines: ['20'] },
    { x: 210, major: true, lines: ['2026', 'Mar'] },
  ];
  const out = a._thinLabels(ticks);
  assert.deepEqual(out[0].lines, ['2026', 'Feb']);
  assert.deepEqual(out[1].lines, []); // collides with the Feb major
  assert.deepEqual(out[2].lines, []); // collides with the Mar major
  assert.deepEqual(out[3].lines, ['2026', 'Mar']);
  assert.equal(out.length, 4); // ticks themselves are never dropped
});

test('_thinLabels drops minor labels that collide with each other, keeping a spread subset', () => {
  const { portalApp } = loadApp();
  const a = portalApp(snaps);
  const ticks = [0, 8, 16, 24, 32].map((x) => ({ x, major: false, lines: ['28'] }));
  const kept = a._thinLabels(ticks).filter((t) => t.lines.length);
  // '28' is ~12px wide, so consecutive 8px-apart labels cannot all fit.
  assert.ok(kept.length < 5);
  assert.ok(kept.length >= 2);
  for (let i = 1; i < kept.length; i++) {
    assert.ok(kept[i].x - kept[i - 1].x >= 16);
  }
});

test('level 3 keeps the last even-day label of a month at a realistic panel width', () => {
  const { portalApp } = loadApp();
  const a = portalApp(snaps);
  a.zoomLevel = 3;
  a._w = 1100; // ~18px per day over the 60-day span

  const dayLabel = (start, end, y, m, d) => {
    a.viewStart = start;
    a.viewEnd = end;
    const target = new Date(y, m, d).getTime();
    return a.ticksInView().find((t) => Math.abs(a.xFor(new Date(y, m, d)) - t.x) < 0.5 && t.lines.length);
  };

  // Feb 2026 is not a leap year: the 28th is the last even day, one tick
  // before the March major.
  assert.ok(dayLabel(new Date(2026, 0, 15), new Date(2026, 2, 15), 2026, 1, 28), 'Feb 28 label missing');
  // Sep 2026 has 30 days.
  assert.ok(dayLabel(new Date(2026, 8, 1), new Date(2026, 9, 31), 2026, 8, 30), 'Sep 30 label missing');
});

test('level 3 walks real calendar days, so each month gets exactly its own day count of ticks', () => {
  const { portalApp } = loadApp();
  const a = portalApp(snaps);
  a.zoomLevel = 3;
  a._w = 1000;
  // Whole of Feb 2024 (leap) and Feb 2026 (common).
  a.viewStart = new Date(2024, 1, 1);
  a.viewEnd = new Date(2024, 1, 29, 23, 59);
  assert.equal(a.ticksInView().length, 29); // 1..29
  a.viewStart = new Date(2026, 1, 1);
  a.viewEnd = new Date(2026, 1, 28, 23, 59);
  assert.equal(a.ticksInView().length, 28); // 1..28
});

test('ticksInView thins colliding labels at a realistic level-3 width', () => {
  const { portalApp } = loadApp();
  const a = portalApp(snaps);
  a.zoomLevel = 3;
  a._w = 600; // narrow panel: 60 days over 600px is 10px/day
  a.viewStart = new Date(2026, 0, 1);
  a.viewEnd = new Date(2026, 2, 2);
  const labeled = a.ticksInView().filter((t) => t.lines.length);
  for (let i = 1; i < labeled.length; i++) {
    assert.ok(
      labeled[i].x - labeled[i - 1].x >= 10 - 1e-6,
      `labels at ${labeled[i - 1].x} and ${labeled[i].x} are too close`,
    );
  }
});

test('toggleDay anchors the picker over the callout it was opened from, and toggles shut', () => {
  const { portalApp } = loadApp();
  const a = portalApp(snaps);
  a._bubbleY = 12;
  a._bubbleHeight = 22;

  a.toggleDay('2026-02-11', snaps, 420);
  assert.equal(a.activeDayKey, '2026-02-11');
  assert.equal(a.activeDayX, 420);
  // Bottom edge of the popup == bottom edge of the dark-blue callout, so
  // the picker covers it rather than floating above it.
  assert.equal(a.activeDayTop, 34);

  a.toggleDay('2026-02-11', snaps, 420);
  assert.equal(a.activeDayKey, null);
  assert.deepEqual(a.activeDayItems, []);
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
