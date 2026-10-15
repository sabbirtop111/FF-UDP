// Offline smoke test and preview renderer for the REAL deployed public.js.
const fs = require('fs');
const vm = require('vm');
const assert = require('assert');
const path = require('path');
const script = fs.readFileSync(path.join(__dirname, '../static/js/public.js'), 'utf8');
const now = new Date().toISOString();
const base = {
  display_name:'Demo / Aster', nickname:'Fictional Bot', game_account_id:'DEMO-8741',
  region:'BD',status:'searching_match',level:8,exp:4091,exp_chart_status:'calculated',
  exp_level_start:3800,exp_next_level_total:4870,exp_needed_for_level:1070,
  exp_to_next_level:779,exp_level_progress_percent:27.2,session_exp_gained:18,
  session_started_at:now,profile_last_checked_at:now,profile_check_status:'changed',
  profile_last_changed_at:now,session_last_exp_gain_at:now,session_exp_gain_events:1,
  matches_searched:24,queue_no_join_streak:12,udp_join_count:3,server_finish_count:2,
  udp_timeout_count:1,last_udp_join_at:now,last_udp_timeout_at:now,last_server_finish_at:now,
  udp_phase:'ack_received',udp_phase_at:now,queue_cooldown_count:0,reconnect_count:1,
  cooldown_until:null,mode_id:1,map_id:1
};
const bots=[base,
  {...base,display_name:'Demo / Nova',nickname:'No server profile yet',game_account_id:null,
   status:'cooldown',level:null,exp:null,exp_chart_status:'awaiting_profile',
   exp_level_start:null,exp_next_level_total:null,exp_to_next_level:null,exp_level_progress_percent:null,
   session_exp_gained:null,profile_check_status:'never',profile_last_checked_at:null,
   session_last_exp_gain_at:null,session_exp_gain_events:0,matches_searched:100,
   queue_no_join_streak:100,udp_join_count:0,server_finish_count:0,udp_timeout_count:2,
   last_udp_join_at:null,last_udp_timeout_at:now,udp_phase:'join_timeout',
   cooldown_until:new Date(Date.now()+7*60*1000).toISOString()},
  {...base,display_name:'Demo / Flux',nickname:'Chart mismatch example',game_account_id:'DEMO-4882',
   status:'inactive',level:2,exp:500,exp_chart_status:'mismatch',exp_level_start:48,
   exp_next_level_total:202,exp_to_next_level:null,exp_level_progress_percent:null,
   session_exp_gained:null,profile_check_status:'unchanged',queue_no_join_streak:0,udp_phase:'none'}
];
const payload={observed_at:now,statuses:{total:3,searching_match:1,in_match:0,cooldown:1},bots};
const elements=new Map();
for(const id of ['public-metrics','bot-search','bot-filter','bot-results','bot-grid','connection-state','last-updated']){
  elements.set(id,{innerHTML:'',textContent:'',value:id==='bot-filter'?'all':'',dataset:{},classList:{add(){},remove(){}},addEventListener(){}});
}
const document={getElementById:id=>elements.get(id),querySelectorAll:()=>[]};
const context=vm.createContext({document,fetch:async()=>({ok:true,json:async()=>payload}),setInterval(){},console});
vm.runInContext(script,context);
setTimeout(()=>{
  const html=elements.get('bot-grid').innerHTML;
  const bars=html.match(/role="progressbar"/g)||[];
  assert.strictEqual(bars.length,1, 'only chart-consistent profile gets a level bar');
  assert(html.includes('aria-valuenow="27.2"'));
  assert(html.includes('779 EXP'));
  assert(html.includes('Awaiting a server profile'));
  assert(html.includes('do not agree with the supplied chart'));
  assert(html.includes('More recorded details'));
  assert(!script.includes('/api/accounts/'));
  const injected=vm.runInContext(`card(${JSON.stringify({...base,display_name:'<img src=x onerror=alert(1)>'})},0)`,context);
  assert(injected.includes('&lt;img'));
  assert(!injected.includes('<img'));
  if(process.argv.includes('--preview')) console.log(JSON.stringify({grid:html,metrics:elements.get('public-metrics').innerHTML,results:elements.get('bot-results').textContent,observed:elements.get('last-updated').textContent}));
  else console.log('Read-only renderer: valid LV line, unknown/mismatch fallback, escaped content ✓');
},25);
