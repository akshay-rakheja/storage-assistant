const $ = id => document.getElementById(id);
let state = null, within = null, selected = null, lastVersion = null, paired = false, lastViewRevision = null, tablePage = 0;
const bytes = value => {if(value===null)return 'Unknown';const n=BigInt(value),fraction=(n%1000000n).toString().padStart(6,'0').slice(0,3).replace(/0+$/,'');return `${(n/1000000n).toLocaleString()}${fraction?'.'+fraction:''} MB`;};
async function api(path, data = {}) {
  const response = await fetch(`/api/${path}`, {method:'POST', headers:{'Content-Type':'application/json','X-Storage-Request':'local-ui'}, body:JSON.stringify(data)});
  const result = await response.json();
  if (!response.ok || result.ok === false) throw new Error(result.error?.message || result.error || 'Local request failed.');
  return result.data || result;
}
function message(text='') { $('message').textContent = text; }
async function act(fn) { try { message(); await fn(); } catch (error) { message(error.message); } }
function el(tag, text, className) { const node=document.createElement(tag); if(text!==undefined)node.textContent=text; if(className)node.className=className;return node; }
function nodeById(id) { return state?.nodes.find(n=>n.nodeId===id); }
function choose(node) { selected=node.nodeId; if(node.kind==='directory'){within=node.nodeId;tablePage=0;} render(); }
function tileRects(items, x=0, y=0, w=100, h=100, vertical=true) {
  if(!items.length)return [];
  if(items.length===1)return [{node:items[0],x,y,w,h}];
  const total=items.reduce((a,n)=>a+BigInt(n.value),0n);let subtotal=0n,index=0;
  while(index<items.length-1 && subtotal*2n<total)subtotal+=BigInt(items[index++].value);
  const ratio=Number(subtotal*1000000n/total)/1000000;
  return vertical ? [...tileRects(items.slice(0,index),x,y,w*ratio,h,false),...tileRects(items.slice(index),x+w*ratio,y,w*(1-ratio),h,false)] : [...tileRects(items.slice(0,index),x,y,w,h*ratio,true),...tileRects(items.slice(index),x,y+h*ratio,w,h*(1-ratio),true)];
}
export { tileRects, bytes };
function render() {
  if(!state)return;
  const scan=state.scan;
  $('rescan').disabled=!state.scopes.length || scan?.state==='running';
  $('cancel').hidden=scan?.state!=='running';$('query').disabled=!scan || scan.state==='running';
  $('scope-label').textContent=state.scopes.length?`${state.selectedFolderLabel}: one folder authorized for 30 minutes. Names stay local.`:'Choose one folder to begin.';
  if(!scan){$('coverage').textContent='No scan';$('logical').textContent=$('allocated').textContent='—';$('rows').replaceChildren();$('treemap').replaceChildren();$('breadcrumbs').replaceChildren();$('query-result').replaceChildren();$('detail').replaceChildren();$('selection').textContent='Select a map tile or table item.';$('caption').textContent='No selected snapshot.';$('progress').textContent='Select one folder locally; tools cannot broaden this choice.';$('gaps').textContent='No retained scan data.';$('question').value='Select one folder locally and start a scan.';return;}
  if(lastVersion!==scan.scanVersion){within=scan.rootNodeId;selected=null;lastVersion=scan.scanVersion;lastViewRevision=null;tablePage=0;$('query-result').replaceChildren();}
  if(!within)within=scan.rootNodeId;
  if(state.view.revision && state.view.revision!==lastViewRevision && state.view.scanVersion===scan.scanVersion){if(state.view.withinNodeId)within=state.view.withinNodeId;if(state.view.basis)$('basis').value=state.view.basis;lastViewRevision=state.view.revision;tablePage=0;}
  const basis=$('basis').value,metric=basis==='logical'?'logicalBytes':'allocatedEstimateBytes';
  $('coverage').textContent=scan.state==='running'?'Scanning…':`${scan.coverage[0].toUpperCase()+scan.coverage.slice(1)} snapshot`;
  $('progress').textContent=`${scan.observedEntries.toLocaleString()} observed entries · ${scan.elapsedMs} ms · version ${scan.scanVersion}`;
  $('logical').textContent=bytes(scan.logicalBytes);$('allocated').textContent=bytes(scan.allocatedEstimateBytes);
  $('gaps').textContent=Object.keys(scan.gapCounts).length?`Gaps: ${Object.entries(scan.gapCounts).map(([k,v])=>`${k.replaceAll('_',' ')} (${v})`).join(', ')}. Unobserved entry count and omitted bytes are unknown.`:'Policy scan finished with no reported gaps. This is an observed folder total, not a volume accounting total.';
  const crumbs=[];let parent=nodeById(within);while(parent){crumbs.unshift(parent);parent=nodeById(parent.parentId);}
  $('breadcrumbs').replaceChildren(...crumbs.map(n=>{const b=el('button',n.displayLabel);b.onclick=()=>{within=n.nodeId;tablePage=0;render();};return b;}));
  const children=state.nodes.filter(n=>n.parentId===within).sort((a,b)=>{const aa=BigInt(a[metric]||0),bb=BigInt(b[metric]||0);return aa===bb?a.displayLabel.localeCompare(b.displayLabel):aa>bb?-1:1;});
  const highlights=state.view.scanVersion===scan.scanVersion?state.view.selectionIds||[]:[];
  tablePage=Math.min(tablePage,Math.max(0,Math.ceil(children.length/100)-1));
  $('rows').replaceChildren(...children.slice(tablePage*100,(tablePage+1)*100).map(n=>{const tr=el('tr');if(n.nodeId===selected||highlights.includes(n.nodeId))tr.className='selected';const item=el('td'),b=el('button',n.displayLabel);b.onclick=()=>choose(n);b.setAttribute('aria-label',`${n.displayLabel}, ${n.kind}, ${bytes(n.logicalBytes)} apparent, ${n.coverage}`);item.append(b);tr.append(item,el('td',`${n.kind} / ${n.category}`),el('td',bytes(n.logicalBytes)),el('td',bytes(n.allocatedEstimateBytes)),el('td',n.coverage));return tr;}));
  $('page-info').textContent=`Page ${tablePage+1} of ${Math.max(1,Math.ceil(children.length/100))}`;$('previous').disabled=tablePage===0;$('next').disabled=(tablePage+1)*100>=children.length;
  $('caption').textContent=`${nodeById(within)?.displayLabel||'Folder'}, version ${scan.scanVersion}. Contributions count hardlinks once. ${children.length} immediate items.`;
  const positive=children.filter(n=>n[metric]!==null&&BigInt(n[metric])>0n).slice(0,100).map(n=>({...n,value:n[metric]}));
  $('treemap').replaceChildren(...tileRects(positive).map(({node:n,x,y,w,h})=>{const b=el('button',undefined,'tile');b.dataset.category=n.category;b.style.left=`${x}%`;b.style.top=`${y}%`;b.style.width=`${w}%`;b.style.height=`${h}%`;b.append(el('span',n.displayLabel),el('span',bytes(n[metric]),'tile-size'));b.setAttribute('aria-label',`${n.displayLabel}, ${bytes(n[metric])}, ${n.kind}, ${n.coverage}`);b.onclick=()=>choose(n);if(n.nodeId===selected||highlights.includes(n.nodeId))b.classList.add('selected');return b;}));
  if(!positive.length)$('treemap').append(el('p','No measured bytes for this view. Inspect zero-byte, skipped or unknown items in the table.'));
  const detail=nodeById(selected);$('selection').textContent=detail?detail.displayLabel:'Select a map tile or table item.';
  $('detail').replaceChildren();if(detail){for(const [k,v] of Object.entries({'Apparent contribution':bytes(detail.logicalBytes),'Item logical length':bytes(detail.itemLogicalBytes),'Allocated estimate':bytes(detail.allocatedEstimateBytes),'Coverage':detail.coverage,'Protection':detail.protection.replaceAll('_',' '),'Placeholder':detail.placeholder,'Hardlink attribution':detail.hardlinkAliasOf?'Counted under another alias':'This observed item','Node reference':detail.nodeId})){ $('detail').append(el('dt',k),el('dd',v)); }}
  $('question').value=`Using storage tools only, what are the three largest immediate contributors within node ${within} of scan ${scan.scanId}, version ${scan.scanVersion}, on the ${basis} basis? Cite the snapshot and explain coverage gaps. Highlight the evidence in the local view.`;
}
async function refresh(){if(!paired)return;try{state=await api('state');render();}catch(error){message(error.message);}}
$('pair-form').onsubmit=e=>{e.preventDefault();act(async()=>{await api('pair',{code:$('code').value});$('code').value='';paired=true;$('pair').hidden=true;$('workspace').hidden=false;await refresh();});};
$('select-toggle').onclick=()=>{$('scope-form').hidden=!$('scope-form').hidden;};
$('scope-form').onsubmit=e=>{e.preventDefault();act(async()=>{await api('select',{path:$('folder').value,disclosureAccepted:$('disclosure').checked});$('folder').value='';$('disclosure').checked=false;$('scope-form').hidden=true;lastVersion=null;within=selected=null;await refresh();});};
$('rescan').onclick=()=>act(async()=>{const requestId=crypto.randomUUID().replaceAll('-','');await api('tool',{name:'storage_start_scan',arguments:{grantId:state.scopes[0].grantId,requestId}});lastVersion=null;await refresh();});
$('cancel').onclick=()=>act(async()=>{await api('tool',{name:'storage_cancel_scan',arguments:{scanId:state.scan.scanId}});await refresh();});
$('release').onclick=()=>act(async()=>{if(state.scopes.length)await api('tool',{name:'storage_release_scope',arguments:{grantId:state.scopes[0].grantId}});within=selected=null;state.view={};await refresh();});
$('basis').onchange=()=>render();
$('previous').onclick=()=>{tablePage--;render();};$('next').onclick=()=>{tablePage++;render();};
$('query').onclick=()=>act(async()=>{
  const result=await api('query',{scanId:state.scan.scanId,scanVersion:state.scan.scanVersion,withinNodeId:within,basis:$('basis').value,aggregate:$('query-kind').value,pageSize:10});
  $('query-result').replaceChildren(...result.rows.map(r=>{
    const count=r.unknownAllocationCount||0;
    const coverage=count?` · ${count} ${count===1?'file':'files'} with unknown allocation`:'';
    const p=el('p',`${r.displayLabel}: ${bytes(r.bytes)}${coverage}`);
    if(r.nodeId){p.tabIndex=0;p.onclick=()=>{selected=r.nodeId;render();};p.onkeydown=e=>{if(e.key==='Enter'){selected=r.nodeId;render();}};}
    return p;
  }));
  await refresh();
});
$('copy').onclick=()=>act(async()=>{try{await navigator.clipboard.writeText($('question').value);message('Question copied. Paste it into Codex with the storage tools connected.');}catch{$('question').focus();$('question').select();message('Copy the selected question and paste it into Codex.');}});
setInterval(refresh,800);
const handoff=/^#pair=([a-f0-9]{32})$/.exec(location.hash);
if(handoff)history.replaceState(null,'',location.pathname);
(handoff?api('pair',{code:handoff[1]}):Promise.resolve()).catch(()=>{}).then(()=>api('state')).then(result=>{state=result;paired=true;$('pair').hidden=true;$('workspace').hidden=false;render();}).catch(()=>{});
