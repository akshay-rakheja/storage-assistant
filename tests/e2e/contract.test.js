// Server contract: what the assistant (the model) sees, tool listing, output size, and refusals.
'use strict';
const { test, after } = require('node:test');
const assert = require('node:assert/strict');
const { withHost, closeAll, BASIC, CLEAN, MB, hex } = require('./harness');
after(closeAll);
const text = r => r.content[0].text;

test('tools/list: names, read-only and destructive hints, view-only visibility, one view per reply', withHost({ load: false }, async h => {
  const tools = (await h.rpc('tools/list')).result.tools, by = Object.fromEntries(tools.map(t => [t.name, t]));
  assert.deepEqual(Object.keys(by).sort(), ['storage_cancel_scan', 'storage_choose_folder', 'storage_cleanup_candidates', 'storage_cleanup_execute', 'storage_cleanup_preview', 'storage_cleanup_settings',
    'storage_get_node', 'storage_list_scopes', 'storage_open_local_view', 'storage_query', 'storage_release_scope', 'storage_render_view', 'storage_scan_status', 'storage_start_scan', 'storage_suggest_cleanup']);
  for (const name of ['storage_cleanup_preview', 'storage_cleanup_execute', 'storage_cleanup_settings', 'storage_cleanup_candidates'])
    assert.deepEqual(by[name]._meta.ui.visibility, ['app'], name);
  assert.deepEqual(tools.filter(t => t._meta?.ui?.resourceUri).map(t => t.name).sort(), ['storage_render_view', 'storage_start_scan'], 'only these two show a view');
  assert.equal(by.storage_cleanup_execute.annotations.destructiveHint, true); assert.equal(by.storage_cleanup_execute.annotations.readOnlyHint, false);
  assert.equal(by.storage_cleanup_candidates.annotations.readOnlyHint, true); assert.equal(by.storage_cleanup_candidates.annotations.destructiveHint, false);
  for (const name of ['storage_suggest_cleanup', 'storage_query', 'storage_scan_status', 'storage_render_view', 'storage_start_scan', 'storage_list_scopes', 'storage_get_node'])
    assert.equal(by[name].annotations.readOnlyHint, true, name);
  assert.equal(by.storage_start_scan.inputSchema.properties.limits, undefined, 'the assistant cannot shrink the scan');
  assert.deepEqual(by.storage_start_scan.inputSchema.properties.freeUpSpace, { type: 'boolean' });
  assert.deepEqual(by.storage_render_view.inputSchema.properties.freeUpSpace, { type: 'boolean' });
  for (const t of tools) assert.equal(t.annotations.openWorldHint, false, t.name);
}));

test('tools that only read are not labelled destructive (preview changes nothing)', withHost({ load: false }, async h => {
  const by = Object.fromEntries((await h.rpc('tools/list')).result.tools.map(t => [t.name, t]));
  assert.match(by.storage_cleanup_preview.description, /Changes nothing/);
  assert.equal(by.storage_cleanup_preview.annotations.destructiveHint, false, 'preview says it changes nothing but is labelled destructive');
}));

test('initialize: instructions tell the assistant to show exactly one view per reply and that it cannot delete', withHost({ load: false }, async h => {
  const init = (await h.rpc('initialize', { protocolVersion: '2025-11-25', capabilities: {} })).result;
  await h.ctl('restart');
  assert.match(init.instructions, /Show at most ONE view per reply: only storage_start_scan and storage_render_view show one\./);
  assert.match(init.instructions, /Every reply about the folder shows ONE view/);
  assert.match(init.instructions, /You cannot delete, move or evict anything/);
  assert.match(init.instructions, /Never start a second scan for the same folder/);
  assert.equal(init.serverInfo.name, 'storage-assistant');
}));

test('a host without MCP Apps gets no view-only tools and cannot call them', withHost({ load: false }, async h => {
  await h.ctl('ui', { on: false });
  const names = (await h.rpc('tools/list')).result.tools.map(t => t.name);
  assert.ok(!names.some(n => /^storage_cleanup_/.test(n)));
  const r = await h.model('storage_cleanup_execute', { token: hex() });
  assert.equal(r.structuredContent.error.code, 'UNSUPPORTED');
  assert.match(r.structuredContent.error.message, /Call storage_open_local_view/);
}));

test('scan result: real names, exact sizes, coverageSummary, date; text mirrors structured content', withHost({ load: false }, async h => {
  const dir = await h.fixture(BASIC);
  const s = await h.scan(dir, { show: false });
  assert.deepEqual(JSON.parse(text(s.result)), s.result.structuredContent);
  assert.equal(s.data.state, 'partial'); assert.equal(s.data.logicalBytes, '16700010');
  assert.equal(s.data.coverageSummary, 'Everything reachable was measured. Not counted: 2 credential folders and keys skipped (.ssh, .aws, .env, keys); 1 links not followed.');
  assert.ok(Math.abs(s.data.scannedAt - Date.now() / 1000) < 60);
  const rows = Object.fromEntries(s.data.view.rows.map(r => [r.displayLabel, r]));
  assert.equal(rows.Films.bytes, '9000000'); assert.equal(rows['big.zip'].path, 'big.zip'); assert.equal(s.data.view.within.displayLabel, 'Basics');
  assert.equal(s.data.view.matchingRows, 10);
}));

test('coverageSummary uses correct grammar for a single item', withHost({ load: false }, async h => {
  const s = await h.scan(await h.fixture({ name: 'One', age: 30, entries: [{ p: 'a.zip', size: 10 }, { p: 'l', link: 'a.zip' }] }), { show: false });
  assert.doesNotMatch(s.data.coverageSummary, /\b1 links\b/, `"${s.data.coverageSummary}"`);
}));

test('where the folder lives is told to the view only (_meta), never to the assistant', withHost({ load: false }, async h => {
  const dir = await h.fixture(CLEAN);
  const s = await h.scan(dir, { show: false, free: true });
  assert.equal(s.result._meta.storageAssistant.folder, dir);
  const parent = dir.slice(0, dir.lastIndexOf('/'));
  const seen = [s.result];
  seen.push(await h.model('storage_render_view', { scanId: s.scanId, scanVersion: s.scanVersion, basis: 'logical' }));
  seen.push(await h.model('storage_query', { scanId: s.scanId, scanVersion: s.scanVersion, basis: 'logical', aggregate: 'largest_files' }));
  seen.push(await h.model('storage_suggest_cleanup', { scanId: s.scanId, scanVersion: s.scanVersion }));
  seen.push(await h.model('storage_scan_status', { scanId: s.scanId }));
  seen.push(await h.model('storage_list_scopes', {}));
  seen.push(await h.model('storage_choose_folder', { path: dir + '/does-not-exist' }));
  for (const r of seen) {
    assert.ok(!text(r).includes(parent), 'absolute path in model-visible text');
    assert.ok(!JSON.stringify(r.structuredContent).includes(parent), 'absolute path in structured content');
  }
  assert.equal((await h.model('storage_suggest_cleanup', { scanId: s.scanId, scanVersion: s.scanVersion }))._meta, undefined);
}));

test('storage_query answers with real names and relative paths: largest files, children, by type', withHost({ load: false }, async h => {
  const s = await h.scan(await h.fixture(BASIC), { show: false });
  const base = { scanId: s.scanId, scanVersion: s.scanVersion, basis: 'logical' };
  const largest = (await h.model('storage_query', { ...base, aggregate: 'largest_files', pageSize: 3 })).structuredContent.data;
  assert.deepEqual(largest.rows.map(r => [r.path, r.bytes]), [['Films/launch.mov', '6000000'], ['big.zip', '4000000'], ['Films/interview.mp4', '3000000']]);
  const films = s.data.view.rows.find(r => r.displayLabel === 'Films').nodeId;
  const inside = (await h.model('storage_query', { ...base, withinNodeId: films })).structuredContent.data;
  assert.deepEqual(inside.rows.map(r => r.displayLabel), ['launch.mov', 'interview.mp4']);
  const types = (await h.model('storage_query', { ...base, aggregate: 'by_category' })).structuredContent.data.rows;
  assert.deepEqual(types.map(r => [r.displayLabel, r.bytes]).slice(0, 2), [['video', '9000000'], ['archive', '4000010']]);
}));

test('file-type counts count the files, including small ones grouped in the scan', withHost({ load: false }, async h => {
  const s = await h.scan(await h.fixture(BASIC), { show: false });
  const types = (await h.model('storage_query', { scanId: s.scanId, scanVersion: s.scanVersion, basis: 'logical', aggregate: 'by_category' })).structuredContent.data.rows;
  assert.equal(types.find(r => r.displayLabel === 'image').count, 2, 'two small pictures are reported as one file');
}));

test('render_view for the assistant: opens a subfolder, file types, and suggestions with freeUpSpace', withHost({ load: false }, async h => {
  const s = await h.scan(await h.fixture(CLEAN), { show: false });
  const base = { scanId: s.scanId, scanVersion: s.scanVersion, basis: 'logical' };
  const proj = s.data.view.rows.find(r => r.displayLabel === 'proj').nodeId;
  const sub = await h.model('storage_render_view', { ...base, withinNodeId: proj });
  assert.equal(sub.structuredContent.data.view.within.displayLabel, 'proj'); assert.equal(sub.structuredContent.data.candidates, undefined);
  assert.equal(sub._meta.storageAssistant.folder.endsWith('/Cleanup'), true);
  const types = await h.model('storage_render_view', { ...base, aggregate: 'by_category' });
  assert.equal(types.structuredContent.data.view.aggregate, 'by_category');
  const free = await h.model('storage_render_view', { ...base, freeUpSpace: true });
  assert.equal(free.structuredContent.data.candidates.length, 6); assert.equal(free.structuredContent.data.freeUpSpace, true);
  assert.equal(free.structuredContent.data.candidatesFreesBytes, '27013120');
  assert.deepEqual(JSON.parse(text(free)), free.structuredContent);
  const bad = await h.model('storage_render_view', { ...base, freeUpSpace: 'yes' });
  assert.equal(bad.structuredContent.error.code, 'INVALID_INPUT');
}));

test('storage_suggest_cleanup: data only, tiers and reasons, subfolder focus', withHost({ load: false }, async h => {
  const s = await h.scan(await h.fixture(CLEAN), { show: false });
  const all = (await h.model('storage_suggest_cleanup', { scanId: s.scanId, scanVersion: s.scanVersion })).structuredContent.data;
  assert.deepEqual(all.candidates.map(c => [c.path, c.tier, c.reasons[0].text]), [
    ['proj/node_modules', 'safe', 'Rebuilds with npm install'], ['old-installer.crdownload', 'safe', 'Unfinished download'], ['partial.download', 'safe', 'Unfinished download'],
    ['ancient.iso', 'glance', 'Untouched since 2024'], ['b/movie.mov', 'glance', 'Possible duplicate of a/movie.mov'], ['archive.zip', 'glance', 'Already unzipped']]);
  assert.deepEqual(all.totals, { safeBytes: '10006528', glanceBytes: '17006592' });
  assert.equal(all.view, undefined);
  const proj = s.data.view.rows.find(r => r.displayLabel === 'proj').nodeId;
  const sub = (await h.model('storage_suggest_cleanup', { scanId: s.scanId, scanVersion: s.scanVersion, withinNodeId: proj })).structuredContent.data;
  assert.deepEqual(sub.candidates.map(c => c.path), ['proj/node_modules']);
}));

test('a big folder with hundreds of suggestions stays under the host output cap and keeps its view', withHost({ load: false }, async h => {
  const entries = Array.from({ length: 400 }, (_, i) => ({ p: `sub${i % 40}/download-${String(i).padStart(3, '0')}-${'long-name-'.repeat(8)}.crdownload`, size: 2000 + i }));
  const s = await h.scan(await h.fixture({ name: 'Huge', age: 30, entries }), { show: false, free: true });
  assert.ok(text(s.result).length <= 60000, `scan result is ${text(s.result).length} characters`);
  assert.equal(s.data.candidatesTotal, 400); assert.equal(s.data.candidates.length, 20);
  assert.match(s.data.candidatesNote, /^Showing the 20 largest of 400 suggestions/);
  assert.equal(s.data.view.rows.length, 30); assert.equal(s.data.view.matchingRows, 40);
  assert.equal(s.data.candidatesFreesBytes, String(entries.reduce((n, e) => n + 4096, 0)));
  const suggest = await h.model('storage_suggest_cleanup', { scanId: s.scanId, scanVersion: s.scanVersion });
  assert.ok(text(suggest).length <= 60000);
  const render = await h.model('storage_render_view', { scanId: s.scanId, scanVersion: s.scanVersion, basis: 'logical', freeUpSpace: true });
  assert.ok(text(render).length <= 60000); assert.equal(render.structuredContent.data.view.rows.length, 30);
}));

test('storage_query with the largest page size stays under the plugin\'s own output budget (ordinary names)', withHost({ load: false }, async h => {
  const entries = Array.from({ length: 120 }, (_, i) => ({ p: `Project ${i % 7}/assets/video-clip-${String(i).padStart(3, '0')}.zip`, size: 2000 + i }));
  const s = await h.scan(await h.fixture({ name: 'Normal', age: 30, entries }), { show: false });
  const r = await h.model('storage_query', { scanId: s.scanId, scanVersion: s.scanVersion, basis: 'logical', aggregate: 'largest_files', pageSize: 100 });
  assert.equal(r.structuredContent.data.rows.length, 100);
  assert.ok(text(r).length <= 60000, `storage_query result is ${text(r).length} characters; the 60,000-character backstop only trims suggestions and view rows`);
}));

test('the view can page through a deep folder of long non-English names without losing rows', withHost(async h => {
  const dir = n => '長い名前のフォルダ'.repeat(6) + n, file = i => 'とても長い名前のファイル'.repeat(5) + String(i).padStart(2, '0') + '.zip';
  const entries = Array.from({ length: 32 }, (_, i) => ({ p: `${dir(1)}/${dir(2)}/${dir(3)}/${file(i)}`, size: 9000 - i }));
  const s = await h.scan(await h.fixture({ name: 'LongRows', age: 30, entries }));
  for (let i = 1; i <= 3; i++) await h.open(dir(i));
  const last = (await h.calls('storage_render_view')).pop().result.structuredContent.data.view;
  assert.equal(last.matchingRows, 32);
  const seen = new Set();
  for (let i = 0; i < 6; i++) {
    for (const r of await h.rows()) seen.add(r.name);
    if (await h.hidden('next') || await h.hidden('view')) break;
    await h.view.click('#next'); await new Promise(r => setTimeout(r, 400));
  }
  assert.equal(seen.size, 32, `only ${seen.size} of 32 files can be reached in the table (the first page arrived with ${last.rows.length} rows)`);
}));

test('choose_folder refuses home and the disk root without opening them, and explains bad paths', withHost({ load: false }, async h => {
  for (const p of ['~', '/', '~/']) {
    const r = await h.model('storage_choose_folder', { path: p });
    assert.equal(r.isError, true, p); assert.ok(['PROTECTED', 'INVALID_INPUT'].includes(r.structuredContent.error.code), p);
  }
  const rel = await h.model('storage_choose_folder', { path: 'Downloads' });
  assert.equal(rel.structuredContent.error.message, 'Use an absolute path or one starting with ~/ (for example ~/Downloads).');
  const extra = await h.model('storage_choose_folder', { path: '/tmp', recursive: true });
  assert.equal(extra.structuredContent.error.code, 'INVALID_INPUT');
  const none = await h.model('storage_choose_folder', {});
  assert.equal(none.structuredContent.error.code, 'NOT_SELECTED');
}));

test('starting a scan twice with the same request does not start a second scan', withHost({ load: false }, async h => {
  const dir = await h.fixture(BASIC);
  const grantId = (await h.model('storage_choose_folder', { path: dir })).structuredContent.data.scopes[0].grantId, requestId = hex();
  const a = await h.model('storage_start_scan', { grantId, requestId }), b = await h.model('storage_start_scan', { grantId, requestId });
  assert.equal(a.structuredContent.data.scanId, b.structuredContent.data.scanId);
  assert.equal(b.structuredContent.data.scanVersion, 1);
}));

test('cleanup preview and execute at the tool level: one-time token, 7-day rule, changed since preview', withHost({ load: false }, async h => {
  const dir = await h.fixture(CLEAN);
  await h.ctl('fake_executor');
  const s = await h.scan(dir, { show: false, free: true });
  const id = name => s.data.view.rows.find(r => r.displayLabel === name)?.nodeId;
  const base = { scanId: s.scanId, scanVersion: s.scanVersion };
  const recent = (await h.model('storage_cleanup_preview', { ...base, nodeIds: [id('fresh.crdownload')], action: 'trash' })).structuredContent.data;
  assert.deepEqual([recent.token, recent.count, recent.skipped[0].reason], [null, 0, 'Changed in the last 7 days']);
  const notSafe = (await h.model('storage_cleanup_preview', { ...base, nodeIds: [id('keep.mov'), id('ancient.iso')], action: 'delete' })).structuredContent.data;
  assert.deepEqual(notSafe.skipped.map(x => x.reason), ['Not offered for this action', 'Not offered for this action'], 'permanent delete is for Safe suggestions only');
  const plan = (await h.model('storage_cleanup_preview', { ...base, nodeIds: [id('keep.mov'), id('old-installer.crdownload')], action: 'trash' })).structuredContent.data;
  assert.equal(plan.count, 2); assert.match(plan.token, /^[a-f0-9]{32}$/);
  await h.ctl('write', { path: dir + '/keep.mov', size: 5 });
  const receipt = (await h.model('storage_cleanup_execute', { token: plan.token })).structuredContent.data;
  assert.deepEqual(receipt.done.map(d => d.path), ['old-installer.crdownload']);
  assert.deepEqual(receipt.skipped.map(x => [x.displayLabel, x.reason]), [['keep.mov', 'Changed since you reviewed it']]);
  assert.equal(receipt.rescan.started, true);
  const replay = await h.model('storage_cleanup_execute', { token: plan.token });
  assert.equal(replay.structuredContent.error.message, 'This approval was already used or is not valid. Review again.');
  const stale = await h.model('storage_cleanup_preview', { ...base, nodeIds: [id('archive.zip')], action: 'trash' });
  assert.equal(stale.structuredContent.error.code, 'STALE_SCAN');
}));

test('the UI resource is self-contained: no network access declared, no external URLs', withHost({ load: false }, async h => {
  const list = (await h.rpc('resources/list')).result.resources;
  assert.equal(list.length, 1); assert.equal(list[0].mimeType, 'text/html;profile=mcp-app');
  assert.deepEqual(list[0]._meta.ui.csp, { connectDomains: [], resourceDomains: [], frameDomains: [], baseUriDomains: [] });
  const html = (await h.rpc('resources/read', { uri: list[0].uri })).result.contents[0].text;
  assert.doesNotMatch(html, /<script[^>]+src=|<link[^>]+href=|fetch\(|XMLHttpRequest|localStorage|document\.cookie/);
  assert.doesNotMatch(html, /BUNDLED_(STYLE|SCRIPT)/);
}));
