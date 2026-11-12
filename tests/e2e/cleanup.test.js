// Suggestions and the cleanup flow: Review, Move to Trash, Delete permanently, receipts and refusals.
// Everything acts on throwaway fixture folders. The Trash is a temp folder unless E2E_REAL_TRASH=1.
'use strict';
const { test, after } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { withHost, closeAll, CLEAN, MB, sleep } = require('./harness');
after(closeAll);

const reviewReady = h => h.view.waitForFunction(() => !document.getElementById('review').hidden && !/Checking/.test(document.getElementById('review-status').textContent) && !document.getElementById('review-back').disabled);
const receiptShown = h => h.view.waitForFunction(() => !document.getElementById('receipt').hidden);
const mapUpdated = h => h.view.waitForFunction(() => /The map now shows/.test(document.getElementById('receipt-status').textContent), null, { timeout: 15000 });
const inner = (h, id) => h.view.evaluate(i => document.getElementById(i).innerText, id);
const list = (h, id) => h.view.evaluate(i => [...document.getElementById(i).children].map(li => li.innerText.replace(/\n+/g, ' | ')), id);
async function review(h) { await h.view.click('#tray-review'); await reviewReady(h); }
async function untickAll(h) { await h.view.evaluate(() => 0); for (const name of (await h.rows()).filter(r => r.checked).map(r => r.name)) await h.row(name).locator('input').uncheck(); }

test('a scan asked to free space opens with suggestions: tiers, reasons, safe ones pre-ticked', withHost(async h => {
  const s = await h.scan(await h.fixture(CLEAN), { free: true });
  await h.view.waitForSelector('.chip'); await h.view.click('#next');
  const by = Object.fromEntries((await h.rows()).map(r => [r.name, r]));
  assert.deepEqual([by['old-installer.crdownload'].chip, by['old-installer.crdownload'].tier, by['old-installer.crdownload'].checked], ['Unfinished download', 'tier-safe', true]);
  assert.deepEqual([by['partial.download'].chip, by['partial.download'].checked], ['Unfinished download', true]);
  assert.deepEqual([by['ancient.iso'].chip, by['ancient.iso'].tier, by['ancient.iso'].checked], ['Untouched since 2024', 'tier-glance', false]);
  assert.equal(by['fresh.crdownload'].chip, null, 'a download from yesterday is not suggested');
  assert.equal(by['keep.mov'].chip, null);
  assert.equal(await h.text('tray-text'), '3 items selected · about 10.0 MB');
  assert.equal(await h.hidden('suggest'), true);
  const tile = (await h.tiles()).find(t => t.title === 'ancient.iso');
  assert.equal(tile.chip, 'Untouched since 2024'); assert.match(tile.label, /suggested: Worth a glance$/);
  assert.equal(await h.view.evaluate(() => document.querySelector('.chip.tier-safe').title), 'Safe to clear: Unfinished download');
  // What the assistant is told matches what the view shows.
  assert.equal(s.data.candidatesTotal, 6);
  assert.deepEqual(s.data.candidates.filter(c => c.tier === 'safe').map(c => c.path).sort(), ['old-installer.crdownload', 'partial.download', 'proj/node_modules']);
}));

test('Suggest what to clear loads suggestions without the assistant', withHost(async h => {
  await h.scan(await h.fixture(CLEAN));
  assert.equal(await h.hidden('suggest'), false); assert.equal(await h.hidden('tray'), true);
  await h.view.click('#suggest');
  await h.view.waitForSelector('.chip');
  assert.equal((await h.calls('storage_suggest_cleanup')).length, 1);
  assert.equal(await h.hidden('suggest'), true);
  assert.equal(await h.text('tray-text'), '3 items selected · about 10.0 MB');
  assert.equal(await h.text('cleanup-status'), '');
}));

test('a folder with nothing to suggest says so', withHost(async h => {
  await h.scan(await h.fixture({ name: 'Plain', age: 30, entries: [{ p: 'film.mov', size: 2 * MB }] }));
  await h.view.click('#suggest');
  await h.view.waitForFunction(() => /Nothing stands out/.test(document.getElementById('cleanup-status').textContent));
  assert.equal(await h.text('cleanup-status'), 'Nothing stands out to suggest here. Tick anything you want to clear.');
  assert.equal(await h.hidden('tray'), true);
}));

test('Review lists each ticked item with its reason and size, what is left out, and the actions on offer', withHost(async h => {
  await h.scan(await h.fixture(CLEAN), { free: true });
  await h.view.waitForSelector('.chip');
  await h.tick('keep.mov'); await h.tick('fresh.crdownload');
  await review(h);
  assert.deepEqual(await list(h, 'review-items'), [
    'node_modules | proj/node_modules · Rebuilds with npm install | 5.0 MB', 'old-installer.crdownload | old-installer.crdownload · Unfinished download | 3.0 MB',
    'partial.download | partial.download · Unfinished download | 2.0 MB', 'keep.mov | keep.mov | 4.0 MB', 'fresh.crdownload | fresh.crdownload | 2.0 MB']);
  assert.deepEqual(await list(h, 'review-skipped'), ['Left out: fresh.crdownload: Changed in the last 7 days']);
  assert.equal(await h.text('do-trash'), 'Move 4 items to Trash');
  assert.equal(await h.text('do-delete'), 'Delete permanently, free 10.0 MB now');
  assert.equal(await h.hidden('do-evict'), true);
  assert.equal(await h.hidden('tray'), true, 'the tray steps aside while reviewing');
  await h.view.click('#review-back');
  assert.equal(await h.hidden('review'), true); assert.equal(await h.hidden('tray'), false);
  assert.equal(await h.text('tray-text'), '5 items selected · about 16.0 MB');
}));

test('only a recently changed item ticked: Review explains the 7-day rule and offers no action', withHost(async h => {
  await h.scan(await h.fixture(CLEAN));
  await h.tick('fresh.crdownload'); await review(h);
  assert.deepEqual(await list(h, 'review-skipped'), ['Left out: fresh.crdownload: Changed in the last 7 days']);
  assert.deepEqual([await h.hidden('do-trash'), await h.hidden('do-delete'), await h.hidden('do-evict')], [true, true, true]);
}));

test('unticking and re-ticking inside Review updates the plan and keeps focus', withHost(async h => {
  await h.scan(await h.fixture(CLEAN), { free: true });
  await h.view.waitForSelector('.chip'); await review(h);
  assert.equal(await h.text('do-trash'), 'Move 3 items to Trash');
  const box = h.view.locator('#review-items input[aria-label="Select partial.download (Safe to clear) in this review"]');
  await box.focus(); await h.page.keyboard.press('Space'); await reviewReady(h);
  assert.equal(await h.text('do-trash'), 'Move 2 items to Trash'); assert.equal(await h.text('do-delete'), 'Delete permanently, free 8.0 MB now');
  assert.equal((await list(h, 'review-items')).length, 3, 'the unticked item stays listed');
  assert.equal((await h.active()).label, 'Select partial.download (Safe to clear) in this review');
  await h.page.keyboard.press('Space'); await reviewReady(h);
  assert.equal(await h.text('do-trash'), 'Move 3 items to Trash');
}));

test('a folder and something inside it both ticked: the inner item is covered by its folder', withHost(async h => {
  await h.scan(await h.fixture(CLEAN));
  await h.tick('a'); await h.open('a'); await h.tick('movie.mov'); await review(h);
  assert.deepEqual(await list(h, 'review-skipped'), ['Left out: movie.mov: Included in its folder']);
  assert.equal(await h.text('do-trash'), 'Move 1 item to Trash');
}));

test('Delete permanently: confirm (Cancel focused, Escape cancels), receipt, files gone, map and suggestions update', withHost(async h => {
  const dir = await h.fixture(CLEAN);
  await h.scan(dir, { free: true });
  await h.view.waitForSelector('.chip'); await review(h);
  await h.view.click('#do-delete');
  assert.equal(await h.text('confirm-text'), "Delete 3 items permanently? This can't be undone.");
  assert.equal(await h.text('confirm-total'), 'Frees about 10.0 MB on this Mac right away.');
  assert.deepEqual((await list(h, 'confirm-items')).sort(), ['node_modules | proj/node_modules', 'old-installer.crdownload | old-installer.crdownload', 'partial.download | partial.download']);
  assert.equal((await h.active()).id, 'confirm-no', 'the safe choice has focus');
  assert.equal(await h.view.evaluate(() => document.getElementById('confirm-delete').getAttribute('role')), 'alertdialog');
  await h.page.keyboard.press('Escape'); await reviewReady(h);
  assert.equal(await h.hidden('confirm-delete'), true);
  assert.equal((await h.calls('storage_cleanup_execute')).length, 0);
  assert.deepEqual((await h.ctl('listdir', { path: dir })).names.includes('old-installer.crdownload'), true);
  await h.view.click('#do-delete'); await h.view.click('#confirm-yes'); await receiptShown(h);
  assert.equal(await h.text('receipt-title'), 'Deleted');
  assert.equal(await h.text('receipt-summary'), 'Deleted 3 items (about 10.0 MB).');
  assert.equal((await list(h, 'receipt-done')).length, 3);
  assert.match(await h.text('receipt-space'), /^Free space on this Mac: .+ before, .+ now \([+−].+\)\.$/);
  assert.equal(await h.hidden('open-trash'), true);
  assert.equal((await h.active()).id, 'receipt-title');
  const names = (await h.ctl('listdir', { path: dir })).names;
  assert.deepEqual(names, ['a', 'ancient.iso', 'archive', 'archive.zip', 'b', 'fresh.crdownload', 'keep.mov', 'notes', 'proj']);
  assert.deepEqual((await h.ctl('listdir', { path: dir + '/proj' })).names, ['package.json', 'src']);
  await mapUpdated(h);
  const rows = await h.rows();
  assert.ok(!rows.some(r => /crdownload$/.test(r.name) && r.name !== 'fresh.crdownload') && !rows.some(r => r.name === 'partial.download'));
  assert.equal(await h.text('summary'), '26.0 MBin 9 items');
  await h.view.waitForSelector('.chip');
  assert.equal(rows.find(r => r.name === 'proj').size, '700 B');
  await h.view.click('#receipt-close');
  assert.equal(await h.hidden('receipt'), true); assert.equal(await h.hidden('tray'), true);
}));

test('Move to Trash: receipt, Open Trash, items moved, map updates', withHost(async h => {
  const dir = await h.fixture(CLEAN);
  const { trash } = await h.ctl('fake_executor');
  await h.scan(dir);
  await h.tick('keep.mov'); await h.tick('archive.zip'); await review(h);
  assert.equal(await h.text('do-trash'), 'Move 2 items to Trash'); assert.equal(await h.hidden('do-delete'), true, 'items the person ticked themselves only go to the Trash');
  await h.view.click('#do-trash'); await receiptShown(h);
  assert.equal(await h.text('receipt-title'), 'Moved to the Trash');
  assert.equal(await h.text('receipt-summary'), 'Moved 2 items to the Trash (about 6.0 MB).');
  assert.equal(await h.text('receipt-note'), 'Space is freed when you empty the Trash.');
  assert.equal(await h.hidden('open-trash'), false); assert.equal(await h.hidden('receipt-hint'), false);
  assert.deepEqual((await h.ctl('listdir', { path: trash })).names, ['archive.zip', 'keep.mov']);
  assert.ok(!(await h.ctl('listdir', { path: dir })).names.includes('keep.mov'));
  await mapUpdated(h);
  assert.ok(!(await h.rows()).some(r => r.name === 'keep.mov'));
  await h.view.click('#open-trash');
  await h.view.waitForFunction(() => document.getElementById('receipt-status').textContent === 'The Trash is open in Finder.');
  assert.deepEqual((await h.ctl('opened')).opened, ['trash']);
}));

test('Move to the real macOS Trash (opt-in: E2E_REAL_TRASH=1)', { skip: process.env.E2E_REAL_TRASH !== '1' }, withHost(async h => {
  const name = `sa-e2e-${Date.now()}-${Math.floor(Math.random() * 1e6)}-trash-me.zip`;
  const dir = await h.fixture({ name: 'RealTrash', age: 30, entries: [{ p: name, size: 5000 }, { p: 'stay.zip', size: 4000 }] });
  await h.scan(dir); await h.tick(name); await review(h);
  await h.view.click('#do-trash'); await receiptShown(h);
  const done = (await h.calls('storage_cleanup_execute'))[0].result.structuredContent.data.done;
  const trashed = done[0].trashPath;
  try {
    assert.equal(await h.text('receipt-summary'), 'Moved 1 item to the Trash (about 8.2 KB).');
    assert.equal(done[0].putBack, true);
    assert.equal(path.dirname(trashed), path.join(os.homedir(), '.Trash')); assert.equal(path.basename(trashed), name);
    assert.ok(fs.existsSync(trashed)); assert.ok(!fs.existsSync(path.join(dir, name)));
  } finally {
    // Remove exactly the one fixture item this test put in the Trash.
    if (trashed && path.basename(trashed) === name && path.dirname(trashed) === path.join(os.homedir(), '.Trash')) fs.rmSync(trashed);
  }
  assert.ok(!fs.existsSync(trashed), 'the fixture file was removed from the Trash again');
}));

test('an item changed after Review is left alone, and the receipt says why', withHost(async h => {
  const dir = await h.fixture(CLEAN);
  await h.ctl('fake_executor');
  await h.scan(dir); await h.tick('keep.mov'); await review(h);
  await h.ctl('write', { path: dir + '/keep.mov', size: 10 });
  await h.view.click('#do-trash'); await receiptShown(h);
  assert.equal(await h.text('receipt-summary'), 'Nothing was changed.');
  assert.equal(await h.hidden('receipt-skipped-title'), false);
  assert.deepEqual(await list(h, 'receipt-skipped'), ['keep.mov: Changed since you reviewed it']);
  assert.equal(await h.hidden('open-trash'), true);
  assert.ok((await h.ctl('exists', { path: dir + '/keep.mov' })).exists);
}));

test('an item changed after the scan is left out at Review', withHost(async h => {
  const dir = await h.fixture(CLEAN);
  await h.scan(dir); await h.ctl('write', { path: dir + '/keep.mov', size: 10 });
  await h.tick('keep.mov'); await review(h);
  assert.deepEqual(await list(h, 'review-skipped'), ['Left out: keep.mov: Changed since you reviewed it']);
  assert.equal(await h.hidden('do-trash'), true);
}));

test('an expired approval is refused; nothing changes and the person can review again', withHost(async h => {
  const dir = await h.fixture(CLEAN);
  await h.ctl('fake_executor');
  await h.scan(dir); await h.tick('keep.mov'); await review(h);
  await h.ctl('expire_tokens');
  await h.view.click('#do-trash');
  await h.view.waitForFunction(() => /expired/.test(document.getElementById('review-status').textContent));
  assert.equal(await h.text('review-status'), 'This approval expired. Review again. Nothing more was sent. Go back and review again; Refresh shows the current state.');
  assert.equal(await h.hidden('do-trash'), true, 'the spent plan offers no button');
  assert.ok((await h.ctl('exists', { path: dir + '/keep.mov' })).exists);
  await h.view.click('#review-back'); await review(h);
  assert.equal(await h.text('do-trash'), 'Move 1 item to Trash');
}));

test('a used approval cannot be replayed', withHost(async h => {
  const dir = await h.fixture(CLEAN);
  await h.ctl('fake_executor');
  await h.scan(dir); await h.tick('keep.mov'); await review(h);
  const token = (await h.calls('storage_cleanup_preview'))[0].result.structuredContent.data.token;
  await h.view.click('#do-trash'); await receiptShown(h);
  const again = await h.model('storage_cleanup_execute', { token });
  assert.equal(again.isError, true); assert.equal(again.structuredContent.error.message, 'This approval was already used or is not valid. Review again.');
}));

test('double-clicking an action sends it once', withHost(async h => {
  const dir = await h.fixture(CLEAN);
  await h.ctl('fake_executor');
  await h.scan(dir); await h.tick('keep.mov'); await review(h);
  await h.setMode({ delay: 300, delayTool: 'storage_cleanup_execute' });
  await h.view.evaluate(() => { const b = document.getElementById('do-trash'); b.click(); b.click(); b.click(); });
  await receiptShown(h);
  assert.equal((await h.calls('storage_cleanup_execute')).length, 1);
}));

test('an item macOS refuses shows under "Didn\'t finish" while the rest goes through', withHost(async h => {
  const dir = await h.fixture(CLEAN);
  await h.ctl('fake_executor'); await h.ctl('fail_executor', { names: ['keep.mov'] });
  await h.scan(dir); await h.tick('keep.mov'); await h.tick('archive.zip'); await review(h);
  await h.view.click('#do-trash'); await receiptShown(h);
  assert.equal(await h.text('receipt-summary'), 'Moved 1 item to the Trash (about 2.0 MB).');
  assert.equal(await h.hidden('receipt-failed-title'), false);
  assert.deepEqual(await list(h, 'receipt-failed'), ['keep.mov: macOS denied access']);
}));

test('when the host loses the answer to an action, the view says the result is unknown and offers Refresh', withHost(async h => {
  const dir = await h.fixture(CLEAN);
  await h.ctl('fake_executor');
  await h.scan(dir); await h.tick('keep.mov'); await review(h);
  await h.setMode({ reject: ['storage_cleanup_execute'] });
  await h.view.click('#do-trash');
  await h.view.waitForFunction(() => /result is unknown/.test(document.getElementById('review-status').textContent));
  assert.equal(await h.hidden('check-refresh'), false); assert.equal(await h.hidden('do-trash'), true);
  assert.equal((await h.active()).id, 'review-title');
  await h.view.click('#check-refresh'); await h.idle();
  assert.equal(await h.hidden('review'), true); assert.equal(await h.hidden('view'), false);
}));

test('clicking in the map right after an action does not turn the view into a read-only snapshot', withHost(async h => {
  const dir = await h.fixture(CLEAN);
  await h.ctl('fake_executor');
  await h.scan(dir); await h.tick('keep.mov'); await review(h);
  await h.view.click('#do-trash'); await receiptShown(h);
  // The plugin is rescanning (the view checks again a second later); the person carries on.
  await h.view.click('#receipt-close'); await h.view.click('#refresh');
  await sleep(2500);
  assert.equal(await h.view.evaluate(() => document.getElementById('view').classList.contains('frozen')), false, `the view froze: "${await h.text('status')}"`);
  assert.ok(!(await h.rows()).some(r => r.name === 'keep.mov'), 'the map shows the folder after the change');
}));

test('after one action the person can tick and act again', withHost(async h => {
  const dir = await h.fixture({ name: 'Twice', age: 30, entries: [{ p: 'one.mov', size: 4 * MB }, { p: 'two.mov', size: 3 * MB }, { p: 'three.mov', size: 2 * MB }] });
  const { trash } = await h.ctl('fake_executor');
  await h.scan(dir); await h.tick('one.mov'); await review(h);
  await h.view.click('#do-trash'); await receiptShown(h); await mapUpdated(h);
  await h.view.click('#receipt-close');
  await h.view.waitForFunction(() => !document.getElementById('refresh').disabled);
  assert.equal(await h.hidden('tray'), true, 'nothing is ticked after the map updates');
  await h.tick('two.mov'); await review(h);
  assert.equal(await h.text('do-trash'), 'Move 1 item to Trash');
  await h.view.click('#do-trash'); await receiptShown(h);
  assert.deepEqual((await h.ctl('listdir', { path: trash })).names, ['one.mov', 'two.mov']);
  assert.deepEqual((await h.ctl('listdir', { path: dir })).names, ['three.mov']);
}));

test('after an action inside a subfolder the map stays in that subfolder', withHost(async h => {
  const dir = await h.fixture({ name: 'Stay', age: 30, entries: [{ p: 'inner/one.mov', size: 4 * MB }, { p: 'inner/two.mov', size: 3 * MB }, { p: 'outer.mov', size: 2 * MB }] });
  await h.ctl('fake_executor');
  await h.scan(dir); await h.open('inner'); await h.tick('one.mov'); await review(h);
  await h.view.click('#do-trash'); await receiptShown(h); await mapUpdated(h);
  assert.deepEqual(await h.crumbs(), ['Stay', 'inner'], 'the map jumped back to the top folder');
}));

test('after an action only what the person ticked is ticked: suggestions never asked for are not pre-selected', withHost(async h => {
  const dir = await h.fixture(CLEAN);
  await h.ctl('fake_executor');
  await h.scan(dir); await h.tick('keep.mov'); await review(h);
  await h.view.click('#do-trash'); await receiptShown(h); await mapUpdated(h);
  await h.view.waitForFunction(() => !document.getElementById('refresh').disabled);
  await sleep(800);
  await h.view.click('#receipt-close');
  assert.equal(await h.hidden('tray'), true, `the person never asked for suggestions, yet the tray now reads "${await h.text('tray-text')}" (one of them is inside proj/)`);
}));

test('code and other protected files cannot be ticked (Review would refuse them)', withHost(async h => {
  await h.scan(await h.fixture({ name: 'Code', age: 30, entries: [{ p: 'data.json', size: 2 * MB }, { p: 'film.mov', size: 2 * MB }, { p: 'store.sqlite', size: 2 * MB }] }));
  const by = Object.fromEntries((await h.rows()).map(r => [r.name, r]));
  assert.equal(by['film.mov'].checkbox, true);
  if (by['data.json'].checkbox) {
    await h.tick('data.json'); await h.tick('store.sqlite'); await review(h);
    const left = await list(h, 'review-skipped');
    assert.fail(`data.json and store.sqlite have tick boxes, and Review then says only: ${JSON.stringify(left)}`);
  }
}));

test('Remove Download (iCloud) flow with a stand-in for iCloud: offer, button, and an honest receipt when macOS does not evict', withHost(async h => {
  const dir = await h.fixture({ name: 'Synced', age: 30, entries: [{ p: 'holiday.mov', size: 12 * MB }, { p: 'small.mov', size: 2 * MB }] });
  await h.ctl('fake_executor'); await h.ctl('icloud');
  const s = await h.scan(dir, { free: true });
  await h.view.waitForSelector('.chip');
  const c = s.data.candidates[0];
  assert.deepEqual([c.path, c.actions, c.reasons.map(r => r.text)], ['holiday.mov', ['evict'], ['Also in iCloud']]);
  assert.equal((await h.rows())[0].chip, 'Also in iCloud');
  if (!(await h.rows())[0].checked) await h.tick('holiday.mov');
  await review(h);
  assert.equal(await h.text('do-evict'), 'Remove 1 download (keep in iCloud)');
  assert.deepEqual([await h.hidden('do-trash'), await h.hidden('do-delete')], [true, true]);
  await h.view.click('#do-evict'); await receiptShown(h);
  assert.equal(await h.text('receipt-title'), 'No changes made');
  assert.equal(await h.text('receipt-summary'), 'Nothing was changed.');
  assert.deepEqual(await list(h, 'receipt-failed'), ['holiday.mov: macOS did not confirm the download was removed']);
  assert.ok((await h.ctl('exists', { path: dir + '/holiday.mov' })).exists);
}));

test('a receipt for an action that changed nothing does not carry a success heading', withHost(async h => {
  const dir = await h.fixture(CLEAN);
  await h.ctl('fake_executor');
  await h.scan(dir); await h.tick('keep.mov'); await review(h);
  await h.ctl('write', { path: dir + '/keep.mov', size: 10 });
  await h.view.click('#do-trash'); await receiptShown(h);
  assert.equal(await h.text('receipt-summary'), 'Nothing was changed.');
  assert.notEqual(await h.text('receipt-title'), 'Moved to the Trash', 'the heading says "Moved to the Trash" while the summary says nothing was changed');
}));

test('a folder with hundreds of suggestions lists them all in the view', withHost(async h => {
  const entries = Array.from({ length: 300 }, (_, i) => ({ p: `download-number-${String(i).padStart(3, '0')}-with-a-fairly-long-descriptive-name.crdownload`, size: 1000 + i }));
  const s = await h.scan(await h.fixture({ name: 'Lots', age: 30, entries }), { free: true });
  assert.equal(s.data.candidatesTotal, 300); assert.equal(s.data.candidates.length, 20);
  await h.view.waitForFunction(() => /selected/.test(document.getElementById('tray-text').textContent));
  await sleep(1500);
  assert.ok((await h.calls('storage_cleanup_candidates')).length >= 1, 'the view asks for the full list (a page at a time)');
  assert.match(await h.text('tray-text'), /^300 items selected/, 'every safe suggestion is pre-ticked, not only the 20 the assistant was told about');
}));
