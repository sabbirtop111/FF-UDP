/* Read-only public Level-Up board. It uses ONLY GET /api/public/overview. */
'use strict';
const $=id=>document.getElementById(id);
const state={bots:[],statuses:{},observed:null};
const esc=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const num=v=>Number(v??0).toLocaleString();
const date=v=>{if(!v)return '—';const d=new Date(v);return Number.isNaN(+d)?'—':d.toLocaleString([], {dateStyle:'medium',timeStyle:'short'});};
const relative=v=>{if(!v)return 'Never';const diff=Math.max(0,Math.floor((Date.now()-Date.parse(v))/1000));return !Number.isFinite(diff)?'—':diff<60?'Just now':diff<3600?`${Math.floor(diff/60)} min ago`:diff<86400?`${Math.floor(diff/3600)} hr ago`:date(v);};
const clock=v=>{const sec=Math.max(0,Math.ceil((Date.parse(v)-Date.now())/1000));return Number.isFinite(sec)?`${String(Math.floor(sec/60)).padStart(2,'0')}:${String(sec%60).padStart(2,'0')}`:'—';};
const active=new Set(['authenticating','online','searching_match','in_match','reconnecting']);
const status=a=>{if(a.status==='in_match')return 'UDP JOINED';if(a.status==='cooldown')return 'COOLDOWN';if(a.status==='searching_match'&&['startup_sent','hello_received','ack_received'].includes(a.udp_phase))return 'AWAITING JOIN';return ({searching_match:'SEARCHING',online:'GATEWAY ONLINE',authenticating:'AUTHENTICATING',reconnecting:'RECONNECTING',inactive:'STOPPED',error:'ERROR'})[a.status]||'UNKNOWN';};
const phase=p=>({queue_sent:'Queue request sent',match_candidate:'Match candidate received',startup_sent:'UDP startup sent',hello_received:'UDP HELLO received',ack_received:'UDP ACK received',joined:'Server UDP JOIN received',join_timeout:'JOIN timeout recorded',handshake_timeout:'UDP handshake timeout',idle_timeout:'UDP idle timeout',server_finish:'Server-end signal recorded'})[p]||'No UDP response recorded';
const check=s=>({changed:'Profile changed',unchanged:'No profile change',error:'Last check failed',never:'Not checked yet'})[s]||'Not checked yet';
const detail=(label,value)=>`<div><dt>${esc(label)}</dt><dd>${esc(value)}</dd></div>`;

function levelPanel(b){
  // Progress is chart-derived and rendered only when the saved server profile agrees with the reference.
  const profile=b.exp!==null&&b.exp!==undefined&&b.level!==null&&b.level!==undefined;
  const start=b.exp_level_start,next=b.exp_next_level_total,progress=b.exp_level_progress_percent;
  const valid=b.exp_chart_status==='calculated'&&profile&&start!=null&&next!=null&&progress!=null&&
    Number.isFinite(Number(progress))&&Number(next)>Number(start)&&Number(b.exp)>=Number(start)&&Number(b.exp)<Number(next)&&
    Number(b.exp_to_next_level)===Number(next)-Number(b.exp);
  const pct=valid?Math.max(0,Math.min(100,Number(progress))):null;
  const note=!profile?'Awaiting a server profile; level and EXP are not available.':
    b.exp_chart_status==='outside_chart'?'The supplied chart only covers LV 1–65.':
    b.exp_chart_status==='mismatch'?'Saved level and EXP do not agree with the supplied chart.':
    'Level progress is unavailable until the saved profile matches the chart.';
  const sessionGain=b.session_exp_gained==null?'No login baseline':`+${num(b.session_exp_gained)} EXP since login`;
  const level=profile?esc(b.level):'—';
  return `<section class="xp-panel" aria-label="Saved profile level and experience">
    <div class="xp-stage">
      <div class="level-orb ${valid?'is-valid':'is-unavailable'}" aria-hidden="true" ${valid?`style="--progress:${pct}%"`:''}>
        <span><small>LV</small><b>${level}</b></span>
      </div>
      <div class="profile-data"><span class="xp-label">SERVER PROFILE · SAVED EXP</span><strong class="exp-total">${profile?num(b.exp):'—'}</strong><div class="exp-subline"><small>TOTAL EXPERIENCE</small><strong>${valid?`${num(b.exp_to_next_level)} EXP TO NEXT`:'PROFILE VERIFIED'}</strong></div></div>
    </div>
    ${valid?`<div class="level-track-wrap"><div class="level-track-head"><span>LEVEL ${level} PROGRESS</span><b>${pct}%</b></div><div class="level-track" role="progressbar" aria-label="Level progress from supplied EXP chart" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${pct}" aria-valuetext="${pct}% through level ${level}"><span style="width:${pct}%"></span></div><div class="level-markers"><span>${num(start)} EXP · LV ${level} start</span><span>${num(next)} EXP · LV ${num(Number(b.level)+1)} target</span></div></div>`:`<div class="level-unavailable">${esc(note)}</div>`}
    <div class="session-row"><span>SESSION EXP CHANGE</span><strong>${esc(sessionGain)}</strong></div>
  </section>`;
}

function dailyPanel(b){
  // Today's verified EXP against the level-based daily target (no invented numbers).
  const limit=Number(b.daily_exp_limit||0);
  if(!limit) return '';
  const gained=Number(b.daily_exp_gained||0);
  const pct=Math.max(0,Math.min(100,Math.round(gained*100/limit)));
  const reached=!!b.daily_exp_reached;
  const left=Math.max(0,limit-gained);
  return `<section class="daily-row" data-reached="${reached?'yes':'no'}" aria-label="Today's EXP against the daily target">
    <div class="daily-head"><span>TODAY'S EXP · TARGET ${num(limit)}</span><b>${num(gained)} / ${num(limit)}</b></div>
    <div class="daily-track" role="progressbar" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${pct}" aria-valuetext="${num(gained)} of ${num(limit)} EXP today"><span style="width:${pct}%"></span></div>
    <div class="daily-meta"><span>${reached?'Daily target reached — offline until the next reset':`${num(left)} EXP left today`}</span><span>${b.daily_auto_stopped?'RESTING TODAY':''}</span></div>
  </section>`;
}

function card(b,i){
  const key=esc(`${b.display_name}|${b.game_account_id||''}|${b.region}`);
  const display=String(b.display_name||'Bot');
  const initials=display.trim().split(/\s+/).slice(0,2).map(part=>Array.from(part)[0]||'').join('').toUpperCase().slice(0,2)||'GC';
  const countdown=b.status==='cooldown'?`<span class="countdown-chip"><b><span data-until="${esc(b.cooldown_until||'')}">${clock(b.cooldown_until)}</span></b> cooldown left</span>`:'';
  return `<article class="bot-card" data-status="${esc(b.status)}">
    <div class="card-head"><span class="bot-id">SAVED BOT <strong>${String(i+1).padStart(2,'0')}</strong> / ${esc(b.region||'—')}</span><span class="bot-status state-badge" data-status="${esc(b.status)}"><i></i>${esc(status(b))}</span></div>
    <div class="bot-player"><div class="bot-avatar" aria-hidden="true">${esc(initials)}</div><div class="bot-identity"><h3>${esc(b.display_name)}</h3><p>${esc(b.nickname||'Nickname unavailable')} · Game ID ${esc(b.game_account_id||'Pending')}</p></div><div class="bot-region"><span>REGION</span><strong>${esc(b.region||'—')}</strong></div></div>
    ${levelPanel(b)}
    ${dailyPanel(b)}
    <div class="bot-stats-title"><span>QUEUE &amp; SERVER SIGNALS</span><b>RECORDED COUNTS</b></div>
    <div class="telemetry-grid"><div class="telemetry-cell"><strong>${num(b.matches_searched)}</strong><small>QUEUE REQUESTS</small></div><div class="telemetry-cell"><strong>${num(b.queue_no_join_streak)}</strong><small>UNJOINED STREAK</small></div><div class="telemetry-cell"><strong>${num(b.udp_join_count)}</strong><small>UDP JOINS</small></div><div class="telemetry-cell"><strong>${num(b.server_finish_count)}</strong><small>SERVER ENDS</small></div></div>
    <div class="phase-strip"><span>LAST UDP STAGE</span><strong>${esc(phase(b.udp_phase))}</strong></div>
    <div class="bot-foot"><span>Mode ${esc(b.mode_id)} · Map ${esc(b.map_id)}</span><span>Profile checked ${esc(relative(b.profile_last_checked_at))}</span>${countdown}</div>
    <details class="card-details" data-key="${key}"><summary>More recorded details</summary><dl class="detail-pairs">${detail('Profile check',check(b.profile_check_status))}${detail('Last profile check',date(b.profile_last_checked_at))}${detail('Last profile change',date(b.profile_last_changed_at))}${detail('Last verified EXP gain',date(b.session_last_exp_gain_at))}${detail('EXP gain checks this login',num(b.session_exp_gain_events))}${detail('Session started',date(b.session_started_at))}${detail('UDP timeouts',num(b.udp_timeout_count))}${detail('Last UDP JOIN',date(b.last_udp_join_at))}${detail('Last UDP timeout',date(b.last_udp_timeout_at))}${detail('Last server end',date(b.last_server_finish_at))}${detail('Cooldowns recorded',num(b.queue_cooldown_count))}${detail('Reconnects recorded',num(b.reconnect_count))}</dl></details>
  </article>`;
}

function compareNullableDesc(a,b,key){
  const av=a[key],bv=b[key],aMissing=av===null||av===undefined||av==='',bMissing=bv===null||bv===undefined||bv==='';
  if(aMissing&&bMissing)return 0;if(aMissing)return 1;if(bMissing)return -1;
  return Number(bv)-Number(av);
}
function render(){
  const s=state.statuses||{};
  const metrics=[['ALL BOTS',s.total,'Saved profiles'],['SEARCHING',s.searching_match,'Queue activity'],['UDP JOINED',s.in_match,'Join signals'],['COOLDOWN',s.cooldown,'Offline timers']];
  $('public-metrics').innerHTML=metrics.map(([label,value,foot],i)=>`<div class="metric"><span>${label}</span><strong>${num(value)}</strong><small>${foot}</small></div>`).join('');
  const search=$('bot-search').value.trim().toLowerCase(),filter=$('bot-filter').value,sort=$('bot-sort')?.value||'name';
  const rows=state.bots.filter(b=>{const match=!search||[b.display_name,b.nickname,b.game_account_id,b.region].some(v=>String(v||'').toLowerCase().includes(search));return match&&(filter==='all'||filter==='active'&&active.has(b.status)||b.status===filter);});
  rows.sort((a,b)=>sort==='level'?compareNullableDesc(a,b,'level'):sort==='exp'?compareNullableDesc(a,b,'exp'):sort==='activity'?String(b.profile_last_checked_at||'').localeCompare(String(a.profile_last_checked_at||'')):String(a.display_name||'').localeCompare(String(b.display_name||'')));
  const open=new Set([...document.querySelectorAll('.card-details[open]')].map(d=>d.dataset.key));
  $('bot-results').textContent=`Showing ${rows.length} of ${state.bots.length} saved Bots`;
  $('bot-grid').innerHTML=rows.length?rows.map(card).join(''):'<div class="empty"><span class="empty-orbit" aria-hidden="true">◎</span><strong>No Bots match this view</strong><span>Try another search or state filter.</span></div>';
  document.querySelectorAll('.card-details').forEach(d=>{if(open.has(d.dataset.key))d.open=true;});
}
async function refresh(){
  const button=$('refresh-bots');if(button){button.disabled=true;button.setAttribute('aria-busy','true');}
  try{
    const r=await fetch('/api/public/overview',{credentials:'omit',cache:'no-store'});if(!r.ok)throw Error(`HTTP ${r.status}`);
    const d=await r.json();state.bots=Array.isArray(d.bots)?d.bots:[];state.statuses=d.statuses||{};state.observed=d.observed_at;
    V17P.watch();V17P.noteSuccess();
    $('connection-state').textContent='LIVE DATA';$('connection-state').classList.add('is-online');$('connection-state').classList.remove('is-error');$('last-updated').textContent=`Updated ${date(d.observed_at)}`;render();
  }catch(e){
    V17P.noteFailure();
    $('connection-state').textContent='DATA OFFLINE';$('connection-state').classList.add('is-error');$('connection-state').classList.remove('is-online');$('last-updated').textContent='Could not update Bot data';
    if(!state.observed)$('bot-grid').innerHTML='<div class="empty"><span class="empty-orbit" aria-hidden="true">!</span><strong>Connection unavailable</strong><span>Could not load saved Bot data. Try refreshing in a moment.</span></div>';
  }finally{if(button){button.disabled=false;button.removeAttribute('aria-busy');}}
}
$('bot-search').addEventListener('input',render);
$('bot-filter').addEventListener('change',render);
const sortControl=$('bot-sort');if(sortControl)sortControl.addEventListener('change',render);
const refreshControl=$('refresh-bots');if(refreshControl)refreshControl.addEventListener('click',refresh);
setInterval(()=>{document.querySelectorAll('[data-until]').forEach(el=>el.textContent=clock(el.dataset.until));},1000);
setInterval(refresh,10000);refresh();

/* =====================================================================================
   V17 PUBLIC RUNTIME — parallax & 3D motion, kinetic fleet ticker, level-up watch.
   It reads ONLY the read-only public snapshot; nothing here invents Bot telemetry.
   ===================================================================================== */
const V17P = (() => {
  const store = { get(k){ try{ return typeof localStorage!=='undefined'? localStorage.getItem(k):null; }catch{ return null; } }, set(k,v){ try{ if(typeof localStorage!=='undefined') localStorage.setItem(k,v); }catch{} } };
  const levels = new Map();
  const celebrated = new Map();
  let overlayTimer = null, failures = 0;
  const reduced = () => !!(window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches);
  const clockStamp = () => { try { return new Date().toLocaleTimeString([], {hour12:false}); } catch { return '--:--:--'; } };

  function ticker() {
    try {
      const track = $('strip-track'); if(!track) return;
      const s = state.statuses||{};
      const tickKey = JSON.stringify([s.total,s.searching_match,s.in_match,s.cooldown,s.error,state.bots.length]);
      if(tickKey===window.__v17pTick) return;
      window.__v17pTick = tickKey;
      const items = [`SAVED BOTS ${num(s.total)}`, `SEARCHING ${num(s.searching_match)}`, `UDP JOINED ${num(s.in_match)}`,
        `COOLDOWN ${num(s.cooldown)}`, `ERRORS ${num(s.error)}`, `PROFILES OBSERVED ${num(state.bots.length)}`,
        `SNAPSHOT ${clockStamp()}`];
      const line = items.map(i=>`<span>${esc(i)}</span><i aria-hidden="true">◆</i>`).join('');
      track.innerHTML = `<div class="dock-run">${line}</div><div class="dock-run" aria-hidden="true">${line}</div>`;
    } catch {}
  }
  function celebrate(bot, from, to) {
    try {
      const key = String(bot.display_name||'bot');
      const last = celebrated.get(key)||0;
      if(Date.now()-last < 120000) return;
      celebrated.set(key, Date.now());
      const overlay = $('levelup-overlay'); if(!overlay) return;
      const set = (id,text)=>{ const el=$(id); if(el) el.textContent=text; };
      set('levelup-title', String(bot.display_name||'Bot'));
      set('levelup-from', `LV ${from}`); set('levelup-to', `LV ${to}`);
      set('levelup-source', `Saved profile updated at ${clockStamp()} · LV ${to} · ${num(bot.exp)} EXP total.`);
      const facts = $('levelup-facts');
      if(facts) facts.innerHTML = [['REGION', bot.region||'—'], ['GAME ID', bot.game_account_id||'Pending'], ['TOTAL EXP', bot.exp!=null?num(bot.exp):'—'],
        ['EXP TO NEXT', bot.exp_to_next_level!=null?num(bot.exp_to_next_level):'—'], ['TODAY\'S EXP', `${num(bot.daily_exp_gained)} / ${num(bot.daily_exp_limit)}`], ['SERVER ENDS', num(bot.server_finish_count)]]
        .map(f=>`<div><span>${esc(f[0])}</span><b>${esc(f[1])}</b></div>`).join('');
      const shards = overlay.querySelector ? overlay.querySelector('.levelup-shards') : null;
      if(shards) shards.innerHTML = Array.from({length:16},(_,i)=>`<i style="left:${Math.round(Math.random()*100)}%;background:${['#2ff0ff','#ff3d94','#8b5cff','#b8ff45'][i%4]};animation-delay:${(i*0.16).toFixed(2)}s"></i>`).join('');
      overlay.classList.remove('is-hidden');
      if(overlayTimer) clearTimeout(overlayTimer);
      overlayTimer = setTimeout(close, 12000);
    } catch {}
  }
  function close() { try { if(overlayTimer) clearTimeout(overlayTimer); overlayTimer=null; $('levelup-overlay')?.classList?.add('is-hidden'); } catch {} }
  function watch() {
    try {
      for(const b of state.bots||[]) {
        const key = `${b.display_name}|${b.game_account_id||''}|${b.region||''}`;
        const lvl = Number(b.level);
        if(!Number.isFinite(lvl) || lvl<=0) continue;
        const prev = levels.get(key);
        levels.set(key, lvl);
        if(prev && lvl>prev) celebrate(b, prev, lvl);
      }
    } catch {}
  }
  function capableDevice() {
    try {
      if(reduced()) return false;
      const cores = Number(navigator.hardwareConcurrency||4);
      const mem = Number(navigator.deviceMemory||4);
      const conn = navigator.connection||{};
      if(conn.saveData) return false;
      if(/2g|slow-2g/.test(String(conn.effectiveType||''))) return false;
      return cores >= 8 && mem >= 8;
    } catch { return false; }
  }
  let fxOn = false;
  function paintToggle() {
    const fx = $('fx-toggle'); const label = $('fx-toggle-label');
    document.body?.classList?.toggle('fx-full', fxOn);
    document.body?.classList?.toggle('is-live', fxOn);
    if(label) label.textContent = fxOn? 'Immersive on':'Fast mode';
    if(fx && fx.setAttribute) fx.setAttribute('aria-pressed', String(fxOn));
  }
  function mountFX() {
    try {
      fxOn = store.get('gc-v17-fx') ? store.get('gc-v17-fx')==='on' : capableDevice();
      if(reduced()) fxOn = false;
      paintToggle();
      if(typeof IntersectionObserver==='function') {
        const io = new IntersectionObserver(entries=>{ entries.forEach(en=>{ if(en.isIntersecting){ en.target.classList.add('is-in'); io.unobserve(en.target); } }); }, { threshold:.1 });
        document.querySelectorAll('.reveal').forEach(el=>io.observe(el));
      }
      if(fxOn && typeof window.addEventListener==='function') {
        const root = document.documentElement;
        let raf=null, mx=0, my=0, sy=0;
        const write = ()=>{ raf=null; if(!root?.style?.setProperty) return;
          root.style.setProperty('--mx', mx.toFixed(3)); root.style.setProperty('--my', my.toFixed(3)); root.style.setProperty('--sy', String(sy)); };
        const queue = ()=>{ if(raf===null && typeof requestAnimationFrame==='function') raf=requestAnimationFrame(write); };
        window.addEventListener('pointermove', e=>{ mx=(e.clientX/window.innerWidth-0.5)*2; my=(e.clientY/window.innerHeight-0.5)*2; queue(); }, { passive:true });
        window.addEventListener('scroll', ()=>{ sy=Math.round(window.scrollY||0); queue(); }, { passive:true });
        queue();
        let card=null, rect=null, pending=null, px2=0, py2=0;
        const apply = ()=>{ pending=null; if(!card||!rect) return;
          const rx=(px2-rect.left)/Math.max(1,rect.width), ry=(py2-rect.top)/Math.max(1,rect.height);
          card.style.transform = `perspective(900px) rotateX(${((0.5-ry)*4.5).toFixed(2)}deg) rotateY(${((rx-0.5)*5.5).toFixed(2)}deg) translate3d(0,-5px,0)`; };
        document.addEventListener('pointerover', e=>{ const c=e.target?.closest?.('.bot-card,.metric,.visual-core'); if(!c||c===card) return;
          if(card) card.style.transform=''; card=c; rect=c.getBoundingClientRect(); }, { passive:true });
        document.addEventListener('pointermove', e=>{ if(!card) return; px2=e.clientX; py2=e.clientY; if(pending===null) pending=requestAnimationFrame(apply); }, { passive:true });
        document.addEventListener('pointerout', e=>{ const c=e.target?.closest?.('.bot-card,.metric,.visual-core'); if(!c) return; c.style.transform=''; if(c===card){card=null;rect=null;} }, { passive:true });
      }
      const fx = $('fx-toggle');
      if(fx) fx.addEventListener('click', ()=>{ fxOn=!fxOn; store.set('gc-v17-fx', fxOn?'on':'off'); paintToggle(); if(!fxOn) close(); });
    } catch {}
  }
  function noteFailure() {
    try {
      failures++;
      const strip = $('strip-track');
      if(strip) strip.innerHTML = `<div class="dock-run"><span>CONNECTION LOST</span><i>◆</i><span>RETRYING ${failures}</span><i>◆</i><span>RECONNECTS AUTOMATICALLY</span><i>◆</i></div>`;
    } catch {}
  }
  function noteSuccess() {
    try { failures=0; ticker(); } catch {}
  }
  function boot() {
    try {
      mountFX();
      const closeButton = $('levelup-close'); if(closeButton) closeButton.addEventListener('click', close);
    } catch {}
  }
  boot();
  return { watch, ticker, noteFailure, noteSuccess, close, celebrate };
})();
