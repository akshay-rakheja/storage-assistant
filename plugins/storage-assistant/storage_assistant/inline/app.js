/* No fetch, cookies, local paths, external assets, analytics or persisted inventory. */
(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  const pending = new Map();
  let nextId = 1, ready = false, disposed = false, closing = false, caps = {}, current = null;
  let selected = null, shared = false, sharing = false, contextMayRemain = false, contextQueue = Promise.resolve(), operation = 0, busy = false, watermark = 0, revision = 0;
  const CLOSED = 'View closed. Ask the assistant to query and render a new view.';
  const UNREVOKED = 'View closed; context revocation was not acknowledged. The host may retain the previous selection. Query and share a new view before a follow-up.';
  // Codes the plugin returns before touching anything; any other failure leaves the outcome unknown.
  const REFUSALS = new Set(['INVALID_INPUT','STALE_SCAN','SCOPE_EXPIRED','NOT_AUTHORIZED','CONFLICT','UNSUPPORTED']);
  const ID = /^[a-f0-9]{32}$/, BYTES = /^(0|[1-9][0-9]{0,29})$/;
  const kinds = new Set(['directory','file','symlink','other']);
  const categories = new Set(['video','audio','image','archive','document','source','other']);
  const validBytes = x => x === null || typeof x === 'string' && BYTES.test(x);
  const bytes = x => {
    if (x === null) return 'Unknown';
    const n = Number(x), units = ['B','KB','MB','GB','TB'];
    let i = 0, v = n; while (v >= 1000 && i < units.length-1) { v /= 1000; i++; }
    return (i ? v.toFixed(v >= 100 ? 0 : 1) : String(n)) + ' ' + units[i];
  };
  const label = n => typeof n.displayLabel === 'string' && n.displayLabel.length > 0 && n.displayLabel.length <= 1024;
  function notify(method, params) { if (!disposed) window.parent.postMessage({jsonrpc:'2.0',method,params}, '*'); }
  function request(method, params, wait=8000) {
    if (disposed || pending.size >= 8 || !ready && method !== 'ui/initialize') return Promise.reject(new Error('Bridge unavailable'));
    const id = nextId++;
    return new Promise((resolve,reject) => {
      const timer = setTimeout(() => { pending.delete(id); reject(new Error('Host response timed out')); },wait);
      pending.set(id,{resolve,reject,timer});
      window.parent.postMessage({jsonrpc:'2.0',id,method,params}, '*');
    });
  }
  function status(text) { $('status').textContent = text; }
  function controls() {
    for (const id of ['basis','aggregate','refresh','first','next','suggest']) $(id).disabled = closing || busy || acting || !ready || !current || !caps.serverTools;
  }
  function fail(code) {
    const messages = {
      NOT_AUTHORIZED:'No folder is authorized. Ask the assistant to have you choose a folder.',
      SCOPE_EXPIRED:'The folder grant expired. Choose the folder again and render a new scan.',
      STALE_SCAN:'Snapshot was replaced. Ask the assistant to query and render the current scan.',
      UNAVAILABLE:'Companion or connection is offline. Start it locally, refresh the connection, then try again.',
      CONFLICT:'The scan is still running. Poll status or deliberately cancel before rendering.',
      INVALID_INPUT:'The page cursor or request is invalid. Refresh the view.',
      NOT_FOUND:'The item or resource is unavailable. Refresh the connection and current view.'
    };
    current = null; selected = null; operation++; busy = false; leaveCleanup();
    // A receipt stays until the person clicks Done or reviews again, even when the map cannot be shown.
    const keep = !$('receipt').hidden; $('view').hidden = !keep; $('view').classList.toggle('receipt-only', keep); $('rows').replaceChildren(); $('map').replaceChildren();
    if (shared || sharing) clearContext();
    status(messages[code] || 'The host returned an unsupported result. Refresh connection metadata and try again.'); controls();
  }
  function node(n) {
    if (!n || !ID.test(n.nodeId) || !kinds.has(n.kind) || !categories.has(n.category) || !validBytes(n.logicalBytes) || !validBytes(n.allocatedEstimateBytes)) return false;
    return label(n) && (n.parentId === null || ID.test(n.parentId)) && ['complete','partial'].includes(n.coverage);
  }
  function valid(v) {
    if (!v || !ID.test(v.scanId) || !Number.isSafeInteger(v.scanVersion) || v.scanVersion<1 || !Number.isSafeInteger(v.viewRevision) || v.viewRevision<1) return false;
    if (!['logical','allocated_estimate'].includes(v.basis) || !['children','by_category'].includes(v.aggregate) || !node(v.within)) return false;
    if (!Array.isArray(v.rows) || v.rows.length>100 || !Array.isArray(v.breadcrumbs) || v.breadcrumbs.length>9 || !v.breadcrumbs.every(node)) return false;
    if (!Array.isArray(v.selectionIds) || v.selectionIds.length>100 || !v.selectionIds.every(i=>typeof i==='string'&&ID.test(i))) return false;
    if (!(v.nextCursor === null || typeof v.nextCursor === 'string' && ID.test(v.nextCursor)) || !Number.isSafeInteger(v.matchingRows) || v.matchingRows<0 || v.matchingRows>40000 || !Number.isSafeInteger(v.unknownAllocationCount) || v.unknownAllocationCount<0) return false;
    if (v.evidence?.scanId!==v.scanId || v.evidence?.scanVersion!==v.scanVersion || v.evidence?.basis!==v.basis || !['partial','complete','cancelled'].includes(v.evidence.coverage)) return false;
    return v.rows.every(r=>validBytes(r.bytes) && (v.aggregate==='children' ? node(r) : categories.has(r.displayLabel)&&Number.isSafeInteger(r.count)&&r.count>=0&&Number.isSafeInteger(r.unknownAllocationCount)&&r.unknownAllocationCount>=0));
  }
  function accept(result) {
    if (disposed || closing) return false;
    const envelope = result?.structuredContent;
    if (envelope?.ok === false || result?.isError) { fail(envelope?.error?.code); return false; }
    if (envelope?.data?.state === 'running' && !envelope.data.view) {
      const n = Number.isSafeInteger(envelope.data.observedEntries) ? envelope.data.observedEntries : 0;
      if (envelope.data.freeUpSpace === true) wantSuggest = true;
      if (!current) { status(`Scanning… ${n.toLocaleString()} entries so far. The map appears here when the scan finishes.`); follow(envelope.data.scanId); }
      return false;
    }
    const data = envelope?.data, v = data?.view, suggested = Array.isArray(data?.candidates);
    // Suggestions without a view belong to the snapshot already shown.
    if (suggested && !v && current) { candidates(data, current); render(); return true; }
    if (!valid(v)) { fail('BAD_RESULT'); return false; }
    if (v.scanVersion<watermark || v.scanVersion===watermark && (current && v.scanId!==current.scanId || v.viewRevision<=revision)) {
      if (suggested && current && v.scanId===current.scanId && v.scanVersion===current.scanVersion) { candidates(data, current); render(); return true; }
      return false;
    }
    if (shared || sharing) clearContext();
    if (!current || current.scanId!==v.scanId || current.scanVersion!==v.scanVersion) { ticked = new Set(); known = new Map(); }
    paged = more; if (!more) shownRows = FEW;
    more = false; watermark=v.scanVersion; revision=v.viewRevision; current=v;
    if (suggested) candidates(data, v);
    else if (data.freeUpSpace === true) wantSuggest = true;
    else if (cleanup && (cleanup.scanId!==v.scanId || cleanup.scanVersion!==v.scanVersion)) leaveCleanup();
    selected=v.rows.find(r=>r.nodeId && v.selectionIds.includes(r.nodeId)) || v.within;
    render();
    if (wantSuggest && !rescanning) { wantSuggest = false; setTimeout(suggest); }
    return true;
  }
  // Keep one progress view current: ask for status until the scan finishes, then render.
  let following = null;
  function follow(scanId) {
    if (following === scanId || !ID.test(scanId || '') || !caps.serverTools) return;
    following = scanId;
    const live = () => !disposed && following === scanId && (!current || rescanning === scanId);
    const tick = () => {
      if (!live()) return;
      request('tools/call', {name:'storage_scan_status', arguments:{scanId}}, 60000)
        .then(result => { if (!live()) return;
          if (result?.structuredContent?.data?.state === 'running') { accept(result); setTimeout(tick, 1500); }
          else { following = null; const ok = accept(result); if (ok) { operation++; busy = false; }
            if (rescanning === scanId) { rescanning = null; afterRescan(ok); } controls(); } })
        .catch(() => { if (!disposed && following === scanId) setTimeout(tick, 5000); });
    };
    setTimeout(tick, 1000);
  }
  function element(tag,text) { const e=document.createElement(tag); if(text!==undefined)e.textContent=text; return e; }
  function button(text,action,pressed) { const b=element('button',text); b.type='button'; b.onclick=action; if(pressed!==undefined)b.setAttribute('aria-pressed',String(pressed)); return b; }
  function choose(row) {
    if(busy||closing||acting)return;
    if(row.kind==='directory') load({withinNodeId:row.nodeId,aggregate:'children'},true);
    else if(pickable(row)) toggle(row.nodeId, !ticked.has(row.nodeId));
  }
  const CATS = {video:'Video',audio:'Audio',image:'Images',document:'Documents',archive:'Archives',source:'Code',other:'Other'};
  const ONE = {video:'Video',audio:'Audio',image:'Image',document:'Document',archive:'Archive',source:'Code',other:'Other'};
  const kindOf = r => r.nodeId ? (r.kind==='directory' ? 'folder' : r.category) : r.displayLabel;
  const typeLabel = r => r.nodeId ? (r.kind==='directory' ? 'Folder' : r.kind==='file' ? (ONE[r.category]||'Other') : r.kind[0].toUpperCase()+r.kind.slice(1)) : `${r.count.toLocaleString()} ${r.count===1?'file':'files'}`;
  // Squarified treemap (Bruls, Huizing, van Wijk): rows of tiles kept close to square.
  function squarify(rows,x,y,w,h){
    const total=rows.reduce((n,r)=>n+Number(r.bytes),0), out=[];
    if(!total||w<=0||h<=0)return out;
    const items=rows.map(r=>({row:r,area:Number(r.bytes)/total*w*h}));
    const worst=(row,side)=>{const s=row.reduce((n,i)=>n+i.area,0),mx=Math.max(...row.map(i=>i.area)),mn=Math.min(...row.map(i=>i.area));return Math.max(side*side*mx/(s*s),(s*s)/(side*side*mn));};
    let i=0;
    while(i<items.length){
      const side=Math.min(w,h); let row=[items[i]]; i++;
      while(i<items.length&&worst(row.concat(items[i]),side)<=worst(row,side)){row.push(items[i]);i++;}
      const s=row.reduce((n,t)=>n+t.area,0);
      if(w>=h){const cw=s/h;let cy=y;for(const t of row){const th=t.area/cw;out.push({row:t.row,x,y:cy,w:cw,h:th});cy+=th;}x+=cw;w-=cw;}
      else{const rh=s/w;let cx=x;for(const t of row){const tw=t.area/rh;out.push({row:t.row,x:cx,y,w:tw,h:rh});cx+=tw;}y+=rh;h-=rh;}
    }
    return out;
  }
  function swatch(kind){const i=element('i');i.className='cat-'+kind;return i;}
  function render() {
    const v=current;if(!v)return;
    $('view').hidden=false; $('view').classList.remove('receipt-only'); $('basis').value=v.basis; $('aggregate').value=v.aggregate; status('');
    const metric=v.basis==='logical'?'logicalBytes':'allocatedEstimateBytes';
    const crumbs=v.breadcrumbs.map(n=>button(n.displayLabel,()=>load({withinNodeId:n.nodeId,aggregate:'children'},true)));
    if(crumbs.length)crumbs[crumbs.length-1].setAttribute('aria-current','location');
    $('breadcrumbs').replaceChildren(...crumbs);
    const cut=['entry_limit','time_limit','metadata_delayed'].some(k=>(v.gapCounts||{})[k]>0);
    const total=element('strong',(cut?'at least ':'')+bytes(v.within[metric]));
    const noun=v.aggregate==='children'?(v.matchingRows===1?'item':'items'):(v.matchingRows===1?'file type':'file types');
    $('summary').replaceChildren(total,document.createTextNode(`${v.basis==='logical'?'in':'on disk across'} ${v.matchingRows.toLocaleString()} ${noun}`));
    // Gap keys/counts are validated before display; host strings never become HTML.
    const words={cloud_placeholder:'iCloud-only',symlink:'link',excluded:'protected',denied:'access denied',changed:'changed while scanning',other_volume:'other disk',time_limit:'time limit reached',entry_limit:'item limit reached',depth_limit:'too deeply nested',allocation_unknown:'unknown disk usage',provider_unknown:'unknown cloud status',unsupported:'special file',metadata_delayed:'scan cut short',unavailable:'scanner error'};
    const gaps=Object.entries(v.gapCounts||{}).filter(([k,n])=>/^[a-z_]{1,40}$/.test(k)&&Number.isSafeInteger(n)&&n>0).slice(0,30);
    $('coverage').textContent=gaps.length?`Not counted: ${gaps.map(([k,n])=>`${n.toLocaleString()} ${words[k]||k.replace(/_/g,' ')}`).join(', ')}.`:'Everything in this folder was measured.';
    const lg=v.within.logicalBytes===null?0:Number(v.within.logicalBytes), al=v.within.allocatedEstimateBytes===null?0:Number(v.within.allocatedEstimateBytes);
    $('notice').textContent=[
      cut?'The scan stopped before the end of this folder, so these totals are too low. Try a smaller folder for complete numbers.':'',
      lg>al*1.25&&lg-al>100e6?`${bytes(String(Math.round(lg-al)))} of this is not stored on this Mac (iCloud-only or sparse files). ${v.basis==='logical'?'Switch to Space on disk to see what this Mac actually holds.':''}`:''
    ].filter(Boolean).join(' ');
    if(v.unknownAllocationCount)$('coverage').textContent+=` ${v.unknownAllocationCount.toLocaleString()} ${v.unknownAllocationCount===1?'file has':'files have'} unknown disk usage.`;
    $('caption').textContent=v.aggregate==='children'?'Items in this folder, largest first':'File types in this folder, largest first';
    const cleanOn=!!cleanup&&cleanup.scanId===v.scanId&&cleanup.scanVersion===v.scanVersion;
    for(const r of v.rows)if(pickable(r)&&!known.has(r.nodeId))known.set(r.nodeId,rowItem(r));
    const largest=v.rows.reduce((m,r)=>r.bytes===null?m:Math.max(m,Number(r.bytes)),0)||1;
    $('rows').replaceChildren(...v.rows.slice(0,shownRows).map(r=>{
      const tr=element('tr'),name=element('td'),type=element('td',typeLabel(r)),size=element('td');
      name.className='name';type.className='type';size.className='size num';
      const label=element('span',r.nodeId?r.displayLabel:(CATS[r.displayLabel]||r.displayLabel));
      const c=cleanOn&&r.nodeId?cleanup.byId.get(r.nodeId):null,inside=cleanOn&&r.kind==='directory'&&!c?within(r):0;
      if(r.nodeId){const b=button('',()=>choose(r),v.selectionIds.includes(r.nodeId));b.append(swatch(kindOf(r)),label);if(c)b.append(chip(c));
        else if(inside){const e=element('em',`${inside} suggested inside`);e.className='chip tier-glance';b.append(e);}
        b.title=r.path||r.displayLabel;
        const pick=element('div');pick.className='pick';
        if(caps.serverTools&&pickable(r)){pick.append(checkbox(item(r.nodeId),'','row'));if(c)tr.className='tier-'+c.tier;}else{const gap=element('i');gap.className='gap';pick.append(gap);}
        pick.append(b);name.append(pick);}
      else name.append(swatch(kindOf(r)),label);
      const meter=element('div'),bar=element('span'),fill=element('i');meter.className='meter';
      fill.style.width=r.bytes===null?'0':`${Math.max(1,Number(r.bytes)/largest*100)}%`;bar.append(fill);
      meter.append(bar,element('b',bytes(r.bytes)+(r.unknownAllocationCount?' +?':'')));size.append(meter);
      tr.append(name,type,size);return tr;
    }));
    const positive=v.rows.filter(r=>r.bytes!==null&&BigInt(r.bytes)>0n);
    const box=$('map'),W=box.clientWidth||800,H=box.clientHeight||340;
    const tiles=squarify(positive,0,0,W,H);
    box.replaceChildren(...tiles.map(t=>{
      const kind=kindOf(t.row),pressed=t.row.nodeId?v.selectionIds.includes(t.row.nodeId):undefined;
      const cell=element('div');cell.className='cell';
      cell.style.cssText=`left:${t.x/W*100}%;top:${t.y/H*100}%;width:${t.w/W*100}%;height:${t.h/H*100}%`;
      const b=button('',()=>t.row.nodeId&&choose(t.row),pressed);
      b.className=`tile cat-${kind}${t.w*t.h>W*H/6?' big':''}${t.w<60||t.h<40?' tiny':''}${ticked.has(t.row.nodeId)?' on':''}`;
      const title=t.row.nodeId?t.row.displayLabel:(CATS[t.row.displayLabel]||t.row.displayLabel);
      b.append(element('strong',title),element('span',bytes(t.row.bytes)));
      b.setAttribute('aria-label',`${title}, ${typeLabel(t.row)}, ${bytes(t.row.bytes)}${t.row.kind==='directory'?', open folder':''}`);
      b.title=`${t.row.path||title} (${bytes(t.row.bytes)})`;
      const c=cleanOn&&t.row.nodeId?cleanup.byId.get(t.row.nodeId):null;
      if(c){b.classList.add('cand','tier-'+c.tier);b.append(chip(c));b.setAttribute('aria-label',b.getAttribute('aria-label')+`, suggested: ${TIERS[c.tier]}`);}
      if(!t.row.nodeId)b.disabled=true;cell.append(b);
      if(caps.serverTools&&pickable(t.row)&&t.w>=34&&t.h>=30)cell.append(tickToggle(item(t.row.nodeId),!!c));
      return cell;
    }));
    const kinds=[...new Set(positive.map(kindOf))];
    $('legend').replaceChildren(...kinds.map(k=>{const li=element('li');li.append(swatch(k),document.createTextNode(k==='folder'?'Folder':(CATS[k]||k)));return li;}));
    const hiddenRows=v.rows.length>shownRows;
    $('next').hidden=!hiddenRows&&!v.nextCursor;
    $('next').textContent=hiddenRows?`Show ${Math.min(MORE,v.rows.length-shownRows)} more`:'Show more';
    $('first').hidden=!paged&&shownRows<=FEW;
    renderCleanup();controls();resize();
  }
  // ---- Cleanup mode -------------------------------------------------------
  // Candidates come from storage_suggest_cleanup. Only the person acts: ticks,
  // Review… (preview per action, one-time tokens), then one execute per click.
  // The executor revalidates everything; the view only shows and forwards ids.
  const TIERS = {safe:'Safe to clear',glance:'Worth a glance',call:'Your call'};
  const ACTIONS = ['trash','evict','delete'];
  let cleanup = null, reviewing = [], ticked = new Set(), seen = new Set(), previews = null, acting = false, resuggest = false, rescanning = null, deleting = null;
  // Anything the person ticks themselves can go to the Trash; suggestions may also offer Delete permanently or Remove Download.
  let known = new Map(), wantSuggest = false, shownRows = 8, more = false, paged = false;
  const FEW = 8, MORE = 20;
  const pickable = r => !!r?.nodeId && (r.kind==='file' || r.kind==='directory') && !r.groupedFiles && label(r) && short(r.path,4096);
  const rowItem = r => ({nodeId:r.nodeId,displayLabel:r.displayLabel,path:r.path,tier:null,preTicked:false,reasons:[],actions:['trash'],
    freesBytes:r.allocatedEstimateBytes ?? r.logicalBytes ?? '0'});
  const item = id => cleanup?.byId.get(id) || known.get(id);
  const within = r => cleanup.list.filter(c => c.path.startsWith(r.path+'/')).length;
  const short = (s,n) => typeof s === 'string' && s.length > 0 && s.length <= n;
  function candidate(c) {
    if (!c || !ID.test(c.nodeId) || !label(c) || !short(c.path,4096) || !TIERS[c.tier] || !validBytes(c.freesBytes) || c.freesBytes===null) return null;
    const reasons = Array.isArray(c.reasons) ? c.reasons.filter(r=>short(r?.text,300)).slice(0,6).map(r=>r.text) : [];
    const actions = Array.isArray(c.actions) ? ACTIONS.filter(a=>c.actions.includes(a)) : [];
    return {nodeId:c.nodeId,displayLabel:c.displayLabel,path:c.path,tier:c.tier,preTicked:c.preTicked===true,reasons,actions,freesBytes:c.freesBytes};
  }
  function candidates(data, v) {
    // Candidates belong to the scan of the view they came with.
    const list = data.candidates.slice(0,500).map(candidate).filter(Boolean);
    const fresh = !cleanup || cleanup.scanId!==v.scanId || cleanup.scanVersion!==v.scanVersion;
    if (fresh) { ticked = new Set(); seen = new Set(); }
    cleanup = {scanId:v.scanId,scanVersion:v.scanVersion,list,byId:new Map(list.map(c=>[c.nodeId,c]))};
    for (const c of list) { if (!seen.has(c.nodeId) && c.preTicked) ticked.add(c.nodeId); seen.add(c.nodeId); }
    for (const id of [...ticked]) if (!cleanup.byId.has(id)) ticked.delete(id);
    previews = null; deleting = null; $('review').hidden = true; $('confirm-delete').hidden = true;
  }
  function leaveCleanup() {
    cleanup = null; ticked = new Set(); seen = new Set(); previews = null; deleting = null; $('cleanup-status').textContent = '';
    for (const id of ['review','confirm-delete','tray']) $(id).hidden = true;
  }
  const sum = list => list.reduce((n,x)=>n+BigInt(x.freesBytes||'0'),0n).toString();
  const items = n => `${n.toLocaleString()} ${n===1?'item':'items'}`;
  function toggle(id, on) {
    if (!item(id) || acting) return;
    on ? ticked.add(id) : ticked.delete(id);
    // Re-rendering replaces the controls: keep keyboard focus on the same kind of control for the same item.
    const a = document.activeElement, key = a?.dataset?.pick ? {pick:a.dataset.pick, node:a.dataset.node} : null;
    if (!$('review').hidden) { review(key, true); return; }
    render(); refocus(key);
  }
  function refocus(key) {
    if (!key) return;
    const target = [...document.querySelectorAll('[data-pick]')].find(e => e.dataset.pick===key.pick && e.dataset.node===key.node);
    if (target && !target.disabled) target.focus();
  }
  function checkbox(c, where, pick) {
    const x = element('input'); x.type = 'checkbox'; x.checked = ticked.has(c.nodeId); x.disabled = acting;
    x.dataset.pick = pick; x.dataset.node = c.nodeId;
    x.setAttribute('aria-label', `Select ${c.displayLabel}${c.tier ? ` (${TIERS[c.tier]})` : ''}${where ? ' '+where : ''}`);
    x.onchange = () => toggle(c.nodeId, x.checked); return x;
  }
  function chip(c) { const e = element('em', c.reasons[0] || TIERS[c.tier]); e.className = 'chip tier-'+c.tier; e.title = `${TIERS[c.tier]}: ${c.reasons.join('; ')}`; return e; }
  function who(name, sub) { const d = element('span'); d.className = 'who'; d.append(element('b', name)); if (sub) d.append(element('small', sub)); return d; }
  function tickToggle(c, suggested) {
    // A separate control on top of the tile: clicking the tile itself still opens folders.
    const on = ticked.has(c.nodeId), b = button(on ? '✓' : '', () => toggle(c.nodeId, !ticked.has(c.nodeId)));
    b.className = 'tick' + (on || suggested ? ' shown' : ''); b.dataset.pick = 'tile'; b.dataset.node = c.nodeId; b.setAttribute('role','checkbox'); b.setAttribute('aria-checked', String(on));
    b.setAttribute('aria-label', `Select ${c.displayLabel}${c.tier ? ` (${TIERS[c.tier]})` : ''}`); b.title = c.tier ? `${TIERS[c.tier]}: ${c.reasons.join('; ')}` : 'Select'; b.disabled = acting;
    return b;
  }
  function renderCleanup() {
    const on = !!cleanup && cleanup.scanId===current?.scanId && cleanup.scanVersion===current?.scanVersion;
    // Shown until this snapshot has suggestions, so they never depend on the assistant asking for them.
    $('suggest').hidden = on || !current || !caps.serverTools;
    if (on && !cleanup.list.length && !$('cleanup-status').textContent) $('cleanup-status').textContent = 'Nothing stands out to suggest here. Tick anything you want to clear.';
    const chosen = [...ticked].map(item).filter(Boolean);
    // The bar shows only once something is ticked, and steps aside while a review or receipt is open.
    $('tray').hidden = !chosen.length || !$('review').hidden || !$('confirm-delete').hidden || !$('receipt').hidden;
    $('tray-text').textContent = `${items(chosen.length)} selected · about ${bytes(sum(chosen))}`;
    $('tray-review').disabled = acting || !chosen.length || !caps.serverTools;
  }
  async function call(name, args, wait=30000) {
    const result = await request('tools/call', {name, arguments:args}, wait);
    const envelope = result?.structuredContent;
    if (envelope?.ok !== true || result?.isError) {
      const m = envelope?.error?.message;
      // A structured refusal (the plugin answered before acting) only for the known pre-action codes.
      throw Object.assign(new Error(short(m,300) ? m : 'The plugin could not do this. Review again.'), {refused:REFUSALS.has(envelope?.error?.code)});
    }
    return envelope.data;
  }
  function busyCleanup(on) { acting = on; for (const id of ['do-trash','do-evict','do-delete','review-back','confirm-yes','confirm-no','open-trash','receipt-close']) $(id).disabled = on; controls(); }
  async function review(key=null, keep=false) {
    if (!current || acting || closing) return;
    // An open review keeps listing items unticked in it, so they can be ticked again (and keep focus).
    if (!keep) reviewing = [...ticked].filter(item);
    const shown = reviewing.map(item).filter(Boolean);
    const v = current, chosen = shown.filter(c=>ticked.has(c.nodeId));
    $('receipt').hidden = true; $('confirm-delete').hidden = true; $('review').hidden = false; $('check-refresh').hidden = true; previews = null;
    $('review-status').textContent = chosen.length ? 'Checking each item again…' : 'Nothing is ticked.';
    for (const a of ACTIONS) $('do-'+a).hidden = true;
    renderReview(shown, {}); renderCleanup(); resize(); refocus(key);
    if (!chosen.length) return;
    busyCleanup(true);
    const results = {};
    try {
      // Preview each action only for the ticked items that offer it.
      await Promise.all(ACTIONS.map(async a => {
        const ids = chosen.filter(c=>c.actions.includes(a)).map(c=>c.nodeId);
        if (ids.length) results[a] = await call('storage_cleanup_preview', {scanId:v.scanId,scanVersion:v.scanVersion,nodeIds:ids,action:a});
      }));
      if (current !== v || $('review').hidden) return;
      previews = results; $('review-status').textContent = '';
    } catch (e) { $('review-status').textContent = e.message; }
    finally { busyCleanup(false); }
    renderReview(shown, previews || {}); renderCleanup(); resize(); refocus(key);
  }
  function plan(a) { const p = previews?.[a]; return p && ID.test(p.token||'') && Number.isSafeInteger(p.count) && p.count>0 && validBytes(p.freesBytes) ? p : null; }
  function renderReview(chosen, results) {
    const detail = new Map();
    for (const a of ACTIONS) for (const i of (Array.isArray(results[a]?.items) ? results[a].items : [])) if (ID.test(i?.nodeId) && !detail.has(i.nodeId)) detail.set(i.nodeId, i);
    $('review-items').replaceChildren(...chosen.map(c => {
      const i = detail.get(c.nodeId), li = element('li'); li.className = (c.tier ? 'tier-'+c.tier : '')+(ticked.has(c.nodeId)?'':' off');
      const why = (Array.isArray(i?.reasons) ? i.reasons.map(r=>r?.text ?? r) : c.reasons).filter(t=>short(t,300)).join('; ');
      li.append(checkbox(c,'in this review','review'), who(c.displayLabel, `${c.path}${why ? ' · '+why : ''}`), element('span', bytes(validBytes(i?.freesBytes) && i.freesBytes ? i.freesBytes : c.freesBytes)));
      li.lastChild.className = 'amt';
      const warnings = Array.isArray(i?.warnings) ? i.warnings.filter(w=>short(typeof w==='string'?w:w?.text,300)).map(w=>typeof w==='string'?w:w.text) : [];
      if (warnings.length) { const w = element('small', warnings.join('; ')); w.className = 'warn'; li.querySelector('.who').append(w); }
      return li;
    }));
    const left = [], once = new Set();
    for (const a of ACTIONS) for (const s of (Array.isArray(results[a]?.skipped) ? results[a].skipped : []))
      if (label(s) && short(s.reason,300) && !once.has(s.displayLabel+s.reason)) { once.add(s.displayLabel+s.reason); left.push(element('li', `Left out: ${s.displayLabel}: ${s.reason}`)); }
    $('review-skipped').replaceChildren(...left);
    const t = plan('trash'), e = plan('evict'), d = plan('delete');
    $('do-trash').hidden = !t; if (t) $('do-trash').textContent = t.count===1 ? 'Move 1 item to Trash' : `Move ${t.count} items to Trash`;
    $('do-evict').hidden = !e; if (e) $('do-evict').textContent = e.count===1 ? 'Remove 1 download (keep in iCloud)' : `Remove ${e.count} downloads (keep in iCloud)`;
    $('do-delete').hidden = !d; if (d) $('do-delete').textContent = `Delete permanently, free ${bytes(d.freesBytes)} now`;
  }
  function confirmDelete() {
    const d = plan('delete'); if (!d || acting) return;
    deleting = d; $('review').hidden = true; $('confirm-delete').hidden = false; $('confirm-status').textContent = '';
    $('confirm-text').textContent = d.count===1 ? 'Delete 1 item permanently? This can\'t be undone.' : `Delete ${d.count} items permanently? This can't be undone.`;
    $('confirm-items').replaceChildren(...d.items.filter(label).slice(0,500).map(i => { const li = element('li'); li.append(who(i.displayLabel, short(i.path,4096) ? i.path : '')); return li; }));
    $('confirm-total').textContent = `Frees about ${bytes(d.freesBytes)} on this Mac right away.`;
    renderCleanup(); resize(); $('confirm-no').focus();  // the safe choice has focus
  }
  async function execute(a) {
    const p = a==='delete' ? deleting : plan(a);
    if (!p || acting || closing) return;
    // Each token is spent once: forget every plan before sending so a second click sends nothing.
    previews = null; deleting = null; busyCleanup(true);
    const where = a==='delete' ? 'confirm-status' : 'review-status';
    $(where).textContent = a==='trash' ? 'Moving to the Trash…' : a==='evict' ? 'Removing downloads…' : 'Deleting…';
    try {
      const r = await call('storage_cleanup_execute', {token:p.token}, 300000);
      receipt(r);
    } catch (e) {
      // Back to a review with no plan: the person ticks and reviews again for a fresh token.
      $('confirm-delete').hidden = true; $('review').hidden = false;
      renderReview(reviewing.map(item).filter(Boolean), {});
      if (e.refused) $('review-status').textContent = `${e.message} Nothing more was sent. Go back and review again; Refresh shows the current state.`;
      else {
        // Timeout, bridge failure or an unexpected error: the plugin may have acted before it stopped.
        $('review-status').textContent = 'The result is unknown: the plugin did not confirm what happened, so some items may already have been moved or deleted. Refresh the map to check what changed before doing anything else.';
        $('check-refresh').hidden = false;
      }
      $('review-title').focus();
    }
    finally { busyCleanup(false); renderCleanup(); resize(); }
  }
  function outcomes(list, title, box) {
    const rows = (Array.isArray(list) ? list : []).filter(x=>label(x)&&short(x.reason,300)).slice(0,500);
    $(title).hidden = !rows.length; $(box).replaceChildren(...rows.map(x=>element('li', `${x.displayLabel}: ${x.reason}`)));
  }
  function receipt(r) {
    const done = (Array.isArray(r?.done) ? r.done : []).filter(label).slice(0,500), a = r?.action;
    for (const id of ['review','confirm-delete']) $(id).hidden = true;
    $('receipt').hidden = false; $('check-refresh').hidden = true;
    $('receipt-title').textContent = a==='trash' ? 'Moved to the Trash' : a==='evict' ? 'Downloads removed' : 'Deleted';
    const est = validBytes(r?.freedEstimateBytes) && r.freedEstimateBytes ? bytes(r.freedEstimateBytes) : 'an unknown amount';
    $('receipt-summary').textContent = done.length ? (a==='trash' ? `Moved ${items(done.length)} to the Trash (about ${est}).` : a==='evict' ? `Removed ${done.length===1?'1 download':done.length.toLocaleString()+' downloads'} from this Mac (about ${est}); they stay in iCloud.` : `Deleted ${items(done.length)} (about ${est}).`) : 'Nothing was changed.';
    $('receipt-done').replaceChildren(...done.map(d => { const li = element('li'); li.append(who(d.displayLabel, short(d.path,4096)?d.path:''), element('span', validBytes(d.freesBytes)&&d.freesBytes?bytes(d.freesBytes):'')); li.lastChild.className = 'amt'; return li; }));
    outcomes(r?.skipped, 'receipt-skipped-title', 'receipt-skipped');
    outcomes(r?.failed, 'receipt-failed-title', 'receipt-failed');
    const before = r?.freeSpaceBefore, after = r?.freeSpaceAfter, ok = Number.isSafeInteger(before) && Number.isSafeInteger(after) && before>=0 && after>=0;
    const change = ok ? after-before : 0;
    $('receipt-space').textContent = ok ? `Free space on this Mac: ${bytes(String(before))} before, ${bytes(String(after))} now (${change>=0?'+':'−'}${bytes(String(Math.abs(change)))}).` : 'The change in free space could not be measured.';
    $('receipt-note').textContent = short(r?.trashNote,300) ? r.trashNote : '';
    const trashed = a==='trash' && done.length > 0;
    $('open-trash').hidden = !trashed; $('receipt-hint').hidden = !trashed;
    if (r?.rescan?.started && ID.test(r.rescan.scanId||'')) {
      $('receipt-status').textContent = 'Updating the map…'; resuggest = true; rescanning = r.rescan.scanId; following = null; follow(r.rescan.scanId);
    } else $('receipt-status').textContent = done.length ? 'Refresh to update the map.' : '';
    $('receipt-title').focus();
  }
  async function openTrash() {
    if (!caps.serverTools || acting) return;
    try { await call('storage_cleanup_settings', {optIn:{}, openTrash:true}); $('receipt-status').textContent = 'The Trash is open in Finder.'; }
    catch (e) { $('receipt-status').textContent = e.message; }
  }
  function afterRescan(ok) {
    $('receipt-status').textContent = ok ? 'The map now shows the folder after this change.' : 'Refresh to update the map.';
    if (ok && resuggest) { resuggest = false; suggest(); }
  }
  async function suggest() {
    // Suggestions load when a scan asked to free space finishes, and again after a rescan; the model sees nothing new.
    const v = current; if (!v || !caps.serverTools || acting) return;
    $('cleanup-status').textContent = 'Looking for suggestions…'; busyCleanup(true);
    try {
      const result = await request('tools/call', {name:'storage_suggest_cleanup', arguments:{scanId:v.scanId,scanVersion:v.scanVersion}}, 120000);
      busyCleanup(false);
      if (result?.structuredContent?.ok !== true) throw new Error(result?.structuredContent?.error?.message);
      if (current === v || current?.scanId === v.scanId && current?.scanVersion === v.scanVersion) { accept(result); $('cleanup-status').textContent = ''; }
    } catch (e) { $('cleanup-status').textContent = short(e?.message,300) ? e.message : 'Suggestions could not be refreshed.'; }
    finally { busyCleanup(false); renderCleanup(); resize(); }
  }
  function resize() { notify('ui/notifications/size-changed',{height:Math.min(document.documentElement.scrollHeight,2400)}); }
  $('tray-review').onclick = review;
  $('suggest').onclick = suggest;
  $('review-back').onclick = () => { if (acting) return; previews = null; $('review').hidden = true; renderCleanup(); resize(); };
  $('do-trash').onclick = () => execute('trash');
  $('do-evict').onclick = () => execute('evict');
  $('do-delete').onclick = confirmDelete;
  $('confirm-yes').onclick = () => execute('delete');
  function cancelDelete() { if (acting) return; deleting = null; $('confirm-delete').hidden = true; review(null, true); $('review-title').focus(); }
  $('confirm-no').onclick = cancelDelete;
  $('confirm-delete').addEventListener('keydown', e => { if (e.key === 'Escape') { e.preventDefault(); cancelDelete(); } });
  $('check-refresh').onclick = () => { if (acting) return; $('review').hidden = true; renderCleanup(); load(); };
  $('open-trash').onclick = openTrash;
  $('receipt-close').onclick = () => {
    if (acting) return; $('receipt').hidden = true;
    if ($('view').classList.contains('receipt-only')) { $('view').classList.remove('receipt-only'); $('view').hidden = true; }
    renderCleanup(); resize();
  };
  function theme(context) {
    // Host context: light/dark theme plus optional standard CSS variables.
    if(!context||typeof context!=='object')return;
    if(context.theme==='dark'||context.theme==='light')document.documentElement.dataset.theme=context.theme;
    const vars=context.styles?.variables;
    if(vars&&typeof vars==='object')for(const [k,val] of Object.entries(vars).slice(0,200))
      if(/^--font-sans$/.test(k)&&typeof val==='string'&&val.length<300)document.documentElement.style.setProperty(k,val);
  }
  function clearContext() {
    shared=false;sharing=false;
    if(ready&&caps.updateModelContext?.structuredContent)updateContext({structuredContent:{storageSelection:null}}).catch(()=>{if(!disposed)status('Selection changed; context revocation was not acknowledged. Re-share the current selection before a follow-up.');});
  }
  function updateContext(params) {
    // Serialize acknowledgments so a delayed old selection cannot be applied
    // after its revocation or a newer deliberate selection.
    const revoke=params.structuredContent.storageSelection===null;
    if(!revoke)contextMayRemain=true;
    contextQueue=contextQueue.catch(()=>{}).then(()=>request('ui/update-model-context',params)).then(result=>{if(revoke)contextMayRemain=false;return result;});
    return contextQueue;
  }
  async function load(overrides={},navigation=false) {
    if(!current||busy||closing||acting||!caps.serverTools)return;
    const token=++operation,v=current;busy=true;controls();status('Loading authoritative measurements…');
    const args={scanId:v.scanId,scanVersion:v.scanVersion,basis:v.basis,withinNodeId:v.within.nodeId,aggregate:v.aggregate,pageSize:30,...overrides};
    try {
      const result=await request('tools/call',{name:'storage_render_view',arguments:args});
      if(disposed||token!==operation)return;
      if(accept(result)&&navigation&&shared)clearContext();
    } catch { if(token===operation)fail('UNAVAILABLE'); }
    finally{if(token===operation){busy=false;controls();}}
  }
  function dispose(message=CLOSED) {
    if(disposed)return;disposed=true;ready=false;operation++;
    for(const p of pending.values()){clearTimeout(p.timer);p.reject(new Error('View disposed'));}pending.clear();
    window.removeEventListener('message',receive);window.removeEventListener('pagehide',unload);
    current=null;selected=null;leaveCleanup();$('view').hidden=true;$('rows').replaceChildren();$('map').replaceChildren();status(message);controls();
  }
  function unload() { dispose(contextMayRemain?UNREVOKED:CLOSED); }
  async function teardown(id) {
    if(closing){window.parent.postMessage({jsonrpc:'2.0',id,error:{code:-32000,message:'View is already closing'}},'*');return;}
    closing=true;operation++;busy=true;controls();status('Closing view and revoking selection context…');
    let timer;
    try{
      // Invalidate pending tools, but keep the bridge alive for queued context
      // acknowledgments. Bound the entire queue wait plus final revocation.
      if(ready&&caps.updateModelContext?.structuredContent)await Promise.race([
        updateContext({structuredContent:{storageSelection:null}}),
        new Promise((_,reject)=>{timer=setTimeout(()=>reject(new Error('Cleanup deadline')),2500);})
      ]);
      else if(contextMayRemain)throw new Error('Context bridge unavailable');
      if(disposed)return;
      window.parent.postMessage({jsonrpc:'2.0',id,result:{}},'*');dispose();
    }catch{
      if(disposed)return;
      window.parent.postMessage({jsonrpc:'2.0',id,error:{code:-32000,message:'Selection context revocation was not acknowledged'}},'*');dispose(UNREVOKED);
    }finally{clearTimeout(timer);}
  }
  function receive(event) {
    if(disposed||event.source!==window.parent)return;
    const m=event.data;if(!m||m.jsonrpc!=='2.0')return;
    try{if(JSON.stringify(m).length>150000)return;}catch{return;}
    if(m.id!==undefined&&pending.has(m.id)&&!m.method){const p=pending.get(m.id);pending.delete(m.id);clearTimeout(p.timer);m.error?p.reject(new Error('Host request rejected')):p.resolve(m.result);return;}
    if(m.method==='ui/resource-teardown'&&m.id!==undefined){teardown(m.id);return;}
    if(m.method==='ping'&&m.id!==undefined){window.parent.postMessage({jsonrpc:'2.0',id:m.id,result:{}},'*');return;}
    if(!ready||closing)return;
    if(m.method==='ui/notifications/tool-result'){if(accept(m.params)){operation++;busy=false;}controls();}
    else if(m.method==='ui/notifications/tool-cancelled')fail('CONFLICT');
    else if(m.method==='ui/notifications/host-context-changed')theme(m.params);
  }
  $('basis').onchange=()=>load({basis:$('basis').value},true);
  $('aggregate').onchange=()=>load({aggregate:$('aggregate').value},true);
  $('refresh').onclick=()=>load();
  // Show a few rows first; reveal the loaded rows in steps, then fetch the next page.
  $('first').onclick=()=>{if(!current)return;if(paged)load();else{shownRows=FEW;render();}};
  $('next').onclick=()=>{if(!current)return;if(current.rows.length>shownRows){shownRows+=MORE;render();}
    else if(current.nextCursor){more=true;shownRows=Infinity;load({cursor:current.nextCursor});}};
  window.addEventListener('message',receive);window.addEventListener('pagehide',unload);controls();
  request('ui/initialize',{protocolVersion:'2026-01-26',appInfo:{name:'storage-assistant',version:'0.2.0'},appCapabilities:{availableDisplayModes:['inline']}})
    .then(result=>{if(disposed)return;if(typeof result?.protocolVersion!=='string')throw new Error('Unsupported UI protocol');caps=result.hostCapabilities||{};theme(result.hostContext);ready=true;notify('ui/notifications/initialized',{});status('Waiting for a scan…');controls();})
    .catch(()=>{if(!disposed)status('MCP Apps bridge unavailable. Refresh the connection in a supported host; headless tools remain available.');});
})();
