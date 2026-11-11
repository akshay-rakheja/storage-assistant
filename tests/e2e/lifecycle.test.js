// Lifecycle: running and cancelled scans, reopened views, restarts, timeouts, host capabilities, updates.
'use strict';
const { test, after } = require('node:test');
const assert = require('node:assert/strict');
const { withHost, closeAll, BASIC, CLEAN, MB, hex, sleep } = require('./harness');
after(closeAll);

const frozen = h => h.view.waitForFunction(() => document.getElementById('view').classList.contains('frozen'));
const isFrozen = h => h.view.evaluate(() => document.getElementById('view').classList.contains('frozen'));
const SLOW = { name: 'Slow', age: 30, entries: Array.from({ length: 60 }, (_, i) => ({ p: `dir${String(i).padStart(2, '0')}/file.zip`, size: 1000 * (i + 1) })) };

test('a scan still running shows live progress, then turns into the map by itself', withHost(async h => {
  await h.ctl('scan_wait', { seconds: 0 }); await h.ctl('scan_throttle', { perLine: 0.03 });
  const s = await h.scan(await h.fixture(SLOW));
  assert.equal(s.data.state, 'running'); assert.equal(s.data.view, undefined);
  assert.match(s.data.coverageSummary, /^Still scanning: \d+ items so far\.$/);
  assert.match(await h.text('status'), /^Scanning… \d+ entries so far\. The map appears here when the scan finishes\.$/);
  assert.equal(await h.hidden('view'), true);
  const first = Number((await h.text('status')).match(/(\d+) entries/)[1]);
  await h.view.waitForFunction(n => { const m = document.getElementById('status').textContent.match(/(\d+) entries/); return m && Number(m[1]) > n; }, first, { timeout: 10000 });
  await h.view.waitForFunction(() => !document.getElementById('view').hidden, null, { timeout: 20000 });
  assert.equal(await h.text('summary'), '1.8 MBin 60 items'); assert.equal(await h.text('status'), '');
  assert.ok((await h.calls('storage_scan_status')).length >= 2);
  await h.idle();
}));

test('a running scan asked to free space shows suggestions when it finishes', withHost(async h => {
  await h.ctl('scan_wait', { seconds: 0 }); await h.ctl('scan_delay', { seconds: 1.5 });
  const s = await h.scan(await h.fixture(CLEAN), { free: true });
  assert.equal(s.data.state, 'running'); assert.equal(s.data.freeUpSpace, true);
  await h.view.waitForSelector('.chip', { timeout: 20000 });
  assert.equal(await h.text('tray-text'), '3 items selected · about 10.0 MB');
}));

async function cancelPartWay(h) {
  await h.ctl('scan_wait', { seconds: 0 }); await h.ctl('scan_throttle', { perLine: 0.05 });
  const s = await h.scan(await h.fixture(SLOW));
  await sleep(1500);
  const cancelled = await h.model('storage_cancel_scan', { scanId: s.scanId });
  assert.equal(cancelled.structuredContent.data.state, 'cancelled');
  assert.ok(cancelled.structuredContent.data.observedEntries > 5 && cancelled.structuredContent.data.observedEntries < 120);
  return s;
}

test('a scan cancelled part-way shows what was measured and marks the totals as partial', withHost(async h => {
  await cancelPartWay(h);
  await h.view.waitForFunction(() => !document.getElementById('view').hidden || !/Scanning/.test(document.getElementById('status').textContent), null, { timeout: 10000 });
  assert.equal(await h.hidden('view'), false, `no map after cancelling: "${await h.text('status')}"`);
  assert.ok((await h.rows()).length > 0);
  const summary = await h.text('summary'), notice = await h.text('notice');
  assert.ok(/^at least/.test(summary) || /stopped|cancel|too low/i.test(notice), `the total reads as complete: "${summary}", notice "${notice}", coverage "${await h.text('coverage')}"`);
}));

test('the assistant is told a cancelled scan is partial', withHost(async h => {
  const s = await cancelPartWay(h);
  const status = await h.model('storage_scan_status', { scanId: s.scanId });
  assert.doesNotMatch(status.structuredContent.data.coverageSummary, /^Everything reachable was measured/, `coverageSummary: "${status.structuredContent.data.coverageSummary}"`);
}));

test('a scan cancelled before anything was measured shows a clear message', withHost(async h => {
  await h.ctl('scan_wait', { seconds: 0 }); await h.ctl('scan_delay', { seconds: 5 });
  const s = await h.scan(await h.fixture(SLOW));
  await h.model('storage_cancel_scan', { scanId: s.scanId });
  await h.view.waitForFunction(() => !/Scanning/.test(document.getElementById('status').textContent), null, { timeout: 10000 });
  assert.doesNotMatch(await h.text('status'), /unsupported result|connection metadata/, 'a cancelled scan is reported as a host fault');
}));

test('the host cancelling the tool call shows a message about cancelling', withHost(async h => {
  await h.send('ui/notifications/tool-cancelled', {});
  await sleep(200);
  assert.doesNotMatch(await h.text('status'), /Poll status|still running/, 'tells the person to "poll status"');
}));

test('view reopened after the plugin restarted: dated read-only snapshot, then Rescan brings it back to life', withHost(async h => {
  const dir = await h.fixture(BASIC);
  const s = await h.scan(dir);
  await h.ctl('restart'); await h.load(); await h.show(s.result);
  await frozen(h);
  assert.match((await h.text('status')).replace(/ /g, ' '), /^Snapshot from \w{3} \d{1,2}, \d{1,2}:\d\d [AP]M\. Claude restarted the plugin or opened another folder since, so it's read-only\. Rescan to see the folder as it is now\.$/);
  assert.equal((await h.rows())[0].name, 'Films'); assert.equal((await h.tiles()).length, 6, 'the map is still shown');
  assert.equal(await h.hidden('rescan'), false);
  assert.ok((await h.rows()).every(r => !r.checkbox) || await h.view.evaluate(() => [...document.querySelectorAll('#rows input')].every(i => getComputedStyle(i).display === 'none')));
  assert.equal(await h.view.evaluate(() => getComputedStyle(document.querySelector('.bar')).display), 'none');
  await h.tile('Films').click({ force: true }); await sleep(300);
  assert.deepEqual(await h.crumbs(), ['Basics'], 'a snapshot does not navigate');
  await h.ctl('write', { path: dir + '/big.zip', size: 3 * MB });
  await h.view.click('#rescan');
  await h.view.waitForFunction(() => !document.getElementById('view').classList.contains('frozen') && !document.getElementById('refresh').disabled, null, { timeout: 20000 });
  assert.equal((await h.rows()).find(r => r.name === 'big.zip').size, '7.0 MB', 'the rescan shows the folder as it is now');
  assert.equal(await h.hidden('rescan'), true); assert.equal(await h.text('status'), '');
  await h.open('Films');
  assert.deepEqual(await h.crumbs(), ['Basics', 'Films']);
}));

test('the plugin restarting under an open view: the next click keeps the map as a snapshot', withHost(async h => {
  await h.scan(await h.fixture(BASIC));
  await h.ctl('restart');
  await h.tile('Films').click(); await frozen(h);
  assert.match(await h.text('status'), /Claude restarted the plugin or opened another folder since/);
  assert.equal((await h.tiles()).length, 6);
}));

test('ticks made before the view froze are not silently kept or shown as selected', withHost(async h => {
  await h.scan(await h.fixture(BASIC));
  await h.tick('big.zip');
  await h.ctl('restart'); await h.view.click('#refresh'); await frozen(h);
  assert.equal(await h.hidden('tray'), true);
  assert.equal((await h.tiles()).find(t => t.title === 'big.zip').classes.includes(' on'), false);
}));

test('folder access timing out (an hour unused): snapshot says so, Rescan works', withHost(async h => {
  await h.scan(await h.fixture(BASIC));
  await h.ctl('expire_grant');
  await h.tile('Films').click(); await frozen(h);
  assert.match(await h.text('status'), /Folder access timed out after an hour unused, so it's read-only\. Rescan to see the folder as it is now\./);
  await h.view.click('#rescan');
  await h.view.waitForFunction(() => !document.getElementById('view').classList.contains('frozen') && !document.getElementById('refresh').disabled, null, { timeout: 20000 });
  assert.equal((await h.rows())[0].name, 'Films');
}));

test('using the view keeps folder access alive (idle clock restarts)', withHost(async h => {
  await h.scan(await h.fixture(BASIC));
  const before = (await h.model('storage_list_scopes', {})).structuredContent.data.scopes[0].expiresInSeconds;
  assert.ok(before > 3590 && before <= 3600);
}));

test('a newer scan started elsewhere: this view becomes a snapshot on the next click', withHost(async h => {
  const dir = await h.fixture(BASIC);
  const s = await h.scan(dir);
  await h.model('storage_start_scan', { grantId: s.grantId, requestId: hex() });
  await h.tile('Films').click(); await frozen(h);
  assert.match(await h.text('status'), /A newer scan replaced it, so it's read-only/);
}));

test('a newer scan result shown in the same view replaces the old map; an older result arriving late is ignored', withHost(async h => {
  const dir = await h.fixture(BASIC);
  const first = await h.scan(dir);
  await h.ctl('write', { path: dir + '/big.zip', size: 3 * MB });
  const second = await h.model('storage_start_scan', { grantId: first.grantId, requestId: hex() });
  await h.show(second);
  await h.view.waitForFunction(() => [...document.querySelectorAll('#rows td.size b')].some(b => b.textContent === '7.0 MB'));
  await h.show(first.result); await sleep(300);
  assert.equal((await h.rows()).find(r => r.name === 'big.zip').size, '7.0 MB');
  assert.equal(await isFrozen(h), false);
}));

test('host that does not advertise tool calls but allows them: the view tests a call and enables itself', withHost({ mode: { advertise: false } }, async h => {
  await h.scan(await h.fixture(BASIC));
  assert.equal((await h.calls('storage_list_scopes')).length, 1);
  assert.equal(await h.hidden('limited'), true);
  await h.idle();
  assert.ok((await h.rows())[0].checkbox);
  await h.open('Films'); assert.deepEqual(await h.crumbs(), ['Basics', 'Films']);
}));

test('host that refuses tool calls: the map still shows, with a note explaining why it cannot open folders or clear files', withHost({ mode: { advertise: false, allow: false } }, async h => {
  await h.scan(await h.fixture(BASIC));
  assert.equal(await h.hidden('limited'), false);
  assert.match(await h.text('limited'), /doesn't let the map open folders or clear files\. Update the Claude app, or ask Claude to open the local view\./);
  assert.equal((await h.tiles()).length, 6); assert.equal((await h.rows())[0].size, '9.0 MB');
  assert.ok((await h.rows()).every(r => !r.checkbox)); assert.ok((await h.tiles()).every(t => !t.tick));
  assert.equal(await h.view.evaluate(() => document.getElementById('refresh').disabled), true);
  assert.equal(await h.hidden('suggest'), true);
  await h.tile('Films').click(); await sleep(300);
  assert.deepEqual(await h.crumbs(), ['Basics']);
  const dead = await h.view.evaluate(() => [...document.querySelectorAll('#map .tile, #rows button, #breadcrumbs button')].filter(b => !b.disabled && getComputedStyle(b).cursor === 'pointer').length);
  assert.equal(dead, 0, 'tiles and names still look clickable (pointer cursor, enabled) but do nothing');
}));

test('update banner: shown when the plugin knows of a newer version; Update asks the host to open the download', withHost(async h => {
  await h.ctl('offer', { version: '9.9.9' });
  const s = await h.scan(await h.fixture(BASIC));
  assert.equal(s.result._meta.storageAssistant.update.version, '9.9.9');
  assert.doesNotMatch(s.result.content[0].text, /9\.9\.9|mcpb/, 'the update offer is for the view, not the assistant');
  assert.equal(await h.hidden('update'), false);
  assert.equal(await h.text('update-text'), 'Storage Assistant 9.9.9 is available.');
  await h.view.click('#update-get');
  await h.view.waitForFunction(() => /Downloading/.test(document.getElementById('update-text').textContent));
  assert.deepEqual((await h.rec()).links, ['https://github.com/akshay-rakheja/storage-assistant/releases/latest/download/storage-assistant.mcpb']);
  assert.equal(await h.text('update-text'), 'Downloading… open storage-assistant.mcpb from your Downloads folder to install it.');
  assert.equal(await h.hidden('update-get'), true);
}));

test('update banner: no banner without an update, and a non-GitHub link is never offered', withHost(async h => {
  const dir = await h.fixture(BASIC);
  await h.scan(dir);
  assert.equal(await h.hidden('update'), true);
  await h.ctl('offer', { version: '9.9.9', url: 'https://evil.example/storage-assistant.mcpb' });
  await h.view.click('#refresh'); await sleep(300); await h.idle();
  assert.equal(await h.hidden('update'), true);
}));

test('update banner: when the host cannot open links the view shows the address to use', withHost({ mode: { links: false } }, async h => {
  await h.ctl('offer', {});
  await h.scan(await h.fixture(BASIC));
  await h.view.click('#update-get');
  await h.view.waitForFunction(() => /Download the new version from/.test(document.getElementById('update-text').textContent));
  assert.match(await h.text('update-text'), /https:\/\/github\.com\/akshay-rakheja\/storage-assistant\/releases\/latest\/download\/storage-assistant\.mcpb/);
}));

test('closing the view (host teardown) answers the host and clears the map', withHost(async h => {
  await h.scan(await h.fixture(BASIC));
  await h.send('ui/resource-teardown', {}, 'bye-1');
  await h.view.waitForFunction(() => /View closed/.test(document.getElementById('status').textContent));
  const { results } = await h.rec();
  assert.deepEqual(results.find(r => r.id === 'bye-1'), { jsonrpc: '2.0', id: 'bye-1', result: {} });
  assert.equal(await h.hidden('view'), true);
}));

test('the view answers host pings and announces itself', withHost(async h => {
  await h.send('ping', {}, 'p1'); await sleep(100);
  const rec = await h.rec();
  assert.ok(rec.results.some(r => r.id === 'p1' && r.result));
  assert.ok(rec.notes.includes('ui/notifications/initialized'));
  assert.equal(rec.init.appInfo.name, 'storage-assistant');
  assert.equal(await h.text('status'), 'Waiting for a scan…');
}));

test('the view never tells the host which files are ticked unless asked (no model-context updates on its own)', withHost(async h => {
  await h.scan(await h.fixture(BASIC)); await h.tick('big.zip'); await h.open('Films');
  assert.deepEqual((await h.rec()).context, []);
}));

test('an error result shown to a fresh view explains what to do', withHost(async h => {
  const bad = await h.model('storage_start_scan', { grantId: hex(), requestId: hex() });
  assert.equal(bad.isError, true);
  await h.show(bad); await sleep(200);
  assert.equal(await h.text('status'), 'Claude restarted the plugin or opened another folder, so this view has nothing to show. Ask Claude to scan the folder again.');
}));

test('Rescan when the folder is gone gives a plain message and keeps the snapshot', withHost(async h => {
  const dir = await h.fixture(BASIC);
  await h.scan(dir); await h.ctl('restart'); await h.view.click('#refresh'); await frozen(h);
  await h.ctl('remove', { path: dir });
  await h.view.click('#rescan');
  await h.view.waitForFunction(() => !/Rescanning/.test(document.getElementById('status').textContent) && !document.getElementById('rescan').disabled);
  const said = await h.text('status');
  assert.equal((await h.tiles()).length, 6, 'the snapshot stays');
  assert.doesNotMatch(said, /permissions were changed/, `"${said}" talks about permissions when the folder no longer exists`);
}));

test('a progress view reopened after the plugin restarted says the scan stopped and offers Rescan', withHost(async h => {
  await h.ctl('scan_wait', { seconds: 0 }); await h.ctl('scan_delay', { seconds: 3 });
  const s = await h.scan(await h.fixture(BASIC), { show: false });
  assert.equal(s.data.state, 'running');
  await h.ctl('scan_delay', { seconds: 0 }); await h.ctl('scan_wait', { seconds: 25 });
  await h.ctl('restart'); await h.load(); await h.show(s.result);
  await h.view.waitForFunction(() => /This scan stopped/.test(document.getElementById('status').textContent), null, { timeout: 10000 });
  assert.equal(await h.text('status'), 'This scan stopped when Claude restarted the plugin. Rescan to measure the folder again.');
  await h.view.click('#rescan');
  await h.view.waitForFunction(() => !document.getElementById('view').hidden && !document.getElementById('refresh').disabled, null, { timeout: 20000 });
  assert.equal((await h.rows())[0].name, 'Films');
}));

test('a view the assistant opens on one file shows its folder with that file highlighted', withHost(async h => {
  const s = await h.scan(await h.fixture(BASIC));
  const films = s.data.view.rows.find(r => r.displayLabel === 'Films').nodeId;
  const inside = (await h.model('storage_query', { scanId: s.scanId, scanVersion: s.scanVersion, basis: 'logical', withinNodeId: films })).structuredContent.data.rows;
  const launch = inside.find(r => r.displayLabel === 'launch.mov').nodeId;
  await h.show(await h.model('storage_render_view', { scanId: s.scanId, scanVersion: s.scanVersion, basis: 'logical', selectionIds: [launch] }));
  await h.view.waitForFunction(() => document.querySelector('#breadcrumbs button[aria-current]')?.textContent === 'Films');
  assert.equal(await h.tile('launch.mov').getAttribute('aria-pressed'), 'true');
  assert.equal(await h.tile('interview.mp4').getAttribute('aria-pressed'), 'false');
  assert.equal(await h.row('launch.mov').locator('td.name button').getAttribute('aria-pressed'), 'true');
}));
