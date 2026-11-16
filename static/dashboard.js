// Dashboard logic. Kept out of index.html so the page runs under a CSP with no inline scripts.
const TOKEN=document.querySelector('meta[name=sm-token]').content;
const $=id=>document.getElementById(id);let cur=null,info=null,guide=null,ws=null,poll=null,history=[],taskId=null,curLabel='';
let guideState={activity:'browse',task_label:'',task_status:''},guideSeq=0,guideAdvice=null;

// Theme: dark by default, remembered in this browser.
function applyTheme(t){document.documentElement.dataset.theme=t;try{localStorage.setItem('sm-theme',t)}catch{}}
applyTheme((()=>{try{return localStorage.getItem('sm-theme')}catch{return null}})()||'dark');
$('theme').onclick=()=>applyTheme(document.documentElement.dataset.theme==='dark'?'light':'dark');

function updateProgress(value){
  const wrap=$('progressWrap');if(value==null||value<=0){wrap.style.display='none';return}
  wrap.style.display='block';const pct=Math.min(100,Math.max(0,Math.round(value)));$('progressBar').style.width=pct+'%';$('progressText').textContent=pct+'%'}
function resetProgress(){$('progressWrap').style.display='none';$('progressBar').style.width='0%';$('progressText').textContent=''}
function toggleCard(id){const e=$(id);if(e&&'open' in e)e.open=!e.open}
function notify(st){if(document.hidden&&'Notification' in window&&Notification.permission==='granted')new Notification('Setup Manager',{body:curLabel+': '+st})}
const toast=(t,bad)=>{const e=$('toast');e.textContent=t;e.style.background=bad?'var(--bad)':'';e.classList.add('show');setTimeout(()=>e.classList.remove('show'),3200)};
async function api(path,opt={}){
  const o={method:opt.method||'GET',headers:{'X-Setup-Token':TOKEN}};
  if(opt.body){o.headers['Content-Type']='application/json';o.body=JSON.stringify(opt.body)}
  const r=await fetch(path,o);let d=null;try{d=await r.json()}catch{}
  if(!r.ok){throw new Error((d&&(typeof d.detail==='string'?d.detail:JSON.stringify(d.detail)))||('HTTP '+r.status))}
  return d}
const qs=o=>'?'+new URLSearchParams(o);
const badge=t=>{const s=document.createElement('span');s.className='badge '+t;s.textContent=t;return s};
// Project and model text is data: escape it before it goes into innerHTML.
const escapeHTML=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const mk=(tag,cls,text)=>{const e=document.createElement(tag);if(cls)e.className=cls;if(text!=null)e.textContent=text;return e};
function log(text,reset){const l=$('log');const atBottom=l.scrollHeight-l.scrollTop-l.clientHeight<40;if(reset)l.textContent='';l.textContent+=text+'\n';if(reset||atBottom)l.scrollTop=l.scrollHeight;syncPopout()}
let runWindow=null;
function outputWindow(){
  if(runWindow&&!runWindow.closed)return runWindow;
  runWindow=window.open('','sm-output','popup=yes,width=960,height=700');
  if(!runWindow)return null;
  const doc=runWindow.document;
  doc.open();
  doc.write('<!DOCTYPE html><html><head><meta charset="utf-8"><title>Setup Manager output</title><style>html,body{margin:0;height:100%;background:#0b0e13;color:#c9d1d9}pre{box-sizing:border-box;margin:0;min-height:100%;padding:16px;font:13px/1.6 ui-monospace,Consolas,monospace;white-space:pre-wrap;word-break:break-word}</style></head><body><pre id="out"></pre></body></html>');
  doc.close();
  return runWindow;
}
function syncPopout(){
  if(!runWindow||runWindow.closed)return;
  const pre=runWindow.document.getElementById('out');
  if(!pre)return;
  const atBottom=runWindow.scrollY+runWindow.innerHeight>=runWindow.document.body.scrollHeight-40;
  pre.textContent=$('log').textContent;
  if(atBottom)runWindow.scrollTo(0,runWindow.document.body.scrollHeight);
}
function openOutputWindow(){
  const w=outputWindow();
  if(!w){toast('Allow pop-up windows for this page, then try Pop out again.',1);return null}
  syncPopout();w.focus();return w;
}

async function loadProjects(){
  try{const list=await api('/projects'+qs({q:$('q').value}));const box=$('plist');box.replaceChildren();
    if(!list.length){box.innerHTML='<div class="empty-state" style="padding:30px 20px"><div class="icon">📂</div><p>No projects found.</p></div>'}
    list.forEach(p=>{const b=mk('button','proj'+(cur===p.path?' sel':''));b.append(mk('b',null,p.name));const s=mk('small');
      (p.types.length?p.types:['unknown']).forEach(t=>s.append(badge(t)));b.append(s);b.onclick=()=>select(p.path);box.append(b)})
  }catch(e){toast(e.message,1)}}

async function select(path){
  cur=path;$('title').textContent=path;$('title').className='';$('explorer').disabled=false;$('manual').value=path;
  $('types').replaceChildren();$('subprojects').textContent='';$('recommended').textContent='Loading…';$('advancedBody').replaceChildren();$('runRecommended').disabled=true;
  loadProjects();
  try{info=await api('/analyze',{method:'POST',body:{path}})}catch(e){info=null;guideState={activity:'project',task_label:'',task_status:''};refreshGuide();toast(e.message,1);showSafetyPrompt(path);return}
  showSafetyPrompt(path,info.folder_id);
  (info.detected||[]).forEach(t=>$('types').append(badge(t)));if(!(info.detected||[]).length)$('types').textContent='No known project type detected';
  buildRun();await buildSetup();buildUtilities();
  guideState={activity:'project',task_label:'',task_status:''};refreshGuide()}

function renderGuide(g){
  guideAdvice=g;
  $('guideHeadline').textContent=g.headline||'';
  $('guideDetail').textContent=g.detail||'';
  $('guideSource').textContent=g.source==='ollama'?'Ollama':'Built-in';
  const after=$('guideAfter');
  let followup=(g.after&&g.after.length)?('Then: '+g.after.join(' · ')):'';
  if(guideState.activity==='finished'&&info&&info.entry_hint)followup=followup?followup+' '+info.entry_hint:info.entry_hint;
  after.textContent=followup;
  const btn=$('guideDo');
  if(g.action){btn.disabled=false;btn.textContent=g.action.label;btn.onclick=()=>runStep(g.action,null).catch(e=>toast(e.message,1))}
  else{btn.disabled=true;btn.textContent='Do this';btn.onclick=null}
  $('guideAsk').disabled=!cur}
async function refreshGuide(){
  const seq=++guideSeq;
  const body={activity:guideState.activity,task_label:guideState.task_label||'',task_status:guideState.task_status||'',use_ai:false};
  if(cur&&guideState.activity!=='browse')body.path=cur;
  let rules;
  try{rules=await api('/guide/advice',{method:'POST',body})}catch(e){if(seq===guideSeq){$('guideHeadline').textContent='Guide unavailable';$('guideDetail').textContent=e.message}return}
  if(seq!==guideSeq)return;
  renderGuide(rules);
  if(!ai||ai.state!=='ready'||guideState.activity==='browse'||guideState.activity==='running')return;
  try{
    const smart=await api('/guide/advice',{method:'POST',body:{...body,use_ai:true}});
    if(seq===guideSeq&&smart.source==='ollama')renderGuide(smart);
  }catch{/* the built-in card stays */}}
function noteTask(st,label){
  guideState={activity:st==='completed'?'finished':st==='failed'?'failed':'project',task_label:label||'',task_status:st};
  refreshGuide()}
$('guideAsk').onclick=()=>{
  if(!cur||!guideAdvice)return toast('Select a project first',1);
  const name=guideAdvice.action?guideAdvice.action.label:guideAdvice.headline;
  $('msg').value='The guide recommends: '+name+'. '+(guideAdvice.detail||'')+' What should I watch for before I do that?';
  $('aiCard').scrollIntoView({behavior:'smooth',block:'nearest'});
  $('send').click()};

function buildRun(){
  const sel=$('candidate');sel.replaceChildren();const c=info.candidates||[];
  c.forEach((x,i)=>{const o=mk('option',null,x.label);o.value=i;sel.append(o)});
  sel.style.display=$('runapp').style.display=c.length?'':'none';
  $('runHint').textContent=(info&&info.entry_hint)||''}
$('runapp').onclick=()=>{const i=Number($('candidate').value);const c=(info.candidates||[])[i];if(!c)return;
  if(confirm('Run on this machine?\n\n'+(c.command||[]).join(' ')+'\n\nin '+cur))start('/task/launch',c.label,{path:cur,candidate_index:i,run_mode:'host'}).catch(e=>toast(e.message,1))};

// ---- Setup: a short recommended list, everything else under "Advanced options" ----
function optionRow(step,checked){
  const row=mk('div','opt');
  const box=mk('input');box.type='checkbox';box.checked=checked;
  const txt=mk('div','txt');txt.append(mk('div','name',step.label));
  if(step.desc)txt.append(mk('div','desc',step.desc));
  if(step.cmd&&!step.action)txt.append(mk('code',null,step.cmd));
  const state=mk('span','state');
  const run=mk('button','b','Run');run.onclick=()=>runStep(step,row).catch(e=>toast(e.message,1));
  row.append(box,txt,state,run);row._step=step;row._box=box;row._state=state;return row}

async function buildSetup(){
  const rec=$('recommended'),adv=$('advancedBody');rec.replaceChildren();adv.replaceChildren();
  try{guide=await api('/setup-guide',{method:'POST',body:{path:cur}})}catch(e){rec.textContent='Could not load the setup steps: '+e.message;return}
  const recommended=guide.essential_steps||[];
  if(guide.readme_tip){const tip=mk('div','hint');tip.append(mk('b',null,'📖 From README: '),document.createTextNode(guide.readme_tip));rec.append(tip)}
  recommended.forEach(s=>rec.append(optionRow(s,true)));
  if(!recommended.length)rec.textContent='Nothing to set up automatically for this project.';
  $('runRecommended').disabled=!recommended.length;$('recState').textContent=recommended.length?recommended.length+' steps, run in order':'';
  const subs=guide.sub_projects||[];
  if(subs.length)$('subprojects').textContent='Also contains: '+subs.map(s=>s.path.slice(cur.length+1).replace(/\\/g,'/')+' ('+s.detected.join(', ')+')').join('; ')+'. Open that folder to set it up on its own.';
  const extra=[...(guide.optional_steps||[])];
  const types=info.detected||[];
  types.filter(t=>['python','nodejs','dotnet','go'].includes(t)).forEach(t=>{
    extra.push({label:'Install '+t+' dependencies',desc:'Download the packages the project lists (asks before running).',endpoint:'/task/install-dependencies/'+t});
    if(t!=='go')extra.push({label:'Set up '+t+' from scratch',desc:'Create the environment and install everything for '+t+'.',endpoint:'/task/setup/'+t})});
  if(types.includes('python'))extra.push({label:'Install missing packages',desc:'Only the declared packages that are not installed yet.',endpoint:'/task/install-missing',body:true});
  if(types.some(t=>['python','nodejs','dotnet'].includes(t)))extra.push({label:'Add developer tools',desc:'Linters, type checkers, test and security tools for this project.',endpoint:'/task/extensions',ext:true});
  extra.forEach(s=>adv.append(optionRow(s,false)));
  $('advanced').querySelector('summary').textContent='Advanced options ('+extra.length+')'}

function waitTask(id){return new Promise(res=>{const t=setInterval(async()=>{try{const r=await api('/task/'+id);if(r.status!=='running'){clearInterval(t);res(r.status)}}catch{clearInterval(t);res('failed')}},1500)})}

async function runStep(step,row){
  const set=(c,t)=>{if(row){row.classList.remove('running','done','failed');if(c)row.classList.add(c);row._state.textContent=t||''}};
  set('running','running…');let id=null;
  try{
    if(step.action==='security-scan'){guideState={activity:'running',task_label:step.label,task_status:'running'};refreshGuide();const ok=await runSecurityScan();if(!ok){set('failed','failed');noteTask('failed',step.label);return 'failed'}set('done','done');noteTask('completed',step.label);return 'completed'}
    if(step.action==='check-deps'){guideState={activity:'running',task_label:step.label,task_status:'running'};refreshGuide();await checkDeps();set('done','done');noteTask('completed',step.label);return 'completed'}
    if(step.action==='quality-checks')id=await start('/task/checks'+qs({project_type:'auto',path:cur}),'Quality checks');
    else if(step.endpoint==='/task/make-target')id=await start('/task/make-target?target='+encodeURIComponent(step.param)+'&'+qs({path:cur}).slice(1),step.label);
    else if(step.body)id=await start(step.endpoint,step.label,{path:cur});
    else if(step.ext)id=await start(step.endpoint+qs({path:cur}),step.label,{project_type:'auto',ext_types:['linters','type_checkers','testers','security']});
    else if(step.endpoint)id=await start(step.endpoint+qs({path:cur}),step.label);
  }catch(e){set('failed','failed');if(step.action==='security-scan'||step.action==='check-deps')noteTask('failed',step.label);throw e}
  if(!id){set('','skipped');return 'skipped'}
  const st=await waitTask(id);set(st==='completed'?'done':'failed',st);return st}

$('runRecommended').onclick=async()=>{
  const rows=[...$('recommended').querySelectorAll('.opt')].filter(r=>r._box.checked);
  if(!rows.length)return toast('Tick at least one step');
  $('runRecommended').disabled=true;
  try{for(const r of rows){const st=await runStep(r._step,r);if(st==='failed'){toast('Stopped: "'+r._step.label+'" failed - see Output',1);break}}}
  catch(e){toast(e.message,1)}finally{$('runRecommended').disabled=false}};

function formatProjectCheck(r){
  const lines=[];
  const missing=r.missing||[];
  lines.push(missing.length?('Missing files and folders ('+missing.length+')'):'No missing files or folders.');
  missing.forEach(item=>lines.push('  '+item.kind+'  '+item.path+'  — '+item.reason));
  const updates=r.updates||[];
  lines.push(updates.length?('Updates ('+updates.length+')'):'No updates.');
  updates.forEach(item=>{
    const now=item.installed||'not installed';
    lines.push('  '+(item.safe?'safe':'held')+'  '+item.name+'  '+now+' -> '+(item.version||'held back')+'  — '+item.reason);
  });
  (r.downloads||[]).forEach(item=>lines.push(item.downloaded?('  saved  '+item.path+'  — '+item.reason):('  skipped  '+item.name+'  — '+item.reason)));
  if((r.downloads||[]).length)lines.push('Saved files are not installed.');
  return lines.join('\n')}
async function checkProjectFiles(){
  if(!cur)return toast('Select a project first',1);
  log('Checking missing files, folders, and updates…',1);
  log(formatProjectCheck(await api('/project-check',{method:'POST',body:{path:cur}})))}
async function downloadSafeUpdates(){
  if(!cur)return toast('Select a project first',1);
  if(!confirm('Download updates that passed the safety check into .setup-manager/updates?\n\nOnly official wheels and npm tarballs with a matching checksum are saved. Nothing is installed.'))return;
  log('Downloading safe updates…',1);
  log(formatProjectCheck(await api('/project-check/download',{method:'POST',body:{path:cur}})))}
function buildUtilities(){
  const u=$('utilities');u.replaceChildren();u.className='row';
  [['📦 Outdated packages','/task/outdated'],['🔑 Generate .env','/task/generate-env'],['🪝 Git hooks','/task/setup-hooks']].forEach(([l,ep])=>{
    const b=mk('button','b',l);b.onclick=()=>start(ep,l,{path:cur}).catch(e=>toast(e.message,1));u.append(b)});
  const check=mk('button','b','📂 Check files and updates');
  check.onclick=()=>checkProjectFiles().catch(e=>toast(e.message,1));
  const save=mk('button','b','⬇ Download safe updates');
  save.title='Saves official packages whose checksum matches. Does not install them.';
  save.onclick=()=>downloadSafeUpdates().catch(e=>toast(e.message,1));
  u.append(check,save)}

// ---- Doctor ----
async function checkDeps(){
  if(!cur)return toast('Select a project first',1);
  log('🔍 Looking for missing dependencies and files…',1);
  const r=await api('/doctor/dependencies',{method:'POST',body:{path:cur}});
  const mark=f=>f.status==='ok'?'✅ ':f.status==='warn'?'⚠️ ':'❌ ';
  log((r.findings||[]).map(f=>mark(f)+f.title+(f.detail?'  — '+f.detail:'')+(f.fix?'\n     → '+f.fix:'')).join('\n')||'Nothing to report.',1)}
$('checkDeps').onclick=()=>checkDeps().catch(e=>toast(e.message,1));
$('repairs').onclick=async()=>{if(!cur)return toast('Select a project first',1);try{
  log('🩺 Looking for problems the doctor can fix…',1);
  const r=await api('/doctor/run',{method:'POST',body:{path:cur}});const repairs=r.repairs||[];
  if(!repairs.length)return log('Nothing for the doctor to repair.',1);
  for(const rep of repairs){if(!(await approve({action:'repair',repair_id:rep.id},rep.title)))continue;
    const res=await api('/doctor/repair',{method:'POST',body:{path:cur,repair_id:rep.id}});
    log((res.changed?'✅ ':'ℹ️ ')+res.message+(res.learned?'  (remembered this fix)':''))}}catch(e){toast(e.message,1)}};
$('diagnose').onclick=async()=>{if(!cur)return toast('Select a project first',1);try{const r=await api('/diagnose',{method:'POST',body:{path:cur}});log(JSON.stringify(r,null,2),1)}catch(e){toast(e.message,1)}};

// ---- Dry run: installs and repairs show their exact commands and wait for Approve ----
function planFor(url){
  const p=url.split('?')[0];let m;
  if((m=p.match(/^\/task\/install-dependencies\/(\w+)/)))return{action:'install-dependencies',project_type:m[1]};
  if((m=p.match(/^\/task\/setup\/(\w+)/)))return{action:'setup',project_type:m[1]};
  if(p==='/task/install-missing')return{action:'install-missing'};
  if(p==='/task/extensions')return{action:'extensions'};
  return null}
async function approve(plan,title){
  const r=await api('/plan',{method:'POST',body:{...plan,path:cur}});
  $('approveTitle').textContent=title||'Approve?';$('approvePath').textContent=r.path;
  $('approveCmds').textContent=r.commands.length?r.commands.join('\n'):'(no shell commands - see below)';
  $('approveNotes').textContent=r.notes.join(' ');
  const d=$('approve');d.showModal();
  return new Promise(ok=>{$('approveYes').onclick=()=>{d.close();ok(true)};$('approveNo').onclick=()=>{d.close();ok(false)};d.oncancel=()=>ok(false)})}
// Starts a task and returns its id (null when the user cancelled the preview).
async function start(url,label,body){
  if($('popoutAuto').checked)openOutputWindow();
  const plan=planFor(url);
  if(plan&&!(await approve(plan,label||url))){toast('Cancelled - nothing was run');return null}
  // Do not auto-prompt for desktop notifications; notify() still runs if permission was already granted.
  const r=await api(url,{method:'POST',body});label=label||url;log('▶ '+label+'…',1);guideState={activity:'running',task_label:label,task_status:'running'};refreshGuide();follow(r.task_id,label);refreshHistory();return r.task_id}
function follow(id,label){
  if(ws)ws.close();clearInterval(poll);taskId=id;curLabel=label;$('cancel').disabled=false;$('runlbl').textContent='— '+label;resetProgress();
  const proto=location.protocol==='https:'?'wss':'ws';ws=new WebSocket(proto+'://'+location.host+'/ws/logs/'+id+'?token='+encodeURIComponent(TOKEN));
  ws.onmessage=e=>{
    if(e.data.startsWith('[status] ')){const st=e.data.slice(9);done();noteTask(st,label);notify(st);toast(st==='completed'?'✅ Finished':st==='cancelled'?'🛑 Cancelled':'❌ Failed',st==='failed');refreshHistory()}
    else{log(e.data);const pm=e.data.match(/\[(\d+)\/(\d+)\]/);if(pm)updateProgress(parseInt(pm[1])/parseInt(pm[2])*100);else{const p2=e.data.match(/(\d+)%/);if(p2)updateProgress(parseInt(p2[1]))}}};
  ws.onerror=()=>{poll=setInterval(async()=>{try{const t=await api('/task/'+id);$('log').textContent=t.logs.join('\n');syncPopout();if(t.progress!=null)updateProgress(t.progress);if(t.status!=='running'){clearInterval(poll);done();noteTask(t.status,label);refreshHistory()}}catch{clearInterval(poll)}},1500)}}
function done(){taskId=null;$('cancel').disabled=true;resetProgress()}
async function refreshHistory(){
  try{history=await api('/tasks');const h=$('hist');h.replaceChildren();if(!history.length){h.textContent='None yet.';return}
    history.slice(0,10).forEach(t=>{const d=mk('div','hist');const dot=mk('span','dot '+t.status);const s=mk('span',null,t.label||t.task_id.slice(0,8));
      const m=mk('span','muted',new Date(t.created_at).toLocaleTimeString());m.style.marginLeft='auto';
      d.append(dot,s,m);d.onclick=async()=>{const r=await api('/task/'+t.task_id+'/logs');log(r.logs.join('\n'),1);$('runlbl').textContent='— '+(t.label||'')};h.append(d)})}catch{}}

$('send').onclick=async()=>{const m=$('msg').value.trim();if(!m)return;$('msg').value='';
  const add=(c,t)=>{const d=mk('div','msg '+c,t);$('chat').append(d);$('chat').scrollTop=1e9;return d};
  add('u',m);const w=add('a','…');try{const r=await api('/ai/chat',{method:'POST',body:{message:m,path:cur,include_context:!!cur}});w.textContent=r.reply||JSON.stringify(r)}catch(e){w.textContent='⚠ '+e.message}};
$('msg').addEventListener('keydown',e=>{if(e.key==='Enter')$('send').click()});
$('runcmd').onclick=()=>{const c=$('cmd').value.trim();if(!cur)return toast('Select a project first',1);if(!c)return;
  if(confirm('Run this command in '+cur+'?\n\n'+c))start('/task/custom',c,{command:c,cwd:cur,confirm:true}).catch(e=>toast(e.message,1))};
$('doctor').onclick=async()=>{
  const r=await api('/system/doctor'+(cur?('?path='+encodeURIComponent(cur)):''));
  const lines=[];
  Object.entries(r.tools).forEach(([k,v])=>{
    if(v.status==='ready') lines.push('✅ '+k+'  '+v.path);
    else if(v.status==='missing') lines.push('❌ '+k+'  '+v.reason+'  → '+v.url);
  });
  const skipped=Object.entries(r.tools).filter(([,v])=>v.status==='not_needed').map(([k])=>k);
  if(skipped.length) lines.push((cur?'Not required for this project: ':'Not required until a project needs them: ')+skipped.join(', '));
  log(lines.join('\n'),1)};
$('cancel').onclick=async()=>{if(!taskId)return;try{await api('/task/'+taskId+'/cancel',{method:'POST'});toast('Cancelling…')}catch(e){toast(e.message,1)}};
$('open').onclick=()=>{const p=$('manual').value.trim();if(p)select(p)};
$('browse').onclick=async()=>{try{const r=await api('/pick-folder',{method:'POST'});if(r.path){$('manual').value=r.path;select(r.path)}}catch(e){toast(e.message,1)}};
$('setroot').onclick=async()=>{const p=$('manual').value.trim();
  if(!confirm(p?'Use this folder as the projects root?\n\n'+p:'Go back to the default projects root?'))return;
  try{const r=await api('/settings/safe-root',{method:'POST',body:{path:p}});$('root').textContent='root: '+r.safe_root;toast('Projects root: '+r.safe_root);loadProjects();statusBar();showSafetyPrompt(r.safe_root)}catch(e){toast(e.message,1)}};
$('explorer').onclick=async()=>{if(!cur)return;try{await api('/open-folder',{method:'POST',body:{path:cur}})}catch(e){toast(e.message,1)}};
$('refresh').onclick=loadProjects;$('clear').onclick=()=>log('',1);
$('popout').onclick=()=>openOutputWindow();
try{$('popoutAuto').checked=localStorage.getItem('sm-popout')==='1'}catch{}
$('popoutAuto').onchange=()=>{try{localStorage.setItem('sm-popout',$('popoutAuto').checked?'1':'0')}catch{}};
$('privacyBtn').onclick=async()=>{
  const btn=$('privacyBtn');const box=$('privacyResults');
  btn.disabled=true;btn.textContent='Checking…';box.replaceChildren();
  try{
    const r=await api('/privacy-check',{method:'POST',body:{}});
    const summary=mk('div');summary.style.fontWeight='600';summary.textContent=r.summary||'';
    box.append(summary);
    (r.findings||[]).forEach(item=>{
      const row=mk('div','settings-row');
      const title=mk('div');title.style.fontWeight='600';title.textContent=(item.status==='warn'?'Tracking program: ':'Remote control: ')+item.title+' ('+item.where+')';
      const detail=mk('div','setting-desc');detail.textContent=item.detail||'';
      row.append(title,detail);box.append(row);
    });
    const list=mk('div','hint');
    (r.checklist||[]).forEach(line=>{const p=mk('div');p.textContent=line;list.append(p)});
    box.append(list);
  }catch(e){toast(e.message,1)}
  finally{btn.disabled=false;btn.textContent='Check this PC'}
};
// Sneaky file watch: off until the user starts it. The page only polls while
// it runs (plus once on load), and stops polling while hidden; the server then
// stops the watch by itself after about a minute without a poll.
const FILE_WATCH_REMIND_SECONDS=30*60;
let fileWatchKey=null,fileWatchTimer=null,fileWatchRunning=false,fileWatchRemindedFor=null,fileWatchBusy=false;
function fileWatchMinutes(seconds){const m=Math.round(seconds/60);return m===1?'1 minute':m+' minutes'}
function renderFileWatch(r){
  const wasRunning=fileWatchRunning;
  fileWatchRunning=!!r.running;
  const status=$('fileWatchStatus');
  if(status){
    let text=r.summary||'';
    if(r.running)text='Running for '+fileWatchMinutes(r.running_seconds||0)+'. '+text;
    status.textContent=text;
  }
  const btn=$('fileWatchToggle');
  btn.textContent=r.running?'Stop watching':'Start watching';
  btn.classList.toggle('pri',!r.running);
  btn.disabled=!!r.disabled&&!r.running;
  $('fileWatchBanner').hidden=!r.running;
  if(wasRunning&&!r.running&&r.auto_stopped)toast('File watch stopped automatically. '+(r.stopped_reason||''));
  if(r.running&&(r.running_seconds||0)>=FILE_WATCH_REMIND_SECONDS&&fileWatchRemindedFor!==r.started_at){
    fileWatchRemindedFor=r.started_at;
    $('fileWatchReminderTime').textContent=fileWatchMinutes(r.running_seconds);
    const d=$('fileWatchReminder');
    if(!d.open){if(d.showModal)d.showModal();else d.setAttribute('open','')}
  }
  if(!r.running)closeFileWatchReminder();
  const alerts=r.alerts||[];
  const key=alerts.map(item=>item.id).join(',');
  if(key===fileWatchKey)return;
  const known=new Set(fileWatchKey?fileWatchKey.split(',').filter(Boolean):[]);
  const first=fileWatchKey===null;
  fileWatchKey=key;
  const box=$('fileWatchAlerts');
  box.replaceChildren();
  if(!alerts.length){if(r.running)box.append(mk('div','hint','Nothing that can run a program has shown up yet.'));return}
  alerts.forEach(item=>{
    const row=mk('div','settings-row');
    const title=mk('div');title.style.fontWeight='600';
    const note=item.kind==='note';
    title.style.color=note?'var(--muted)':(item.when==='already'?'var(--warnc)':'var(--bad)');
    const label=note?'':(item.when==='already'?'Already here: ':(item.kind==='program'?'Program started: ':'New file: '));
    title.textContent=label+item.title;
    const detail=mk('div','setting-desc');detail.textContent=item.detail||'';
    row.append(title,detail);
    if(item.where){const where=mk('div','setting-desc');where.textContent=item.where;row.append(where)}
    box.append(row);
    if(!first && !note && item.when!=='already' && item.id && !known.has(item.id)) toast(title.textContent,1);
  });
}
function scheduleFileWatch(){
  const want=fileWatchRunning&&!document.hidden;
  if(want&&!fileWatchTimer)fileWatchTimer=setInterval(pollFileWatch,8000);
  if(!want&&fileWatchTimer){clearInterval(fileWatchTimer);fileWatchTimer=null}
}
async function pollFileWatch(){
  try{renderFileWatch(await api('/file-watch'))}
  catch(e){
    const status=$('fileWatchStatus');
    if(status) status.textContent='The watch could not be read. '+e.message;
  }
  scheduleFileWatch();
}
async function setFileWatch(on){
  if(fileWatchBusy)return;
  fileWatchBusy=true;$('fileWatchToggle').disabled=true;
  try{renderFileWatch(await api(on?'/file-watch/start':'/file-watch/stop',{method:'POST',body:{}}))}
  catch(e){toast(e.message,1)}
  finally{fileWatchBusy=false;$('fileWatchToggle').disabled=false;await pollFileWatch()}
}
function closeFileWatchReminder(){const d=$('fileWatchReminder');if(d.open){if(d.close)d.close();else d.removeAttribute('open')}}
$('fileWatchToggle').addEventListener('click',()=>setFileWatch(!fileWatchRunning));
$('fileWatchBannerStop').addEventListener('click',()=>setFileWatch(false));
$('fileWatchReminderStop').addEventListener('click',()=>{closeFileWatchReminder();setFileWatch(false)});
$('fileWatchKeep').addEventListener('click',closeFileWatchReminder);
$('fileWatchReminder').addEventListener('cancel',e=>{e.preventDefault();closeFileWatchReminder()});
document.addEventListener('visibilitychange',()=>{if(document.hidden)scheduleFileWatch();else pollFileWatch()});
pollFileWatch();
$('copy').onclick=()=>navigator.clipboard.writeText($('log').textContent).then(()=>toast('Copied'));
$('dl').onclick=()=>{const a=mk('a');a.href=URL.createObjectURL(new Blob([$('log').textContent],{type:'text/plain'}));a.download='setup-manager-log.txt';a.click()};
let t;$('q').oninput=()=>{clearTimeout(t);t=setTimeout(()=>{loadProjects();lookFor()},250)};
async function lookFor(){
  const q=$('q').value.trim();const box=$('where');
  if(q.length<2){box.replaceChildren();return}
  try{
    const r=await api('/where'+qs({q}));
    box.replaceChildren();
    if(r.looking)$('lookline').textContent=r.looking;
    if(!r.matches.length){box.append(mk('p','hint','No folder with that name in those places.'));return}
    r.matches.forEach(m=>{const b=mk('button','proj');b.append(mk('b',null,m.name),mk('small',null,m.note),mk('small',null,m.path));b.onclick=()=>select(m.path);box.append(b)})
  }catch(e){toast(e.message,1)}}
addEventListener('keydown',e=>{if(e.key==='/'&&!/INPUT|TEXTAREA/.test(document.activeElement.tagName)){e.preventDefault();$('q').focus()}});

// Online search
$('searchBtn').onclick=runSearch;
$('searchInput').addEventListener('keydown',e=>{if(e.key==='Enter')runSearch()});
async function runSearch(){
  const query=$('searchInput').value.trim();
  if(!query){toast('Enter a search query',1);return}
  const results=$('searchResults');
  results.innerHTML='<div class="muted">🔍 Searching…</div>';
  try{
    const r=await api('/search',{method:'POST',body:{query,source:'auto'}});
    results.innerHTML='';
    const sources=r.sources||{};
    for(const [name,info] of Object.entries(sources)){
      const div=document.createElement('div');
      div.className='settings-row';
      const label=document.createElement('div');
      const title=document.createElement('div');
      title.textContent=name==='pypi'?'🐍 PyPI':name==='npm'?'📦 npm':name==='crates'?'🦀 crates.io':'📚 Documentation';
      title.style.fontWeight='600';
      label.appendChild(title);
      if(info.package){
        const desc=document.createElement('div');
        desc.className='setting-desc';
        const parts=[];
        if(info.package.name)parts.push(info.package.name);
        if(info.package.version)parts.push('v'+info.package.version);
        if(info.package.summary||info.package.description)parts.push(info.package.summary||info.package.description);
        desc.textContent=parts.join(' — ');
        label.appendChild(desc);
      }
      div.appendChild(label);
      const btn=document.createElement('button');
      btn.className='b';
      btn.textContent='Open';
      btn.onclick=()=>window.open(info.url,'_blank');
      div.appendChild(btn);
      if(info.package&&info.package.homepage){
        const homeBtn=document.createElement('button');
        homeBtn.className='b';
        homeBtn.textContent='Homepage';
        homeBtn.onclick=()=>window.open(info.package.homepage,'_blank');
        div.appendChild(homeBtn);
      }
      results.appendChild(div);
    }
    if(!Object.keys(sources).length){
      results.innerHTML='<div class="muted">No results found.</div>';
    }
  }catch(e){
    results.innerHTML='<div class="warn">❌ Search failed: '+escapeHTML(e.message)+'</div>';
    toast(e.message,1);
  }
}
// Settings management
async function loadSettings(){
  try{
    const settings=await api('/settings');
    renderSettings(settings);
  }catch(e){
    $('settingsBody').innerHTML='<div class="muted">Could not load settings.</div>';
  }
}

function renderSettings(settings){
  const body=$('settingsBody');
  body.replaceChildren();

  // Build settings
  const buildSection=document.createElement('div');
  buildSection.className='settings-section';
  buildSection.innerHTML='<h3>🔨 Build</h3>';
  buildSection.append(
    createSettingRow('Auto-install dependencies', 'Automatically install dependencies before building', 'checkbox', settings.build.auto_install_deps, 'build', 'auto_install_deps'),
    createSettingRow('Parallel builds', 'Run builds in parallel when possible', 'checkbox', settings.build.parallel_builds, 'build', 'parallel_builds'),
    createSettingRow('Clean before build', 'Clean build artifacts before each build', 'checkbox', settings.build.clean_before_build, 'build', 'clean_before_build'),
    createSettingRow('Build timeout (seconds)', 'Maximum time allowed for build operations', 'number', settings.build.build_timeout, 'build', 'build_timeout', {min:30, max:3600, step:30})
  );
  body.append(buildSection);

  // Execution settings
  const execSection=document.createElement('div');
  execSection.className='settings-section';
  execSection.innerHTML='<h3>▶️ Execution</h3>';
  execSection.append(
    createSettingRow('Auto-open browser', 'Automatically open browser when starting web apps', 'checkbox', settings.execution.auto_open_browser, 'execution', 'auto_open_browser'),
    createSettingRow('Notifications', 'Show desktop notifications when tasks complete', 'checkbox', settings.execution.notify_on_complete, 'execution', 'notify_on_complete'),
    createSettingRow('Max concurrent tasks', 'Maximum number of simultaneous tasks', 'number', settings.execution.max_concurrent_tasks, 'execution', 'max_concurrent_tasks', {min:1, max:16, step:1})
  );
  body.append(execSection);

  // Deployment settings
  const deploySection=document.createElement('div');
  deploySection.className='settings-section';
  deploySection.innerHTML='<h3>🚀 Deployment</h3>';
  deploySection.append(
    createSettingRow('Build Docker on deploy', 'Automatically build Docker images when deploying', 'checkbox', settings.deployment.docker_build_on_deploy, 'deployment', 'docker_build_on_deploy'),
    createSettingRow('Tag as latest', 'Always tag Docker images with :latest', 'checkbox', settings.deployment.docker_tag_latest, 'deployment', 'docker_tag_latest'),
    createSettingRow('Health check', 'Enable health checks after deployment', 'checkbox', settings.deployment.health_check_enabled, 'deployment', 'health_check_enabled')
  );
  body.append(deploySection);

  // Actions
  const actions=document.createElement('div');
  actions.className='settings-actions';
  const resetBtn=document.createElement('button');
  resetBtn.className='b';
  resetBtn.textContent='🔄 Reset to defaults';
  resetBtn.onclick=resetAllSettings;
  actions.append(resetBtn);
  body.append(actions);
}

function createSettingRow(label, description, type, value, category, key, options={}){
  const row=document.createElement('div');
  row.className='settings-row';

  const labelEl=document.createElement('label');
  labelEl.innerHTML=`<div>${escapeHTML(label)}</div><div class="setting-desc">${escapeHTML(description)}</div>`;
  row.append(labelEl);

  let control;
  if(type==='checkbox'){
    control=document.createElement('input');
    control.type='checkbox';
    control.checked=value;
    control.onchange=async()=>{
      await saveSetting(category, key, control.checked);
      toast('✅ Setting saved');
    };
  }else if(type==='number'){
    control=document.createElement('input');
    control.type='number';
    control.value=value;
    if(options.min) control.min=options.min;
    if(options.max) control.max=options.max;
    if(options.step) control.step=options.step;
    control.onchange=async()=>{
      await saveSetting(category, key, parseInt(control.value));
      toast('✅ Setting saved');
    };
  }

  if(control){
    row.append(control);
  }

  return row;
}

async function saveSetting(category, key, value){
  try{
    await api('/settings',{
      method:'POST',
      body:{category, settings:{[key]: value}}
    });
  }catch(e){
    toast('❌ Failed to save: '+e.message, true);
  }
}

async function resetAllSettings(){
  if(confirm('Reset all settings to defaults?')){
    try{
      await api('/settings/reset',{method:'POST'});
      await loadSettings();
      toast('✅ Settings reset to defaults');
    }catch(e){
      toast('❌ Failed to reset: '+e.message, true);
    }
  }
}

// Security scan
let lastSecurityReport=null;
async function runSecurityScan(path){
  const target=typeof path==='string'?path:cur;
  if(!target){toast('Select a project first',true);return false}
  const btn=$('securityScanBtn');
  const body=$('securityBody');
  btn.disabled=true;
  btn.textContent='⏳ Scanning…';
  body.innerHTML='';
  body.append(btn);

  try{
    const r=await api('/security-scan',{method:'POST',body:{path:target}});
    renderSecurityReport(r);
    return true;
  }catch(e){
    body.innerHTML=`<div class="muted" style="text-align:center;padding:20px">❌ Scan failed: ${escapeHTML(e.message)}</div>`;
    return false;
  }finally{
    btn.disabled=false;
    btn.textContent='🔍 Scan this project';
  }
}

function renderSecurityReport(r){
  const body=$('securityBody');
  body.replaceChildren();

  const report=document.createElement('div');
  report.className='security-report';

  // Determine risk class
  const riskClass = r.risk_band || (r.risk_score >= 50 ? 'critical' : r.risk_score >= 30 ? 'high' : r.risk_score >= 15 ? 'medium' : r.risk_score >= 1 ? 'low' : 'clean');

  // Header with risk score
  const header=document.createElement('div');
  header.className=`security-header ${riskClass}`;
  header.innerHTML=`
    <h3>🛡️ Security Scan Results</h3>
    <div class="risk-score">${escapeHTML(r.risk_score || 0)}</div>
    <div class="risk-level">${escapeHTML(r.risk_level || '✅ CLEAN')}</div>
    ${r.cap_reason ? `<div class="scan-meta">${escapeHTML(r.cap_reason)}</div>` : ''}
    <div class="scan-meta">${escapeHTML(r.files_scanned)} files scanned · ${escapeHTML(r.total_findings || 0)} findings${r.truncated ? ' (truncated)' : ''}</div>
  `;
  report.append(header);

  // Malware scan status (Defender)
  const malware=r.malware_scan||{};
  if(malware.status){
    const malDiv=document.createElement('div');
    const malClass = malware.status === 'clean' ? 'clean' : malware.status === 'threats' ? 'threats' : malware.status === 'error' ? 'error' : 'unavailable';
    malDiv.className=`security-malware-status ${malClass}`;
    const malIcon = malware.status === 'clean' ? '✅' : malware.status === 'threats' ? '🚨' : malware.status === 'error' ? '⚠️' : 'ℹ️';
    malDiv.textContent=`${malIcon} Microsoft Defender: ${malware.message || malware.status}`;
    report.append(malDiv);
  }

  // Category summary
  const catSummary=r.category_summary||{};
  if(Object.keys(catSummary).length){
    const catTitle=document.createElement('h3');
    catTitle.style.cssText='font-size:13px;font-weight:600;text-transform:uppercase;letter-spacing:0.04em;color:var(--muted);margin:4px 0 8px';
    catTitle.textContent='📊 Findings by Category';
    report.append(catTitle);

    const catGrid=document.createElement('div');
    catGrid.className='security-categories';
    for(const [label, info] of Object.entries(catSummary)){
      const catDiv=document.createElement('div');
      catDiv.className='security-cat';
      const icon=label.split(' ')[0]||'⚠️';
      const name=label.split(' ').slice(1).join(' ')||label;
      catDiv.innerHTML=`
        <span class="cat-icon">${escapeHTML(icon)}</span>
        <div class="cat-info">
          <div class="cat-name">${escapeHTML(name)}</div>
          <div class="cat-count">${escapeHTML(info.count)} finding${info.count!==1?'s':''}</div>
        </div>
        <span class="cat-severity ${escapeHTML(info.worst_severity)}">${escapeHTML(String(info.worst_severity).toUpperCase())}</span>
      `;
      catGrid.append(catDiv);
    }
    report.append(catGrid);
  }

  // Suspicious files
  const suspFiles=r.suspicious_files||[];
  if(suspFiles.length){
    const suspTitle=document.createElement('h3');
    suspTitle.style.cssText='font-size:13px;font-weight:600;text-transform:uppercase;letter-spacing:0.04em;color:var(--muted);margin:4px 0 8px';
    suspTitle.textContent='⚠️ Suspicious Files';
    report.append(suspTitle);
    suspFiles.forEach(sf=>{
      const sfDiv=document.createElement('div');
      sfDiv.className='security-rec high';
      sfDiv.textContent=`📁 ${sf.file} — ${sf.reason}`;
      report.append(sfDiv);
    });
  }

  // Recommendations
  const recs=r.recommendations||[];
  if(recs.length){
    const recTitle=document.createElement('h3');
    recTitle.style.cssText='font-size:13px;font-weight:600;text-transform:uppercase;letter-spacing:0.04em;color:var(--muted);margin:4px 0 8px';
    recTitle.textContent='💡 Recommendations';
    report.append(recTitle);

    const recsDiv=document.createElement('div');
    recsDiv.className='security-recs';
    recs.forEach(rec=>{
      const recDiv=document.createElement('div');
      const recClass = rec.includes('CRITICAL') ? 'critical' : rec.includes('HIGH') ? 'high' : rec.includes('MEDIUM') ? 'medium' : 'low';
      recDiv.className=`security-rec ${recClass}`;
      recDiv.textContent=rec;
      recsDiv.append(recDiv);
    });
    report.append(recsDiv);
  }

  // Detailed findings (collapsible)
  const findings=r.findings||[];
  if(findings.length){
    const findTitle=document.createElement('div');
    findTitle.className='group-toggle';
    findTitle.style.cssText='margin-top:8px';
    findTitle.innerHTML=`<span class="arrow">▶</span> Detailed Findings <span class="muted" style="font-weight:400;text-transform:none;font-size:12px">(${findings.length})</span>`;
    const findBody=document.createElement('div');
    findBody.className='group-body security-findings';
    findTitle.onclick=()=>{
      findTitle.classList.toggle('open');
      findBody.classList.toggle('open');
    };

    findings.slice(0, 100).forEach(f=>{
      const fDiv=document.createElement('div');
      fDiv.className='security-finding';
      fDiv.innerHTML=`<span class="finding-rule">${escapeHTML(f.rule)}</span> <span class="finding-severity ${escapeHTML(f.severity)}">${escapeHTML(f.severity)}</span> <span class="finding-file">${escapeHTML(f.file)}:${escapeHTML(f.line)}</span> — ${escapeHTML(f.message)}`;
      if(f.fix_hint){
        const hint=document.createElement('div');
        hint.className='finding-file';
        hint.textContent=f.fix_hint;
        fDiv.append(hint);
      }
      findBody.append(fDiv);
    });
    if(findings.length > 100){
      const more=document.createElement('div');
      more.className='muted';
      more.style.cssText='padding:8px 10px;font-size:12px';
      more.textContent=`... and ${findings.length - 100} more findings`;
      findBody.append(more);
    }
    report.append(findTitle, findBody);
  }

  // Scope note
  if(r.scope){
    const scope=document.createElement('div');
    scope.className='muted';
    scope.style.cssText='font-size:11px;text-align:center;margin-top:8px;padding:8px;border-top:1px solid var(--line)';
    scope.textContent=r.scope;
    report.append(scope);
  }

  const fixable=findings.filter(f=>f.fixable).length;
  if(findings.length){
    const hint=document.createElement('div');
    hint.className='hint';
    hint.textContent=fixable
      ? (fixable+' finding'+(fixable===1?'':'s')+' can be fixed in the project files. Anything left stays listed, because changing it would change how the code runs.')
      : 'Fix these is off. None of these can be rewritten safely. The note on a line says why.';
    report.append(hint);
  }
  if(r.fix_result){
    const applied=(r.fix_result.applied||[]).length;
    const note=document.createElement('div');
    note.className='hint';
    note.textContent=applied
      ? ('Updated '+r.fix_result.files_changed+' file'+(r.fix_result.files_changed===1?'':'s')+'. '+applied+' shell=True call'+(applied===1?'':'s')+' now use shell=False.')
      : 'No files were changed.';
    report.append(note);
  }

  const actions=document.createElement('div');
  actions.className='row';
  actions.style.marginTop='8px';
  if(findings.length){
    const fixBtn=document.createElement('button');
    fixBtn.className='b pri';
    fixBtn.textContent=fixable?('🔧 Fix these ('+fixable+')'):'🔧 Fix these';
    fixBtn.disabled=!fixable;
    fixBtn.title=fixable?'Rewrite the safe findings in this project':'Nothing here can be rewritten without changing how the code runs';
    fixBtn.onclick=fixSecurityFindings;
    actions.append(fixBtn);
  }
  const btn=document.createElement('button');
  btn.className='b';
  btn.id='securityScanBtn';
  btn.textContent='🔍 Re-scan project';
  btn.onclick=runSecurityScan;
  actions.append(btn);
  report.append(actions);

  body.append(report);
  lastSecurityReport=r;
}

async function fixSecurityFindings(){
  if(!cur){toast('Select a project first',true);return}
  const fixable=((lastSecurityReport&&lastSecurityReport.findings)||[]).filter(f=>f.fixable).length;
  if(!fixable){toast('Nothing here can be rewritten safely',true);return}
  if(!confirm('Apply '+fixable+' safe fix'+(fixable===1?'':'es')+' in this project? This edits source files.'))return;
  const body=$('securityBody');
  body.innerHTML='<div class="muted" style="text-align:center;padding:20px">⏳ Applying safe fixes…</div>';
  try{
    const r=await api('/security-scan/fix',{method:'POST',body:{path:cur}});
    renderSecurityReport(r);
    const n=(r.fix_result&&r.fix_result.applied||[]).length;
    toast(n?('Fixed '+n+' finding'+(n===1?'':'s')):'No files changed');
  }catch(e){
    toast(e.message,true);
    runSecurityScan();
  }
}

// Safety reminder when a folder is opened.
// "Don't show again" is keyed by path plus folder_id, which changes on a re-clone.
let safetyPath='', safetyId='';
const safetyKey=(path,id)=>'sm.safetyPrompt.hide:'+path+'|'+(id||'');
function showSafetyPrompt(path,id){
  if(!path) return;
  try{if(localStorage.getItem(safetyKey(path,id))==='1') return}catch{}
  safetyPath=path;safetyId=id||'';
  $('safetyFolder').textContent=path;
  $('safetyDontShow').checked=false;
  const d=$('safetyModal');
  if(!d.open){if(d.showModal)d.showModal();else d.setAttribute('open','')}
  $('safetyScan').focus();
}
function closeSafetyPrompt(){
  if($('safetyDontShow').checked){try{localStorage.setItem(safetyKey(safetyPath,safetyId),'1')}catch{}}
  const d=$('safetyModal');
  if(d.close)d.close();else d.removeAttribute('open');
}
$('safetyLater').addEventListener('click',closeSafetyPrompt);
$('safetyModal').addEventListener('cancel',e=>{e.preventDefault();closeSafetyPrompt()});
$('safetyScan').addEventListener('click',()=>{
  const path=safetyPath;
  closeSafetyPrompt();
  runSecurityScan(path);
  const box=$('securityBody');if(box.scrollIntoView)box.scrollIntoView({behavior:'smooth',block:'start'});
});
$('securityScanBtn').addEventListener('click',()=>runSecurityScan());

// Fix Dependencies
$('depsDiagnoseBtn').onclick = diagnoseDependencies;
$('depsFixBtn').onclick = fixDependencies;

function diagnosisSummary(summary) {
  if (summary == null || summary === '') return 'No issues found.';
  if (typeof summary === 'string') return summary;
  const fail = Number(summary.fail || 0);
  const warn = Number(summary.warn || 0);
  if (!fail && !warn) return 'Nothing is missing for this program to run.';
  const parts = [];
  if (fail) parts.push(fail + ' problem' + (fail === 1 ? '' : 's'));
  if (warn) parts.push(warn + ' note' + (warn === 1 ? '' : 's'));
  return parts.join(', ') + '.';
}

async function diagnoseDependencies() {
  if (!cur) { toast('Select a project first', true); return; }
  const btn = $('depsDiagnoseBtn');
  const results = $('depsResults');
  const fixBtn = $('depsFixBtn');
  btn.disabled = true;
  btn.textContent = '⏳ Diagnosing…';
  results.innerHTML = '<div class="muted">🔍 Checking dependencies…</div>';

  try {
    const r = await api('/dependencies/diagnose', { method: 'POST', body: { path: cur } });
    results.innerHTML = '';

    // Summary
    const summaryDiv = document.createElement('div');
    summaryDiv.className = 'settings-row';
    summaryDiv.style.background = 'var(--bg)';
    const summaryLabel = document.createElement('div');
    const summaryTitle = document.createElement('div');
    summaryTitle.style.fontWeight = '600';
    summaryTitle.textContent = '📊 Diagnosis Summary';
    const summaryDesc = document.createElement('div');
    summaryDesc.className = 'setting-desc';
    summaryDesc.textContent = diagnosisSummary(r.summary);
    summaryLabel.appendChild(summaryTitle);
    summaryLabel.appendChild(summaryDesc);
    summaryDiv.appendChild(summaryLabel);
    results.appendChild(summaryDiv);

    if (r.left_out && r.left_out.length) {
      const note = document.createElement('div');
      note.className = 'settings-row';
      const noteLabel = document.createElement('div');
      const noteTitle = document.createElement('div');
      noteTitle.style.fontWeight = '600';
      noteTitle.textContent = 'Left out, not needed to run';
      const noteDesc = document.createElement('div');
      noteDesc.className = 'setting-desc';
      noteDesc.textContent = r.left_out.join(', ');
      noteLabel.appendChild(noteTitle);
      noteLabel.appendChild(noteDesc);
      note.appendChild(noteLabel);
      results.appendChild(note);
    }

    // Python missing packages
    if (r.python_missing && r.python_missing.length) {
      r.python_missing.forEach(pkg => {
        const div = document.createElement('div');
        div.className = 'settings-row';
        const label = document.createElement('div');
        label.innerHTML = '<div style="font-weight:600">🐍 ' + escapeHTML(pkg) + '</div><div class="setting-desc">Declared Python dependency not installed</div>';
        div.appendChild(label);
        results.appendChild(div);
      });
    }

    // Node missing packages
    if (r.node_missing && r.node_missing.length) {
      r.node_missing.forEach(pkg => {
        const div = document.createElement('div');
        div.className = 'settings-row';
        const label = document.createElement('div');
        label.innerHTML = '<div style="font-weight:600">📦 ' + escapeHTML(pkg) + '</div><div class="setting-desc">Declared Node.js dependency not installed</div>';
        div.appendChild(label);
        results.appendChild(div);
      });
    }

    // Venv missing warning
    if (r.venv_missing) {
      const div = document.createElement('div');
      div.className = 'settings-row';
      div.style.borderColor = '#eab308';
      const label = document.createElement('div');
      label.innerHTML = '<div style="font-weight:600">⚠️ No virtual environment</div><div class="setting-desc">Python dependencies cannot be installed without a venv. "Fix All" will create one.</div>';
      div.appendChild(label);
      results.appendChild(div);
    }

    // Other problems (undeclared imports, etc.)
    if (r.other_problems && r.other_problems.length) {
      r.other_problems.forEach(prob => {
        const div = document.createElement('div');
        div.className = 'settings-row';
        const label = document.createElement('div');
        const icon = prob.type === 'undeclared_import' ? '📥' : '⚠️';
        label.innerHTML = '<div style="font-weight:600">' + escapeHTML(icon) + ' ' + escapeHTML(prob.title) + '</div><div class="setting-desc">' + escapeHTML(prob.detail || '') + '</div>';
        div.appendChild(label);
        if (prob.search_url) {
          const btn = document.createElement('button');
          btn.className = 'b';
          btn.textContent = 'Search';
          btn.onclick = () => window.open(prob.search_url, '_blank');
          div.appendChild(btn);
        }
        if (prob.fix) {
          const fixLabel = document.createElement('button');
          fixLabel.className = 'b';
          fixLabel.textContent = 'ℹ️';
          fixLabel.title = prob.fix;
          fixLabel.onclick = () => toast(prob.fix);
          div.appendChild(fixLabel);
        }
        results.appendChild(div);
      });
    }

    // Enable fix button if there's something to fix
    fixBtn.disabled = !r.can_auto_fix;
    if (!r.can_auto_fix && !r.python_missing?.length && !r.node_missing?.length) {
      const okDiv = document.createElement('div');
      okDiv.className = 'muted';
      okDiv.style.textAlign = 'center';
      okDiv.style.padding = '12px';
      okDiv.textContent = '✅ No auto-fixable dependency issues found.';
      results.appendChild(okDiv);
    }
  } catch (e) {
    results.innerHTML = '<div class="warn">❌ Diagnosis failed: ' + escapeHTML(e.message) + '</div>';
    toast(e.message, 1);
  } finally {
    btn.disabled = false;
    btn.textContent = '🔍 Diagnose';
  }
}

async function fixDependencies() {
  if (!cur) { toast('Select a project first', true); return; }
  if (!(await approve({action:'fix-dependencies'}, 'Fix missing dependencies'))) return toast('Cancelled - nothing was run');

  const fixBtn = $('depsFixBtn');
  const results = $('depsResults');
  fixBtn.disabled = true;
  fixBtn.textContent = '⏳ Fixing…';

  try {
    const r = await api('/dependencies/fix', { method: 'POST', body: { path: cur } });
    // Follow the task progress in the log area
    log('🔧 Fixing dependencies…', 1);
    follow(r.task_id, 'Fix dependencies');

    // Re-diagnose after task completes (polling)
    const checkDone = setInterval(async () => {
      try {
        const t = await api('/task/' + r.task_id);
        if (t.status !== 'running') {
          clearInterval(checkDone);
          fixBtn.disabled = false;
          fixBtn.textContent = '🔧 Fix All';
          // Re-run diagnosis to show updated status
          await diagnoseDependencies();
        }
      } catch {
        clearInterval(checkDone);
        fixBtn.disabled = false;
        fixBtn.textContent = '🔧 Fix All';
      }
    }, 2000);
  } catch (e) {
    results.innerHTML += '<div class="warn">❌ Fix failed: ' + escapeHTML(e.message) + '</div>';
    toast(e.message, 1);
    fixBtn.disabled = false;
    fixBtn.textContent = '🔧 Fix All';
  }
}
// One-line status bar: version, projects root, AI state.
let cfg=null,ai=null;
function statusBar(){
  const parts=[];if(cfg)parts.push('Setup Manager v'+cfg.version);
  parts.push('Root: '+($('root').textContent.replace('root: ','')||'?'));
  if(ai)parts.push(ai.state==='ready'?'AI ready ('+ai.model+')':(ai.message||'').split(' (')[0]);
  $('statusbar').textContent=parts.join('  ·  ')}
async function checkAi(){
  const previous=ai&&ai.state;
  try{ai=await api('/ai/status')}catch{ai={state:'offline',message:'AI offline - start Ollama'}}
  const ok=ai.state==='ready';$('ai').textContent='AI: '+(ok?'ready':ai.state==='no_model'?'model missing':'offline');
  $('ai').title=ai.message||'';$('ai').className='pill '+(ok?'ok':'bad');
  if(!ok&&!$('aiNote')){const n=document.createElement('div');n.id='aiNote';n.className='muted';$('chat').before(n)}
  if($('aiNote'))$('aiNote').textContent=ok?'':ai.message;
  statusBar();
  if(cur&&ai.state!==previous)refreshGuide()}
$('ai').style.cursor='pointer';$('ai').onclick=checkAi;
// Initialize app
(async()=>{try{cfg=await api('/api/config');$('ver').textContent='v'+cfg.version;$('root').textContent='root: '+cfg.safe_root;if(cfg.roots&&cfg.roots.length)$('lookline').textContent='Looking in '+cfg.roots.join(' and ')+'. Type a name if you are not sure which folder.'}catch(e){toast(e.message,1)}
  statusBar();checkAi();setInterval(checkAi,60000);
  refreshGuide();loadProjects();refreshHistory();loadSettings();
  if(cfg&&cfg.last_project)select(cfg.last_project)})();
