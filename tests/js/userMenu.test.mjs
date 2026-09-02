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

// --- Color theme (issue #29) ---

function themeEnv(stored, osPrefersLight = false) {
  const store = stored ? { pfr_theme: stored } : {};
  const attrs = {};
  const window = {
    location: {},
    localStorage: {
      getItem: (k) => (k in store ? store[k] : null),
      setItem: (k, v) => {
        store[k] = v;
      },
    },
    matchMedia: () => ({ matches: osPrefersLight, addEventListener() {}, addListener() {} }),
  };
  const document = {
    documentElement: {
      setAttribute: (k, v) => {
        attrs[k] = v;
      },
    },
  };
  return { window, document, store, attrs };
}

test('init defaults to auto and, with no OS preference, resolves to dark', () => {
  const { window, document, attrs } = themeEnv(null);
  const { userMenu } = loadApp({ window, document });
  const m = userMenu('bob@pve');
  m.init();
  assert.equal(m.theme, 'auto');
  assert.equal(attrs['data-theme'], 'dark');
});

test('init falls back to the admin default theme when nothing is stored', () => {
  const { window, document, attrs } = themeEnv(null);
  window.__PFR_DEFAULT_THEME__ = 'light';
  const { userMenu } = loadApp({ window, document });
  const m = userMenu('bob@pve');
  m.init();
  assert.equal(m.theme, 'light');
  assert.equal(attrs['data-theme'], 'light');
});

test('a stored choice overrides the admin default', () => {
  const { window, document, attrs } = themeEnv('dark');
  window.__PFR_DEFAULT_THEME__ = 'light';
  const { userMenu } = loadApp({ window, document });
  const m = userMenu('bob@pve');
  m.init();
  assert.equal(m.theme, 'dark');
  assert.equal(attrs['data-theme'], 'dark');
});

test('init honours an OS light preference under auto', () => {
  const { window, document, attrs } = themeEnv(null, true);
  const { userMenu } = loadApp({ window, document });
  const m = userMenu('bob@pve');
  m.init();
  assert.equal(attrs['data-theme'], 'light');
});

test('init restores a stored explicit theme', () => {
  const { window, document, attrs } = themeEnv('light');
  const { userMenu } = loadApp({ window, document });
  const m = userMenu('bob@pve');
  m.init();
  assert.equal(m.theme, 'light');
  assert.equal(attrs['data-theme'], 'light');
});

test('openThemeModal seeds the draft and closes the dropdown', () => {
  const { window, document } = themeEnv('dark');
  const { userMenu } = loadApp({ window, document });
  const m = userMenu('bob@pve');
  m.init();
  m.open = true;
  m.openThemeModal();
  assert.equal(m.open, false);
  assert.equal(m.themeOpen, true);
  assert.equal(m.themeChoice, 'dark');
});

test('saveTheme persists the choice, applies it, and closes the modal', () => {
  const { window, document, store, attrs } = themeEnv(null);
  const { userMenu } = loadApp({ window, document });
  const m = userMenu('bob@pve');
  m.init();
  m.openThemeModal();
  m.themeChoice = 'light';
  m.saveTheme();
  assert.equal(store.pfr_theme, 'light');
  assert.equal(attrs['data-theme'], 'light');
  assert.equal(m.theme, 'light');
  assert.equal(m.themeOpen, false);
});

test('saveTheme accepts the proxmox-dark choice verbatim', () => {
  const { window, document, store, attrs } = themeEnv(null);
  const { userMenu } = loadApp({ window, document });
  const m = userMenu('bob@pve');
  m.init();
  m.openThemeModal();
  m.themeChoice = 'proxmox-dark';
  m.saveTheme();
  assert.equal(store.pfr_theme, 'proxmox-dark');
  assert.equal(attrs['data-theme'], 'proxmox-dark');
});
