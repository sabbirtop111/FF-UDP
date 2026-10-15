/* Browser-like, offline login submit regression for the actual admin app.js. */
const fs=require('fs'),vm=require('vm'),assert=require('assert'),path=require('path');
const src=fs.readFileSync(path.join(__dirname,'../static/js/app.js'),'utf8');
const html=fs.readFileSync(path.join(__dirname,'../static/index.html'),'utf8');
const ids=[...html.matchAll(/id="([^"]+)"/g)].map(x=>x[1]);
function fixture(mode){
 const nodes=new Map(),calls=[];
 for(const id of ids){
  const flags=new Set(id==='admin-shell'?['is-hidden']:[]),events={};
  nodes.set(id,{id,events,fields:{username:'admin',password:'correct-strong-password'},
    disabled:id==='login-submit'&&/id="login-submit"[^>]*disabled/.test(html),value:'',innerHTML:'',textContent:'',style:{},dataset:{},
    classList:{add(x){flags.add(x)},remove(x){flags.delete(x)},contains(x){return flags.has(x)},toggle(x,on){if(on===undefined? !flags.has(x):on)flags.add(x);else flags.delete(x)}},
    addEventListener(type,fn){events[type]=fn},reset(){this.resetCalled=true},querySelector(){return null}});
 }
 let authed=false;
 const document={getElementById:id=>nodes.get(id),querySelectorAll:()=>[],body:{style:{}},addEventListener(){}};
 const fetch=async(url,opts={})=>{
   calls.push(url);
   if(mode==='offline')throw new TypeError('Failed to fetch');
   if(url==='/api/auth/login'){
     const credentials=JSON.parse(opts.body);
     if(mode==='invalid'||credentials.password!=='correct-strong-password')return {ok:false,status:401,json:async()=>({detail:'Invalid administrator username or password.'})};
     authed=true;return {ok:true,status:200,json:async()=>({ok:true,user:{username:'admin',role:'admin'}})};
   }
   if(url==='/api/auth/me'){
     if(mode==='boot-pending'&&!authed)return new Promise(()=>{});
     return authed?{ok:true,status:200,json:async()=>({ok:true,user:{username:'admin',role:'admin'}})}:{ok:false,status:401,json:async()=>({detail:'Authentication required.'})};
   }
   if(url==='/api/dashboard/overview')return {ok:true,status:200,json:async()=>({accounts:[],summary:{},status_breakdown:{},recent_activity:[]})};
   if(url==='/api/regions')return {ok:true,status:200,json:async()=>({regions:[{code:'BD'}]})};
   throw Error('Unexpected route '+url);
 };
 const FormData=class{constructor(form){this.fields=form.fields}get(k){return this.fields[k]}};
 const fastTimeout=(fn,delay,...args)=>delay>=10000?{deferred:true}:setTimeout(fn,delay,...args);
 const fastClearTimeout=id=>{if(!id?.deferred)clearTimeout(id);};
 const ctx=vm.createContext({document,fetch,FormData,window:{scrollTo(){}},setInterval(){},setTimeout:fastTimeout,clearTimeout:fastClearTimeout,AbortController,console});
 vm.runInContext(src,ctx);
 return {nodes,calls};
}
const pause=()=>new Promise(r=>setTimeout(r,20));
async function submit(nodes){const form=nodes.get('login-form');await form.events.submit({preventDefault(){},target:form});await pause()}
(async()=>{
 const ok=fixture('valid');
 assert.strictEqual(ok.nodes.get('login-submit').disabled,false,'login must be available before the boot check finishes');
 await pause();
 assert.strictEqual(ok.nodes.get('login-submit').disabled,false);
 assert(ok.nodes.get('login-status').textContent.includes('ready'));
 await submit(ok.nodes);
 assert(ok.calls.includes('/api/auth/login')&&ok.calls.includes('/api/auth/me'));
 assert.strictEqual(ok.nodes.get('admin-shell').classList.contains('is-hidden'),false);
 assert.strictEqual(ok.nodes.get('login-screen').classList.contains('is-hidden'),true);
 assert(ok.nodes.get('login-form').resetCalled);
 const pending=fixture('boot-pending');
 assert.strictEqual(pending.nodes.get('login-submit').disabled,false);
 await submit(pending.nodes);
 assert(pending.calls.includes('/api/auth/login'));
 assert.strictEqual(pending.nodes.get('admin-shell').classList.contains('is-hidden'),false,'a pending boot check must not block sign-in');
 const denied=fixture('invalid');await pause();await submit(denied.nodes);
 assert(denied.nodes.get('login-error').textContent.includes('Invalid administrator'));
 assert.strictEqual(denied.nodes.get('admin-shell').classList.contains('is-hidden'),true);
 assert.strictEqual(denied.nodes.get('login-submit').disabled,false);
 const offline=fixture('offline');await pause();await submit(offline.nodes);
 assert(/Cannot reach|Failed to fetch/.test(offline.nodes.get('login-error').textContent));
 assert(offline.nodes.get('login-status').textContent.includes('failed'));
 console.log('Admin UI login: success, boot-pending, rejection, offline feedback, button state ✓');
})().catch(e=>{console.error(e);process.exitCode=1});
