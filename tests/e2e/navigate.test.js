// Navigation, selecting and keyboard use of the rendered view.
'use strict';
const { test, after } = require('node:test');
const assert = require('node:assert/strict');
const { withHost, closeAll, BASIC, CLEAN, MB, sleep } = require('./harness');
after(closeAll);

test('clicking a folder tile opens it; breadcrumbs lead back', withHost(async h => {
  await h.scan(await h.fixture(BASIC));
  await h.tile('Films').click();
  await h.view.waitForFunction(() => document.querySelector('#breadcrumbs button[aria-current]')?.textContent === 'Films');
  assert.deepEqual(await h.crumbs(), ['Basics', 'Films']);
  assert.deepEqual((await h.rows()).map(r => [r.name, r.size]), [['launch.mov', '6.0 MB'], ['interview.mp4', '3.0 MB']]);
  assert.equal(await h.text('summary'), '9.0 MBin 2 items');
  await h.view.click('#breadcrumbs button:first-child');
  await h.view.waitForFunction(() => document.querySelectorAll('#breadcrumbs button').length === 1);
  assert.equal((await h.rows())[0].name, 'Films');
}));

test('clicking a folder name in the table opens it, several levels deep, and each crumb goes to its level', withHost(async h => {
  await h.scan(await h.fixture({ name: 'Tree', age: 30, entries: [{ p: 'one/two/three/deep.zip', size: 5000 }, { p: 'one/side.zip', size: 2000 }] }));
  await h.open('one'); await h.open('two'); await h.open('three');
  assert.deepEqual(await h.crumbs(), ['Tree', 'one', 'two', 'three']);
  assert.deepEqual((await h.rows()).map(r => r.name), ['deep.zip']);
  await h.view.click('#breadcrumbs button:nth-child(2)');
  await h.view.waitForFunction(() => document.querySelectorAll('#breadcrumbs button').length === 2);
  assert.deepEqual((await h.rows()).map(r => r.name), ['two', 'side.zip']);
}));

test('Refresh asks the plugin again for the folder shown and keeps the place', withHost(async h => {
  await h.scan(await h.fixture(BASIC));
  await h.open('Films');
  const before = (await h.calls('storage_render_view')).length;
  await h.view.click('#refresh'); await sleep(300); await h.idle();
  const calls = await h.calls('storage_render_view');
  assert.equal(calls.length, before + 1);
  assert.deepEqual(await h.crumbs(), ['Basics', 'Films']);
}));

test('ticks survive navigation and the tray counts items and size', withHost(async h => {
  await h.scan(await h.fixture(BASIC));
  await h.tick('big.zip');
  assert.equal(await h.text('tray-text'), '1 item selected · about 4.0 MB'); assert.equal(await h.hidden('tray'), false);
  await h.open('Films'); await h.tick('launch.mov');
  assert.equal(await h.text('tray-text'), '2 items selected · about 10.0 MB');
  await h.view.click('#breadcrumbs button:first-child'); await h.view.waitForFunction(() => document.querySelectorAll('#breadcrumbs button').length === 1);
  assert.equal((await h.rows()).find(r => r.name === 'big.zip').checked, true);
  assert.equal(await h.text('tray-text'), '2 items selected · about 10.0 MB');
  await h.row('big.zip').locator('input').uncheck();
  assert.equal(await h.text('tray-text'), '1 item selected · about 6.0 MB');
  await h.open('Films'); assert.equal((await h.rows())[0].checked, true);
  await h.row('launch.mov').locator('input').uncheck();
  assert.equal(await h.hidden('tray'), true);
}));

test('a file tile and its row stay in step: ticking either ticks both', withHost(async h => {
  await h.scan(await h.fixture(BASIC));
  await h.tile('big.zip').click();
  assert.equal((await h.rows()).find(r => r.name === 'big.zip').checked, true);
  assert.equal((await h.tiles()).find(t => t.title === 'big.zip').ticked, true);
  await h.tileTick('big.zip').click();
  assert.equal((await h.rows()).find(r => r.name === 'big.zip').checked, false);
  await h.tileTick('Films').click({ force: true });
  assert.equal((await h.rows()).find(r => r.name === 'Films').checked, true, 'a folder is ticked from its tile tick without opening it');
  assert.deepEqual(await h.crumbs(), ['Basics']);
  assert.equal(await h.text('tray-text'), '1 item selected · about 9.0 MB');
}));

test('things that cannot be cleared have no tick box: links, grouped small files, credential items', withHost(async h => {
  await h.scan(await h.fixture(BASIC));
  await h.view.click('#next');
  const by = Object.fromEntries((await h.rows()).map(r => [r.name, r]));
  assert.equal(by.shortcut.checkbox, false, 'link');
  assert.equal(by['big.zip'].checkbox, true);
  assert.deepEqual([by['.ssh'].checkbox, by['.env'].checkbox], [false, false], 'skipped credential items (.ssh, .env) offer a tick box that Review then refuses');
}));

test('keyboard: Enter on a folder tile opens it and focus stays in the map', withHost(async h => {
  await h.scan(await h.fixture(BASIC));
  await h.tile('Films').focus(); await h.page.keyboard.press('Enter');
  await h.view.waitForFunction(() => document.querySelector('#breadcrumbs button[aria-current]')?.textContent === 'Films');
  await h.idle();
  assert.notEqual((await h.active()).tag, 'BODY', 'focus was dropped to the page body; the next Tab starts from the top');
}));

test('keyboard: Space on a file name ticks it and focus stays on that item', withHost(async h => {
  await h.scan(await h.fixture(BASIC));
  await h.row('big.zip').locator('td.name button').focus(); await h.page.keyboard.press('Space');
  assert.equal(await h.text('tray-text'), '1 item selected · about 4.0 MB');
  assert.notEqual((await h.active()).tag, 'BODY', 'focus was dropped to the page body');
}));

test('keyboard: Enter on a file tile ticks it and focus stays on that tile', withHost(async h => {
  await h.scan(await h.fixture(BASIC));
  await h.tile('big.zip').focus(); await h.page.keyboard.press('Enter');
  assert.equal((await h.tiles()).find(t => t.title === 'big.zip').ticked, true);
  assert.notEqual((await h.active()).tag, 'BODY', 'focus was dropped to the page body');
}));

test('keyboard: Space on a row tick box toggles it and keeps focus there', withHost(async h => {
  await h.scan(await h.fixture(BASIC));
  await h.row('big.zip').locator('input').focus(); await h.page.keyboard.press('Space');
  assert.equal(await h.text('tray-text'), '1 item selected · about 4.0 MB');
  assert.deepEqual([(await h.active()).tag, (await h.active()).label], ['INPUT', 'Select big.zip']);
  await h.page.keyboard.press('Space');
  assert.equal(await h.hidden('tray'), true); assert.equal((await h.active()).label, 'Select big.zip');
}));

test('keyboard: the tile tick is reachable by Tab, toggles with Space and keeps focus', withHost(async h => {
  await h.scan(await h.fixture(BASIC));
  await h.tile('Films').focus(); await h.page.keyboard.press('Tab');
  const a = await h.active(); assert.equal(a.label, 'Select Films'); assert.equal(a.pick, 'tile');
  assert.equal(await h.view.evaluate(() => getComputedStyle(document.activeElement).opacity), '1', 'the focused tick is visible');
  await h.page.keyboard.press('Space');
  assert.equal(await h.text('tray-text'), '1 item selected · about 9.0 MB');
  assert.equal((await h.active()).label, 'Select Films');
}));

test('keyboard: Tab order runs controls, map, table, then the tray Review button', withHost(async h => {
  await h.scan(await h.fixture({ name: 'Two', age: 30, entries: [{ p: 'a.zip', size: 3000 }, { p: 'b.zip', size: 2000 }] }));
  await h.tick('a.zip');
  await h.view.locator('#basis').focus();
  const seen = [];
  for (let i = 0; i < 14; i++) { await h.page.keyboard.press('Tab'); const a = await h.active(); seen.push(a.id || a.label || a.text.trim()); if (a.id === 'tray-review') break; }
  assert.deepEqual(seen, ['aggregate', 'refresh', 'suggest', 'a.zip, Archive, 3.0 KB', 'Select a.zip', 'b.zip, Archive, 2.0 KB', 'Select b.zip', 'Select a.zip', 'a.zip', 'Select b.zip', 'b.zip', 'tray-review']);
}));

test('controls have accessible names', withHost(async h => {
  await h.scan(await h.fixture(BASIC));
  const unnamed = await h.view.evaluate(() => [...document.querySelectorAll('button,select,input')].filter(e => !e.hidden && e.offsetParent !== null).filter(e => !(e.getAttribute('aria-label') || e.textContent.trim() || e.closest('label')?.textContent.trim())).map(e => e.outerHTML.slice(0, 80)));
  assert.deepEqual(unnamed, []);
  assert.equal(await h.view.evaluate(() => document.getElementById('map').getAttribute('aria-label')), 'Treemap of this folder');
  assert.equal(await h.view.evaluate(() => document.querySelector('#breadcrumbs').getAttribute('aria-label')), 'Folder path');
}));

test('a second click while a folder is loading does not open the wrong folder or wedge the view', withHost(async h => {
  await h.scan(await h.fixture(BASIC));
  await h.setMode({ delay: 400, delayTool: 'storage_render_view' });
  await h.tile('Films').click(); await h.tile('Sound').click({ force: true });
  await h.view.waitForFunction(() => document.querySelectorAll('#breadcrumbs button').length === 2);
  await h.idle();
  assert.deepEqual(await h.crumbs(), ['Basics', 'Films']);
  assert.equal((await h.calls('storage_render_view')).length, 1);
  await h.setMode({ delay: 0 });
  await h.view.click('#breadcrumbs button:first-child'); await h.view.waitForFunction(() => document.querySelectorAll('#breadcrumbs button').length === 1);
}));

test('a slow plugin answer (9 s) for a folder does not wipe the map', withHost(async h => {
  await h.scan(await h.fixture(BASIC));
  await h.setMode({ delay: 9000, delayTool: 'storage_render_view' });
  await h.tile('Films').click();
  await sleep(8600);
  assert.equal(await h.hidden('view'), false, `after 8 s the view was replaced by: "${await h.text('status')}"`);
}));

test('suggestions inside a folder show as a count on the folder, and as chips once inside', withHost(async h => {
  await h.scan(await h.fixture(CLEAN), { free: true });
  await h.view.waitForSelector('.chip');
  const by = Object.fromEntries((await h.rows()).map(r => [r.name, r]));
  assert.equal(by.proj.chip, '1 suggested inside'); assert.equal(by.b.chip, '1 suggested inside');
  await h.open('proj');
  const inside = Object.fromEntries((await h.rows()).map(r => [r.name, r]));
  assert.equal(inside.node_modules.chip, 'Rebuilds with npm install'); assert.equal(inside.node_modules.checked, true);
  assert.equal((await h.tiles()).find(t => t.title === 'node_modules').chip, 'Rebuilds with npm install');
  await h.view.click('#breadcrumbs button:first-child'); await h.view.waitForFunction(() => document.querySelectorAll('#breadcrumbs button').length === 1);
  assert.equal((await h.rows()).find(r => r.name === 'proj').chip, '1 suggested inside', 'chips are still there after coming back');
}));
