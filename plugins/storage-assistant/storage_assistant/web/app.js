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
  $('cancel').hidden=scan?.state!=='running';$('suggest').disabled=!scan||scan.state==='running'||acting;if(acting)$('rescan').disabled=true;$('query').disabled=!scan || scan.state==='running';
  $('scope-label').textContent=state.scopes.length?`${state.selectedFolderLabel}: one folder authorized for 30 minutes. Names stay local.`:'Choose one folder to begin.';
  if(!scan){if(cleanup)leaveCleanup();$('coverage').textContent='No scan';$('logical').textContent=$('allocated').textContent='—';$('rows').replaceChildren();$('treemap').replaceChildren();$('breadcrumbs').replaceChildren();$('query-result').replaceChildren();$('detail').replaceChildren();$('selection').textContent='Select a map tile or table item.';$('caption').textContent='No selected snapshot.';$('progress').textContent='Select one folder locally; tools cannot broaden this choice.';$('gaps').textContent='No retained scan data.';$('question').value='Select one folder locally and start a scan.';return;}
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
  $('rows').replaceChildren(...children.slice(tablePage*100,(tablePage+1)*100).map(n=>{const tr=el('tr');const c=suggested(n.nodeId);if(n.nodeId===selected||highlights.includes(n.nodeId))tr.className='selected';const item=el('td'),b=el('button',n.displayLabel);b.onclick=()=>choose(n);b.setAttribute('aria-label',`${n.displayLabel}, ${n.kind}, ${bytes(n.logicalBytes)} apparent, ${n.coverage}${c?`, suggested: ${TIERS[c.tier]}${ticked.has(c.nodeId)?', ticked':''}`:''}`);item.append(b);if(c){tr.classList.add('tier-'+c.tier);item.append(chip(c));}tr.append(item,el('td',`${n.kind} / ${n.category}`),el('td',bytes(n.logicalBytes)),el('td',bytes(n.allocatedEstimateBytes)),el('td',n.coverage));return tr;}));
  $('page-info').textContent=`Page ${tablePage+1} of ${Math.max(1,Math.ceil(children.length/100))}`;$('previous').disabled=tablePage===0;$('next').disabled=(tablePage+1)*100>=children.length;
  $('caption').textContent=`${nodeById(within)?.displayLabel||'Folder'}, version ${scan.scanVersion}. Contributions count hardlinks once. ${children.length} immediate items.`;
  const positive=children.filter(n=>n[metric]!==null&&BigInt(n[metric])>0n).slice(0,100).map(n=>({...n,value:n[metric]}));
  $('treemap').replaceChildren(...tileRects(positive).map(({node:n,x,y,w,h})=>{const b=el('button',undefined,'tile');b.dataset.category=n.category;b.style.left=`${x}%`;b.style.top=`${y}%`;b.style.width=`${w}%`;b.style.height=`${h}%`;b.append(el('span',n.displayLabel),el('span',bytes(n[metric]),'tile-size'));b.setAttribute('aria-label',`${n.displayLabel}, ${bytes(n[metric])}, ${n.kind}, ${n.coverage}`);b.onclick=()=>choose(n);const c=suggested(n.nodeId);if(c){b.classList.add('cand','tier-'+c.tier);if(ticked.has(c.nodeId))b.classList.add('ticked');b.append(chip(c));b.setAttribute('aria-label',b.getAttribute('aria-label')+`, suggested: ${TIERS[c.tier]}${ticked.has(c.nodeId)?', ticked':''}`);}if(n.nodeId===selected||highlights.includes(n.nodeId))b.classList.add('selected');return b;}));
  if(!positive.length)$('treemap').append(el('p','No measured bytes for this view. Inspect zero-byte, skipped or unknown items in the table.'));
  const detail=nodeById(selected);$('selection').textContent=detail?detail.displayLabel:'Select a map tile or table item.';
  $('detail').replaceChildren();if(detail){for(const [k,v] of Object.entries({'Apparent contribution':bytes(detail.logicalBytes),'Item logical length':bytes(detail.itemLogicalBytes),'Allocated estimate':bytes(detail.allocatedEstimateBytes),'Coverage':detail.coverage,'Protection':detail.protection.replaceAll('_',' '),'Placeholder':detail.placeholder,'Hardlink attribution':detail.hardlinkAliasOf?'Counted under another alias':'This observed item','Node reference':detail.nodeId})){ $('detail').append(el('dt',k),el('dd',v)); }}
  $('question').value=`Using storage tools only, what are the three largest immediate contributors within node ${within} of scan ${scan.scanId}, version ${scan.scanVersion}, on the ${basis} basis? Cite the snapshot and explain coverage gaps. Highlight the evidence in the local view.`;
}
async function refresh(){if(!paired)return;try{state=await api('state');syncCleanup();render();}catch(error){message(error.message);}}
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
// ---- Freeing space ----------------------------------------------------------
// The same flow as the inline view: suggest → tick → Review… (one preview per action,
// each a one-time token) → one click per action → receipt. Only this paired view sends
// execute; the local executor re-checks every item. Names only ever go in via textContent.
const TIERS={safe:'Safe to clear',glance:'Worth a glance',call:'Your call'};
const ACTIONS=['trash','evict','delete'];
const HEX=/^[a-f0-9]{32}$/,DIGITS=/^(0|[1-9][0-9]{0,29})$/;
let cleanup=null,ticked=new Set(),seen=new Set(),reviewing=[],previews=null,deleting=null,acting=false,resuggest=null;
const okBytes=x=>typeof x==='string'&&DIGITS.test(x);
const short=(s,n)=>typeof s==='string'&&s.length>0&&s.length<=n;
const human=x=>{const n=Number(x),units=['B','KB','MB','GB','TB'];let i=0,v=n;while(v>=1000&&i<units.length-1){v/=1000;i++;}return (i?v.toFixed(v>=100?0:1):String(n))+' '+units[i];};
const items=n=>`${n.toLocaleString()} ${n===1?'item':'items'}`;
const sum=list=>list.reduce((n,c)=>n+BigInt(c.freesBytes||'0'),0n).toString();
const cleanOn=()=>!!cleanup&&!!state?.scan&&cleanup.scanId===state.scan.scanId&&cleanup.scanVersion===state.scan.scanVersion;
function suggested(id){return cleanOn()?cleanup.byId.get(id)||null:null;}
function chip(c){const e=el('em',c.reasons[0]||TIERS[c.tier],'chip tier-'+c.tier);e.title=`${TIERS[c.tier]}: ${c.reasons.join('; ')}`;return e;}
function who(name,sub){const d=el('span',undefined,'who');d.append(el('b',name));if(sub)d.append(el('small',sub));return d;}
async function cleanupApi(route,data){
  let response,result;
  try{response=await fetch(`/api/cleanup/${route}`,{method:'POST',headers:{'Content-Type':'application/json','X-Storage-Request':'local-ui'},body:JSON.stringify(data)});result=await response.json();}
  catch{throw Object.assign(new Error('No answer came back from the local session.'),{refused:false});}
  if(!response.ok||result?.ok!==true){
    // A structured refusal: the local session answered and says what did not happen.
    const m=typeof result?.error==='string'?result.error:result?.error?.message;
    throw Object.assign(new Error(short(m,300)?m:'The local session could not do this. Review again.'),{refused:true});
  }
  return result.data;
}
function candidate(c){
  if(!c||!HEX.test(c.nodeId)||!short(c.displayLabel,1024)||!short(c.path,4096)||!TIERS[c.tier]||!okBytes(c.freesBytes))return null;
  const reasons=Array.isArray(c.reasons)?c.reasons.filter(r=>short(r?.text,300)).slice(0,6).map(r=>r.text):[];
  const actions=Array.isArray(c.actions)?ACTIONS.filter(a=>c.actions.includes(a)):[];
  return {nodeId:c.nodeId,displayLabel:c.displayLabel,path:c.path,tier:c.tier,preTicked:c.preTicked===true,reasons,actions,freesBytes:c.freesBytes};
}
function accept(data,scan){
  const list=(Array.isArray(data?.candidates)?data.candidates:[]).slice(0,500).map(candidate).filter(Boolean);
  const fresh=!cleanup||cleanup.scanId!==scan.scanId||cleanup.scanVersion!==scan.scanVersion;
  if(fresh){ticked=new Set();seen=new Set();}
  cleanup={scanId:scan.scanId,scanVersion:scan.scanVersion,list,byId:new Map(list.map(c=>[c.nodeId,c]))};
  for(const c of list){if(!seen.has(c.nodeId)&&c.preTicked)ticked.add(c.nodeId);seen.add(c.nodeId);}
  for(const id of [...ticked])if(!cleanup.byId.has(id))ticked.delete(id);
  previews=null;deleting=null;$('review').hidden=true;$('confirm-delete').hidden=true;
  return fresh;
}
function leaveCleanup(){
  cleanup=null;ticked=new Set();seen=new Set();previews=null;deleting=null;resuggest=null;
  for(const id of ['cleanup','review','confirm-delete','receipt','tray'])$(id).hidden=true;
}
function syncCleanup(){
  // Candidates belong to one scan. After a change the plugin rescans; suggest again once that finishes.
  if(!cleanup||acting||cleanOn())return;  // never while a request is in flight: its answer decides
  const scan=state?.scan;
  if(resuggest&&scan?.scanId===resuggest){
    if(scan.state!=='running'&&!acting){resuggest=null;$('receipt-status').textContent='The map now shows the folder after this change.';suggest();}
    else{$('cleanup').hidden=true;$('tray').hidden=true;}
    return;
  }
  leaveCleanup();
}
function busy(on){
  acting=on;
  for(const e of document.querySelectorAll('.cleanup button,.cleanup input,#tray button'))e.disabled=on;
  for(const id of ['suggest','rescan','release','select-toggle'])$(id).disabled=on;
  if(!on){renderCleanup();render();}
}
function toggle(id,on){
  if(!cleanOn()||!cleanup.byId.has(id)||acting)return;
  on?ticked.add(id):ticked.delete(id);
  // Re-rendering replaces the controls: keep keyboard focus on the same control for the same item.
  const a=document.activeElement,key=a?.dataset?.pick?{pick:a.dataset.pick,node:a.dataset.node}:null;
  if(!$('review').hidden){review(key,true);return;}
  renderCleanup();render();refocus(key);
}
function refocus(key){
  if(!key)return;
  const target=[...document.querySelectorAll('[data-pick]')].find(e=>e.dataset.pick===key.pick&&e.dataset.node===key.node);
  if(target&&!target.disabled)target.focus();
}
function checkbox(c,where,pick){
  const x=el('input');x.type='checkbox';x.checked=ticked.has(c.nodeId);x.disabled=acting;x.dataset.pick=pick;x.dataset.node=c.nodeId;
  x.setAttribute('aria-label',`Include ${c.displayLabel} (${TIERS[c.tier]}) ${where}`);x.onchange=()=>toggle(c.nodeId,x.checked);return x;
}
function revealButton(c,quick){
  const b=el('button',quick?'Quick Look':'Show in Finder','small secondary');b.disabled=acting;b.dataset.pick=quick?'look':'finder';b.dataset.node=c.nodeId;
  b.setAttribute('aria-label',`${quick?'Quick Look':'Show in Finder'}: ${c.displayLabel}`);b.onclick=()=>reveal(c,quick);return b;
}
function renderCleanup(){
  const on=cleanOn();
  $('cleanup').hidden=!on;
  if(!on){$('tray').hidden=true;return;}
  $('candidates').replaceChildren(...(cleanup.list.length?cleanup.list.map(c=>{
    const li=el('li',undefined,'tier-'+c.tier);
    li.append(checkbox(c,'in the clean-up','list'),who(c.displayLabel,`${TIERS[c.tier]} · ${c.path}`),chip(c),el('span',human(c.freesBytes),'amt'),revealButton(c,true),revealButton(c,false));
    return li;
  }):[el('li','Nothing to suggest in this folder.')]));
  const chosen=cleanup.list.filter(c=>ticked.has(c.nodeId));
  $('tray').hidden=!$('review').hidden||!$('confirm-delete').hidden||!$('receipt').hidden;
  $('tray-text').textContent=`${items(chosen.length)} · frees about ${human(sum(chosen))} on this Mac`;
  $('tray-review').disabled=acting||!chosen.length;
  for(const id of ['optin-git','optin-dup'])$(id).disabled=acting;
}
async function suggest(){
  const scan=state?.scan;if(!scan||scan.state==='running'||acting)return;
  $('cleanup').hidden=false;$('cleanup-status').textContent='Looking for suggestions…';busy(true);
  let fresh=false;
  try{
    const data=await cleanupApi('suggest',{scanId:scan.scanId,scanVersion:scan.scanVersion});
    if(state?.scan?.scanId===scan.scanId&&state.scan.scanVersion===scan.scanVersion){fresh=accept(data,scan);$('cleanup-status').textContent='';}
  }catch(e){if(cleanOn())$('cleanup-status').textContent=e.message;else{$('cleanup-status').textContent='';message(e.message);}}
  finally{busy(false);}
  if(fresh)settings({});
}
async function review(key=null,keep=false){
  if(!cleanOn()||acting)return;
  // An open review keeps listing items unticked in it, so they can be ticked again (and keep focus).
  if(!keep)reviewing=cleanup.list.filter(c=>ticked.has(c.nodeId)).map(c=>c.nodeId);
  const v=cleanup,shown=reviewing.map(id=>v.byId.get(id)).filter(Boolean),chosen=shown.filter(c=>ticked.has(c.nodeId));
  $('receipt').hidden=true;$('confirm-delete').hidden=true;$('review').hidden=false;$('check-refresh').hidden=true;previews=null;
  $('review-status').textContent=chosen.length?'Checking each item again…':'Nothing is ticked.';
  for(const a of ACTIONS)$('do-'+a).hidden=true;
  renderReview(shown,{});renderCleanup();refocus(key);
  if(!chosen.length)return;
  busy(true);
  const results={};
  try{
    // Preview each action only for the ticked items that offer it.
    await Promise.all(ACTIONS.map(async a=>{
      const ids=chosen.filter(c=>c.actions.includes(a)).map(c=>c.nodeId);
      if(ids.length)results[a]=await cleanupApi('preview',{scanId:v.scanId,scanVersion:v.scanVersion,nodeIds:ids,action:a});
    }));
    if(cleanup!==v||$('review').hidden)return;
    previews=results;$('review-status').textContent='';
  }catch(e){$('review-status').textContent=e.message;}
  finally{busy(false);}
  renderReview(shown,previews||{});renderCleanup();refocus(key);
}
function plan(a){const p=previews?.[a];return p&&HEX.test(p.token||'')&&Number.isSafeInteger(p.count)&&p.count>0&&okBytes(p.freesBytes)?p:null;}
function renderReview(chosen,results){
  const detail=new Map();
  for(const a of ACTIONS)for(const i of (Array.isArray(results[a]?.items)?results[a].items:[]))if(HEX.test(i?.nodeId)&&!detail.has(i.nodeId))detail.set(i.nodeId,i);
  $('review-items').replaceChildren(...chosen.map(c=>{
    const i=detail.get(c.nodeId),li=el('li',undefined,'tier-'+c.tier+(ticked.has(c.nodeId)?'':' off'));
    const why=(Array.isArray(i?.reasons)?i.reasons.map(r=>r?.text??r):c.reasons).filter(t=>short(t,300)).join('; ');
    li.append(checkbox(c,'in this review','review'),who(c.displayLabel,`${c.path}${why?' · '+why:''}`),el('span',human(okBytes(i?.freesBytes)&&i.freesBytes!=='0'?i.freesBytes:c.freesBytes),'amt'));
    const warnings=Array.isArray(i?.warnings)?i.warnings.map(w=>typeof w==='string'?w:w?.text).filter(w=>short(w,300)):[];
    if(warnings.length)li.querySelector('.who').append(el('small',warnings.join('; '),'warn'));
    return li;
  }));
  const left=[],once=new Set();
  for(const a of ACTIONS)for(const s of (Array.isArray(results[a]?.skipped)?results[a].skipped:[]))
    if(short(s?.displayLabel,1024)&&short(s.reason,300)&&!once.has(s.displayLabel+s.reason)){once.add(s.displayLabel+s.reason);left.push(el('li',`Left out: ${s.displayLabel}: ${s.reason}`));}
  $('review-skipped').replaceChildren(...left);
  const t=plan('trash'),e=plan('evict'),d=plan('delete');
  $('do-trash').hidden=!t;if(t)$('do-trash').textContent=t.count===1?'Move 1 item to Trash':`Move ${t.count} items to Trash`;
  $('do-evict').hidden=!e;if(e)$('do-evict').textContent=e.count===1?'Remove 1 download (keep in iCloud)':`Remove ${e.count} downloads (keep in iCloud)`;
  $('do-delete').hidden=!d;if(d)$('do-delete').textContent=`Delete permanently, free ${human(d.freesBytes)} now`;
}
function confirmDelete(){
  const d=plan('delete');if(!d||acting)return;
  deleting=d;$('review').hidden=true;$('confirm-delete').hidden=false;$('confirm-status').textContent='';
  $('confirm-text').textContent=d.count===1?'Delete 1 item permanently? This can\'t be undone.':`Delete ${d.count} items permanently? This can't be undone.`;
  $('confirm-items').replaceChildren(...(Array.isArray(d.items)?d.items:[]).filter(i=>short(i?.displayLabel,1024)).slice(0,500).map(i=>{const li=el('li');li.append(who(i.displayLabel,short(i.path,4096)?i.path:''));return li;}));
  $('confirm-total').textContent=`Frees about ${human(d.freesBytes)} on this Mac right away.`;
  renderCleanup();$('confirm-no').focus();  // the safe choice has focus
}
async function execute(a){
  const p=a==='delete'?deleting:plan(a);
  if(!p||acting)return;
  // Each token is spent once: forget every plan before sending so a second click sends nothing.
  previews=null;deleting=null;busy(true);
  const where=a==='delete'?'confirm-status':'review-status';
  $(where).textContent=a==='trash'?'Moving to the Trash…':a==='evict'?'Removing downloads…':'Deleting…';
  try{receipt(await cleanupApi('execute',{token:p.token}));}
  catch(e){
    // Back to a review with no plan: the person ticks and reviews again for a fresh token.
    $('confirm-delete').hidden=true;$('review').hidden=false;
    renderReview(cleanup?reviewing.map(id=>cleanup.byId.get(id)).filter(Boolean):[],{});
    if(e.refused)$('review-status').textContent=`${e.message} Nothing more was sent. Go back and review again; Refresh shows the current state.`;
    else{
      // The connection failed: the session may have acted before the answer was lost.
      $('review-status').textContent='The result is unknown: no answer came back from the local session, so some items may already have been moved or deleted. Refresh the map to check what changed before doing anything else.';
      $('check-refresh').hidden=false;
    }
    $('review-title').focus();
  }
  finally{busy(false);}
}
function outcomes(list,title,box){
  const rows=(Array.isArray(list)?list:[]).filter(x=>short(x?.displayLabel,1024)&&short(x.reason,300)).slice(0,500);
  $(title).hidden=!rows.length;$(box).replaceChildren(...rows.map(x=>el('li',`${x.displayLabel}: ${x.reason}`)));
}
function receipt(r){
  const done=(Array.isArray(r?.done)?r.done:[]).filter(d=>short(d?.displayLabel,1024)).slice(0,500),a=r?.action;
  for(const id of ['review','confirm-delete'])$(id).hidden=true;
  $('receipt').hidden=false;$('check-refresh').hidden=true;
  $('receipt-title').textContent=a==='trash'?'Moved to the Trash':a==='evict'?'Downloads removed':'Deleted';
  const est=okBytes(r?.freedEstimateBytes)&&r.freedEstimateBytes!=='0'?human(r.freedEstimateBytes):'an unknown amount';
  $('receipt-summary').textContent=done.length?(a==='trash'?`Moved ${items(done.length)} to the Trash (about ${est}).`:a==='evict'?`Removed ${done.length===1?'1 download':done.length.toLocaleString()+' downloads'} from this Mac (about ${est}); they stay in iCloud.`:`Deleted ${items(done.length)} (about ${est}).`):'Nothing was changed.';
  $('receipt-done').replaceChildren(...done.map(d=>{const li=el('li');li.append(who(d.displayLabel,short(d.path,4096)?d.path:''),el('span',okBytes(d.freesBytes)&&d.freesBytes!=='0'?human(d.freesBytes):'','amt'));return li;}));
  outcomes(r?.skipped,'receipt-skipped-title','receipt-skipped');
  outcomes(r?.failed,'receipt-failed-title','receipt-failed');
  const before=r?.freeSpaceBefore,after=r?.freeSpaceAfter,ok=Number.isSafeInteger(before)&&Number.isSafeInteger(after)&&before>=0&&after>=0,change=ok?after-before:0;
  $('receipt-space').textContent=ok?`Free space on this Mac: ${human(String(before))} before, ${human(String(after))} now (${change>=0?'+':'−'}${human(String(Math.abs(change)))}).`:'The change in free space could not be measured.';
  $('receipt-note').textContent=short(r?.trashNote,300)?r.trashNote:'';
  const trashed=a==='trash'&&done.length>0;
  $('open-trash').hidden=!trashed;$('receipt-hint').hidden=!trashed;
  if(r?.rescan?.started&&HEX.test(r.rescan.scanId||'')){$('receipt-status').textContent='Updating the map…';resuggest=r.rescan.scanId;}
  else $('receipt-status').textContent=done.length?'Refresh to update the map.':'';
  $('receipt-title').focus();
}
async function settings(change,openTrash=false){
  if(acting)return;
  const changing=Object.keys(change).length>0,out=openTrash?'receipt-status':'cleanup-status';
  const args={optIn:change};if(openTrash)args.openTrash=true;
  let ok=false;
  if(changing)busy(true);
  try{
    const data=await cleanupApi('settings',args);
    if(typeof data?.optIn?.gitHealth==='boolean')$('optin-git').checked=data.optIn.gitHealth;
    if(typeof data?.optIn?.exactDuplicates==='boolean')$('optin-dup').checked=data.optIn.exactDuplicates;
    if(openTrash)$(out).textContent='The Trash is open in Finder.';
    ok=true;
  }catch(e){
    $(out).textContent=e.message;
    if('gitHealth' in change)$('optin-git').checked=!change.gitHealth;
    if('exactDuplicates' in change)$('optin-dup').checked=!change.exactDuplicates;
  }finally{if(changing)busy(false);}
  if(changing&&ok)await suggest();  // switching a check forgets earlier suggestions
}
async function reveal(c,quick){
  if(!cleanOn()||acting)return;
  $('cleanup-status').textContent='';
  try{await cleanupApi('reveal',{scanId:cleanup.scanId,scanVersion:cleanup.scanVersion,nodeId:c.nodeId,quickLook:quick});}
  catch(e){$('cleanup-status').textContent=`${c.displayLabel}: ${e.message}`;}
}
function cancelDelete(){if(acting)return;deleting=null;$('confirm-delete').hidden=true;review(null,true);$('review-title').focus();}
$('suggest').onclick=()=>suggest();
$('cleanup-close').onclick=()=>{if(!acting){leaveCleanup();render();}};
$('tray-review').onclick=()=>review();
$('review-back').onclick=()=>{if(acting)return;previews=null;$('review').hidden=true;renderCleanup();};
$('do-trash').onclick=()=>execute('trash');
$('do-evict').onclick=()=>execute('evict');
$('do-delete').onclick=confirmDelete;
$('confirm-yes').onclick=()=>execute('delete');
$('confirm-no').onclick=cancelDelete;
$('confirm-delete').addEventListener('keydown',e=>{if(e.key==='Escape'){e.preventDefault();cancelDelete();}});
$('check-refresh').onclick=()=>{if(acting)return;leaveCleanup();lastVersion=null;refresh();};
$('open-trash').onclick=()=>settings({},true);
$('receipt-close').onclick=()=>{if(acting)return;$('receipt').hidden=true;renderCleanup();};
$('optin-git').onchange=()=>settings({gitHealth:$('optin-git').checked});
$('optin-dup').onchange=()=>settings({exactDuplicates:$('optin-dup').checked});
setInterval(refresh,800);
const handoff=/^#pair=([a-f0-9]{32})$/.exec(location.hash);
if(handoff)history.replaceState(null,'',location.pathname);
(handoff?api('pair',{code:handoff[1]}):Promise.resolve()).catch(()=>{}).then(()=>api('state')).then(result=>{state=result;paired=true;$('pair').hidden=true;$('workspace').hidden=false;render();}).catch(()=>{});
