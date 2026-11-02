/* Regression coverage for asynchronous note and conversation ownership. */
const test=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const vm=require('node:vm');
const source=path.resolve(__dirname,'../techdoc/static');
const deferred=()=>{let resolve;const promise=new Promise(r=>resolve=r);return {promise,resolve};};
class Element{
 constructor(text=''){this.textContent=text;this.value='';this.children=[];this.listeners={};this.dataset={};this.hidden=false;this.disabled=false;this.checked=false;this.options=[{},{}];this.scrollHeight=0;this.scrollTop=0;this.clientHeight=0;}
 append(...nodes){this.children.push(...nodes);} replaceChildren(...nodes){this.children=nodes;}
 addEventListener(name,fn){this.listeners[name]=fn;} setAttribute(){} close(){this.open=false;} showModal(){this.open=true;} focus(){} scrollIntoView(){}
}
function environment(file,api){
 const nodes=new Map(),storage=new Map(),errors=[];
 const node=id=>{if(!nodes.has(id))nodes.set(id,new Element());return nodes.get(id);};
 const state={kb:{id:'kb',state:'active'},view:{mode:'all_ready',draft:''},documents:[{id:'doc'}],capabilities:{generative_answer_enabled:true}};
 const sandbox={window:{WorkspaceModel:{selected:()=>['doc']}},document:{getElementById:node,createTextNode:s=>new Element(s)},
  sessionStorage:{getItem:k=>storage.get(k),setItem:(k,v)=>storage.set(k,v),removeItem:k=>storage.delete(k)},
  crypto:{randomUUID:(()=>{let n=0;return ()=>`request-${++n}`;})()},requestAnimationFrame:fn=>fn(),
  AbortController,TextDecoder,setTimeout,clearTimeout,fetch:()=>{throw Error('Unexpected stream');}};
 vm.runInNewContext(fs.readFileSync(path.join(source,file),'utf8'),sandbox);
 const options={api,current:()=>state,el:(_tag,text)=>new Element(text),button:(text,fn)=>Object.assign(new Element(text),{invoke:fn}),
  guard:async fn=>{try{return await fn();}catch(e){errors.push(e);}},setUrl:()=>{},saveView:()=>{},notice:()=>{},
  action:()=>{},closeAction:()=>{},pauseAuth:()=>{},sessionId:()=>null};
 return {sandbox,options,state,node,storage,errors};
}
function note(id,body='SERVER ORIGINAL'){return {id,artifact_id:'artifact-'+id,kb_id:'kb',title:id,body,revision:1,edited:true,citations:[],source_state:'valid'};}
async function edit(e,text){e.node('note-body').value=text;await e.node('note-body').listeners.input();}

test('P1: edits typed while copying remain recoverable in the original note',async()=>{
 const copy=deferred();let copyStarted=false;
 const e=environment('workspace_studio.js',async(url,opts)=>{
  if(url.includes('/notes?'))return {items:[note('original')]};
  if(opts?.method==='POST'){copyStarted=true;return copy.promise;}
  if(url.startsWith('/notes/copy?'))return note('copy','COPY SNAPSHOT');
  if(url.startsWith('/notes/original?'))return note('original');
  throw Error(url);
 });
 const studio=e.sandbox.window.WorkspaceStudio.create(e.options);await studio.load();await studio.open('original');
 await edit(e,'COPY SNAPSHOT');const pending=e.node('note-copy').listeners.click();assert.ok(copyStarted);
 await edit(e,'NEW TEXT ENTERED WHILE COPYING');copy.resolve({id:'copy'});await pending;
 await studio.open('original');
 assert.equal(e.node('note-body').value,'NEW TEXT ENTERED WHILE COPYING');
});

test('P2: revoking a note also hides the conflict server-version pane',async()=>{
 let revoked=false;
 const e=environment('workspace_studio.js',async(url,opts)=>{
  if(opts?.method==='PATCH')throw Object.assign(Error('Conflict'),{status:412});
  if(url.includes('/notes?'))return {items:[note('original')]};
  if(url.startsWith('/notes/original?'))return revoked?{...note('original',''),source_state:'revoked'}:note('original');
  throw Error(url);
 });
 const studio=e.sandbox.window.WorkspaceStudio.create(e.options);await studio.load();await studio.open('original');
 await edit(e,'LOCAL DRAFT');await e.node('note-save').listeners.click();await e.node('note-load-server').listeners.click();
 assert.equal(e.node('note-server').hidden,false);assert.match(e.node('note-server-body').textContent,/SERVER ORIGINAL/);
 revoked=true;await studio.load();assert.equal(e.node('note-body').value,'');
 assert.ok(e.node('note-server').hidden||!e.node('note-server-body').textContent.includes('SERVER ORIGINAL'));
});

function session(id,kb='kb'){return {id,kb_id:kb,revision:1,last_message_id:null};}
function messages(items=[],next=null){return {items,next_before:next,session_revision:1,last_message_id:null};}
async function typeQuestion(e,text){e.node('question').value=text;await e.node('question').listeners.input();}

test('P1: recovering session A cannot clear session B unconfirmed submission',async()=>{
 const found=deferred();let recovering=false;
 const e=environment('workspace_chat.js',async(url,opts)=>{
  if(url.includes('/submissions/')){recovering=true;return found.promise;}
  if(opts?.method==='POST'&&url.endsWith('/messages'))throw Error('Lost response');
  if(url.endsWith('/sessions'))return {items:[session('s1'),session('s2')]};
  if(url.startsWith('/sessions/')&&url.includes('/messages'))return messages();
  if(url.startsWith('/sessions/s1?'))return session('s1');
  if(url.startsWith('/sessions/s2?'))return session('s2');
  throw Error(url);
 });
 const chat=e.sandbox.window.WorkspaceConversation.create(e.options);e.node('answer-mode').value='answer';
 await chat.load(new URLSearchParams('session=s1'));await typeQuestion(e,'QUESTION A');await e.node('send-chat').listeners.click();
 const pending=chat.recover();assert.ok(recovering);
 await chat.load(new URLSearchParams('session=s2'));await typeQuestion(e,'QUESTION B');await e.node('send-chat').listeners.click();
 const key='rag.chat.state.v1:kb:s2';assert.ok(JSON.parse(e.storage.get(key)).pending);
 found.resolve({id:'runA',session_id:'s1',status:'succeeded',session_revision:2,last_message_id:'messageA'});await pending;
 assert.ok(JSON.parse(e.storage.get(key)).pending,'The unconfirmed submission for B was cleared by A');
});

function textOf(element){return [element.textContent||'',...element.children.map(textOf)].join(' ');}
test('P1: a delayed older page cannot insert answers from another knowledge base',async()=>{
 const older=deferred();let requested=false;
 const e=environment('workspace_chat.js',async(url)=>{
  if(url.includes('before_sequence=')){requested=true;return older.promise;}
  if(url.includes('/knowledge-bases/kb2/sessions'))return {items:[session('s2','kb2')]};
  if(url.endsWith('/sessions'))return {items:[session('s1')]};
  if(url.startsWith('/sessions/s1?'))return session('s1');
  if(url.startsWith('/sessions/s2?'))return session('s2','kb2');
  if(url.startsWith('/sessions/s1/messages'))return messages([],50);
  if(url.startsWith('/sessions/s2/messages'))return messages([{id:'b',role:'user',body:'KB2 question'}]);
  throw Error(url);
 });
 const chat=e.sandbox.window.WorkspaceConversation.create(e.options);await chat.load(new URLSearchParams('session=s1'));
 const pending=e.node('older-messages').listeners.click();assert.ok(requested);
 chat.detach();e.state.kb={id:'kb2',state:'active'};await chat.load(new URLSearchParams('session=s2'));
 older.resolve(messages([{id:'old',role:'assistant',run_id:'run1',run:{id:'run1',session_id:'s1',status:'succeeded',source_state:'valid',allowed_document_count:1,result:{status:'GENERATED_ANSWER',safe_output:'KB1 SOURCE ANSWER',source_extracts:[]}}}]));await pending;
 assert.ok(!textOf(e.node('messages')).includes('KB1 SOURCE ANSWER'),'KB1 answer appeared under KB2');
});

test('an unchanged copy opens the saved snapshot',async()=>{
 const e=environment('workspace_studio.js',async(url,opts)=>{
  if(url.includes('/notes?'))return {items:[note('original'),note('copy')]};
  if(opts?.method==='POST')return {id:'copy',client_revision:opts.body.client_revision};
  return url.startsWith('/notes/copy?')?note('copy','COPIED TEXT'):note('original');
 });
 const studio=e.sandbox.window.WorkspaceStudio.create(e.options);await studio.load();await studio.open('original');
 await edit(e,'COPIED TEXT');await e.node('note-copy').listeners.click();
 assert.equal(e.node('note-body').value,'COPIED TEXT');assert.equal(e.node('note-title').value,'copy');assert.equal(e.errors.length,0);
});

test('a server-version response started before revocation cannot reveal the old body later',async()=>{
 const server=deferred();let revoked=false,waiting=false;
 const e=environment('workspace_studio.js',async(url)=>{
  if(url.includes('/notes?'))return {items:[note('original')]};
  if(waiting){waiting=false;return server.promise;}
  return revoked?{...note('original',''),source_state:'revoked'}:note('original');
 });
 const studio=e.sandbox.window.WorkspaceStudio.create(e.options);await studio.load();await studio.open('original');
 waiting=true;const pending=e.node('note-load-server').listeners.click();revoked=true;await studio.load();
 server.resolve(note('original'));await pending;
 assert.equal(e.node('note-server').hidden,true);assert.equal(e.node('note-server-body').textContent,'');assert.equal(e.node('note-body').value,'');
});

function recoveryFixture(){
 let retry=null;const posts=[];
 const e=environment('workspace_chat.js',async(url,opts)=>{
  if(url.includes('/submissions/'))throw Object.assign(Error('Not received'),{status:404});
  if(opts?.method==='POST'&&url.endsWith('/messages')){
   posts.push({url,body:opts.body});if(posts.length===1)throw Error('Lost response');
   return {id:'runA',session_id:'s1',status:'succeeded',session_revision:2,last_message_id:'messageA'};
  }
  if(url.endsWith('/sessions'))return {items:[session('s1'),session('s2')]};
  if(url.includes('/messages'))return messages();
  return url.startsWith('/sessions/s1?')?session('s1'):session('s2');
 });
 e.options.action=(_title,_nodes,submit)=>{retry=submit;};
 const chat=e.sandbox.window.WorkspaceConversation.create(e.options);
 return {e,chat,posts,retry:()=>retry};
}

test('explicit recovery retry keeps the original endpoint and request key',async()=>{
 const {e,chat,posts,retry}=recoveryFixture();await chat.load(new URLSearchParams('session=s1'));
 await typeQuestion(e,'QUESTION A');await e.node('send-chat').listeners.click();await chat.recover();
 await retry()();assert.equal(posts.length,2);assert.equal(posts[0].url,posts[1].url);assert.equal(posts[0].body.request_key,posts[1].body.request_key);
 assert.equal(JSON.parse(e.storage.get('rag.chat.state.v1:kb:s1')).pending,null);
});

test('a retry dialog belonging to a departed session cannot post into the new session',async()=>{
 const {e,chat,posts,retry}=recoveryFixture();await chat.load(new URLSearchParams('session=s1'));
 await typeQuestion(e,'QUESTION A');await e.node('send-chat').listeners.click();await chat.recover();const oldRetry=retry();
 await chat.load(new URLSearchParams('session=s2'));
 await assert.rejects(oldRetry,/对话已切换/);assert.equal(posts.length,1);
 assert.ok(JSON.parse(e.storage.get('rag.chat.state.v1:kb:s1')).pending);
});

test('returning to the same session does not accept a pagination response from its earlier visit',async()=>{
 const old=deferred();let calls=0;
 const e=environment('workspace_chat.js',async(url)=>{
  if(url.includes('before_sequence=')){calls++;return old.promise;}
  if(url.endsWith('/sessions'))return {items:[session('s1'),session('s2')]};
  if(url.includes('/messages'))return messages([],50);
  return url.startsWith('/sessions/s1?')?session('s1'):session('s2');
 });
 const chat=e.sandbox.window.WorkspaceConversation.create(e.options);await chat.load(new URLSearchParams('session=s1'));
 const pending=e.node('older-messages').listeners.click();await e.node('older-messages').listeners.click();assert.equal(calls,1);
 await chat.load(new URLSearchParams('session=s2'));await chat.load(new URLSearchParams('session=s1'));
 old.resolve(messages([{id:'stale',role:'user',body:'STALE VISIT'}]));await pending;
 assert.ok(!textOf(e.node('messages')).includes('STALE VISIT'));assert.equal(e.node('older-messages').disabled,false);
});

test('a current pagination request appends only previously unseen message ids',async()=>{
 const e=environment('workspace_chat.js',async(url)=>{
  if(url.endsWith('/sessions'))return {items:[session('s1')]};
  if(url.includes('before_sequence='))return messages([{id:'old',role:'user',body:'OLDER'},{id:'latest',role:'user',body:'LATEST'}]);
  if(url.includes('/messages'))return messages([{id:'latest',role:'user',body:'LATEST'}],50);
  return session('s1');
 });
 const chat=e.sandbox.window.WorkspaceConversation.create(e.options);await chat.load(new URLSearchParams('session=s1'));
 await e.node('older-messages').listeners.click();assert.equal(e.node('messages').children.length,2);
 assert.equal(e.node('older-messages').hidden,true);assert.equal(e.node('older-messages').disabled,false);assert.equal(e.errors.length,0);
});

test('conversation input stays locked until both session identity and messages are loaded',async()=>{
 const sessions=deferred(),page=deferred();
 const e=environment('workspace_chat.js',url=>url.endsWith('/sessions')?sessions.promise:page.promise);
 e.storage.set('rag.chat.state.v1:kb:s1',JSON.stringify({draft:'EXISTING SESSION DRAFT'}));
 const chat=e.sandbox.window.WorkspaceConversation.create(e.options),loading=chat.load(new URLSearchParams());
 assert.equal(e.node('question').disabled,true);assert.equal(e.node('answer-mode').disabled,true);
 sessions.resolve({items:[session('s1')]});await Promise.resolve();await Promise.resolve();
 assert.equal(e.node('question').disabled,true);
 page.resolve(messages());await loading;
 assert.equal(e.node('question').disabled,false);assert.equal(e.node('question').value,'EXISTING SESSION DRAFT');
 assert.ok(!e.node('chat-status').textContent.includes('正在加载'));
});

test('a failed session load preserves the saved draft and exposes a persistent recovery instruction',async()=>{
 const e=environment('workspace_chat.js',async()=>{throw Error('offline');});
 const saved=JSON.stringify({draft:'RECOVERABLE'});e.storage.set('rag.chat.state.v1:kb:s1',saved);
 const chat=e.sandbox.window.WorkspaceConversation.create(e.options);
 await assert.rejects(chat.load(new URLSearchParams()),/offline/);
 assert.equal(e.node('question').disabled,true);assert.equal(e.storage.get('rag.chat.state.v1:kb:s1'),saved);
 assert.match(e.node('chat-status').textContent,/刷新重试/);
});

test('a departed knowledge base cannot unlock or overwrite the new conversation after a slow load',async()=>{
 const old=deferred();const e=environment('workspace_chat.js',async url=>{
  if(url==='/knowledge-bases/kb/sessions')return old.promise;
  if(url==='/knowledge-bases/kb2/sessions')return {items:[]};
  throw Error(url);
 });
 const chat=e.sandbox.window.WorkspaceConversation.create(e.options),pending=chat.load(new URLSearchParams());
 chat.detach();e.state.kb={id:'kb2',state:'active'};e.state.view={mode:'all_ready',draft:'KB2 DRAFT'};
 await chat.load(new URLSearchParams());old.resolve({items:[session('s1')]});await pending;
 assert.equal(e.node('question').disabled,false);assert.equal(e.node('question').value,'KB2 DRAFT');
});

function findChild(root,text){return root.children.find(n=>n.textContent===text);}
test('note pagination reaches the oldest item, deduplicates boundaries and prevents overlapping requests',async()=>{
 const page=deferred();let calls=0;const first=Array.from({length:100},(_,i)=>note('n'+i));
 const e=environment('workspace_studio.js',async url=>{
  if(url.includes('offset=100')){calls++;return page.promise;}
  return {items:first,next_offset:100};
 });
 const studio=e.sandbox.window.WorkspaceStudio.create(e.options);await studio.load();
 const more=findChild(e.node('studio-notes'),'加载更多笔记');assert.ok(more);
 const pending=more.invoke();await more.invoke();assert.equal(calls,1);
 page.resolve({items:[note('n99'),note('oldest')],next_offset:null});await pending;
 assert.equal(e.node('studio-notes').children.length,101);assert.ok(findChild(e.node('studio-notes'),'oldest'));
 assert.ok(!findChild(e.node('studio-notes'),'加载更多笔记'));
});

test('failed note pagination keeps its existing list and can retry the same offset',async()=>{
 let failed=true;const offsets=[];const e=environment('workspace_studio.js',async url=>{
  if(url.includes('offset=100')){offsets.push(url);if(failed)throw Error('offline');return {items:[note('oldest')],next_offset:null};}
  return {items:[note('recent')],next_offset:100};
 });
 const studio=e.sandbox.window.WorkspaceStudio.create(e.options);await studio.load();
 await assert.rejects(findChild(e.node('studio-notes'),'加载更多笔记').invoke(),/offline/);
 assert.ok(findChild(e.node('studio-notes'),'recent'));const retry=findChild(e.node('studio-notes'),'加载更多笔记');assert.equal(retry.disabled,false);
 failed=false;await retry.invoke();assert.equal(offsets[0],offsets[1]);assert.ok(findChild(e.node('studio-notes'),'oldest'));
});

test('a late note page cannot insert items into a different knowledge base',async()=>{
 const page=deferred();const e=environment('workspace_studio.js',async url=>{
  if(url.includes('offset=100'))return page.promise;
  return {items:[note(url.includes('/kb2/')?'KB2 NOTE':'KB1 NOTE')],next_offset:url.includes('/kb2/')?null:100};
 });
 const studio=e.sandbox.window.WorkspaceStudio.create(e.options);await studio.load();
 const pending=findChild(e.node('studio-notes'),'加载更多笔记').invoke();
 studio.detach();e.state.kb={id:'kb2',state:'active'};await studio.load();
 page.resolve({items:[note('OLD KB SECRET')],next_offset:null});await pending;
 assert.ok(findChild(e.node('studio-notes'),'KB2 NOTE'));assert.ok(!findChild(e.node('studio-notes'),'OLD KB SECRET'));
});
