// Test harness: starts the fake host (host.py), opens it in Chromium and gives helpers to drive the real view.
'use strict';
const { spawn } = require('node:child_process');
const path = require('node:path');
const os = require('node:os');
const fs = require('node:fs');

function findPlaywright() {
  const tried = [process.env.PLAYWRIGHT_PATH, 'playwright', 'playwright-core'];
  const npx = path.join(os.homedir(), '.npm', '_npx');
  if (fs.existsSync(npx)) for (const d of fs.readdirSync(npx)) tried.push(path.join(npx, d, 'node_modules', 'playwright'));
  for (const t of tried.filter(Boolean)) { try { return require(t); } catch { /* next */ } }
  throw new Error('Playwright not found: set PLAYWRIGHT_PATH to a playwright package folder.');
}
const { chromium } = findPlaywright();

let browser = null;
const hosts = new Set();
async function getBrowser() { return browser || (browser = await chromium.launch()); }

// Same formatting as the view (decimal units), so tests can state expected sizes from fixture bytes.
function bytes(n) {
  const units = ['B', 'KB', 'MB', 'GB', 'TB'];
  let i = 0, v = Number(n);
  while (v >= 1000 && i < units.length - 1) { v /= 1000; i++; }
  return (i ? v.toFixed(v >= 100 ? 0 : 1) : String(n)) + ' ' + units[i];
}
const hex = () => [...crypto.getRandomValues(new Uint8Array(16))].map(b => b.toString(16).padStart(2, '0')).join('');

class Host {
  static async start({ width = 760, mode = {}, colorScheme = 'light', load = true } = {}) {
    const h = new Host();
    h.proc = spawn('python3', [path.join(__dirname, 'host.py')], { stdio: ['ignore', 'pipe', 'pipe'], env: { ...process.env, STORAGE_ASSISTANT_UPDATE_CHECK: '0' } });
    h.stderr = '';
    h.proc.stderr.on('data', d => { h.stderr += d; });
    h.port = await new Promise((resolve, reject) => {
      let out = '';
      h.proc.stdout.on('data', d => { out += d; const m = /ready (\d+)/.exec(out); if (m) resolve(Number(m[1])); });
      h.proc.on('exit', code => reject(new Error('host exited ' + code + '\n' + h.stderr)));
    });
    hosts.add(h);
    h.context = await (await getBrowser()).newContext({ viewport: { width: Math.max(width + 40, 400), height: 1200 }, colorScheme });
    h.page = await h.context.newPage();
    h.errors = [];
    h.page.on('pageerror', e => h.errors.push(String(e)));
    h.page.on('console', m => { if (m.type() === 'error') h.errors.push(m.text()); });
    await h.page.goto(`http://127.0.0.1:${h.port}/?w=${width}`);
    await h.page.evaluate(m => Object.assign(window.mode, m), mode);
    if (load) await h.load();
    return h;
  }
  async load() {
    await this.page.evaluate(() => window.load());
    this.view = this.page.frames().find(f => f !== this.page.mainFrame());
    await this.view.waitForFunction(() => document.getElementById('status').textContent !== 'Connecting to the host…');
  }
  async stop() {
    hosts.delete(this);
    try { await this.context.close(); } catch { /* closed */ }
    if (this.proc.exitCode === null) { this.proc.kill('SIGTERM'); await new Promise(r => this.proc.on('exit', r)); }
  }
  ctl(op, args) { return this.page.evaluate(([o, a]) => window.ctl(o, a), [op, args]).then(r => { if (r && r.harnessError) throw new Error(r.harnessError); return r; }); }
  async fixture(spec) { return (await this.ctl('fixture', { spec })).path; }
  rpc(method, params) { return this.page.evaluate(([m, p]) => window.rpc(m, p), [method, params || {}]); }
  // A tool call made by the assistant (not the view).
  model(name, args) { return this.page.evaluate(([n, a]) => window.call(n, a), [name, args || {}]); }
  show(result) { return this.page.evaluate(r => window.replay(r), result); }
  send(method, params, id) { return this.page.evaluate(([m, p, i]) => window.send(m, p, i), [method, params, id]); }
  setMode(m) { return this.page.evaluate(x => Object.assign(window.mode, x), m); }
  rec() { return this.page.evaluate(() => window.rec); }
  async calls(name) { return (await this.rec()).calls.filter(c => !name || c.name === name); }
  // What the assistant does for "scan this folder": choose, scan, and the host shows the scan result in the view.
  async scan(folder, { free = false, show = true } = {}) {
    const chosen = await this.model('storage_choose_folder', { path: folder });
    if (chosen.isError) throw new Error('choose failed: ' + JSON.stringify(chosen.structuredContent));
    const grantId = chosen.structuredContent.data.scopes[0].grantId;
    const result = await this.model('storage_start_scan', Object.assign({ grantId, requestId: hex() }, free ? { freeUpSpace: true } : {}));
    if (show) { await this.show(result); if (result.structuredContent.data.view) await this.shown(); }
    const data = result.structuredContent.data;
    return { grantId, result, data, scanId: data.scanId, scanVersion: data.scanVersion };
  }
  shown() { return this.view.waitForFunction(() => !document.getElementById('view').hidden && document.getElementById('rows').children.length + document.getElementById('map').children.length >= 0 && document.getElementById('breadcrumbs').children.length > 0); }
  idle() { return this.view.waitForFunction(() => !document.getElementById('refresh').disabled || document.getElementById('view').classList.contains('frozen')); }
  text(id) { return this.view.evaluate(i => document.getElementById(i).textContent, id); }
  hidden(id) { return this.view.evaluate(i => { const e = document.getElementById(i); return e.hidden || getComputedStyle(e).display === 'none'; }, id); }
  rows() {
    return this.view.evaluate(() => [...document.querySelectorAll('#rows tr')].map(tr => ({
      name: (tr.querySelector('td.name button span') || tr.querySelector('td.name span')).textContent,
      type: tr.querySelector('td.type').textContent, size: tr.querySelector('td.size b').textContent,
      checkbox: !!tr.querySelector('input[type=checkbox]'), checked: !!tr.querySelector('input[type=checkbox]')?.checked,
      chip: tr.querySelector('.chip')?.textContent || null, title: tr.querySelector('td.name button')?.title || null,
      tier: tr.className || null })));
  }
  tiles() {
    return this.view.evaluate(() => [...document.querySelectorAll('#map .cell')].map(c => {
      const b = c.querySelector('.tile'), r = c.getBoundingClientRect();
      return { title: b.querySelector('strong').textContent, size: b.querySelector('span').textContent, label: b.getAttribute('aria-label'),
        disabled: b.disabled, tick: !!c.querySelector('.tick'), ticked: c.querySelector('.tick')?.getAttribute('aria-checked') === 'true',
        chip: b.querySelector('.chip')?.textContent || null, classes: b.className, w: r.width, h: r.height, area: r.width * r.height, tooltip: b.title };
    }));
  }
  crumbs() { return this.view.evaluate(() => [...document.querySelectorAll('#breadcrumbs button')].map(b => b.textContent)); }
  tile(title) { return this.view.locator('#map .tile', { has: this.view.locator('strong', { hasText: new RegExp('^' + title.replace(/[.*+?^${}()|[\]\\]/g, '\\$&') + '$') }) }); }
  tileTick(title) { return this.view.locator(`#map .tick[aria-label="Select ${title}"], #map .tick[aria-label^="Select ${title} ("]`); }
  row(name) { return this.view.locator('#rows tr', { has: this.view.locator('td.name span', { hasText: new RegExp('^' + name.replace(/[.*+?^${}()|[\]\\]/g, '\\$&') + '$') }) }); }
  // Rows start collapsed to eight: reveal loaded rows until the named one is listed.
  async reveal(name) { for (let i = 0; i < 3 && !(await this.row(name).count()) && !(await this.hidden('next')) && /Show \d+ more/.test(await this.text('next')); i++) await this.view.click('#next'); }
  async open(name) { await this.reveal(name); await this.row(name).locator('td.name button').click(); await this.view.waitForFunction(n => document.querySelector('#breadcrumbs button[aria-current]')?.textContent === n, name); await this.idle(); }
  async tick(name) { await this.reveal(name); await this.row(name).locator('input[type=checkbox]').check(); }
  active() { return this.view.evaluate(() => { const a = document.activeElement; return { tag: a.tagName, id: a.id, text: (a.textContent || '').slice(0, 80), label: a.getAttribute('aria-label'), pick: a.dataset?.pick || null }; }); }
}

// Runs one test against its own host (own plugin, own view), always stopping it afterwards.
function withHost(options, body) {
  if (typeof options === 'function') { body = options; options = {}; }
  return async () => { const h = await Host.start(options); try { await body(h); } finally { await h.stop(); } };
}
// WCAG contrast of the text of the first element matching `selector` against the background behind it.
function contrast(view, selector) {
  return view.evaluate(sel => {
    const parse = c => { const m = c.match(/rgba?\(([^)]+)\)/); if (!m) return [0, 0, 0, 0]; const p = m[1].split(/[, /]+/).map(Number); return [p[0], p[1], p[2], p.length > 3 ? p[3] : 1]; };
    const e = document.querySelector(sel); if (!e) return null;
    let bg = [255, 255, 255, 1], opacity = 1;
    for (let n = e; n; n = n.parentElement) { const s = getComputedStyle(n); if (n === e || true) opacity *= n === e ? Number(s.opacity) : 1; const b = parse(s.backgroundColor); if (b[3] > 0.99) { bg = b; break; } }
    const fg = parse(getComputedStyle(e).color), a = fg[3] * opacity;
    const mix = fg.slice(0, 3).map((c, i) => c * a + bg[i] * (1 - a));
    const lum = c => { const v = c.map(x => { x /= 255; return x <= 0.03928 ? x / 12.92 : ((x + 0.055) / 1.055) ** 2.4; }); return 0.2126 * v[0] + 0.7152 * v[1] + 0.0722 * v[2]; };
    const l1 = lum(mix), l2 = lum(bg.slice(0, 3));
    return (Math.max(l1, l2) + 0.05) / (Math.min(l1, l2) + 0.05);
  }, selector);
}
async function closeAll() {
  for (const h of [...hosts]) await h.stop();
  if (browser) { await browser.close(); browser = null; }
}
const sleep = ms => new Promise(r => setTimeout(r, ms));
const MB = 1000 * 1000;
// A small, varied folder. Sizes are exact so tests can state what the view must show. Everything is 30 days old.
const BASIC = { name: 'Basics', age: 30, entries: [
  { p: 'Films/launch.mov', size: 6 * MB }, { p: 'Films/interview.mp4', size: 3 * MB },
  { p: 'Sound/recording.wav', size: 2 * MB }, { p: 'Documents/notes.pdf', size: 1200000 },
  { p: 'Pictures/a.png', size: 300000 }, { p: 'Pictures/b.png', size: 200000 },
  { p: 'Empty', dir: true }, { p: 'big.zip', size: 4 * MB }, { p: 'tiny.zip', size: 10 },
  { p: 'shortcut', link: 'Films' }, { p: '.env', size: 20 }, { p: '.ssh/id_rsa', size: 30 },
] };
// A folder with things worth suggesting. Ages in days.
const CLEAN = { name: 'Cleanup', age: 60, entries: [
  { p: 'old-installer.crdownload', size: 3 * MB }, { p: 'partial.download/data.bin', size: 2 * MB },
  { p: 'proj/package.json', size: 200 }, { p: 'proj/src/index.js', size: 500 }, { p: 'proj/node_modules/lib/big.bin', size: 5 * MB },
  { p: 'archive.zip', size: 2 * MB }, { p: 'archive/readme.txt', size: 100 },
  { p: 'a/movie.mov', size: 3 * MB }, { p: 'b/movie.mov', size: 3 * MB },
  { p: 'ancient.iso', size: 12 * MB, age: 800 }, { p: 'fresh.crdownload', size: 2 * MB, age: 1 },
  { p: 'keep.mov', size: 4 * MB }, { p: 'notes/small.txt', size: 50 },
] };
module.exports = { CLEAN, Host, withHost, contrast, closeAll, bytes, hex, sleep, MB, BASIC };
