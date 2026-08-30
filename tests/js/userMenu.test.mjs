import test from 'node:test';
import assert from 'node:assert/strict';
import { loadApp } from './helpers.mjs';

test('stores the identity and starts closed', () => {
  const { userMenu } = loadApp();
  const m = userMenu('bob@pve');
  assert.equal(m.identity, 'bob@pve');
  assert.equal(m.open, false);
  assert.equal(m.aboutOpen, false);
});

test('logout closes the menu and navigates to /logout', () => {
  const { userMenu, window } = loadApp();
  const m = userMenu('alice@pam');
  m.open = true;
  m.logout();
  assert.equal(m.open, false);
  assert.equal(window.location, '/logout');
});
