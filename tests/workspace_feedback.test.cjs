/* Real controllers with controllable network, clocks and DOM; no generated answers. */
const test=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
const deferred=()=>{let resolve;const promise=new Promise(r=>resolve=r);return {promise,resolve};};
class Element{
 constructor(tag,text='',attrs={}){this.tag=tag;this.textContent=text;this.attrs=attrs;this.children=[];this.listeners={};this.dataset={};this.value='';this.options=[{},{}];this.scrollTop=0;this.scrollHeight=0;this.clientHeight=0;this.checked=false;}
 append(...n){this.children.push(...n);}replaceChildren(...n){this.children=n;}addEventListener(k,f){this.listeners[k]=f;}setAttribute(k,v){this.attrs[k]=v;}focus(){this.focused=true;}close(){this.open=false;}showModal(){this.open=true;}
}
const all=n=>[n,...n.children.flatMap(all)],text=n=>all(n).map(x=>x.textContent||'').join(' ');
function fixture(file,handler,{local=new Map(),storage=new Map()}={}){
 const nodes=new Map(),requests=[],timers=new Map(),notices=[],errors=[];let tick=0,now=Date.parse('2026-10-30T12:00:00Z'),seq=0;
 const node=id=>{if(!nodes.has(id))nodes.set(id,new Element('div'));return nodes.get(id);};
 const state={kb:{id:'kb',state:'active'},documents:[{id:'doc'}],view:{mode:'all_ready',draft:''},capabilities:{generative_answer_enabled:true}};
 class Clock extends Date{static now(){return now;}}
 const sandbox={window:{WorkspaceModel:{selected:()=>state.documents.map(d=>d.id)}},document:{getElementById:node,createTextNode:t=>new Element('text',t)},
  Date:Clock,crypto:{randomUUID:()=>`req-${++seq}`},AbortController,TextDecoder,URLSearchParams,
  sessionStorage:{getItem:k=>storage.get(k),setItem:(k,v)=>storage.set(k,v),removeItem:k=>storage.delete(k)},
  localStorage:{getItem:k=>local.get(k),setItem:(k,v)=>local.set(k,v)},
  setTimeout:(fn,ms)=>{const id=++tick;timers.set(id,{fn,ms});return id;},clearTimeout:id=>timers.delete(id),requestAnimationFrame:fn=>fn(),fetch:()=>new Promise(()=>{})};
 vm.runInNewContext(fs.readFileSync(path.join(__dirname,'../techdoc/static',file),'utf8'),sandbox);
 const options={current:()=>state,api:async(url,opts)=>{requests.push({url,opts});return handler(url,opts);},
  el:(tag,t='',attrs={})=>new Element(tag,t,attrs),button:(t,invoke,cls)=>Object.assign(new Element('button',t,{class:cls}),{invoke}),
  guard:async f=>{try{return await f();}catch(e){errors.push(e);}},notice:t=>notices.push(t),setUrl:()=>{},sessionId:()=>null,
  pauseAuth:()=>{state.authPaused=true;},action:()=>{},openCitation:()=>{}};
 return {node,state,requests,sandbox,options,timers,notices,errors,storage,local,
  advance(ms){now+=ms;const entry=[...timers].find(([,v])=>v.ms===1000);assert.ok(entry,'Expected a waiting timer');timers.delete(entry[0]);entry[1].fn();}};
}
const session=id=>({id,kb_id:'kb',revision:1,last_message_id:'m2'});
function chatFixture(status='failed',extra={},handler,settings){
 const run={id:'run',session_id:'s1',status,source_state:'valid',created_at:'2026-10-30T11:59:20Z',result:null,...extra};
 const items=[{id:'m1',role:'user',run_id:'run',body:'原始完整问题'},{id:'m2',role:'assistant',run_id:'run',run}];
 const e=fixture('workspace_chat.js',async(url,opts)=>{
  const custom=handler?.(url,opts);if(custom!==undefined)return custom;
  if(url.endsWith('/sessions'))return {items:[session('s1')]};
  if(url.includes('/messages'))return {items,next_before:null,session_revision:1,last_message_id:'m2'};
  if(url.startsWith('/runs/'))return run;
  return session(url.includes('s2')?'s2':'s1');
 },settings);e.run=run;e.items=items;e.controller=e.sandbox.window.WorkspaceConversation.create(e.options);return e;
}

test('provider limits have their own label and evidence mode requires explicit send',async()=>{
 const e=chatFixture('abstained',{result:{status:'ABSTAIN_PROVIDER_FAILURE',reason:'PRIVATE供应商原始错误',provider_failure:{kind:'quota'}}});
 await e.controller.load(new URLSearchParams('session=s1'));assert.match(text(e.node('messages')),/模型用量已达上限/);assert.doesNotMatch(text(e.node('messages')),/需要补充依据|PRIVATE/);
 const action=all(e.node('messages')).find(n=>n.textContent==='只查原文');await action.invoke();
 assert.equal(e.node('answer-mode').value,'evidence_only');assert.equal(e.node('question').value,'原始完整问题');assert.equal(e.requests.filter(r=>r.opts?.method==='POST').length,0);
 assert.match(e.node('chat-status').textContent,/尚未提交/);
});

test('evidence fallback preserves an existing draft and its answer mode',async()=>{
 const e=chatFixture('abstained',{result:{status:'ABSTAIN_PROVIDER_FAILURE',provider_failure:{kind:'network'}}});
 await e.controller.load(new URLSearchParams('session=s1'));e.node('question').value='正在编辑的草稿';e.node('answer-mode').value='answer';
 await all(e.node('messages')).find(n=>n.textContent==='只查原文').invoke();
 assert.equal(e.node('question').value,'正在编辑的草稿');assert.equal(e.node('answer-mode').value,'answer');assert.equal(e.requests.filter(r=>r.opts?.method==='POST').length,0);
});

test('legacy provider errors and hostile metadata never become evidence shortage or raw messages',async()=>{
 for(const kind of [undefined,'__proto__',{private:'PRIVATE'}]){
  const e=chatFixture('abstained',{result:{status:'ABSTAIN_PROVIDER_FAILURE',reason:'PRIVATE供应商异常',provider_failure:{kind}}});
  await e.controller.load(new URLSearchParams('session=s1'));assert.match(text(e.node('messages')),/模型处理未完成/);assert.doesNotMatch(text(e.node('messages')),/PRIVATE|需要补充依据/);
 }
});
test('waiting uses persisted creation time, advances without rebuilding the card, and stops on detach',async()=>{
 const e=chatFixture('running');await e.controller.load(new URLSearchParams('session=s1'));
 const card=e.node('messages').children[1],clock=all(card).find(n=>n.attrs.class==='wait-elapsed');assert.match(clock.textContent,/40 秒/);
 e.advance(7000);assert.equal(e.node('messages').children[1],card);assert.match(clock.textContent,/47 秒/);assert.equal(clock.attrs['aria-live'],'off');
 e.controller.detach();assert.equal([...e.timers.values()].filter(t=>t.ms===1000).length,0);
});
test('reload reconstructs the same waiting time and completion removes its timer',async()=>{
 const e=chatFixture('running');await e.controller.load(new URLSearchParams('session=s1'));e.controller.detach();
 await e.controller.load(new URLSearchParams('session=s1'));assert.match(text(e.node('messages')),/40 秒/);
 e.run.status='cancelled';await e.controller.refresh();assert.equal([...e.timers.values()].filter(t=>t.ms===1000).length,0);
});
test('SSE elapsed fallback survives reload when the server creation time is unavailable',async()=>{
 const storage=new Map([['rag.chat.state.v1:kb:s1',JSON.stringify({activeRunId:'run',stage:'筛选相关资料',progress:{id:'run',elapsed:31000,received:Date.parse('2026-10-30T11:59:58Z')}})]]);
 const e=chatFixture('running',{created_at:null},undefined,{storage});await e.controller.load(new URLSearchParams('session=s1'));
 assert.match(text(e.node('messages')),/33 秒/);e.controller.detach();
});
test('a failed question is copied into an empty draft without sending or cloning old scope',async()=>{
 const e=chatFixture();await e.controller.load(new URLSearchParams('session=s1'));
 await all(e.node('messages')).find(n=>n.textContent==='重新编辑此问题').invoke();
 assert.equal(e.node('question').value,'原始完整问题');assert.equal(e.requests.filter(r=>r.opts?.method==='POST').length,0);
 assert.match(e.node('chat-status').textContent,/当前所选资料/);assert.equal(e.state.view.draft,'原始完整问题');
});
test('re-edit never replaces a draft that already exists',async()=>{
 const e=chatFixture('cancelled');await e.controller.load(new URLSearchParams('session=s1'));
 e.node('question').value='正在写的新问题';e.node('question').listeners.input();
 await all(e.node('messages')).find(n=>n.textContent==='重新编辑此问题').invoke();
 assert.equal(e.node('question').value,'正在写的新问题');assert.match(e.notices[0],/草稿/);
});
test('late history lookup cannot overwrite typing or the draft of a departed session',async()=>{
 for(const switchSession of [false,true]){
  const response=deferred(),e=chatFixture('interrupted',{},url=>url.includes('around_run_id')?response.promise:undefined);
  e.items.shift();await e.controller.load(new URLSearchParams('session=s1'));
  const editing=all(e.node('messages')).find(n=>n.textContent==='重新编辑此问题').invoke();
  if(switchSession)await e.controller.load(new URLSearchParams('session=s2'));
  e.node('question').value='新的草稿';e.node('question').listeners.input();response.resolve({items:[{role:'user',run_id:'run',body:'旧问题'}]});await editing;
  assert.equal(e.node('question').value,'新的草稿');
 }
});
test('read-only state disables re-edit',async()=>{
 const e=chatFixture();await e.controller.load(new URLSearchParams('session=s1'));e.state.kb.state='archived';e.controller.scopeChanged();
 assert.equal(all(e.node('messages')).find(n=>n.textContent==='重新编辑此问题').disabled,true);
});
test('unconfirmed submission keeps its recovery contract and disables re-edit',async()=>{
 const e=chatFixture('failed',{},(_url,opts)=>opts?.method==='POST'?Promise.reject(Error('Lost acknowledgement')):undefined);
 e.node('answer-mode').value='answer';await e.controller.load(new URLSearchParams('session=s1'));
 e.node('question').value='新问题';e.node('question').listeners.input();await e.node('send-chat').listeners.click();
 assert.equal(all(e.node('messages')).find(n=>n.textContent==='重新编辑此问题').disabled,true);
 assert.ok(JSON.parse(e.storage.get('rag.chat.state.v1:kb:s1')).pending);
});
test('an active newer run disables edits of an older failed question',async()=>{
 const busy={id:'busy',session_id:'s1',status:'running',created_at:'2026-10-30T12:00:00Z',result:null};
 const e=chatFixture('failed',{},url=>url.startsWith('/runs/busy')?busy:undefined);
 e.items.push({id:'m3',role:'user',run_id:'busy',body:'新问题'},{id:'m4',role:'assistant',run_id:'busy',run:busy});
 await e.controller.load(new URLSearchParams('session=s1'));
 assert.equal(all(e.node('messages')).find(n=>n.textContent==='重新编辑此问题').disabled,true);e.controller.detach();
});
test('actual SSE parsing retains server elapsed_ms and updates the stage without query resubmission',async()=>{
 const e=chatFixture('running',{created_at:null}),readers=[];
 vm.runInNewContext(fs.readFileSync(path.join(__dirname,'../techdoc/static/query_stream.js'),'utf8'),e.sandbox);
 e.sandbox.fetch=async(_url,{signal})=>({ok:true,status:200,body:{getReader:()=>({
  read:()=>new Promise((resolve,reject)=>{readers.push({resolve,signal});signal.addEventListener('abort',()=>reject(Error('aborted')),{once:true});}),releaseLock(){}
 })}});
 await e.controller.load(new URLSearchParams('session=s1'));for(let i=0;i<10;i++)await Promise.resolve();
 const active=readers.filter(r=>!r.signal.aborted).at(-1);assert.ok(active);
 active.resolve({done:false,value:Buffer.from('id: 5\nevent: progress\ndata: '+JSON.stringify({stage:'rerank',label:'筛选相关资料',elapsed_ms:12500,pairs_completed:20,pairs_total:52,cache_hits:4,scored_pairs:16})+'\n\n')});
 for(let i=0;i<10;i++)await Promise.resolve();
 assert.match(text(e.node('messages')),/筛选相关资料/);assert.match(text(e.node('messages')),/12 秒/);
 assert.equal(JSON.parse(e.storage.get('rag.chat.state.v1:kb:s1')).progress.elapsed,12500);
 assert.match(text(e.node('messages')),/20\/52 对候选（缓存 4，实际评分 16）/);
 assert.equal(e.requests.filter(r=>r.opts?.method==='POST').length,0);e.controller.detach();
});
test('history loads additional pages, searches literally and offers reversible archival',async()=>{
 const requests=[],rows=Array.from({length:23},(_,i)=>({id:'h'+i,title:'对话 '+i,revision:1,state:'active'}));
 const e=chatFixture('failed',{},(url,opts)=>{
  if(url.includes('/sessions?')){requests.push(url);const u=new URL(url,'https://test.invalid'),offset=Number(u.searchParams.get('offset')),search=u.searchParams.get('search');return {items:rows.filter(r=>!search||r.title.includes(search)).slice(offset,offset+20),next_offset:!search&&offset===0?20:null};}
 });
 e.options.action=(title,nodes,submit)=>{e.node('action-dialog').open=true;e.node('action-body').replaceChildren(...nodes);e.action={title,submit};};
 await e.controller.load(new URLSearchParams('session=s1'));await e.node('history-chat').listeners.click();
 assert.equal(all(e.node('action-body')).filter(n=>n.attrs.class==='history-row').length,20);
 await all(e.node('action-body')).find(n=>n.textContent==='加载更多对话').invoke();
 assert.equal(all(e.node('action-body')).filter(n=>n.attrs.class==='history-row').length,23);
 const search=all(e.node('action-body')).find(n=>n.tag==='input');search.value='对话 22';search.listeners.keydown({key:'Enter',preventDefault(){}});
 for(let i=0;i<10;i++)await Promise.resolve();assert.equal(all(e.node('action-body')).filter(n=>n.attrs.class==='history-row').length,1);
 all(e.node('action-body')).find(n=>n.textContent==='归档').invoke();assert.equal(e.action.title,'归档对话');assert.match(text(e.node('action-body')),/已保存笔记保留/);
 assert.ok(requests.some(u=>u.includes('offset=20')));
});
test('archiving the current session preserves its draft and leaves the archived conversation',async()=>{
 let body;const e=chatFixture('failed',{},(url,opts)=>{
  if(url.includes('/sessions?'))return {items:[{id:'s1',title:'当前对话',revision:1}],next_offset:null};
  if(url.endsWith('/archive')){body=opts.body;return {id:'s1',state:'archived',revision:2};}
 });
 e.options.action=(_title,nodes,submit)=>{e.node('action-dialog').open=true;e.node('action-body').replaceChildren(...nodes);e.submit=submit;};
 await e.controller.load(new URLSearchParams('session=s1'));e.node('question').value='保留草稿';e.node('question').listeners.input();
 await e.node('history-chat').listeners.click();all(e.node('action-body')).find(n=>n.textContent==='归档').invoke();await e.submit();
 assert.equal(e.controller.sessionId(),undefined);assert.equal(body.kb_id,'kb');
 assert.equal(JSON.parse(e.storage.get('rag.chat.state.v1:kb:s1')).draft,'保留草稿');
});
test('disconnect and auth loss are shown as uncertainty without resubmitting the question',async()=>{
 for(const auth of [false,true]){
  const e=chatFixture('running');e.sandbox.fetch=async()=>{if(auth)return {status:401};throw Error('offline');};
  await e.controller.load(new URLSearchParams('session=s1'));for(let i=0;i<15;i++)await Promise.resolve();
  assert.match(text(e.node('messages')),auth?/登录已失效/:/连接已断开/);
  assert.equal(e.requests.filter(r=>r.opts?.method==='POST').length,0);e.controller.detach();
 }
});
test('generated answer discloses model review; evidence-only and refused output do not imply generated success',async()=>{
 for(const [status,resultStatus,body] of [['succeeded','GENERATED_ANSWER','回答'],['succeeded','EVIDENCE_ONLY','原文'],['abstained','ABSTAIN_SUPPORT_REVIEW','UNREVIEWED']]){
  const e=chatFixture(status,{result:{status:resultStatus,safe_output:body,source_extracts:[]}});await e.controller.load(new URLSearchParams('session=s1'));
  assert.equal(text(e.node('messages')).includes('未经人工审核'),resultStatus==='GENERATED_ANSWER');
  if(status==='abstained')assert.ok(!text(e.node('messages')).includes('UNREVIEWED'));
 }
});
function overviewFixture(handler,settings){
 const e=fixture('workspace_overview.js',async(url,opts)=>{
  const custom=handler?.(url,opts);if(custom!==undefined)return custom;
  if(url.includes('/artifacts?'))return {items:[],next_offset:null};
  if(url.endsWith('/overview/resolve'))return {artifact:null};
  if(url.endsWith('/overview'))return {id:'overview',status:'running',questions:[]};
  throw Error(url);
 },settings);e.controller=e.sandbox.window.WorkspaceOverview.create(e.options);return e;
}
const autoBox=e=>all(e.node('overview-main')).find(n=>n.tag==='input');
test('opening a ready library defaults to manual generation without a model submission',async()=>{
 const e=overviewFixture();await e.controller.load();assert.equal(e.requests.filter(r=>r.url.endsWith('/overview')).length,0);
 assert.equal(autoBox(e).checked,false);assert.match(text(e.node('overview-main')),/会调用模型/);
 await all(e.node('overview-main')).find(n=>n.textContent==='生成当前范围概览').invoke();assert.equal(e.requests.filter(r=>r.url.endsWith('/overview')).length,1);
});
test('explicit auto consent is remembered per library and turning it off never cancels active work',async()=>{
 const e=overviewFixture();await e.controller.load();autoBox(e).checked=true;await autoBox(e).listeners.change();
 assert.equal(e.local.get('rag.overview.auto.v1:kb'),'1');assert.equal(e.requests.filter(r=>r.url.endsWith('/overview')).length,1);
 autoBox(e).checked=false;await autoBox(e).listeners.change();assert.equal(e.local.get('rag.overview.auto.v1:kb'),'0');
 assert.equal(e.requests.filter(r=>r.url.endsWith('/cancel')).length,0);assert.match(e.notices.at(-1),/已有任务|已开始的任务/);
 e.state.kb.id='other';await e.controller.load();assert.equal(autoBox(e).checked,false);
});
test('switching auto off invalidates an older resolve before it can submit a model task',async()=>{
 const response=deferred(),e=overviewFixture(undefined,{local:new Map([['rag.overview.auto.v1:kb','1']])});await e.controller.load();
 // Start with a rendered setting, then defer the next resolve from a scope refresh.
 const original=e.options.api;let wait=true;e.options.api=async(url,opts)=>url.endsWith('/overview/resolve')&&wait?response.promise:original(url,opts);
 const loading=e.controller.load();await Promise.resolve();await Promise.resolve();await Promise.resolve();
 const prior=e.requests.filter(r=>r.url.endsWith('/overview')).length;wait=false;autoBox(e).checked=false;await autoBox(e).listeners.change();
 response.resolve({artifact:null});await loading;assert.equal(e.requests.filter(r=>r.url.endsWith('/overview')).length,prior);
});
test('storage write failure still honors the current-page choice and selected scopes stay manual',async()=>{
 const e=overviewFixture();e.sandbox.localStorage.setItem=()=>{throw Error('QuotaExceeded');};await e.controller.load();
 autoBox(e).checked=true;await autoBox(e).listeners.change();assert.equal(autoBox(e).checked,true);assert.match(e.notices[0],/无法保存/);
 e.state.view.mode='selected';const before=e.requests.filter(r=>r.url.endsWith('/overview')).length;await e.controller.load();
 assert.equal(e.requests.filter(r=>r.url.endsWith('/overview')).length,before);
});
test('overview refresh preserves an open settings panel',async()=>{
 const e=overviewFixture();await e.controller.load();all(e.node('overview-main')).find(n=>n.attrs.class==='overview-settings').open=true;
 await e.controller.load();assert.equal(all(e.node('overview-main')).find(n=>n.attrs.class==='overview-settings').open,true);
});
