/* No fetch, cookies, local paths, external assets, analytics or persisted inventory. */
(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  const pending = new Map();
  let nextId = 1, ready = false, disposed = false, closing = false, caps = {}, current = null;
  let selected = null, shared = false, sharing = false, contextMayRemain = false, contextQueue = Promise.resolve(), operation = 0, busy = false, watermark = 0, revision = 0;
  const CLOSED = 'View closed. Ask the assistant to query and render a new view.';
  const UNREVOKED = 'View closed; context revocation was not acknowledged. The host may retain the previous selection. Query and share a new view before a follow-up.';
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
    for (const id of ['basis','aggregate','refresh','first','next','share']) $(id).disabled = closing || busy || !ready || !current || !caps.serverTools;
    $('next').disabled ||= !current?.nextCursor;
    $('share').disabled ||= !caps.updateModelContext?.structuredContent || current?.aggregate !== 'children';
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
    current = null; selected = null; operation++; busy = false;
    $('view').hidden = true; $('rows').replaceChildren(); $('map').replaceChildren();
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
      if (!current) { status(`Scanning… ${n.toLocaleString()} entries so far. The map appears here when the scan finishes.`); follow(envelope.data.scanId); }
      return false;
    }
    const v = envelope?.data?.view;
    if (!valid(v)) { fail('BAD_RESULT'); return false; }
    if (v.scanVersion<watermark || v.scanVersion===watermark && (current && v.scanId!==current.scanId || v.viewRevision<=revision)) return false;
    if (shared || sharing) clearContext();
    watermark=v.scanVersion; revision=v.viewRevision; current=v;
    selected=v.rows.find(r=>r.nodeId && v.selectionIds.includes(r.nodeId)) || v.within;
    render(); return true;
  }
  // Keep one progress view current: ask for status until the scan finishes, then render.
  let following = null;
  function follow(scanId) {
    if (following === scanId || !ID.test(scanId || '') || !caps.serverTools) return;
    following = scanId;
    const tick = () => {
      if (disposed || current || following !== scanId) return;
      request('tools/call', {name:'storage_scan_status', arguments:{scanId}}, 60000)
        .then(result => { if (disposed || current || following !== scanId) return;
          if (result?.structuredContent?.data?.state === 'running') { accept(result); setTimeout(tick, 1500); }
          else { following = null; if (accept(result)) { operation++; busy = false; } controls(); } })
        .catch(() => { if (!disposed && following === scanId) setTimeout(tick, 5000); });
    };
    setTimeout(tick, 1000);
  }
  function element(tag,text) { const e=document.createElement(tag); if(text!==undefined)e.textContent=text; return e; }
  function button(text,action,pressed) { const b=element('button',text); b.type='button'; b.onclick=action; if(pressed!==undefined)b.setAttribute('aria-pressed',String(pressed)); return b; }
  function choose(row) {
    if(busy||closing)return;
    if(row.kind==='directory') load({withinNodeId:row.nodeId,aggregate:'children'},true);
    else { selected=row; if(shared)clearContext(); render(); }
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
    $('view').hidden=false; $('basis').value=v.basis; $('aggregate').value=v.aggregate; status('');
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
    const largest=v.rows.reduce((m,r)=>r.bytes===null?m:Math.max(m,Number(r.bytes)),0)||1;
    $('rows').replaceChildren(...v.rows.map(r=>{
      const tr=element('tr'),name=element('td'),type=element('td',typeLabel(r)),size=element('td');
      name.className='name';type.className='type';size.className='size num';
      const label=element('span',r.nodeId?r.displayLabel:(CATS[r.displayLabel]||r.displayLabel));
      if(r.nodeId){const b=button('',()=>choose(r),r.nodeId===selected?.nodeId||v.selectionIds.includes(r.nodeId));b.append(swatch(kindOf(r)),label);b.title=r.path||r.displayLabel;name.append(b);}
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
      const kind=kindOf(t.row),pressed=t.row.nodeId?t.row.nodeId===selected?.nodeId||v.selectionIds.includes(t.row.nodeId):undefined;
      const b=button('',()=>t.row.nodeId&&choose(t.row),pressed);
      b.className=`tile cat-${kind}${t.w*t.h>W*H/6?' big':''}${t.w<60||t.h<40?' tiny':''}`;
      b.style.cssText=`left:${t.x/W*100}%;top:${t.y/H*100}%;width:${t.w/W*100}%;height:${t.h/H*100}%`;
      const title=t.row.nodeId?t.row.displayLabel:(CATS[t.row.displayLabel]||t.row.displayLabel);
      b.append(element('strong',title),element('span',bytes(t.row.bytes)));
      b.setAttribute('aria-label',`${title}, ${typeLabel(t.row)}, ${bytes(t.row.bytes)}${t.row.kind==='directory'?', open folder':''}`);
      b.title=`${t.row.path||title} (${bytes(t.row.bytes)})`;
      if(!t.row.nodeId)b.disabled=true;return b;
    }));
    const kinds=[...new Set(positive.map(kindOf))];
    $('legend').replaceChildren(...kinds.map(k=>{const li=element('li');li.append(swatch(k),document.createTextNode(k==='folder'?'Folder':(CATS[k]||k)));return li;}));
    $('first').hidden=!v.rows.length||!current.nextCursor&&v.matchingRows<=v.rows.length;
    $('next').hidden=!v.nextCursor;
    $('share').hidden=!caps.updateModelContext?.structuredContent;
    $('selection').textContent=selected&&selected!==v.within?`Selected: ${selected.displayLabel}${shared?', shared with the chat.':'.'}`:'';
    $('caveats').replaceChildren(...[
      'File size is how big files say they are; space on disk is an estimate and can be much smaller for sparse or cloud files.',
      'Files linked in several places are counted once.',
      'Neither number tells you exactly how much space deleting something would free (APFS clones and snapshots share space).',
      'Items that change during the scan, iCloud-only files, links and protected folders are listed above when skipped.'
    ].map(t=>element('li',t)));
    controls();notify('ui/notifications/size-changed',{height:Math.min(document.documentElement.scrollHeight,1600)});
  }
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
    if(!current||busy||closing||!caps.serverTools)return;
    const token=++operation,v=current;busy=true;controls();status('Loading authoritative measurements…');
    const args={scanId:v.scanId,scanVersion:v.scanVersion,basis:v.basis,withinNodeId:v.within.nodeId,aggregate:v.aggregate,pageSize:30,...overrides};
    try {
      const result=await request('tools/call',{name:'storage_render_view',arguments:args});
      if(disposed||token!==operation)return;
      if(accept(result)&&navigation&&shared)clearContext();
    } catch { if(token===operation)fail('UNAVAILABLE'); }
    finally{if(token===operation){busy=false;controls();}}
  }
  async function share() {
    if(!current||!selected||busy||closing||!caps.updateModelContext?.structuredContent)return;
    const v=current,selection=selected,token=++operation;busy=true;sharing=true;controls();
    try{
      // Revalidate against the executor at sharing time. A cached widget cannot
      // authorize expired/released/replaced snapshot context.
      const result=await request('tools/call',{name:'storage_get_node',arguments:{scanId:v.scanId,scanVersion:v.scanVersion,nodeId:selection.nodeId}});
      if(token!==operation||disposed)return;
      const envelope=result?.structuredContent,s=envelope?.data?.node,e=envelope?.data?.evidence;
      if(envelope?.ok!==true||result?.isError){fail(envelope?.error?.code);return;}
      if(!node(s)||s.nodeId!==selection.nodeId||e?.scanId!==v.scanId||e?.scanVersion!==v.scanVersion){fail('BAD_RESULT');return;}
      // Construct a minimal owned projection, never forward _meta/input/state.
      const storageSelection={scanId:v.scanId,scanVersion:v.scanVersion,viewRevision:v.viewRevision,basis:v.basis,coverage:e.coverage,nodeId:s.nodeId,displayLabel:s.displayLabel,kind:s.kind,category:s.category,logicalBytes:s.logicalBytes,allocatedEstimateBytes:s.allocatedEstimateBytes};
      await updateContext({structuredContent:{storageSelection}});if(token===operation){shared=true;render();}
    }
    catch{if(token===operation)status('Host did not accept selection context. Retry before asking a selected-item follow-up.');}
    finally{if(token===operation){sharing=false;busy=false;controls();}}
  }
  function dispose(message=CLOSED) {
    if(disposed)return;disposed=true;ready=false;operation++;
    for(const p of pending.values()){clearTimeout(p.timer);p.reject(new Error('View disposed'));}pending.clear();
    window.removeEventListener('message',receive);window.removeEventListener('pagehide',unload);
    current=null;selected=null;$('view').hidden=true;$('rows').replaceChildren();$('map').replaceChildren();status(message);controls();
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
  $('refresh').onclick=()=>load();$('first').onclick=()=>load();$('next').onclick=()=>load({cursor:current.nextCursor});$('share').onclick=share;
  window.addEventListener('message',receive);window.addEventListener('pagehide',unload);controls();
  request('ui/initialize',{protocolVersion:'2026-01-26',appInfo:{name:'storage-assistant',version:'0.2.0'},appCapabilities:{availableDisplayModes:['inline']}})
    .then(result=>{if(disposed)return;if(typeof result?.protocolVersion!=='string')throw new Error('Unsupported UI protocol');caps=result.hostCapabilities||{};theme(result.hostContext);ready=true;notify('ui/notifications/initialized',{});status('Waiting for a scan…');controls();})
    .catch(()=>{if(!disposed)status('MCP Apps bridge unavailable. Refresh the connection in a supported host; headless tools remain available.');});
})();
