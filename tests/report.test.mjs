// Browser smoke test for the report page. Run from the repo root:
//
//     node --test tests/
//
// Scans a small fixture twice (so history and the change view have data), opens the
// report in headless Chrome over the DevTools protocol, and exercises every panel,
// color mode, zoom and search, failing on any JavaScript error. Skips if Chrome
// isn't installed.
import { test, before, after } from 'node:test';
import assert from 'node:assert/strict';
import { spawn, execFileSync } from 'node:child_process';
import { mkdtempSync, mkdirSync, writeFileSync, readFileSync, existsSync, rmSync, realpathSync } from 'node:fs';
import { randomBytes } from 'node:crypto';
import { tmpdir } from 'node:os';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';

const REPO = dirname(dirname(fileURLToPath(import.meta.url)));
const CHROME = process.env.CHROME || '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome';
const skip = existsSync(CHROME) ? false : `Chrome not found at ${CHROME} (set CHROME=…)`;
const sleep = ms => new Promise(r => setTimeout(r, ms));

let tmp, chrome, ws, errors = [];
let nextId = 0;
const pending = new Map();
const send = (method, params = {}) => new Promise((resolve, reject) => {
  const id = ++nextId;
  pending.set(id, { resolve, reject });
  ws.send(JSON.stringify({ id, method, params }));
});
async function evaluate(expression) {
  const r = await send('Runtime.evaluate', { expression, returnByValue: true, awaitPromise: true });
  if (r.exceptionDetails) throw new Error(r.exceptionDetails.exception?.description || r.exceptionDetails.text);
  return r.result.value;
}
const click = selector => evaluate(`(() => { const e = document.querySelector(${JSON.stringify(selector)});
  if (!e) throw new Error('no element ' + ${JSON.stringify(selector)}); e.click(); return true; })()`);

function fixture(root) {
  const file = (path, bytes) => { mkdirSync(dirname(join(root, path)), { recursive: true }); writeFileSync(join(root, path), bytes); };
  const blob = randomBytes(2_000_000);
  file('photos/2024/a.jpg', randomBytes(3_000_000));
  file('photos/2024/b.jpg', randomBytes(1_500_000));
  file('projects/app/node_modules/lib/index.js', randomBytes(400_000));
  file('projects/app/src/main.py', Buffer.from("print('hi')\n"));
  file('copies/one/data.bin', blob);
  file('copies/two/data.bin', blob);
  file('notes.txt', Buffer.from('hello'));
}

function scanTwice(root, out) {
  const args = [join(REPO, 'diskmap.py'), root, '--out', out, '--no-system', '--jobs', '2', '--min-size', '1K', '--dupe-min', '1M'];
  execFileSync('python3', args, { stdio: 'ignore' });
  writeFileSync(join(root, 'photos/2024/new.mov'), randomBytes(11_000_000));  // > 10 MB, so it's listed as grown
  execFileSync('python3', args, { stdio: 'ignore' });
}

before(async () => {
  if (skip) return;
  tmp = realpathSync(mkdtempSync(join(tmpdir(), 'diskmap-report-')));
  const root = join(tmp, 'root'), out = join(tmp, 'out');
  fixture(root);
  scanTwice(root, out);

  const profile = join(tmp, 'profile');
  chrome = spawn(CHROME, ['--headless=new', '--disable-gpu', '--remote-debugging-port=0', '--window-size=1500,913',
                          `--user-data-dir=${profile}`, 'about:blank'], { stdio: 'ignore' });
  let port;
  for (let i = 0; i < 80 && !port; i++) {
    await sleep(100);
    try { port = readFileSync(join(profile, 'DevToolsActivePort'), 'utf8').split('\n')[0]; } catch {}
  }
  assert.ok(port, 'Chrome started with a debugging port');
  const targets = await (await fetch(`http://127.0.0.1:${port}/json`)).json();
  ws = new WebSocket(targets.find(t => t.type === 'page').webSocketDebuggerUrl);
  await new Promise(r => ws.onopen = r);
  ws.onmessage = e => {
    const m = JSON.parse(e.data);
    if (m.id && pending.has(m.id)) {
      const p = pending.get(m.id); pending.delete(m.id);
      m.error ? p.reject(new Error(m.error.message)) : p.resolve(m.result);
    } else if (m.method === 'Runtime.exceptionThrown') {
      errors.push(m.params.exceptionDetails.exception?.description || m.params.exceptionDetails.text);
    } else if (m.method === 'Runtime.consoleAPICalled' && m.params.type === 'error') {
      errors.push(m.params.args.map(a => a.value ?? a.description).join(' '));
    } else if (m.method === 'Log.entryAdded' && m.params.entry.level === 'error') {
      errors.push(m.params.entry.text);
    }
  };
  await send('Runtime.enable');
  await send('Log.enable');
  await send('Page.enable');
  await send('Page.navigate', { url: `file://${join(out, 'index.html')}` });
  for (let i = 0; i < 50; i++) {
    await sleep(100);
    if (await evaluate(`document.querySelectorAll('#map .cell').length`).catch(() => 0)) break;
  }
});

after(async () => {
  ws?.close();
  if (chrome && chrome.exitCode === null) {
    const exited = new Promise(r => chrome.once('exit', r));
    chrome.kill();
    await exited;  // Chrome writes to its profile until it exits
  }
  if (tmp) rmSync(tmp, { recursive: true, force: true, maxRetries: 5, retryDelay: 200 });
});

test('loads without JavaScript errors and draws the map', { skip }, async () => {
  assert.ok(await evaluate(`document.querySelectorAll('#map .cell').length`) > 3);
  assert.match(await evaluate(`document.querySelector('#meta').textContent`), /Stardate/);
  assert.deepEqual(errors, []);
});

test('every panel renders', { skip }, async () => {
  const tabs = await evaluate(`[...document.querySelectorAll('#tabs button')].map(b => b.dataset.t)`);
  assert.equal(tabs.length, 8);
  for (const t of tabs) {
    await click(`#tabs button[data-t="${t}"]`);
    const [title, text, on] = await evaluate(`[document.querySelector('#ptitle').textContent,
      document.querySelector('#panel').textContent.trim(), document.querySelector('#tabs .on')?.dataset.t]`);
    assert.ok(title, `${t}: panel title`);
    assert.ok(text.length > 20, `${t}: panel has content`);
    assert.equal(on, t, `${t}: button marked active`);
  }
  assert.deepEqual(errors, []);
});

test('panels show this scan\'s findings', { skip }, async () => {
  await click('#tabs button[data-t="junk"]');
  assert.match(await evaluate(`document.querySelector('#panel').textContent`), /node_modules/);
  await click('#tabs button[data-t="changes"]');
  assert.match(await evaluate(`document.querySelector('#panel').textContent`), /new\.mov/);
  await click('#tabs button[data-t="dupes"]');
  const dupes = await evaluate(`document.querySelector('#panel').textContent`);
  assert.match(dupes, /Duplicate (folders|files)/);
  assert.match(dupes, /data\.bin|copies/);
});

test('every color mode draws the map', { skip }, async () => {
  const modes = await evaluate(`[...document.querySelectorAll('#modes button')].map(b => [b.dataset.m, b.disabled])`);
  assert.deepEqual(modes.map(m => m[0]), ['folder', 'type', 'junk', 'change', 'age']);
  assert.ok(modes.every(([, disabled]) => !disabled), 'with two scans, every mode is available');
  for (const [m] of modes) {
    await click(`#modes button[data-m="${m}"]`);
    const [on, cells] = await evaluate(`[document.querySelector('#modes .on').dataset.m, document.querySelectorAll('#map .cell').length]`);
    assert.equal(on, m);
    assert.ok(cells > 3, `${m}: map drawn`);
  }
  await click('#modes button[data-m="folder"]');
  assert.deepEqual(errors, []);
});

test('clicking a folder zooms in with an animation, Escape zooms back out', { skip }, async () => {
  const state = `(() => { const m = document.querySelector('#map');
    return { layers: m.querySelectorAll('.layer').length, anims: m.getAnimations({ subtree: true }).length,
             here: document.querySelector('#crumbs .here').textContent }; })()`;
  const start = await evaluate(state);
  const name = await evaluate(`(() => { const c = [...document.querySelectorAll('#map .layer .cell.box')]
    .find(c => c._n.depth === 1); c.click(); return c._n.data.n; })()`);
  const during = await evaluate(state);
  assert.equal(during.here, name);
  assert.equal(during.layers, 2, 'old and new view both on screen while zooming');
  assert.ok(during.anims > 0, 'zoom is animated');
  await sleep(700);
  assert.deepEqual(await evaluate(state), { layers: 1, anims: 0, here: name });
  assert.equal(await evaluate('decodeURIComponent(location.hash.slice(1)).endsWith("/" + ' + JSON.stringify(name) + ')'), true,
    'zoom position kept in the URL');

  await evaluate(`document.body.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }))`);
  assert.ok((await evaluate(state)).anims > 0, 'zooming out is animated');
  await sleep(700);
  assert.deepEqual(await evaluate(state), { layers: 1, anims: 0, here: start.here });
  assert.deepEqual(errors, []);
});

test('search finds items and highlights them on the map', { skip }, async () => {
  await evaluate(`(() => { const q = document.querySelector('#q'); q.value = 'data'; q.dispatchEvent(new Event('input')); })()`);
  await sleep(400);
  const [visible, rows, hits] = await evaluate(`[!document.querySelector('#results').hidden,
    document.querySelectorAll('#results .row').length, document.querySelectorAll('#map .cell.hit').length]`);
  assert.ok(visible);
  assert.ok(rows >= 2, 'both data.bin copies listed');
  assert.ok(hits >= 1, 'matches highlighted on the map');
  assert.deepEqual(errors, []);
});
