import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import path from 'node:path';

const here = path.dirname(fileURLToPath(import.meta.url));
const APP_JS = path.resolve(here, '../../backend/static/app.js');
const source = readFileSync(APP_JS, 'utf8');

/**
 * app.js is a plain browser script (no module exports) whose top level is
 * just function declarations. We wrap it in a function that takes the
 * browser-only globals it references inside method bodies (window,
 * document, htmx) as parameters, then hands back the factory functions.
 */
export function loadApp(env = {}) {
  const windowStub = env.window ?? { location: {}, Alpine: undefined };
  const documentStub = env.document ?? { querySelector: () => null, querySelectorAll: () => [] };
  const htmxStub = env.htmx ?? { ajax: async () => {} };

  const factory = new Function(
    'window',
    'document',
    'htmx',
    `${source}\n;return { taskPicker, userMenu, fileGridState, portalApp, restoreJobsWidget };`,
  );
  return { ...factory(windowStub, documentStub, htmxStub), window: windowStub };
}

export function fakeTbody(rows) {
  const order = [];
  return {
    querySelectorAll: (sel) => (sel === 'tr' ? rows.slice() : []),
    appendChild: (r) => order.push(r),
    get order() {
      return order;
    },
  };
}
