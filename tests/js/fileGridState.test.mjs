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
