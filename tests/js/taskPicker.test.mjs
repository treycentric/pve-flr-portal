import test from 'node:test';
import assert from 'node:assert/strict';
import { loadApp } from './helpers.mjs';

const groups = [
  { type: 'vm', vmid: '133', name: 'web', last_backup: 1690000000 },
  { type: 'ct', vmid: '104', name: null, last_backup: 0 },
];

test('currentLabel uses the guest name when present', () => {
  const { taskPicker } = loadApp();
  assert.equal(taskPicker(groups, 'vm:133').currentLabel, '133 (web)');
});

test('currentLabel falls back to TYPE + vmid without a name', () => {
  const { taskPicker } = loadApp();
  assert.equal(taskPicker(groups, 'ct:104').currentLabel, 'CT 104');
});

test('filtered matches vmid, type or name and returns all when empty', () => {
  const { taskPicker } = loadApp();
  const p = taskPicker(groups, 'vm:133');
  assert.equal(p.filtered.length, 2);
  p.filter = 'web';
  assert.deepEqual(p.filtered.map((g) => g.vmid), ['133']);
  p.filter = 'ct';
  assert.deepEqual(p.filtered.map((g) => g.vmid), ['104']);
  p.filter = '104';
  assert.deepEqual(p.filtered.map((g) => g.vmid), ['104']);
  p.filter = '   ';
  assert.equal(p.filtered.length, 2);
});

test('formatLastBackup returns empty string for a falsy timestamp', () => {
  const { taskPicker } = loadApp();
  assert.equal(taskPicker(groups, 'vm:133').formatLastBackup(0), '');
});

test('formatLastBackup renders a zero-padded local timestamp', () => {
  const { taskPicker } = loadApp();
  const ts = 1690000000;
  const d = new Date(ts * 1000);
  const pad = (n) => String(n).padStart(2, '0');
  const expected =
    `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ` +
    `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
  assert.equal(taskPicker(groups, 'vm:133').formatLastBackup(ts), expected);
});

test('confirm navigates to the encoded task', () => {
  const { taskPicker, window } = loadApp();
  const p = taskPicker(groups, 'vm:133');
  p.selected = 'ct:104';
  p.confirm();
  assert.equal(window.location, '/?task=ct%3A104');
});
