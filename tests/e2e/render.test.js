// Rendering and information: what the person sees must match what is on disk.
'use strict';
const { test, after } = require('node:test');
const assert = require('node:assert/strict');
const { withHost, contrast, closeAll, bytes, MB, BASIC, sleep } = require('./harness');
after(closeAll);

test('table rows show real names, types and exact sizes, largest first', withHost(async h => {
  await h.scan(await h.fixture(BASIC));
  assert.deepEqual(await h.crumbs(), ['Basics']);
  await h.view.click('#next');
  const rows = await h.rows(), by = Object.fromEntries(rows.map(r => [r.name, r]));
  assert.equal(by.Films.size, bytes(9 * MB)); assert.equal(by.Films.type, 'Folder');
  assert.equal(by['big.zip'].size, '4.0 MB'); assert.equal(by['big.zip'].type, 'Archive');
  assert.equal(by.Sound.size, '2.0 MB'); assert.equal(by.Documents.size, '1.2 MB'); assert.equal(by.Pictures.size, '500 KB');
  assert.equal(by['tiny.zip'].size, '10 B'); assert.equal(by.Empty.size, '0 B'); assert.equal(by.shortcut.type, 'Symlink');
  assert.deepEqual(rows.slice(0, 6).map(r => r.name), ['Films', 'big.zip', 'Sound', 'Documents', 'Pictures', 'tiny.zip']);
  assert.deepEqual(h.errors, []);
}));

test('summary shows the folder total and its item count', withHost(async h => {
  await h.scan(await h.fixture(BASIC));
  // 6+3+2+1.2+0.5+4 MB + 10 B; .env and .ssh are skipped, the link is not followed.
  assert.equal(await h.text('summary'), `${bytes(16700010)}in 10 items`);
}));

test('treemap tiles match the rows and their areas follow the sizes', withHost(async h => {
  await h.scan(await h.fixture(BASIC));
  const tiles = await h.tiles(), by = Object.fromEntries(tiles.map(t => [t.title, t]));
  assert.deepEqual(tiles.map(t => t.title), ['Films', 'big.zip', 'Sound', 'Documents', 'Pictures', 'tiny.zip']);
  assert.equal(by.Films.size, '9.0 MB'); assert.equal(by['big.zip'].size, '4.0 MB');
  assert.equal(by.Films.label, 'Films, Folder, 9.0 MB, open folder');
  assert.equal(by['big.zip'].label, 'big.zip, Archive, 4.0 MB');
  const total = tiles.reduce((n, t) => n + t.area, 0);
  for (const [name, size] of [['Films', 9], ['big.zip', 4], ['Sound', 2], ['Documents', 1.2], ['Pictures', 0.5]])
    assert.ok(Math.abs(by[name].area / total - size / 16.70001) < 0.01, `${name} area share ${by[name].area / total}`);
  assert.ok(!tiles.some(t => t.title === 'Empty'), 'zero-byte items take no tile');
}));

test('sizes just under a unit boundary are not shown as "1000 KB"', withHost(async h => {
  await h.scan(await h.fixture({ name: 'Round', age: 30, entries: [{ p: 'edge.zip', size: 999960 }] }));
  assert.notEqual((await h.rows())[0].size, '1000 KB');
}));

test('legend names every kind of tile shown', withHost(async h => {
  await h.scan(await h.fixture(BASIC));
  assert.deepEqual(await h.view.evaluate(() => [...document.querySelectorAll('#legend li')].map(l => l.textContent)), ['Folder', 'Archives']);
}));

test('coverage line says what was not counted, and when it was measured', withHost(async h => {
  const s = await h.scan(await h.fixture(BASIC));
  assert.equal(await h.text('coverage'), 'Not counted: 1 link, 2 protected.');
  const when = new Date(s.data.scannedAt * 1000).toLocaleString('en-US', { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' });
  assert.equal((await h.text('when')).replace(/ /g, ' '), `Measured ${when}.`);
}));

test('a fully measured folder says so', withHost(async h => {
  await h.scan(await h.fixture({ name: 'Whole', age: 30, entries: [{ p: 'a.zip', size: 5000 }, { p: 'sub/b.zip', size: 7000 }] }));
  assert.equal(await h.text('coverage'), 'Everything in this folder was measured.');
  assert.equal(await h.text('notice'), '');
}));

test('File size and Space on disk show different numbers for a sparse file, with a notice', withHost(async h => {
  await h.scan(await h.fixture({ name: 'Sparse', age: 30, entries: [{ p: 'sparse.mov', size: 250 * MB, sparse: true }, { p: 'real.mov', size: 2 * MB }] }));
  assert.equal(await h.text('summary'), '252 MBin 2 items');
  assert.match(await h.text('notice'), /^250 MB of this is not stored on this Mac \(iCloud-only or sparse files\)\. Switch to Space on disk/);
  assert.equal((await h.rows())[0].name, 'sparse.mov'); assert.equal((await h.rows())[0].size, '250 MB');
  await h.view.selectOption('#basis', 'allocated_estimate'); await h.idle();
  await h.view.waitForFunction(() => document.getElementById('summary').textContent.includes('on disk'));
  assert.match(await h.text('summary'), /^2\.\d MBon disk across 2 items$/);
  const rows = await h.rows();
  assert.equal(rows[0].name, 'real.mov'); assert.equal(rows[1].size, '0 B');
  assert.deepEqual((await h.tiles()).map(t => t.title), ['real.mov']);
  assert.doesNotMatch(await h.text('notice'), /Switch to Space on disk/);
}));

test('File types groups files by type with counts; type tiles are not clickable or tickable', withHost(async h => {
  await h.scan(await h.fixture(BASIC));
  await h.view.selectOption('#aggregate', 'by_category'); await h.idle();
  await h.view.waitForFunction(() => document.getElementById('summary').textContent.includes('file type'));
  const rows = await h.rows(), by = Object.fromEntries(rows.map(r => [r.name, r]));
  assert.equal(by.Video.size, '9.0 MB'); assert.equal(by.Video.type, '2 files');
  assert.equal(by.Archives.size, '4.0 MB'); assert.equal(by.Archives.type, '2 files');
  assert.equal(by.Audio.type, '1 file'); assert.equal(by.Images.size, '500 KB');
  assert.equal(by.Images.type, '2 files', 'two small pictures are two files, not one');
  assert.ok(rows.every(r => !r.checkbox));
  const tiles = await h.tiles();
  assert.ok(tiles.length >= 4 && tiles.every(t => t.disabled && !t.tick));
  assert.match(await h.text('summary'), /^16\.7 MBin \d file types$/);
}));

test('small files are grouped into one row that cannot be ticked or opened', withHost(async h => {
  await h.scan(await h.fixture(BASIC));
  await h.open('Pictures');
  const rows = await h.rows();
  assert.equal(rows.length, 1); assert.equal(rows[0].name, '2 small image files'); assert.equal(rows[0].size, '500 KB');
  assert.equal(rows[0].checkbox, false);
  const tiles = await h.tiles(); assert.equal(tiles[0].tick, false);
  await h.tile('2 small image files').click(); await sleep(200);
  assert.equal(await h.hidden('tray'), true); assert.deepEqual(await h.crumbs(), ['Basics', 'Pictures']);
}));

const MANY = n => ({ name: 'Many', age: 30, entries: Array.from({ length: n }, (_, i) => ({ p: `f${String(i).padStart(2, '0')}.zip`, size: (100 - i) * 1000 })) });

test('Show N more reveals loaded rows in steps and Show fewer collapses them', withHost(async h => {
  await h.scan(await h.fixture(MANY(30)));
  assert.equal((await h.rows()).length, 8); assert.equal(await h.text('next'), 'Show 20 more'); assert.equal(await h.hidden('first'), true);
  await h.view.click('#next'); assert.equal((await h.rows()).length, 28); assert.equal(await h.text('next'), 'Show 2 more');
  await h.view.click('#next'); assert.equal((await h.rows()).length, 30); assert.equal(await h.hidden('next'), true);
  await h.view.click('#first'); assert.equal((await h.rows()).length, 8);
  assert.equal((await h.tiles()).length, 30, 'the map always shows every loaded item');
}));

test('Show more past the first page works on the view a scan opens', withHost(async h => {
  await h.scan(await h.fixture(MANY(45)));
  await h.view.click('#next'); await h.view.click('#next');
  assert.equal(await h.text('next'), 'Show more');
  await h.view.click('#next'); await sleep(600);
  assert.equal(await h.hidden('view'), false, `the view disappeared: "${await h.text('status')}"`);
  assert.ok((await h.rows()).some(r => r.name === 'f44.zip'), 'the last item is listed');
}));

test('Show more past the first page adds rows instead of replacing the biggest items', withHost(async h => {
  await h.scan(await h.fixture(MANY(45)));
  await h.view.click('#refresh'); await h.idle();  // paging works only after the view has reloaded itself
  await h.view.click('#next'); await h.view.click('#next'); await h.view.click('#next'); await sleep(600);
  const names = (await h.rows()).map(r => r.name);
  assert.ok(names.includes('f44.zip'), 'later rows are listed');
  assert.ok(names.includes('f00.zip'), `the largest item is still listed (got ${names.length} rows starting at ${names[0]})`);
  assert.equal((await h.tiles())[0].title, 'f00.zip', 'the map still shows the largest item');
}));

test('Show more still works after the page has been open for a few minutes (cursor expired)', withHost(async h => {
  await h.scan(await h.fixture(MANY(45)));
  await h.view.click('#refresh'); await h.idle();
  await h.view.click('#next'); await h.view.click('#next');
  await h.ctl('expire_cursors');  // the plugin keeps a page cursor for 120 seconds
  await h.view.click('#next'); await sleep(600);
  assert.equal(await h.hidden('view'), false, `the view disappeared: "${await h.text('status')}"`);
}));

test('long and unicode names render in full, with the full name on hover and no sideways overflow', withHost(async h => {
  const long = 'A very long file name ' + 'x'.repeat(180) + '.zip', uni = '日本語のファイル名 🎬 café — naïve.zip', html = '<img src=x onerror=alert(1)>&amp;.zip';
  await h.scan(await h.fixture({ name: 'Names', age: 30, entries: [{ p: long, size: 3000 }, { p: uni, size: 2000 }, { p: html, size: 1000 }] }));
  const rows = await h.rows();
  assert.deepEqual(rows.map(r => r.name), [long, uni, html]);
  assert.equal(rows[0].title, long);
  assert.deepEqual((await h.tiles()).map(t => t.title), [long, uni, html]);
  assert.equal(await h.view.evaluate(() => document.querySelectorAll('#view img').length), 0, 'names never become HTML');
  assert.ok(await h.view.evaluate(() => document.documentElement.scrollWidth <= document.documentElement.clientWidth), 'no horizontal overflow');
  assert.deepEqual(h.errors, []);
}));

test('an empty folder opens and says it is empty', withHost(async h => {
  await h.scan(await h.fixture({ name: 'Top', age: 30, entries: [{ p: 'Void', dir: true }, { p: 'a.zip', size: 3000 }] }));
  await h.open('Void');
  assert.equal(await h.text('summary'), '0 Bin 0 items');
  assert.equal((await h.rows()).length, 0); assert.equal((await h.tiles()).length, 0);
  const visible = await h.view.evaluate(() => document.getElementById('view').innerText);
  assert.match(visible, /empty|nothing (is )?in this folder|no items/i, 'some words say the folder is empty, rather than a bare header and an empty table');
}));

test('deeply nested folders can be opened all the way down', withHost(async h => {
  await h.scan(await h.fixture({ name: 'Deep', age: 30, entries: [{ p: 'l1/l2/l3/l4/l5/l6/l7/l8/l9/l10/l11/bottom.zip', size: 2 * MB }] }));
  for (let i = 1; i <= 11; i++) {
    await h.row('l' + i).locator('td.name button').click(); await sleep(350);
    assert.equal(await h.hidden('view'), false, `level ${i}: the view disappeared: "${await h.text('status')}"`);
  }
  assert.deepEqual((await h.rows()).map(r => r.name), ['bottom.zip']);
}));

test('a scan that hit the item limit says totals are too low', withHost(async h => {
  await h.ctl('limits', { limits: { maxEntries: 6 } });
  const s = await h.scan(await h.fixture({ name: 'Cut', age: 30, entries: Array.from({ length: 20 }, (_, i) => ({ p: `d${i}/f.zip`, size: 5000 })) }));
  assert.ok(s.data.gapCounts.entry_limit > 0);
  assert.match(await h.text('summary'), /^at least /);
  assert.match(await h.text('notice'), /The scan stopped before the end of this folder, so these totals are too low/);
  assert.match(await h.text('coverage'), /item limit reached/);
  assert.match(s.data.coverageSummary, /^Stopped early; totals are a lower bound/);
}));

test('iCloud-only placeholder files are named in the coverage line', withHost(async h => {
  const s = await h.scan(await h.fixture({ name: 'Cloud', age: 30, entries: [{ p: '.report.pdf.icloud', size: 100 }, { p: 'here.zip', size: 4000 }] }));
  assert.match(await h.text('coverage'), /1 iCloud-only/);
  assert.match(s.data.coverageSummary, /1 iCloud-only items not downloaded/);
}));

test('a hard-linked file is counted once', withHost(async h => {
  await h.scan(await h.fixture({ name: 'Links', age: 30, entries: [{ p: 'one.wav', size: 2 * MB }, { p: 'two.wav', hardlink: 'one.wav' }] }));
  assert.equal(await h.text('summary'), '2.0 MBin 2 items');
}));

const { CLEAN } = require('./harness');
for (const theme of ['light', 'dark']) {
  test(`${theme} theme: text is readable (WCAG AA contrast)`, withHost({ mode: { theme } }, async h => {
    await h.scan(await h.fixture(CLEAN), { free: true });
    await h.view.waitForSelector('.chip');
    assert.equal(await h.view.evaluate(() => document.documentElement.dataset.theme), theme);
    const paper = await h.view.evaluate(() => getComputedStyle(document.body).backgroundColor);
    assert.equal(paper, theme === 'dark' ? 'rgb(30, 33, 39)' : 'rgb(255, 255, 255)');
    const low = [];
    for (const sel of ['#summary strong', '#summary', '#breadcrumbs button:last-child', '.tile strong', '.tile span', '.tile.cat-archive strong', '.tile.cat-video span', '.tile.cat-folder span',
      'td.name button span', 'td.type', 'td.size b', '.fine', '#refresh', '#tray-text', '#tray-review', 'th', '#legend li', '.tick.shown']) {
      const c = await contrast(h.view, sel);
      if (c !== null && c < 4.5) low.push(`${sel} ${c.toFixed(2)}`);
    }
    assert.deepEqual(low, []);
  }));
  test(`${theme} theme: suggestion chips are readable (WCAG AA contrast)`, withHost({ mode: { theme } }, async h => {
    await h.scan(await h.fixture(CLEAN), { free: true });
    await h.view.waitForSelector('.chip');
    const low = [];
    for (const sel of ['.chip.tier-safe', '.chip.tier-glance']) { const c = await contrast(h.view, sel); if (c < 4.5) low.push(`${sel} ${c.toFixed(2)}`); }
    assert.deepEqual(low, []);
  }));
}

test('every tile that shows a size also shows its name, with or without a suggestion chip', withHost(async h => {
  // One large item and five suggested ones: the small tiles are wide enough for text but only about 48px tall.
  const entries = [{ p: 'film.mov', size: 2 * MB }, ...[84, 83, 82, 81, 80].map(k => ({ p: `part-${k}.crdownload`, size: k * 1000 }))];
  await h.scan(await h.fixture({ name: 'Short', age: 30, entries }), { free: true });
  await h.view.waitForSelector('.chip');
  const nameless = await h.view.evaluate(() => [...document.querySelectorAll('#map .tile:not(.tiny)')].filter(t => t.querySelector('span').getBoundingClientRect().height > 8 && t.querySelector('strong').getBoundingClientRect().height < 8).map(t => t.getAttribute('aria-label')));
  assert.deepEqual(nameless, [], 'these tiles show a size and a reason chip but no name');
}));

test('dark theme follows the system when the host names no theme', withHost({ mode: { theme: null }, colorScheme: 'dark' }, async h => {
  await h.scan(await h.fixture(BASIC));
  assert.equal(await h.view.evaluate(() => getComputedStyle(document.body).backgroundColor), 'rgb(30, 33, 39)');
}));

test('host theme change switches the view', withHost(async h => {
  await h.scan(await h.fixture(BASIC));
  await h.send('ui/notifications/host-context-changed', { theme: 'dark' });
  await h.view.waitForFunction(() => getComputedStyle(document.body).backgroundColor === 'rgb(30, 33, 39)');
}));

for (const width of [360, 480]) {
  test(`narrow host (${width}px): nothing overflows sideways and names stay readable beside their chips`, withHost({ width }, async h => {
    await h.scan(await h.fixture(CLEAN), { free: true });
    await h.view.waitForSelector('.chip');
    const m = await h.view.evaluate(() => ({ sw: document.documentElement.scrollWidth, cw: document.documentElement.clientWidth,
      squeezed: [...document.querySelectorAll('td.name button span')].filter(s => s.scrollWidth > s.clientWidth && s.clientWidth < 60).map(s => `${s.textContent} (${s.clientWidth}px)`) }));
    assert.ok(m.sw <= m.cw, `page scrolls sideways (${m.sw} > ${m.cw})`);
    assert.deepEqual(m.squeezed, [], 'names cut to a few letters');
  }));
}

test('the view reports its height to the host after rendering', withHost(async h => {
  await h.scan(await h.fixture(BASIC));
  const { sizes } = await h.rec();
  const height = await h.view.evaluate(() => document.documentElement.scrollHeight);
  assert.ok(sizes.length > 0 && Math.abs(sizes[sizes.length - 1] - height) <= 2, `last reported ${sizes[sizes.length - 1]}, actual ${height}`);
}));
