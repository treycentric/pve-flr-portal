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
  assert.equal(a.viewEnd - a.viewStart, 4 * 24 * 60 * 60 * 1000);
  assert.equal((a.viewStart.getTime() + a.viewEnd.getTime()) / 2, mid);

  a.stepZoom(-1);
  assert.equal(a.zoomLevel, 1);
  a.stepZoom(-1);
  assert.equal(a.zoomLevel, 1); // clamped

  a.zoomLevel = 5;
  a.stepZoom(1);
  assert.equal(a.zoomLevel, 5); // clamped
});

test('the selected callout number is the snapshot position among all snapshots, not a same-tick count', () => {
  const { portalApp } = loadApp();
  const a = portalApp(snaps);
  a.selectedVolume = 'v2';
  // selectedIndex drives the dark-blue callout label (posStr = index + 1).
  assert.equal(a.selectedIndex + 1, 2);
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
