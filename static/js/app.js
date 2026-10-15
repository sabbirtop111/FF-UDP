/* GC administrator workspace: server-sourced data, no simulated Bot activity. */
'use strict';
const $ = id => document.getElementById(id);
const S = {accounts:[], summary:{}, breakdown:{}, recent:[], selected:new Set(), view:'overview', current:null, cursor:null, events:[], regions:['BD','ME','IND','SG','TH','PH','VN','MY','ID','HK','TW','PK','CIS','BR','NA','SAC','EUROPE'], active:false, busy:false, detailSnapshot:null, accountListSnapshot:null, scrollLocked:false, lockedScrollY:0};
const esc = v => String(v ?? '').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const num = v => Number(v ?? 0).toLocaleString();
const dt = v => {if(!v)return '—';const x=new Date(v);return Number.isNaN(+x)?'—':x.toLocaleString([], {dateStyle:'medium',timeStyle:'short'});};
const ago = v => {if(!v)return '—';const sec=Math.max(0,Math.floor((Date.now()-Date.parse(v))/1000));if(!Number.isFinite(sec))return '—';return sec<60?'Just now':sec<3600?`${Math.floor(sec/60)} min ago`:sec<86400?`${Math.floor(sec/3600)} hr ago`:dt(v);};
const remaining = v => {const sec=Math.max(0,Math.ceil((Date.parse(v)-Date.now())/1000));return Number.isFinite(sec)?`${String(Math.floor(sec/60)).padStart(2,'0')}:${String(sec%60).padStart(2,'0')}`:'—';};
const activeStates=new Set(['authenticating','online','searching_match','in_match','reconnecting']);
const statusText = a => {if(a.status==='in_match')return 'UDP joined';if(a.status==='cooldown')return 'Cooldown';if(a.status==='searching_match'&&['startup_sent','hello_received','ack_received'].includes(a.udp_phase))return 'Awaiting UDP JOIN';return ({searching_match:'Searching',online:'Gateway online',authenticating:'Authenticating',reconnecting:'Reconnecting',error:'Error',inactive:'Stopped'})[a.status] || 'Unknown';};
const phaseText = p => ({queue_sent:'Request sent',match_candidate:'Match candidate',startup_sent:'UDP startup sent',hello_received:'UDP HELLO',ack_received:'UDP ACK',joined:'UDP JOIN',join_timeout:'JOIN timeout',handshake_timeout:'Handshake timeout',idle_timeout:'UDP idle timeout',server_finish:'Server end'})[p] || 'No UDP phase';
const pill=a=>`<span class="state-pill" data-state="${esc(a.status)}"><i></i>${esc(statusText(a))}</span>`;
const safeId = a => esc(a.id);
const noData = msg => `<div class="empty-state">${esc(msg)}</div>`;
let toastTimer;
function toast(msg,error=false){const el=$('toast');el.textContent=String(msg);el.classList.toggle('is-error',error);el.classList.remove('is-hidden');clearTimeout(toastTimer);toastTimer=setTimeout(()=>el.classList.add('is-hidden'),5000);}
async function api(path,method='GET',body){const opts={method,credentials:'same-origin',headers:{}};if(body!==undefined){opts.headers['Content-Type']='application/json';opts.body=JSON.stringify(body);}let r;try{r=await fetch(path,opts);}catch(netErr){V17.noteError('network',`${method} ${path} could not reach the admin service.`,netErr?.message||'Network request failed');throw Error('Cannot reach the admin server. Open the running site at /admin, not an HTML preview.');}let data;try{data=await r.json();}catch{data={};}if(r.status===401&&!path.includes('/auth/login')){V17.noteError('session','Administrator session ended; sign-in required again.',path);showLogin();throw Error('Administrator session ended. Sign in again.');}if(!r.ok){const detail=typeof data.detail==='string'?data.detail:`Request failed (${r.status})`;V17.noteError('server',`${method} ${path} → ${detail}`,JSON.stringify(data?.errors||data?.detail||'').slice(0,400));throw Error(detail);}V17.clearTransient(path);return data;}
function showLogin(){S.active=false;V17.stop();$('login-screen').classList.remove('is-hidden');$('admin-shell').classList.add('is-hidden');closeDrawer();closeModal();$('sync-indicator').textContent='● DISCONNECTED';$('login-status').textContent='Sign in required.';}
function enterAdmin(user){S.active=true;V17.start();$('login-screen').classList.add('is-hidden');$('admin-shell').classList.remove('is-hidden');$('admin-name').textContent=(user?.username||'ADMIN').toUpperCase();refresh();loadRegions();}
async function refresh(){if(!S.active||S.busy||document.hidden)return;S.busy=true;try{const d=await api('/api/dashboard/overview');S.accounts=d.accounts||[];S.summary=d.summary||{};S.breakdown=d.status_breakdown||{};S.recent=d.recent_activity||[];$('sync-indicator').textContent='● UPDATED '+new Date().toLocaleTimeString([], {hour:'2-digit',minute:'2-digit'});$('sync-indicator').classList.remove('is-error');renderAll();loadHostHealth();V17.onSync(d,'poll');}catch(e){$('sync-indicator').textContent='● UPDATE FAILED';$('sync-indicator').classList.add('is-error');if(S.active)toast(e.message,true);}finally{S.busy=false;}}
function renderAll(){renderOverview();renderAccounts();renderAccountFilter();renderDailyWatch();renderSafety();if(S.current){const a=S.accounts.find(x=>x.id===S.current);if(a)renderDetail(a);else closeDrawer();}if(S.view==='activity'&&!S.events.length||(S.view==='activity'&&Date.now()-(S.lastActivityLoad||0)>12000))loadActivity(true);}
function navigate(view){if(!['overview','accounts','activity','settings'].includes(view))return;S.view=view;document.querySelectorAll('.view-pane').forEach(x=>x.classList.toggle('is-hidden',x.id!==`view-${view}`));document.querySelectorAll('.rail-link').forEach(x=>x.classList.toggle('is-active',x.dataset.view===view));$('breadcrumb-current').textContent=view.toUpperCase();if(view==='activity')loadActivity(true);window.scrollTo(0,0);}
function renderOverview(){const b=S.breakdown;const total=S.accounts.length;const stat=[['REGISTERED',total,'Saved accounts'],['SEARCHING',b.searching_match||0,'Queue / awaiting UDP'],['UDP JOINED',b.in_match||0,'JOIN signal received'],['OFFLINE',b.cooldown||0,'Cooldown timer running']];const ovKey=JSON.stringify([total,b.searching_match||0,b.in_match||0,b.cooldown||0,S.accounts.map(a=>[a.id,a.status,a.display_name,a.nickname,a.region,a.exp,a.exp_chart_status,a.queue_no_join_streak,a.udp_phase,a.cooldown_until,a.daily_exp_gained,a.daily_exp_reached,a.daily_auto_stopped,a.exp_stalled,a.exp_stall_checks]),S.recent.slice(0,6).map(e=>[e.created_at,e.account_label,e.message])]);if(S.overviewKey===ovKey)return;S.overviewKey=ovKey;$('overview-stats').innerHTML=stat.map(x=>`<div class="stat-unit"><span>${x[0]}</span><div class="big-number">${num(x[1])}</div><small>${x[2]}</small></div>`).join('');$('rail-account-count').textContent=total;
const current=S.accounts.slice().sort((a,b)=>(activeStates.has(b.status)?1:0)-(activeStates.has(a.status)?1:0)||a.display_name.localeCompare(b.display_name)).slice(0,6);
$('overview-accounts').innerHTML=current.length?current.map(a=>`<div class="snapshot-row" tabindex="0" role="button" data-action="details" data-id="${safeId(a)}"><div class="snapshot-identity"><strong>${esc(a.display_name)}</strong><small>${esc(a.region)} · ${esc(a.nickname||'Profile pending')}</small></div><div>${pill(a)}</div><b>${a.exp_chart_status==='awaiting_profile'?'—':num(a.exp)} <span>EXP</span></b></div>`).join(''):noData('No accounts saved.');
const observe=S.accounts.filter(a=>a.status==='searching_match'||a.status==='cooldown').sort((a,b)=>(b.status==='cooldown')-(a.status==='cooldown')||Number(b.queue_no_join_streak||0)-Number(a.queue_no_join_streak||0)).slice(0,5);const limit=Math.max(1,Number(S.summary.queue_no_join_limit)||100);
$('queue-observation').innerHTML=observe.length?observe.map(a=>`<div class="observation-row"><div class="observation-row-head"><div><strong>${esc(a.display_name)}</strong><small>${esc(a.status==='cooldown'?'Offline until '+dt(a.cooldown_until):phaseText(a.udp_phase))}</small></div><b>${a.status==='cooldown'?`<span data-until="${esc(a.cooldown_until)}">${remaining(a.cooldown_until)}</span>`:`${num(a.queue_no_join_streak||0)} / ${num(limit)}`}</b></div><div class="observation-meter"><span style="width:${Math.min(100,Math.round(Number(a.queue_no_join_streak||0)*100/limit))}%"></span></div></div>`).join(''):noData('No account is searching or cooling down.');
$('recent-activity').innerHTML=S.recent.length?S.recent.slice(0,6).map(e=>`<div class="recent-item"><span>${esc(ago(e.created_at))}</span><b>${esc(e.account_label||'System')}</b><p>${esc(e.message)}</p></div>`).join(''):noData('No recorded activity yet.');}
function db_reset_hint(){const a=S.accounts.find(x=>x.daily_reset_at);return a?a.daily_reset_at:null;}
function stallFlag(a){
  if(!a.exp_stalled) return '';
  const title=esc(a.exp_stall_message||'No verified EXP increase from the game server.');
  return `<div class="stall-flag" title="${title}"><span aria-hidden="true">▲</span> NO EXP FROM SERVER · ${esc(ago(a.exp_stall_since)).replace(' ago','')} · ${num(a.exp_stall_checks)} checks</div>`;
}
function stallDetail(a){
  if(!a.exp_stalled) return '';
  return `<div class="stall-block"><div class="stall-head"><span>EXP CREDIT CHECK</span><b>NO SERVER CREDIT YET</b></div><p>${esc(a.exp_stall_message||'No verified EXP increase from the game server.')}</p><ul><li>The bot is running and the profile check itself is working — the EXP number in the server profile simply did not move.</li><li>EXP is credited by the game server after a match settles; a join that never settles gives 0 EXP.</li><li>Check the mode/map, whether the match really ends, and the account's own daily EXP limit inside Free Fire.</li><li>Press <b>Check profile EXP</b> after finishing a real match to confirm in seconds.</li></ul></div>`;
}
function dailyPct(a){
  const limit=Number(a.daily_limit||0);
  if(!limit) return 0;
  return Math.max(0,Math.min(100,Math.round(Number(a.daily_exp_gained||0)*100/limit)));
}
function dailyChip(a){
  const limit=Number(a.daily_limit||0);
  if(!limit) return '';
  const gained=Number(a.daily_exp_gained||0);
  const reached=!!a.daily_exp_reached;
  return `<div class="daily-chip" data-reached="${reached?'yes':'no'}" title="Verified EXP gained today against this account's daily target">
    <span>TODAY ${num(gained)} / ${num(limit)}</span>
    <div class="daily-track"><span style="width:${dailyPct(a)}%"></span></div>
    <small>${reached?`TARGET REACHED${a.daily_auto_stopped?' · OFFLINE':''} · RESETS <span data-until="${esc(a.daily_reset_at)}">${remaining(a.daily_reset_at)}</span>`:`${num(Math.max(0,limit-gained))} EXP LEFT TODAY`}</small>
  </div>`;
}
function renderSafety(){
  const box=$('safety-grid'); if(!box) return;
  const safe=S.summary&&S.summary.safety; if(!safe) return;
  const host=S.hostHealth||{};
  const key=[safe.profile_checks_per_minute,safe.profile_failures_per_minute,safe.game_api_state,safe.active_workers,safe.throttled_accounts,host.pressure,host.memory_mb,host.load_per_core,host.database_mb,host.note].join('|');
  if(S.safetyKey===key) return;
  S.safetyKey=key;
  const tile=(label,value,hint)=>`<div class="safety-tile"><span>${label}</span><b>${value}</b><small>${hint||''}</small></div>`;
  const perAccount=Math.max(1,Math.round(60/Math.max(1,safe.poll_floor_seconds)));
  box.innerHTML=[
    tile('GAME PROFILE READS / MIN',num(safe.profile_checks_per_minute),`about ${num(perAccount)} per account at most · floor ${num(safe.poll_floor_seconds)}s`),
    tile('GAME API STATE',safe.game_api_state==='throttled'?'SLOWED DOWN':'NORMAL',safe.throttled_accounts?`${num(safe.throttled_accounts)} account(s) waiting longer`:`slowest wait ${num(safe.poll_max_seconds)}s`),
    tile('READS AT THE SAME TIME',`${num(safe.check_concurrency)} max`,safe.matchmaking_min_gap_seconds?`match requests ${num(safe.matchmaking_min_gap_seconds)}s apart across accounts`:(safe.forced_min_interval_seconds?`early check at most 1 in ${num(safe.forced_min_interval_seconds)}s`:'')),
    tile('FAILED READS / MIN',num(safe.profile_failures_per_minute),safe.profile_failures_total?`${num(safe.profile_failures_total)} since start`:'none since start'),
    tile('WORKERS RUNNING',`${num(safe.active_workers)} / ${num(safe.max_workers)}`,`logins at once ${num(safe.max_concurrent_logins)} · new logins ${num(safe.start_stagger_seconds||0)}s apart`),
    tile('INSTANCE MEMORY',host.memory_mb!=null?`${num(host.memory_mb)} MB`:'—',host.host_memory_total_mb?`of ${num(host.host_memory_total_mb)} MB on this box`:'process usage'),
    tile('CPU LOAD / CORE',host.load_per_core!=null?host.load_per_core:'—',host.cpu_count?`${num(host.cpu_count)} cores on this box`:''),
    tile('DATA SIZE',`${host.database_mb!=null?host.database_mb:0} MB`,host.data_dir_mb?`${host.data_dir_mb} MB on disk in total`:'database file size')
  ].join('');
  const note=$('safety-note');
  if(note) note.textContent=host.note||(safe.game_api_state==='throttled'
    ? 'The game servers answered with errors, so profile reads slowed down automatically until they recover.'
    : 'All limits are inside their range: the game servers see a steady low request rate and this instance has headroom.');
}
async function loadHostHealth(){
  if(!S.active||document.hidden) return;
  const now=Date.now();
  if(now-(S.hostHealthAt||0)<60000) return;
  S.hostHealthAt=now;
  try{
    const res=await api('/api/health');
    S.hostHealth=res.host||{};
    if(res.game_api) S.summary=Object.assign({},S.summary,{safety:res.game_api});
    renderSafety();
  }catch{}
}
function renderDailyWatch(){
  const box=$('daily-watch'); if(!box) return;
  const rows=S.accounts.slice().sort((a,b)=>Number(b.daily_exp_gained||0)-Number(a.daily_exp_gained||0)||String(a.display_name).localeCompare(String(b.display_name)));
  const rules=S.summary&&S.summary.daily_rules;
  const totals=(S.summary&&S.summary.daily_totals)||{};
  const summary=$('daily-watch-summary');
  const ruleText=$('daily-rule-text');
  if(ruleText&&rules) ruleText.textContent=`${rules.summary} · next reset ${dt(db_reset_hint())}`;
  if(summary&&rules){
    const stalled=rows.filter(a=>a.exp_stalled).length;
    summary.textContent=`${rules.summary} · verified today ${num(totals.total_exp)} EXP · ${num(totals.accounts_at_target||0)} at target${stalled?` · ${num(stalled)} account(s) not credited EXP yet`:''}`;
  }
  if(!rows.length){box.innerHTML=noData('No accounts saved.');return;}
  box.innerHTML=rows.slice(0,8).map(a=>{
    const limit=Number(a.daily_limit||0);
    const gained=Number(a.daily_exp_gained||0);
    const reached=!!a.daily_exp_reached;
    return `<div class="daily-watch-row" data-reached="${reached?'yes':'no'}">
      <div class="daily-watch-head"><div><strong>${esc(a.display_name)}</strong><small>LV ${esc(a.level)} · ${esc(a.region)} · ${reached?(a.daily_auto_stopped?'offline for today':'target reached'):'running under target'}</small></div><b>${num(gained)} / ${num(limit)}</b></div>
      <div class="daily-track"><span style="width:${dailyPct(a)}%"></span></div>
      <div class="daily-watch-meta"><span>${reached?`Next reset in <span data-until="${esc(a.daily_reset_at)}">${remaining(a.daily_reset_at)}</span>`:`${num(Math.max(0,limit-gained))} EXP allowed today`}</span><span>${a.exp_stalled?'No server EXP credit yet':'—'}</span><span>${esc(statusText(a))}</span></div>
    </div>`;
  }).join('');
}
async function resetDaily(id){
  const a=S.accounts.find(x=>x.id===id);
  const label=a?a.display_name:'this account';
  if(!confirm(`Clear today's EXP counter for ${label} so it can run again today?`))return;
  try{
    const res=await api(`/api/accounts/${encodeURIComponent(id)}/reset-daily`,'POST');
    toast(`Today's counter cleared for ${label}.`);
    await refresh();
  }catch(e){toast(e.message,true);}
}
async function resetAllDaily(){
  if(!confirm('Clear the EXP counter for every account today?'))return;
  try{
    const res=await api('/api/system/reset-daily','POST');
    toast(`Cleared ${num(res.cleared)} daily counters.`);
    await refresh();
  }catch(e){toast(e.message,true);}
}
function filtered(){let list=S.accounts.slice();const q=$('account-search').value.trim().toLowerCase();if(q)list=list.filter(a=>[a.display_name,a.uid,a.nickname,a.game_account_id].some(x=>String(x||'').toLowerCase().includes(q)));const state=$('account-status').value;if(state==='active')list=list.filter(a=>activeStates.has(a.status));else if(state!=='all')list=list.filter(a=>a.status===state);const sort=$('account-sort').value;list.sort((a,b)=>sort==='streak'?Number(b.queue_no_join_streak||0)-Number(a.queue_no_join_streak||0):sort==='activity'?String(b.last_activity||'').localeCompare(String(a.last_activity||'')):sort==='added'?String(b.created_at||'').localeCompare(String(a.created_at||'')):String(a.display_name||'').localeCompare(String(b.display_name||'')));return list;}
function renderAccounts(){
  const list=filtered(),live=new Set(S.accounts.map(a=>a.id));
  S.selected.forEach(id=>{if(!live.has(id))S.selected.delete(id);});
  const limit=Math.max(1,Number(S.summary.queue_no_join_limit)||100);
  const renderKey=JSON.stringify([
    $('account-search').value,$('account-status').value,$('account-sort').value,limit,
    [...S.selected].sort(),
    list.map(a=>[
      a.id,a.display_name,a.region,a.uid,a.nickname,a.game_account_id,a.auth_type,
      a.status,a.status_message,a.cooldown_until,a.queue_no_join_streak,a.udp_phase,
      a.udp_join_count,a.exp_chart_status,a.exp,a.level,a.exp_to_next_level,
      a.profile_check_status,a.profile_last_checked_at,a.last_activity,a.created_at,
      a.daily_exp_gained,a.daily_limit,a.daily_exp_reached,a.daily_auto_stopped,a.daily_reset_at,
      a.exp_stalled,a.exp_stall_since,a.exp_stall_checks
    ])
  ]);
  $('account-checked').textContent=new Date().toLocaleTimeString([], {hour:'2-digit',minute:'2-digit'});
  if(S.accountListSnapshot===renderKey)return;
  S.accountListSnapshot=renderKey;
  $('account-list').innerHTML=list.length?list.map(a=>{
    const cooling=a.status==='cooldown',running=activeStates.has(a.status);
    const streak=Number(a.queue_no_join_streak)||0;
    const exp=a.exp_chart_status==='awaiting_profile'?'—':num(a.exp);
    const daily=dailyChip(a);
    return `<div class="account-row"><div class="account-name"><input type="checkbox" data-select="${safeId(a)}" aria-label="Select ${esc(a.display_name)}" ${S.selected.has(a.id)?'checked':''}><div><strong>${esc(a.display_name)}</strong><small>${esc(a.region)} · UID ${esc(a.uid)}</small><small>Game ID ${esc(a.game_account_id||'Pending')}</small><small class="auth-method-label">${a.auth_type==='access_token'?'Access Token → JWT API':'UID + password'}</small></div></div><div class="account-cell">${pill(a)}<small>${cooling?`<span data-until="${esc(a.cooldown_until)}">${remaining(a.cooldown_until)}</span> to restart`:esc(a.status_message||'—')}</small></div><div class="account-cell"><b>${num(streak)} / ${num(limit)}</b><small>${esc(phaseText(a.udp_phase))} · ${num(a.udp_join_count)} UDP joins</small><div class="mini-meter"><span style="width:${Math.min(100,Math.round(streak*100/limit))}%"></span></div></div><div class="account-cell"><b>${exp}</b><small>Lvl ${esc(a.exp_chart_status==='awaiting_profile'?'—':a.level)} · ${a.exp_chart_status==='calculated'&&a.exp_to_next_level!=null?num(a.exp_to_next_level)+' to next':'No chart estimate'}</small><small>${esc(a.profile_check_status==='error'?'Check failed':ago(a.profile_last_checked_at))}</small>${stallFlag(a)}${daily}</div><div class="row-actions"><button data-action="details" data-id="${safeId(a)}">Details</button><button class="primary" data-action="${running||cooling?'stop':'start'}" data-id="${safeId(a)}">${cooling?'Cancel timer':running?'Stop':'Start'}</button><button data-action="edit" data-id="${safeId(a)}">Edit</button></div></div>`;
  }).join(''):noData('No account matches these filters.');
  $('account-results').textContent=`${list.length} accounts shown`;
  $('selected-count').textContent=`${S.selected.size} selected`;
  $('select-all').checked=!!list.length&&list.every(a=>S.selected.has(a.id));
}
function renderAccountFilter(){const el=$('log-account'),before=el.value;const key=JSON.stringify(S.accounts.map(a=>[a.id,a.display_name,a.region]));if(S.filterKey===key)return;S.filterKey=key;el.innerHTML='<option value="">All accounts</option>'+S.accounts.map(a=>`<option value="${safeId(a)}">${esc(a.display_name)} (${esc(a.region)})</option>`).join('');el.value=S.accounts.some(a=>a.id===before)?before:'';}
async function loadActivity(reset=true){if(!S.active)return;S.lastActivityLoad=Date.now();const qs=new URLSearchParams({limit:'100'});if($('log-account').value)qs.set('account_id',$('log-account').value);if($('log-level').value)qs.set('level',$('log-level').value);if($('log-type').value)qs.set('event_type',$('log-type').value);if(!reset&&S.cursor)qs.set('before_id',String(S.cursor));try{const d=await api(`/api/activity?${qs}`);S.events=reset?(d.events||[]):S.events.concat(d.events||[]);S.cursor=d.next_before_id||null;$('log-count').textContent=`${S.events.length} events displayed`;$('load-more').classList.toggle('is-hidden',!S.cursor);V17.ingest(S.events,reset);$('activity-list').innerHTML=S.events.length?S.events.map(e=>`<div class="activity-row" data-level="${esc(e.level)}"><time>${esc(dt(e.created_at))}</time><div><strong>${esc(e.account_label||'System')}</strong><small>${esc(e.event_type)}</small></div><p>${esc(e.message)}</p></div>`).join(''):noData('No events match these filters.');}catch(e){toast(e.message,true);}}
function syncPageScrollLock(){
  const drawerOpen=!$('detail-drawer').classList.contains('is-hidden');
  const modalOpen=!$('modal').classList.contains('is-hidden');
  const overlayOpen=drawerOpen||modalOpen;
  const root=document.documentElement;
  if(overlayOpen&&!S.scrollLocked){
    S.lockedScrollY=window.scrollY||root?.scrollTop||0;
    document.body.style.position='fixed';
    document.body.style.top=`-${S.lockedScrollY}px`;
    document.body.style.left='0';
    document.body.style.right='0';
    document.body.style.width='100%';
    root?.classList?.add('overlay-open');
    S.scrollLocked=true;
  }else if(!overlayOpen&&S.scrollLocked){
    const restoreY=S.lockedScrollY;
    document.body.style.position='';
    document.body.style.top='';
    document.body.style.left='';
    document.body.style.right='';
    document.body.style.width='';
    root?.classList?.remove('overlay-open');
    S.scrollLocked=false;
    if(typeof window.scrollTo==='function')window.scrollTo(0,restoreY);
  }
}
function showDrawer(){ $('drawer-scrim').classList.remove('is-hidden');$('detail-drawer').classList.remove('is-hidden');syncPageScrollLock();}
function closeDrawer(){S.current=null;S.detailSnapshot=null;$('drawer-scrim').classList.add('is-hidden');$('detail-drawer').classList.add('is-hidden');$('detail-drawer').scrollTop=0;syncPageScrollLock();}
function detailItem(k,v){return `<div><span>${esc(k)}</span><b>${esc(v??'—')}</b></div>`;}
function profileCheckText(a){
  const state=({changed:'Server profile changed',unchanged:'Server profile unchanged',error:'Profile check failed',never:'Not checked yet'})[a.profile_check_status]||'Unknown result';
  const message=String(a.profile_check_message||'').trim();
  return message?`${state}: ${message}`:state;
}
function detailSignature(a,limit){
  return JSON.stringify([
    a.id,a.display_name,a.region,a.nickname,a.auth_type,a.uid,a.game_account_id,
    a.status,a.status_message,a.exp_chart_status,a.level,a.exp,a.exp_to_next_level,
    a.session_exp_gained,a.profile_check_status,a.profile_check_message,a.profile_last_checked_at,
    a.queue_no_join_streak,a.matches_searched,a.udp_phase,a.udp_join_count,
    a.server_finish_count,a.udp_timeout_count,a.mode_id,a.map_id,a.cooldown_until,
    a.last_udp_join_at,limit
  ]);
}
function dailyDetailBlock(a){
  const limit=Number(a.daily_limit||0);
  if(!limit) return '';
  const gained=Number(a.daily_exp_gained||0);
  const reached=!!a.daily_exp_reached;
  return `<div class="daily-row" data-reached="${reached?'yes':'no'}">
    <div class="daily-head"><span>TODAY'S VERIFIED EXP · TARGET ${num(limit)}</span><b>${num(gained)} / ${num(limit)}</b></div>
    <div class="daily-track"><span style="width:${dailyPct(a)}%"></span></div>
    <div class="daily-meta"><span>${reached?(a.daily_auto_stopped?'Worker taken offline automatically':'Target reached'):`${num(Math.max(0,limit-gained))} EXP left today`}</span><span>RESET IN <span data-until="${esc(a.daily_reset_at)}">${remaining(a.daily_reset_at)}</span></span></div>
    <div class="detail-action"><button class="button button-outline" data-action="reset-daily" data-id="${safeId(a)}">Reset today's EXP</button></div>
  </div>`;
}
function renderDetail(a){
  const limit=Math.max(1,Number(S.summary.queue_no_join_limit)||100);
  const snapshot=detailSignature(a,limit);
  if(S.current===a.id&&snapshot===S.detailSnapshot)return;
  const drawer=$('detail-drawer'),scrollTop=drawer.scrollTop;
  const running=activeStates.has(a.status);
  $('detail-body').innerHTML=`<h2>${esc(a.display_name)}</h2><div class="detail-meta">${esc(a.region)} · ${esc(a.nickname||'Profile pending')}</div>${pill(a)}${dailyDetailBlock(a)}${stallDetail(a)}<div class="detail-callout">${esc(a.status_message||'No worker message available')}</div><div class="detail-grid">${detailItem('Authentication',a.auth_type==='access_token'?'Access Token → JWT API':'UID + password')}${detailItem('Account key',a.uid)}${detailItem('Game ID',a.game_account_id)}${detailItem('Current level',a.exp_chart_status==='awaiting_profile'?'—':a.level)}${detailItem('Server profile EXP',a.exp_chart_status==='awaiting_profile'?'Awaiting profile':num(a.exp))}${detailItem('To next level (chart)',a.exp_chart_status==='calculated'?num(a.exp_to_next_level):'Unavailable')}${detailItem('EXP since login',a.session_exp_gained==null?'No baseline':'+'+num(a.session_exp_gained))}${detailItem('Last profile check',dt(a.profile_last_checked_at))}${detailItem('Profile check result',profileCheckText(a))}${detailItem('Queue request streak',`${num(a.queue_no_join_streak)} / ${num(limit)}`)}${detailItem('All-time queue requests',num(a.matches_searched))}${detailItem('UDP phase',phaseText(a.udp_phase))}${detailItem('Server UDP JOIN signals',num(a.udp_join_count))}${detailItem('Server end signals',num(a.server_finish_count))}${detailItem('UDP timeouts',num(a.udp_timeout_count))}${detailItem('Mode / Map',`${a.mode_id} / ${a.map_id}`)}${detailItem('Cooldown until',a.cooldown_until?dt(a.cooldown_until):'—')}${detailItem('Last UDP JOIN',dt(a.last_udp_join_at))}</div><div class="detail-action"><button class="button button-dark" data-action="${running||a.status==='cooldown'?'stop':'start'}" data-id="${safeId(a)}">${running||a.status==='cooldown'?'Stop':'Start'}</button><button class="button button-outline" data-action="refresh-profile" data-id="${safeId(a)}">Check profile EXP</button><button class="button button-outline" data-action="logs-for" data-id="${safeId(a)}">Account logs</button><button class="button button-outline" data-action="edit" data-id="${safeId(a)}">Edit</button><button class="button button-outline" data-action="restart" data-id="${safeId(a)}">Restart</button><button class="button button-outline" data-action="clear-errors" data-id="${safeId(a)}">Clear errors</button><button class="button button-danger" data-action="delete" data-id="${safeId(a)}">Delete</button></div>`;
  S.detailSnapshot=snapshot;
  drawer.scrollTop=scrollTop;
}
async function details(id){
  const a=S.accounts.find(x=>x.id===id);
  if(!a)return;
  if(S.current!==id){S.detailSnapshot=null;$('detail-drawer').scrollTop=0;}
  S.current=id;
  renderDetail(a);
  showDrawer();
}
function modal(title,html,kicker='ADMIN ACTION'){$('modal-title').textContent=title;$('modal-kicker').textContent=kicker;$('modal-body').innerHTML=html;$('modal-scrim').classList.remove('is-hidden');$('modal').classList.remove('is-hidden');syncPageScrollLock();$('modal').querySelector('input,textarea,button,select')?.focus();}
function closeModal(){$('modal-scrim').classList.add('is-hidden');$('modal').classList.add('is-hidden');$('modal-body').innerHTML='';syncPageScrollLock();}
function regionOptions(selected='BD'){return S.regions.map(r=>`<option value="${esc(r)}" ${r===selected?'selected':''}>${esc(r)}</option>`).join('');}
function syncAuthFields(root=document){const form=root.querySelector('#account-form');if(!form)return;const method=form.querySelector('[name="auth_type"]')?.value||'uid_password';form.querySelectorAll('[data-auth-only]').forEach(block=>{block.classList.toggle('is-hidden',block.dataset.authOnly!==method);});form.querySelectorAll('[data-required-on-create="true"],[data-required-for-switch]').forEach(input=>{const block=input.closest('[data-auth-only]'),active=!block||block.dataset.authOnly===method;input.required=active&&((input.dataset.requiredOnCreate==='true'&&!form.dataset.edit)||(!!form.dataset.edit&&input.dataset.requiredForSwitch===method));});}
function accountForm(a=null){const authType=a?.auth_type==='access_token'?'access_token':'uid_password';modal(a?'Edit account':'Add account',`<form id="account-form" class="form-stack" data-edit="${a?safeId(a):''}"><label>Display name<input name="display_name" maxlength="80" value="${esc(a?.display_name||'')}" placeholder="Bot name" required></label><label>Authentication method<select name="auth_type" id="account-auth-type"><option value="uid_password" ${authType==='uid_password'?'selected':''}>UID + password</option><option value="access_token" ${authType==='access_token'?'selected':''}>Access Token → JWT API</option></select></label>${a?`<label>Account key<input value="${esc(a.uid)}" readonly></label>`:''}<div class="auth-field" data-auth-only="uid_password">${a?(authType==='access_token'?`<label>Guest UID (required when switching to UID + password)<input name="uid" minlength="3" maxlength="64" pattern="[A-Za-z0-9_-]+" data-required-for-switch="uid_password" autocomplete="off" placeholder="Enter Guest UID"></label>`:''):`<label>Guest UID<input name="uid" minlength="3" maxlength="64" pattern="[A-Za-z0-9_-]+" data-required-on-create="true" autocomplete="off"></label>`}<label>${a?'New guest password (leave blank to keep)':'Guest password'}<input type="password" name="password" ${a?(authType==='access_token'?'data-required-for-switch="uid_password"':''):'data-required-on-create="true" minlength="3"'} autocomplete="new-password"></label></div><div class="auth-field" data-auth-only="access_token"><label>${a?'Replace Access Token (optional)':'Garena Access Token'}<input type="password" name="access_token" maxlength="8192" ${a?(authType==='uid_password'?'data-required-for-switch="access_token"':''):'data-required-on-create="true"'} autocomplete="new-password" spellcheck="false" placeholder="${a?'Leave blank to keep the current encrypted token.':'Paste the rotated Access Token here'}"></label><p class="auth-note">POSTed server-to-server to the configured <code>/access-jwt</code> API; its login metadata is used by the encrypted regional login flow. Stored encrypted; never returned in account responses. Do not paste tokens into chat.</p></div><div class="modal-grid"><label>Region<select name="region">${regionOptions(a?.region||'BD')}</select></label><label>Mode ID<input type="number" name="mode_id" min="1" max="20" value="${Number(a?.mode_id)||1}" required></label></div><div class="modal-grid"><label>Map ID<input type="number" name="map_id" min="1" max="20" value="${Number(a?.map_id)||1}" required></label><span></span></div><label>Notes (admin only)<textarea name="notes" maxlength="500">${esc(a?.notes||'')}</textarea></label><button type="submit" class="button button-dark">${a?'Save account':'Add account'}</button></form>`,'PRIVATE / ACCOUNT');syncAuthFields($('modal-body'));}
async function accountSubmit(form){const f=new FormData(form),edit=form.dataset.edit,authType=String(f.get('auth_type')||'uid_password'),uid=String(f.get('uid')||'').trim(),password=String(f.get('password')||'').trim(),accessToken=String(f.get('access_token')||'').trim();const body={auth_type:authType,display_name:String(f.get('display_name')||'').trim(),region:String(f.get('region')||'BD'),mode_id:Number(f.get('mode_id')),map_id:Number(f.get('map_id')),notes:String(f.get('notes')||'')};if(authType==='uid_password'){if(uid)body.uid=uid;if(password)body.password=password;}else if(accessToken)body.access_token=accessToken;await api(edit?`/api/accounts/${encodeURIComponent(edit)}`:'/api/accounts',edit?'PUT':'POST',body);closeModal();toast(edit?'Account updated.':'Account added.');await refresh();}
async function doAccount(action,id){const a=S.accounts.find(x=>x.id===id);if(!a)return;if(action==='delete'&&!confirm(`Delete ${a.display_name}? This cannot be undone.`))return;try{const res=await api(`/api/accounts/${encodeURIComponent(id)}${action==='refresh-profile'?'/refresh-profile':action==='delete'?'':`/${action}`}`,action==='delete'?'DELETE':'POST');if(res.ok===false)throw Error(res.check?.reason||res.message||'Action did not complete');toast(action==='refresh-profile'?res.check?.ok?`Profile checked: ${num(res.check.exp)} EXP.`:(res.check?.reason||'Profile check failed'):res.message||`${action} completed.`);if(action==='delete')closeDrawer();await refresh();}catch(e){toast(e.message,true);}}
async function bulk(action){const ids=Array.from(S.selected);if(!ids.length)return toast('Select at least one account.',true);let region=null;if(action==='change_region'){region=window.prompt('Target region code (for example BD, ME, IND):','BD');if(!region)return;region=region.trim().toUpperCase();if(!S.regions.includes(region))return toast('Unsupported region code.',true);}if(!confirm(`${action.replace('_',' ')} ${ids.length} selected accounts?`))return;try{const d=await api('/api/accounts/bulk-action','POST',{account_ids:ids,action,region});toast(d.error_count?`${d.processed_count} completed, ${d.error_count} failed. ${d.errors?.[0]?.error||''}`:`${d.processed_count} accounts processed.`,!!d.error_count);S.selected.clear();await refresh();}catch(e){toast(e.message,true);}}
async function exportMetadata(){try{const r=await fetch('/api/accounts/export/metadata?format=json',{credentials:'same-origin'});if(!r.ok)throw Error('Export failed. Sign in again if your session ended.');const url=URL.createObjectURL(await r.blob()),link=document.createElement('a');link.href=url;link.download='gamer-corp-account-metadata.json';link.click();setTimeout(()=>URL.revokeObjectURL(url),1000);toast('Metadata downloaded. Review identifiers before sharing.');}catch(e){toast(e.message,true);}}
async function loadRegions(){try{const d=await api('/api/regions');S.regions=(d.regions||[]).map(x=>x.code);$('diagnostic-region').innerHTML=S.regions.map(r=>`<option value="${esc(r)}">${esc(r)}</option>`).join('');$('diagnostic-region').value=S.regions.includes('BD')?'BD':S.regions[0];}catch(e){toast(e.message,true);}}
async function diagnostics(){const r=$('diagnostic-region').value;try{const d=(await api(`/api/system/diagnostics?region=${encodeURIComponent(r)}`,'POST')).diagnostics;$('diagnostic-result').textContent=`${r} · DNS ${d.dns_resolved_ip||'unavailable'} · ${d.dns_latency_ms??'—'} ms · PlayStore ${d.playstore_version||'unavailable'} · Release ${d.release_version||'unavailable'} · Checked ${dt(d.last_checked)}`;toast('Diagnostics response received.');}catch(e){toast(e.message,true);}}
async function chart(){try{const d=await api('/api/exp/chart');modal('Level reference',`<p class="modal-note">User-supplied EXP chart. Levels 19, 20 and 54 have inconsistent supplied needed values; next cumulative threshold is used.</p><div class="table-scroll"><table class="chart-table"><thead><tr><th>Level</th><th>Start EXP</th><th>To next</th></tr></thead><tbody>${d.levels.map(x=>`<tr><td>${esc(x.level)}${x.chart_row_conflict?' *':''}</td><td>${num(x.start_exp)}</td><td>${num(x.needed_for_next)}</td></tr>`).join('')}</tbody></table></div>`, 'REFERENCE / NOT SERVER DATA');}catch(e){toast(e.message,true);}}
function tick(){document.querySelectorAll('[data-until]').forEach(el=>{el.textContent=remaining(el.dataset.until);});}
async function handleAction(button){const action=button.dataset.action,id=button.dataset.id;if(!action)return;if(action==='close-drawer')return closeDrawer();if(action==='close-modal')return closeModal();if(action==='refresh')return refresh();if(action==='goto-accounts')return navigate('accounts');if(action==='goto-activity')return navigate('activity');if(action==='refresh-log')return loadActivity(true);if(action==='load-more')return loadActivity(false);if(action==='open-add')return accountForm();if(action==='open-bulk')return modal('Add multiple accounts',`<p class="modal-note">One account per line: UID:PASSWORD or UID,PASSWORD,REGION,NAME. Bulk add is UID/password only; use Add Bot for Access Token login. Credentials are never shown in public cards.</p><form id="bulk-form" class="form-stack"><label>Default region<select name="region">${regionOptions('BD')}</select></label><label>Accounts<textarea name="raw_lines" placeholder="UID:PASSWORD" required></textarea></label><button class="button button-dark" type="submit">Add accounts</button></form>`,'PRIVATE / BULK ADD');if(action==='open-import')return modal('Import metadata',`<p class="modal-note">Choose a JSON metadata export. Passwords and Access Tokens are never included; re-enter the matching credential in Edit before starting an imported account.</p><form id="import-form" class="form-stack"><label>JSON file<input type="file" name="file" accept=".json,application/json" required></label><button class="button button-dark" type="submit">Import metadata</button></form>`,'PRIVATE / IMPORT');if(action==='open-chart')return chart();if(action==='diagnostics')return diagnostics();if(action==='export')return exportMetadata();if(action==='bulk-start'||action==='bulk-stop')return bulk(action.slice(5));if(action==='run-bulk'){const selected=$('bulk-action-select').value;if(!selected)return toast('Choose a bulk action.',true);return bulk(selected);}if(action==='details')return details(id);if(action==='edit')return accountForm(S.accounts.find(x=>x.id===id));if(action==='logs-for'){closeDrawer();navigate('activity');$('log-account').value=id;return loadActivity(true);}if(action==='reset-daily')return resetDaily(id);
if(action==='open-daily'){navigate('overview');const el=$('daily-watch');if(el&&el.scrollIntoView)el.scrollIntoView({behavior:'smooth',block:'center'});return;}
if(action==='reset-all-daily')return resetAllDaily();
if(action==='clear-errors'){try{const d=await api(`/api/accounts/${encodeURIComponent(id)}/errors`,'DELETE');toast(`${d.cleared} error entries cleared.`);await refresh();}catch(e){toast(e.message,true);}return;}if(['start','stop','restart','delete','refresh-profile'].includes(action))return doAccount(action,id);}
document.addEventListener('click',e=>{const nav=e.target.closest('[data-view]');if(nav){navigate(nav.dataset.view);return;}const el=e.target.closest('[data-action]');if(el)handleAction(el);});
document.addEventListener('keydown',e=>{if(e.key==='Escape'){closeModal();closeDrawer();}if((e.key==='Enter'||e.key===' ')&&e.target.classList.contains('snapshot-row')){e.preventDefault();details(e.target.dataset.id);}});
document.addEventListener('change',e=>{if(e.target.dataset.select){if(e.target.checked)S.selected.add(e.target.dataset.select);else S.selected.delete(e.target.dataset.select);renderAccounts();}if(e.target.id==='select-all'){filtered().forEach(a=>e.target.checked?S.selected.add(a.id):S.selected.delete(a.id));renderAccounts();}if(e.target.id==='account-auth-type')syncAuthFields($('modal-body'));if(['account-status','account-sort'].includes(e.target.id))renderAccounts();if(['log-account','log-level','log-type'].includes(e.target.id))loadActivity(true);});
$('account-search').addEventListener('input',renderAccounts);
let loginRequest=0;
const loginButton=$('login-submit'), loginLabel=$('login-submit-label');
const loginStatus=(message,error=false)=>{const el=$('login-status');el.textContent=message;el.classList.toggle('is-error',error);};
$('login-form').addEventListener('submit',async e=>{
  e.preventDefault();if(loginButton.disabled)return;
  const requestId=++loginRequest,form=e.target,f=new FormData(form);
  $('login-error').textContent='';loginButton.disabled=true;loginLabel.textContent='Checking credentials…';loginStatus('Contacting the admin service…');
  const controller=new AbortController(),timer=setTimeout(()=>controller.abort(),12000);
  try{
    const res=await fetch('/api/auth/login',{method:'POST',credentials:'same-origin',cache:'no-store',headers:{'Content-Type':'application/json'},body:JSON.stringify({username:String(f.get('username')||''),password:String(f.get('password')||'')}),signal:controller.signal});
    let data={};try{data=await res.json();}catch{}
    if(!res.ok)throw Error(typeof data.detail==='string'?data.detail:`Login failed (HTTP ${res.status}).`);
    if(!data.ok||!data.user)throw Error('Server did not confirm the administrator session.');
    const check=await fetch('/api/auth/me',{credentials:'same-origin',cache:'no-store',signal:controller.signal});
    if(!check.ok)throw Error('Session cookie was not accepted. Open /admin on the running server over the same origin.');
    if(requestId!==loginRequest)return;
    form.reset();loginStatus('Authenticated.');enterAdmin(data.user);
  }catch(err){
    if(requestId!==loginRequest)return;
    $('login-error').textContent=err.name==='AbortError'?'Server did not respond. Check that the app is running and try again.':err instanceof TypeError?'Cannot reach the admin server. Open the running site at /admin, not an HTML preview.':err.message;
    loginStatus('Sign-in failed — see the message above.',true);
  }finally{clearTimeout(timer);if(requestId===loginRequest){loginButton.disabled=false;loginLabel.textContent='Sign in to workspace';}}
});
$('logout-btn').addEventListener('click',async()=>{try{await api('/api/auth/logout','POST');}catch{}showLogin();});
$('modal').addEventListener('submit',async e=>{if(!['account-form','bulk-form','import-form'].includes(e.target.id))return;e.preventDefault();const button=e.target.querySelector('[type=submit]');button.disabled=true;try{if(e.target.id==='account-form')await accountSubmit(e.target);else if(e.target.id==='bulk-form'){const f=new FormData(e.target),d=await api('/api/accounts/bulk-add','POST',{raw_lines:String(f.get('raw_lines')||''),default_region:String(f.get('region')||'BD')});closeModal();toast(`${d.created_count} added${d.error_count?`, ${d.error_count} failed: ${d.errors?.[0]?.error||''}`:''}`,!!d.error_count);await refresh();}else{const file=e.target.querySelector('input[type=file]').files[0];const parsed=JSON.parse(await file.text());const d=await api('/api/accounts/import/metadata','POST',{accounts:Array.isArray(parsed)?parsed:parsed.accounts,default_region:'BD'});closeModal();toast(`${d.imported_count} imported, ${d.updated_count} updated${d.error_count?`, ${d.error_count} errors`:''}`,!!d.error_count);await refresh();}}catch(err){toast(err.message,true);}finally{button.disabled=false;}});
$('password-form').addEventListener('submit',async e=>{e.preventDefault();const f=new FormData(e.target);try{await api('/api/auth/change-password','POST',{current_password:String(f.get('current')||''),new_password:String(f.get('next')||'')});e.target.reset();showLogin();toast('Password saved. Sign in again with your new password.');}catch(err){toast(err.message,true);}});
setInterval(()=>{if(S.active)refresh();},10000);setInterval(tick,1000);
// Boot only checks for an existing session; it never gates the login button.
// A stale boot response cannot override a login started while this request is pending.
(async()=>{
  const bootController=new AbortController(),bootTimer=setTimeout(()=>bootController.abort(),10000);
  try{
    const response=await fetch('/api/auth/me',{credentials:'same-origin',cache:'no-store',signal:bootController.signal});
    if(loginRequest)return;
    if(response.ok){const data=await response.json();if(!loginRequest)enterAdmin(data.user);}
    else if(response.status===401){showLogin();loginStatus('Admin service ready. Enter your credentials.');}
    else{showLogin();loginStatus(`Admin service returned HTTP ${response.status}. Try again later.`,true);}
  }catch{
    if(!loginRequest){showLogin();loginStatus('Cannot reach server. Open the running site at /admin, not an HTML preview.',true);}
  }finally{clearTimeout(bootTimer);}
})();

/* =====================================================================================
   V17 NEON-GLASS RUNTIME — real WebSocket stream, real activity console, error rack,
   level-up radar & server-confirmed level-up celebration, parallax/3D motion engine.
   Nothing in this block invents telemetry: every line rendered comes from the server API.
   ===================================================================================== */
const V17 = (() => {
  const state = {
    active:false, paused:false, filter:'all', ws:null, wsRetry:0, wsTimer:null,
    logs:[], seen:new Set(), errors:[], levels:new Map(), celebrateAt:new Map(),
    lastCelebratedEvent:null, overlayTimer:null, ticker:null, radar:null, fx:null, reduced:false
  };
  const MAX_LOGS = 600, MAX_ERRORS = 60;
  const clock = v => { try{ const d = v? new Date(v) : new Date(); return Number.isNaN(+d)?'--:--:--':d.toLocaleTimeString([], {hour12:false}); }catch{ return '--:--:--'; } };
  const canFX = () => !state.reduced;
  const store = { get(k){ try{ return typeof localStorage!=='undefined'? localStorage.getItem(k):null; }catch{ return null; } },
                  set(k,v){ try{ if(typeof localStorage!=='undefined') localStorage.setItem(k,v); }catch{} } };

  /* ---------- error rack: every client/server failure becomes a visible, dismissible line ---------- */
  function noteError(kind, message, detail) {
    try {
      const entry = { id:`${kind}:${Date.now()}:${state.errors.length}`, kind, message:String(message||'Unknown failure'),
                      detail:detail?String(detail).slice(0,400):'', at:new Date().toISOString(), path:'' };
      const m = /^\w+ ([^ ]+)/.exec(entry.message); if(m) entry.path = m[1];
      const dup = state.errors.find(e=>e.message===entry.message&&e.kind===kind);
      if(dup){ dup.at=entry.at; dup.count=(dup.count||1)+1; } else { state.errors.unshift(entry); }
      state.errors.splice(MAX_ERRORS);
      renderErrors();
      const counter = $('error-count'); if(counter) counter.textContent = `${state.errors.length} open`;
    } catch {}
  }
  function clearTransient(path) {
    try {
      if(!path) return;
      const before = state.errors.length;
      state.errors = state.errors.filter(e=>!(e.kind==='network' && e.path && path.startsWith(e.path)));
      if(state.errors.length!==before) renderErrors();
    } catch {}
  }
  function renderErrors() {
    try {
      const rack = $('error-rack'); if(!rack) return;
      if(!state.errors.length) {
        rack.innerHTML = '<div class="error-empty">No client or server error recorded in this session.</div>';
      } else {
        rack.innerHTML = state.errors.map(e=>`<div class="error-item" title="Click to dismiss"><strong>${esc(e.kind)} · ${esc(clock(e.at))}${e.count>1?` ×${e.count}`:''}</strong><p>${esc(e.message)}</p>${e.detail?`<small>${esc(e.detail)}</small>`:''}</div>`).join('');
      }
      const counter = $('error-count'); if(counter) counter.textContent = `${state.errors.length} open`;
    } catch {}
  }

  /* ---------- real activity console ---------- */
  function ingest(events, reset) {
    try {
      if(!Array.isArray(events)) return;
      if(reset) { state.logs = []; state.seen.clear(); }
      let added = 0;
      for(const ev of events) {
        if(!ev || ev.id===undefined || state.seen.has(ev.id)) continue;
        state.seen.add(ev.id);
        state.logs.push({ id:ev.id, at:ev.created_at, level:String(ev.level||'info'), type:String(ev.event_type||'event'),
                          label:String(ev.account_label||'System'), message:String(ev.message||'') });
        added++;
        if(String(ev.event_type)==='level_up') celebrateFromEvent(ev);
      }
      state.logs.sort((a,b)=>Number(b.id)-Number(a.id));
      if(state.logs.length>MAX_LOGS){ state.logs.length = MAX_LOGS; }
      if(state.seen.size>2000) state.seen = new Set(state.logs.map(l=>l.id));
      if(added) { renderConsole(); }
    } catch(e) { noteError('ui','Incoming events could not be displayed.', e?.message||''); }
  }
  function renderConsole() {
    try {
      const box = $('log-stream'); if(!box) return;
      const rows = state.logs.filter(l=>state.filter==='all' || l.type===state.filter || (state.filter==='error' && l.level==='error'));
      const counter = $('log-count-live'); if(counter) counter.textContent = `${rows.length} lines`;
      if(!rows.length) {
        box.innerHTML = '<div class="log-hint">No recorded event matches this filter yet. The console only prints rows the server actually wrote.</div>';
        return;
      }
      const pinned = box.scrollTop + box.clientHeight >= box.scrollHeight - 24;
      box.innerHTML = rows.slice(0,300).map(l=>`<div class="log-line" data-level="${esc(l.level)}"><time>${esc(clock(l.at))}</time><span class="tag">${esc(l.type)}</span><p>${esc(l.label)} · ${esc(l.message)}</p></div>`).join('');
      if(pinned || state.paused===false) { try{ box.scrollTop = 0; }catch{} }
    } catch {}
  }
  async function syncStream() {
    if(!state.active || state.paused || document.hidden) return;
    state.streamAt = Date.now();
    try {
      const d = await api('/api/activity?limit=120');
      ingest(d?.events||[], false);
    } catch(e) { /* api() already reported this to the error rack */ }
  }

  /* ---------- level-up radar (real saved profile levels) ---------- */
  function renderRadar() {
    try {
      const list = $('level-radar'); if(!list) return;
      const radarKey = JSON.stringify(S.accounts.map(a=>[a.id,a.display_name,a.level,a.exp,a.exp_chart_status,a.exp_to_next_level,a.exp_level_progress_percent,a.profile_check_status]));
      if(state.radarKey===radarKey) return;
      state.radarKey = radarKey;
      const rows = S.accounts.filter(a=>a && (a.level!==null&&a.level!==undefined)).sort((a,b)=>Number(b.level||0)-Number(a.level||0)||String(a.display_name).localeCompare(String(b.display_name)));
      const counter = $('radar-count'); if(counter) counter.textContent = `${rows.length} profiles`;
      if(!rows.length) { list.innerHTML = '<div class="error-empty">No server profile level saved yet. Rows appear after a real profile check.</div>'; return; }
      list.innerHTML = rows.slice(0,6).map(a=>{
        const start = Number(a.exp_level_start), next = Number(a.exp_next_level_total), pctRaw = Number(a.exp_level_progress_percent);
        const valid = String(a.exp_chart_status)==='calculated' && Number.isFinite(start) && Number.isFinite(next) && Number.isFinite(pctRaw) && next>start;
        const pct = valid ? Math.max(0,Math.min(100,Math.round(pctRaw))) : 0;
        return `<div class="radar-row"><div class="radar-top"><strong>${esc(a.display_name)}</strong><span class="radar-level">LV ${esc(a.level)} · ${esc(a.exp_chart_status==='awaiting_profile'?'awaiting profile':num(a.exp)+' EXP')}</span></div>
          <div class="radar-track" role="progressbar" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${pct}" aria-label="Saved profile progress for ${esc(a.display_name)}"><span style="width:${pct}%"></span></div>
          <div class="radar-meta"><span>${valid?`${num(a.exp_to_next_level)} EXP TO NEXT`:'CHART PROGRESS UNAVAILABLE'}</span><span>${esc(a.profile_check_status==='changed'?'PROFILE CHANGED':a.profile_check_status==='unchanged'?'NO CHANGE':a.profile_check_status==='error'?'CHECK FAILED':'NOT CHECKED')}</span></div></div>`;
      }).join('');
    } catch(e) { noteError('ui','Level progress could not be displayed.', e?.message||''); }
  }

  /* ---------- level-up celebration (server-confirmed only) ---------- */
  function celebrate(payload) {
    try {
      const key = payload.key || `acc:${payload.name}`;
      const last = state.celebrateAt.get(key)||0;
      if(Date.now()-last < 90000) return false;
      state.celebrateAt.set(key, Date.now());
      const overlay = $('levelup-overlay'); if(!overlay) return false;
      const set = (id,text)=>{ const el=$(id); if(el) el.textContent = text; };
      set('levelup-title', payload.title || 'LEVEL UP');
      set('levelup-from', `LV ${payload.from}`);
      set('levelup-to', `LV ${payload.to}`);
      set('levelup-source', payload.source || 'Recorded from a saved server profile check.');
      const facts = $('levelup-facts');
      if(facts) facts.innerHTML = (payload.facts||[]).map(f=>`<div><span>${esc(f[0])}</span><b>${esc(f[1])}</b></div>`).join('');
      const shards = overlay.querySelectorAll ? overlay.querySelectorAll('.levelup-shards') : [];
      for(const s of shards) { s.innerHTML = Array.from({length:16},(_,i)=>`<i style="left:${Math.round(Math.random()*100)}%;background:${['#2ff0ff','#ff3d94','#8b5cff','#b8ff45'][i%4]};animation-delay:${(i*0.16).toFixed(2)}s"></i>`).join(''); }
      overlay.classList.remove('is-hidden');
      if(overlay.scrollTop!==undefined) overlay.scrollTop = 0;
      if(state.overlayTimer) clearTimeout(state.overlayTimer);
      state.overlayTimer = setTimeout(closeCelebration, 12000);
      return true;
    } catch(e) { noteError('ui','The level-up card could not be displayed.', e?.message||''); return false; }
  }
  function closeCelebration() {
    try { if(state.overlayTimer) clearTimeout(state.overlayTimer); state.overlayTimer=null; const o=$('levelup-overlay'); if(o) o.classList.add('is-hidden'); } catch {}
  }
  function celebrateFromEvent(ev) {
    try {
      const acc = S.accounts.find(a=>a.id===ev.account_id);
      const msg = String(ev.message||'');
      const jump = /LV\s*(\d+)\s*(?:→|->|to)\s*LV\s*(\d+)/i.exec(msg) || /level\s*(\d+)\s*(?:→|->|to)\s*(\d+)/i.exec(msg);
      const from = jump? jump[1] : (acc && Number(acc.level)-1>0 ? String(Number(acc.level)-1) : '?');
      const to = jump? jump[2] : (acc? String(acc.level) : '?');
      const ok = celebrate({ key:`ev:${ev.id}`, name:ev.account_label, from, to,
        source:`Server-recorded level_up event · ${ev.created_at||''}`.trim(),
        facts:[['ACCOUNT', ev.account_label||'—'], ['RECORDED', ev.created_at?dt(ev.created_at):'—'], ['TOTAL EXP', acc&&acc.exp!=null?num(acc.exp):'—'], ['EXP TO NEXT', acc&&acc.exp_to_next_level!=null?num(acc.exp_to_next_level):'—']] });
      if(!ok) return;
      try { toast(`${ev.account_label||'Account'} reached a new level — recorded by the server.`); } catch {}
    } catch {}
  }
  function detectLevelUps() {
    try {
      const fresh = [];
      for(const a of S.accounts) {
        if(!a || a.level===null || a.level===undefined) continue;
        const lvl = Number(a.level);
        if(!Number.isFinite(lvl) || lvl<=0) continue;
        const prev = state.levels.get(a.id);
        state.levels.set(a.id, lvl);
        if(prev && lvl>prev) fresh.push({ a, prev, lvl });
      }
      for(const f of fresh) {
        const started = celebrate({ key:`acc:${f.a.id}`, name:f.a.display_name, from:f.prev, to:f.lvl,
          source:`Server profile check${f.a.profile_last_checked_at?` · ${dt(f.a.profile_last_checked_at)}`:''}. Level and EXP are saved observations, not reward promises.`,
          facts:[['ACCOUNT', f.a.display_name], ['REGION', f.a.region||'—'], ['TOTAL EXP', f.a.exp!=null?num(f.a.exp):'—'], ['EXP TO NEXT', f.a.exp_to_next_level!=null?num(f.a.exp_to_next_level):'—'], ['UDP JOINS', num(f.a.udp_join_count)], ['QUEUE STREAK', `${num(f.a.queue_no_join_streak)} / ${num(S.summary.queue_no_join_limit)}`]] });
        if(started) { try { toast(`${f.a.display_name} levelled up: LV ${f.prev} → LV ${f.lvl} (server profile).`); } catch {} }
      }
    } catch(e) { noteError('ui','Level tracking stopped.', e?.message||''); }
  }

  /* ---------- kinetic ticker built from real numbers ---------- */
  function renderTicker() {
    try {
      const track = $('dock-ticker'); if(!track) return;
      const s = S.summary||{};
      const tickKey = JSON.stringify([s.total_accounts,s.active_accounts,s.cooldown_accounts,s.total_matches_completed,s.total_matches_searched,s.total_exp,s.average_level,s.total_errors,S.breakdown&&S.breakdown.in_match,S.breakdown&&S.breakdown.searching_match]);
      if(state.tickKey===tickKey) return;
      state.tickKey = tickKey;
      const items = [
        `FLEET ${num(s.total_accounts)}`, `ACTIVE ${num(s.active_accounts)}`, `UDP JOINED ${num(S.breakdown?.in_match)}`,
        `SEARCHING ${num(S.breakdown?.searching_match)}`, `COOLDOWN ${num(s.cooldown_accounts)}`,
        `SERVER ENDS ${num(s.total_matches_completed)}`, `QUEUES ${num(s.total_matches_searched)}`,
        `TOTAL EXP ${num(s.total_exp)}`, `AVG LEVEL ${s.average_level??'—'}`, `OPEN ERRORS ${num(s.total_errors)}`
      ];
      const line = items.map(i=>`<span>${esc(i)}</span><i aria-hidden="true">◆</i>`).join('');
      track.innerHTML = `<div class="dock-run">${line}</div><div class="dock-run" aria-hidden="true">${line}</div>`;
    } catch {}
  }

  /* ---------- live transport: WebSocket first, polling fallback ---------- */
  function setDockState(online, label) {
    try {
      const el = $('dock-live-state'); if(el) el.classList.toggle('is-down', !online);
      const lab = $('dock-live-label'); if(lab && label) lab.textContent = label;
    } catch {}
  }
  function connectWS() {
    if(!state.active) return;
    if(typeof WebSocket!=='function' || typeof location==='undefined') { setDockState(false,'POLLING'); return; }
    try {
      const proto = location.protocol==='https:'?'wss':'ws';
      const socket = new WebSocket(`${proto}://${location.host}/ws/live`);
      state.ws = socket;
      socket.onopen = () => { state.wsRetry=0; setDockState(true,'LIVE'); };
      socket.onmessage = (ev) => {
        try {
          const msg = JSON.parse(ev.data);
          if(msg?.type==='dashboard_sync' && msg.data) { applySync(msg.data,'ws'); }
          else if(msg?.type==='pong') { setDockState(true,'STREAM / LIVE WS'); }
        } catch(e) { noteError('live','An incoming live update could not be read.', e?.message||''); }
      };
      socket.onerror = () => { noteError('live','The live connection dropped; updates continue by polling.',''); setDockState(false,'STREAM / POLLING'); };
      socket.onclose = () => { state.ws=null; scheduleReconnect(); };
    } catch(e) { noteError('live','The live connection could not be opened.', e?.message||''); scheduleReconnect(); }
  }
  function scheduleReconnect() {
    if(!state.active) return;
    if(state.wsTimer) clearTimeout(state.wsTimer);
    const delay = Math.min(30000, 1000 * Math.pow(2, Math.min(state.wsRetry++, 5)));
    setDockState(false, `RETRY ${Math.round(delay/1000)}S`);
    state.wsTimer = setTimeout(()=>{ state.wsTimer=null; connectWS(); }, delay);
  }
  function applySync(d, source) {
    if(document.hidden) return;   // no DOM work while the tab is in the background
    S.accounts = d.accounts||S.accounts;
    S.summary = d.summary||{};
    S.breakdown = d.status_breakdown||{};
    S.recent = d.recent_activity||S.recent;
    // The websocket frame carries the newest activity: feed it to the live log too,
    // so an event never waits for the next poll to appear on screen.
    try { ingest(d.recent_activity||[], false); } catch {}
    renderAll();
    onSync(d, source);
    try { $('sync-indicator').textContent = `● LIVE ${clock()}`; $('sync-indicator').classList.remove('is-error'); } catch {}
  }

  /* ---------- immersive motion engine (GPU-only, reduced-motion aware) ---------- */
  /* Detect whether this device can afford the immersive layer. Weak phones,
     low-core laptops, save-data connections and reduced-motion users stay in the
     static (fast) mode no matter what is stored. */
  function capableDevice() {
    try {
      if(state.reduced) return false;
      const cores = Number(navigator.hardwareConcurrency||4);
      const mem = Number(navigator.deviceMemory||4);
      const conn = navigator.connection||{};
      if(conn.saveData) return false;
      if(/2g|slow-2g/.test(String(conn.effectiveType||''))) return false;
      return cores >= 8 && mem >= 8;
    } catch { return false; }
  }

  function mountFX() {
    try {
      document.querySelectorAll('.stat-unit,.setting-block').forEach(el=>el.classList.add('tilt'));
      if(typeof IntersectionObserver==='function') {
        const io = new IntersectionObserver(entries=>{ entries.forEach(en=>{ if(en.isIntersecting){ en.target.classList.add('is-in'); io.unobserve(en.target); } }); }, { threshold:.12 });
        document.querySelectorAll('.reveal').forEach(el=>io.observe(el));
      }
      if(!state.fx) { document.documentElement?.style?.setProperty?.('--mx','0'); document.documentElement?.style?.setProperty?.('--my','0'); return; }
      document.body?.classList?.add('is-live');
      const root = document.documentElement;
      let raf = null, mx = 0, my = 0, sy = 0;
      const write = () => { raf = null; if(!root?.style?.setProperty) return;
        root.style.setProperty('--mx', mx.toFixed(3));
        root.style.setProperty('--my', my.toFixed(3));
        root.style.setProperty('--sy', String(sy)); };
      const queue = () => { if(raf===null && typeof requestAnimationFrame==='function') raf = requestAnimationFrame(write); };
      if(typeof window.addEventListener==='function') {
        window.addEventListener('pointermove', e=>{ mx=(e.clientX/window.innerWidth-0.5)*2; my=(e.clientY/window.innerHeight-0.5)*2; queue(); }, { passive:true });
        window.addEventListener('scroll', ()=>{ sy = Math.round(window.scrollY||0); queue(); }, { passive:true });
        queue();
      }
      /* 3D tilt: one rAF-throttled listener, rectangle cached per hovered card */
      let tiltCard = null, tiltRect = null, tiltPending = null;
      const applyTilt = () => {
        tiltPending = null; if(!tiltCard || !tiltRect) return;
        const px = (tiltX-tiltRect.left)/Math.max(1,tiltRect.width), py=(tiltY-tiltRect.top)/Math.max(1,tiltRect.height);
        if(!tiltCard.querySelector('.tilt-glare')) { const g=document.createElement('span'); g.className='tilt-glare'; tiltCard.appendChild(g); }
        tiltCard.style.setProperty('--gx', `${Math.round(px*100)}%`); tiltCard.style.setProperty('--gy', `${Math.round(py*100)}%`);
        tiltCard.style.transform = `perspective(900px) rotateX(${(0.5-py)*5}deg) rotateY(${(px-0.5)*6}deg) translate3d(0,-4px,0)`;
      };
      let tiltX = 0, tiltY = 0;
      document.addEventListener('pointerover', e=>{
        const card = e.target?.closest?.('.tilt'); if(!card || card===tiltCard) return;
        if(tiltCard) { tiltCard.classList.remove('is-tilting'); tiltCard.style.transform=''; }
        tiltCard = card; tiltRect = card.getBoundingClientRect(); card.classList.add('is-tilting');
      }, { passive:true });
      document.addEventListener('pointermove', e=>{
        if(!tiltCard) return; tiltX = e.clientX; tiltY = e.clientY;
        if(tiltPending===null) tiltPending = requestAnimationFrame(applyTilt);
      }, { passive:true });
      document.addEventListener('pointerout', e=>{
        const card = e.target?.closest?.('.tilt'); if(!card) return;
        card.classList.remove('is-tilting'); card.style.transform=''; if(card===tiltCard) { tiltCard=null; tiltRect=null; }
      }, { passive:true });
    } catch(e) { noteError('ui','Extra effects are unavailable on this device; the fast layout is used.', e?.message||''); }
  }
  function setFX(on) {
    try {
      const wanted = !!on && !state.reduced;
      state.fx = wanted; store.set('gc-v17-fx', wanted?'on':'off');
      document.body?.classList?.toggle('fx-full', wanted);
      document.body?.classList?.toggle('is-live', wanted);
      const b1=$('fx-toggle'), b2=$('fx-toggle-2');
      const label=$('fx-toggle-label');
      if(label) label.textContent = wanted? 'Immersive on':'Fast mode';
      if(b1&&b1.setAttribute) b1.setAttribute('aria-pressed', String(!!on));
      if(b2&&b2.setAttribute) b2.setAttribute('aria-pressed', String(!!on));
      if(!on) closeCelebration();
    } catch {}
  }
  function setDock(on) {
    try {
      store.set('gc-v17-dock', on?'on':'off');
      const dock=$('live-dock'); if(dock) dock.classList.toggle('is-hidden', !on);
      const label=$('dock-toggle-label'); if(label) label.textContent = on? 'Live deck on':'Live deck off';
      const b1=$('dock-toggle'), b2=$('dock-toggle-2');
      if(b1&&b1.setAttribute) b1.setAttribute('aria-pressed', String(!!on));
      if(b2&&b2.setAttribute) b2.setAttribute('aria-pressed', String(!!on));
    } catch {}
  }

  /* ---------- public API ---------- */
  function onSync(d, source) {
    try { renderRadar(); renderTicker(); renderDailyWatch(); renderSafety(); detectLevelUps(); } catch {}
    try { if(source==='ws' && !state.paused && Date.now()-(state.streamAt||0)>6000) void syncStream(); } catch {}
  }
  function start() {
    try {
      state.active = true;
      renderErrors(); renderConsole(); renderRadar(); renderTicker();
      setDockState(false,'CONNECTING');
      void syncStream();
      connectWS();
      if(state.ticker===null) state.ticker = setInterval(()=>{ if(state.active && !state.paused && !document.hidden) void syncStream(); }, 8000);
      if(typeof document.addEventListener==='function') {
        document.addEventListener('visibilitychange', ()=>{ if(document.visibilityState==='visible' && state.active) { void syncStream(); try{ refresh(); }catch{} } });
      }
    } catch(e) { noteError('ui','The live log could not start.', e?.message||''); }
  }
  function stop() {
    try {
      state.active=false; closeCelebration();
      if(state.wsTimer){ clearTimeout(state.wsTimer); state.wsTimer=null; }
      if(state.ws){ try{ state.ws.close(); }catch{} state.ws=null; }
      state.wsRetry=0; setDockState(false,'SIGNED OUT');
    } catch {}
  }
  function boot() {
    try {
      state.reduced = !!(window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches);
      const savedFX = store.get('gc-v17-fx'); const savedDock = store.get('gc-v17-dock');
      setFX(savedFX ? savedFX==='on' : capableDevice());   // fast by default on weak devices
      setDock(savedDock!=='off');
      if(Number(navigator.hardwareConcurrency||4) <= 2) document.body?.classList?.add('low-fx');
      mountFX();
      const filter=$('console-filter');
      if(filter) filter.addEventListener('change', e=>{ state.filter = String(e.target.value||'all'); renderConsole(); });
      const pause=$('console-pause');
      if(pause) pause.addEventListener('click', ()=>{ state.paused=!state.paused; pause.textContent = state.paused? 'Resume stream':'Pause stream'; pause.classList.toggle('button-dark', state.paused); if(pause.setAttribute) pause.setAttribute('aria-pressed', String(state.paused)); });
      const clear=$('console-clear');
      if(clear) clear.addEventListener('click', ()=>{ state.logs=[]; state.seen.clear(); renderConsole(); toast('View cleared. Saved records are untouched.'); });
      const dl=$('console-download');
      if(dl) dl.addEventListener('click', ()=>{
        try {
          if(typeof document.createElement!=='function' || typeof Blob!=='function') return;
          const text = state.logs.map(l=>`${l.at}\t${l.level}\t${l.type}\t${l.label}\t${l.message}`).join('\n');
          const url = URL.createObjectURL(new Blob([text||'no recorded events loaded in this browser session'],{type:'text/plain'}));
          const a = document.createElement('a'); a.href=url; a.download=`gamer-corp-activity-${Date.now()}.log`; a.click();
          setTimeout(()=>URL.revokeObjectURL(url),1000);
        } catch(e) { noteError('ui','Log download failed.', e?.message||''); }
      });
      const rack=$('error-rack');
      if(rack) rack.addEventListener('click', e=>{ const item=e.target?.closest?.('.error-item'); if(!item) return; const first=item.querySelector?.('strong')?.textContent||''; state.errors = state.errors.filter(x=>!first.includes(x.kind)||!first.includes(clock(x.at))); renderErrors(); });
      ['fx-toggle','fx-toggle-2'].forEach(id=>{ const b=$(id); if(b) b.addEventListener('click', ()=>setFX(!state.fx)); });
      ['dock-toggle','dock-toggle-2'].forEach(id=>{ const b=$(id); if(b) b.addEventListener('click', ()=>setDock($('live-dock')?.classList?.contains('is-hidden') ?? false)); });
      const close=$('levelup-close'); if(close) close.addEventListener('click', closeCelebration);
      if(typeof window.addEventListener==='function') {
        window.addEventListener('error', e=>noteError('page', e?.message||'Unexpected page error', `${e?.filename||''}:${e?.lineno||0}`));
        window.addEventListener('unhandledrejection', e=>noteError('page', e?.reason?.message||String(e?.reason||'Unexpected background error'), 'promise'));
        window.addEventListener('offline', ()=>{ noteError('network','You appear to be offline; live updates are paused.',''); setDockState(false,'OFFLINE'); });
        window.addEventListener('online', ()=>{ clearTransient(''); if(state.active){ connectWS(); void syncStream(); } toast('Connection restored. Live updates resumed.'); });
      }
      setInterval(()=>{ if(state.active && state.ws && state.ws.readyState===1) { try{ state.ws.send('ping'); }catch{} } }, 25000);
    } catch(e) { noteError('ui','The dashboard could not finish starting.', e?.message||''); }
  }
  boot();
  return { start, stop, onSync, ingest, noteError, clearTransient, renderErrors, renderRadar, renderConsole, celebrate, closeCelebration: closeCelebration };
})();
