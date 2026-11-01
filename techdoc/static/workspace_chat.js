/* Durable runs: navigation, reconnect and refresh never replay a message POST. */
window.WorkspaceConversation={outcome(result){
 const messages={quota:['模型用量已达上限','大模型服务的用量已达上限。可待额度恢复后重试，或选择只查原文。'],rate_limit:['模型调用受限','大模型服务暂时限制调用频率。请稍后重试，或选择只查原文。'],network:['模型连接未完成','大模型服务连接超时或中断。可稍后重试，或选择只查原文。'],authentication:['模型认证未通过','请检查后端大模型密钥配置；也可选择只查原文。'],access_denied:['模型访问受限','请检查后端模型配置和服务权限；也可选择只查原文。'],unavailable:['模型服务暂不可用','大模型服务暂不可用。可稍后重试，或选择只查原文。'],invalid_response:['模型回复未通过检查','大模型返回内容未通过格式或完整性检查。可重试，或选择只查原文。'],unknown:['模型处理未完成','大模型处理未完成。可稍后重试，或选择只查原文。']};
 if(result?.status==='ABSTAIN_PROVIDER_FAILURE'){const kind=result.provider_failure?.kind,pair=Object.hasOwn(messages,kind)?messages[kind]:messages.unknown;return {label:pair[0],reason:pair[1]};}
 if(result?.status==='ABSTAIN_FACT_CONTRADICTION')return {label:'回答依据存在冲突',reason:'回答中的事实关系未通过引用检查。可查看原文核对，或调整问题后重试。'};
 if(result?.status==='ABSTAIN_CONTEXT_BUDGET')return {label:'本次资料范围过大',reason:'本次上下文超过处理范围。请减少所选资料或缩小问题范围后重试。'};
 return {label:'需要补充依据',reason:null};
},create(o){
 'use strict';
 const $=id=>document.getElementById(id),M=window.WorkspaceModel;
 const done=new Set(['succeeded','abstained','failed','cancelled','interrupted']);
 let kb=null,session=null,vm=null,messages=[],before=null,subscription=null,loadEpoch=0,sending=false,ready=false,sessionEpoch=0,olderPending=null;
 const cache=new Map();let waitTimer=null,waitNodes=[],editButtons=[],historyEpoch=0;
 function context(){return o.current();}
 function persist(){if(!kb||!vm)return;try{sessionStorage.setItem('rag.chat.state.v1:'+kb+':'+(session?.id||'new'),JSON.stringify(vm));}catch{}}
 function stateFor(k,s){const key=k+':'+(s||'new');if(cache.has(key))return cache.get(key);let saved={};try{saved=JSON.parse(sessionStorage.getItem('rag.chat.state.v1:'+key)||'{}');}catch{}const state={draft:saved.draft||'',localRevision:saved.localRevision||0,pending:saved.pending||null,activeRunId:saved.activeRunId||null,cursor:saved.cursor||0,cancelUnknown:saved.cancelUnknown||false,stage:saved.stage||'',progress:saved.progress||null,connection:'connecting'};cache.set(key,state);return state;}
 function stopSubscription(){subscription?.abort();subscription=null;}
 function valid(k,s){return context().kb?.id===k&&kb===k&&session?.id===s;}
 function owner(){return {kb,sessionId:session?.id,model:vm,epoch:sessionEpoch};}
 function owns(value){return value.epoch===sessionEpoch&&valid(value.kb,value.sessionId)&&vm===value.model;}
 function stopWait(){if(waitTimer!==null){clearTimeout(waitTimer);waitTimer=null;}waitNodes=[];}
 function canEdit(){const c=context();return ready&&!sending&&!vm?.pending&&!vm?.activeRunId&&c.kb?.state==='active'&&!c.authPaused;}
 function elapsed(run){
  const now=Date.now(),created=Date.parse(run.created_at),progress=vm?.progress?.id===run.id?vm.progress:null;
  let ms=Number.isFinite(created)?Math.max(0,now-created):0;
  if(progress&&Number.isFinite(progress.elapsed)&&Number.isFinite(progress.received))ms=Math.max(ms,progress.elapsed+Math.max(0,now-progress.received));
  return Math.floor(ms/1000);
 }
 function updateWait(){
  for(const entry of waitNodes)entry.node.textContent='已等待约 '+elapsed(entry.run)+' 秒（含排队）';
  if(waitNodes.length)waitTimer=setTimeout(()=>{waitTimer=null;updateWait();},1000);
 }
 function connectionLabel(run){
  if(context().authPaused||vm?.connection==='auth')return '登录已失效，暂无法确认任务状态。';
  if(vm?.connection==='disconnected')return '连接已断开，正在读取服务器任务状态；不会自动重复提交。';
  if(run.status==='cancel_requested')return '正在确认停止状态…';
  return vm?.stage||(run.status==='queued'?'等待处理…':'正在处理资料…');
 }
 async function editQuestion(run,mode=null){
  if(!canEdit())return;
  const t=owner(),revision=vm.localRevision;
  if($('question').value.trim()){o.notice('已有问题草稿，已保留。请先复制、发送或清空草稿，再重新编辑。');$('question').focus();return;}
  let original=messages.find(m=>m.role==='user'&&m.run_id===run.id);
  if(!original){const page=await o.api('/sessions/'+t.sessionId+'/messages?around_run_id='+encodeURIComponent(run.id));original=page.items.find(m=>m.role==='user'&&m.run_id===run.id);}
  if(!owns(t)||vm.localRevision!==revision||!canEdit()||$('question').value.trim())return;
  if(!original?.body){o.notice('未找到原问题，请加载对应的历史消息后复制问题。');return;}
  $('question').value=original.body;vm.draft=original.body;vm.localRevision++;context().view.draft=original.body;if(mode==='evidence_only')$('answer-mode').value=mode;persist();scopeChanged();$('question').focus();
  $('chat-status').textContent=mode==='evidence_only'?'已切换为只查原文并回填问题，尚未提交。发送时使用当前所选资料。':'问题已回填，尚未提交。发送时使用当前所选资料；追问请补全具体对象。';
 }
 function selectMode(){const caps=context().capabilities||{};$('answer-mode').options[0].disabled=!caps.generative_answer_enabled;if(!caps.generative_answer_enabled)$('answer-mode').value='evidence_only';}
 function scopeChanged(){if(!context().view)return;const c=context(),count=M.selected(c.view,c.documents).length,readonly=c.kb?.state!=='active',busy=!ready||sending||!!(vm?.activeRunId||vm?.pending);$('send-chat').disabled=readonly||!count||busy||!$('question').value.trim()||c.authPaused||($('compare-sources').checked&&(count<2||count>4));$('new-chat').disabled=readonly||!ready||sending;$('rename-chat').disabled=readonly||!ready||sending||!session;$('history-chat').disabled=!ready||sending;$('compare-sources').disabled=readonly;$('stop-chat').hidden=!vm?.activeRunId;$('recover-chat').hidden=!vm?.pending;$('question').readOnly=readonly;for(const b of editButtons)b.disabled=!canEdit();if($('compare-sources').checked&&(count<2||count>4))$('chat-status').textContent='对比需要选择 2～4 份资料。';}
 async function refreshMessages(){const t=owner(),k=t.kb,s=t.sessionId;if(!s)return;const data=await o.api('/sessions/'+s+'/messages');if(!owns(t))return;messages=data.items;before=data.next_before;session.revision=data.session_revision;session.last_message_id=data.last_message_id;const pending=messages.find(m=>m.role==='assistant'&&!done.has(m.run.status));if(pending&&vm.activeRunId!==pending.run_id){if(vm.progress?.id!==pending.run_id){vm.stage='';vm.progress=null;}vm.connection='connecting';vm.activeRunId=pending.run_id;persist();subscribe(vm.activeRunId);}render();}
 function textWithCitations(parent,text,extracts,run){const lookup=new Map(extracts.map(e=>[e.evidence_key,e]));let pos=0;for(const match of text.matchAll(/\[(E\d+)\]/g)){parent.append(document.createTextNode(text.slice(pos,match.index)));const source=lookup.get(match[1]);if(source){const b=o.button('['+match[1]+']',()=>o.openCitation(source.citation_id,{sessionId:run.session_id}),'citation-link');parent.append(b);}else parent.append(document.createTextNode(match[0]));pos=match.index+match[0].length;}parent.append(document.createTextNode(text.slice(pos)));}
 function reason(result){const safe=window.WorkspaceConversation.outcome(result);if(safe.reason)return safe.reason;if(result?.reason&&/[\u4e00-\u9fff]/.test(result.reason))return result.reason;return '资料中的依据不足，或本次处理未完成。可以调整资料范围或补充问题。';}
 function evidenceMap(card,result,sources,run){
  if(!['succeeded','abstained'].includes(run.status)||!['GENERATED_ANSWER','EVIDENCE_ONLY','MODEL_ABSTAIN','ABSTAIN_SUPPORT_REVIEW','ABSTAIN_INCOMPLETE_ANSWER'].includes(result.status))return;
  const map=result.question_evidence;if(map?.contract!=='question_evidence_v1'||map.method!=='shared_terms_v1'||map.semantic_coverage_verified!==false||!Array.isArray(map.parts)||map.parts.length<2||map.parts.length>6)return;
  const lookup=new Map(sources.filter(s=>typeof s.citation_id==='string').map(s=>[s.citation_id,s]));
  const rows=map.parts.filter(p=>typeof p.question==='string'&&p.question.trim());if(rows.length<2)return;
  const details=o.el('details','',{class:'question-evidence-map'});details.append(o.el('summary','按问题核对原文'),o.el('p','按问题与原文的文字关联整理，仅供核对，不代表每部分已回答正确。未关联的部分可继续查看下方全部引用。',{class:'hint'}));
  const list=o.el('ol');for(const part of rows){const row=o.el('li');row.append(o.el('p',part.question.slice(0,350)));
   const refs=[...new Set(Array.isArray(part.citation_ids)?part.citation_ids:[])].filter(id=>lookup.has(id)).slice(0,4);
   row.append(o.el('p',refs.length?'供核对的相关原文':'未自动关联到引用',{class:'hint'}));
   for(const id of refs){const source=lookup.get(id);row.append(o.button('['+source.evidence_key+'] '+(source.document_name||'资料'),()=>o.openCitation(id,{sessionId:run.session_id}),'source-citation'));}
   list.append(row);
  }details.append(list);card.append(details);
 }
 function render(){stopWait();editButtons=[];const root=$('messages'),panel=$('messages'),nearBottom=panel.scrollHeight-panel.scrollTop-panel.clientHeight<100,oldScroll=panel.scrollTop;root.replaceChildren();$('chat-welcome').hidden=messages.length>0;$('messages').hidden=messages.length===0;$('older-messages').hidden=before===null;$('older-messages').disabled=!!(olderPending&&owns(olderPending.owner));o.onMessages?.(kb+':'+(session?.id||'new'),messages.length>0);$('session-title').textContent=session?.title||'新对话';
  for(const message of messages){const card=o.el('article','',{class:'chat-message '+message.role});card.dataset.message=message.id;if(message.role==='user'){card.append(o.el('p',message.body));root.append(card);continue;}const run=message.run,result=run.result;card.dataset.run=run.id;card.append(o.el('p',done.has(run.status)?(run.status==='succeeded'?(result?.status==='GENERATED_ANSWER'?'已生成 · 模型复核通过':'原文摘录'):run.status==='abstained'?window.WorkspaceConversation.outcome(result).label:({cancelled:'已停止',interrupted:'任务已中断',failed:'处理未完成'})[run.status]):'正在处理',{class:'answer-label'}));
   if(!done.has(run.status)){const clock=o.el('p','',{class:'wait-elapsed',role:'timer','aria-live':'off'});waitNodes.push({node:clock,run});card.append(o.el('p',connectionLabel(run),{class:'muted'}),clock);const pairs=vm?.progress?.pairs;if(pairs&&vm.progress.id===run.id)card.append(o.el('p',`已处理 ${pairs.done}/${pairs.total} 对候选（缓存 ${pairs.cached}，实际评分 ${pairs.scored}）`,{class:'rerank-count'}));card.append(o.el('p','全新问题首次处理可能需要数十秒。可继续阅读资料，或停止本次任务。',{class:'hint'}));}
   else if(run.source_state==='revoked'){card.append(o.el('p','回答关联的资料已移除，内容暂不可查看。',{class:'hint'}));}
    else if(result){if(run.status==='succeeded'&&result.safe_output){const body=o.el('div','',{class:'answer-body'});textWithCitations(body,result.safe_output,result.source_extracts||[],run);card.append(body);}else card.append(o.el('p',reason(result)));
     const sources=result.source_extracts||[];if(sources.length){evidenceMap(card,result,sources,run);const details=o.el('details'),summary=o.el('summary',run.status==='abstained'?`相关原文参考（未生成回答） · ${sources.length} 处引用`:`当次依据 · ${sources.length} 处引用 / ${run.allowed_document_count} 份允许检索的资料`);details.append(summary);for(const source of sources){const b=o.button((source.document_name||'资料')+' · '+source.evidence_key,()=>o.openCitation(source.citation_id,{sessionId:run.session_id}),'source-citation');details.append(b);}card.append(details);}
    if(run.source_state==='historical')card.append(o.el('p','此回答使用历史版本，引用仍可核对。',{class:'hint'}));
     if(result.safe_output&&run.status==='succeeded'){if(result.status==='GENERATED_ANSWER')card.append(o.el('p','引用已核对保存的资料文本；云端解析或 OCR 可能存在识别误差。回答由模型生成和复核，未经人工审核；关键结论与数值请对照原件核对。',{class:'answer-disclosure'}));const actions=o.el('div','',{class:'answer-actions'});actions.append(o.button('复制',()=>o.copyText(result.safe_output)),o.button('保存为笔记',()=>o.collect(run)));card.append(actions);}
   }
   if(['failed','cancelled','interrupted','abstained'].includes(run.status)){const b=o.button('重新编辑此问题',()=>editQuestion(run),'question-reedit');editButtons.push(b);card.append(b);}
   if(run.status==='abstained'&&run.source_state!=='revoked'&&result?.status==='ABSTAIN_PROVIDER_FAILURE'){const b=o.button('只查原文',()=>editQuestion(run,'evidence_only'),'question-evidence');editButtons.push(b);card.append(b);}
   root.append(card);
  }
  if(waitNodes.length)updateWait();if(nearBottom)requestAnimationFrame(()=>panel.scrollTop=panel.scrollHeight);else panel.scrollTop=oldScroll;scopeChanged();
 }
 async function readRun(id,k,s){const t=owner(),data=await o.api('/runs/'+id+'?kb_id='+encodeURIComponent(k));if(!owns(t)||!valid(k,s))return data;const target=messages.find(m=>m.role==='assistant'&&m.run_id===id);if(target)target.run=data;if(done.has(data.status)&&vm.activeRunId===id){vm.activeRunId=null;vm.cancelUnknown=false;vm.stage='';vm.progress=null;$('chat-status').textContent=data.status==='succeeded'?'回答已完成。':data.status==='cancelled'?'已停止。':'本次处理已结束。';persist();}render();return data;}
 async function subscribe(id){
  stopSubscription();const control=new AbortController();subscription=control;const t=owner(),k=kb,s=session.id,local=vm;let delay=700;
  const current=()=>!control.signal.aborted&&owns(t)&&local.activeRunId===id;
  while(current()&&!context().authPaused){
   try{
    const response=await fetch('/api/v1/runs/'+id+'/events?after_seq='+local.cursor,{credentials:'same-origin',signal:control.signal});
    if(!current())return;
    if(response.status===401){local.connection='auth';o.pauseAuth();render();return;}
    if(!response.ok||!response.body)throw Error('stream unavailable');
    local.connection='connected';render();
    const reader=response.body.getReader(),decoder=new TextDecoder(),parser=window.TechDocStream.createSSEParser((kind,data,meta)=>{
     if(!current())return;
     if(meta?.id)local.cursor=Math.max(local.cursor,Number(meta.id)||0);
     if(kind==='progress'){
      local.stage=data.label||'正在处理';
      const counts=[data.pairs_completed,data.pairs_total,data.cache_hits,data.scored_pairs];
      let pairs=data.stage==='rerank'?local.progress?.pairs:null;
      if(data.stage==='rerank'&&counts.every(Number.isInteger)&&counts[0]>=0&&counts[0]<=counts[1]&&counts[1]<=64&&counts[2]>=0&&counts[3]>=0&&counts[2]+counts[3]===counts[0])pairs={done:counts[0],total:counts[1],cached:counts[2],scored:counts[3]};
      if(Number.isFinite(data.elapsed_ms)&&data.elapsed_ms>=0)local.progress={id,elapsed:data.elapsed_ms,received:Date.now(),pairs};
      $('chat-status').textContent=local.stage;render();
     }persist();
    });
    try{while(current()){const item=await reader.read();if(item.done)break;parser.push(decoder.decode(item.value,{stream:true}));}parser.push(decoder.decode());parser.finish();}finally{reader.releaseLock();}
    if(!current())return;const run=await readRun(id,k,s);if(done.has(run.status))return;delay=700;
   }catch(e){
    if(!current())return;local.connection=context().authPaused?'auth':'disconnected';render();
    if(context().authPaused)return;
    $('chat-status').textContent='连接已断开，正在读取服务器任务状态…';
    try{const run=await readRun(id,k,s);if(done.has(run.status))return;}catch{}
    if(!current())return;if(context().authPaused){local.connection='auth';render();return;}delay=Math.min(8000,delay*2);
   }
   await new Promise(resolve=>setTimeout(resolve,delay));
  }
 }
 async function loadSession(row){sessionEpoch++;olderPending=null;persist();stopSubscription();stopWait();ready=false;session=row;vm=stateFor(kb,row?.id);if(!row&&context().view?.draft&&!vm.draft)vm.draft=context().view.draft;$('question').value=vm.draft;messages=[];before=null;selectMode();render();if(row){await refreshMessages();if(!valid(kb,row.id))return;const pending=messages.find(m=>m.role==='assistant'&&!done.has(m.run.status));if(pending)vm.activeRunId=pending.run_id;ready=true;scopeChanged();if(vm.pending)await recover();else if(vm.activeRunId){const run=await readRun(vm.activeRunId,kb,session.id);if(!done.has(run.status))subscribe(vm.activeRunId);}}else{ready=true;scopeChanged();}}
 async function load(params){const epoch=++loadEpoch;persist();stopSubscription();stopWait();session=null;vm=null;messages=[];kb=context().kb?.id;if(!kb)return;const k=kb;const data=await o.api('/knowledge-bases/'+k+'/sessions');if(epoch!==loadEpoch||context().kb?.id!==k)return;let row=null;if(params.get('session')){row=await o.api('/sessions/'+encodeURIComponent(params.get('session'))+'?kb_id='+encodeURIComponent(k));}else row=data.items[0]||null;if(epoch!==loadEpoch||context().kb?.id!==k)return;await loadSession(row);if(epoch!==loadEpoch||context().kb?.id!==k)return;if(row&&!params.has('source')&&!params.has('citation')&&!params.has('artifact'))o.setUrl({kb:k,session:row.id},true);}
 async function ensureSession(){if(session)return;const k=kb,previous=vm||stateFor(k,'new'),key=previous.creationKey||(previous.creationKey=crypto.randomUUID());persist();const row=await o.api('/knowledge-bases/'+k+'/sessions',{method:'POST',body:{request_key:key}});if(context().kb?.id!==k)return;session=row;vm=stateFor(k,row.id);Object.assign(vm,{draft:previous.draft,localRevision:previous.localRevision});$('question').value=vm.draft;o.setUrl({kb:k,session:row.id},true);persist();}
 function confirm(run,pending){session.revision=run.session_revision;session.last_message_id=run.last_message_id;if(vm.localRevision===pending.localRevision&&$('question').value.trim()===pending.body.question.trim()){vm.draft='';$('question').value='';context().view.draft='';}vm.pending=null;vm.activeRunId=done.has(run.status)?null:run.id;vm.stage='';vm.progress=null;vm.connection='connecting';vm.cursor=0;persist();}
 async function sendInner(){const k=kb;vm=vm||stateFor(k,'new');vm.draft=$('question').value;await ensureSession();if(context().kb?.id!==k||!session)return;const c=context(),chosen=M.selected(c.view,c.documents),body={question:$('question').value.trim(),request_key:crypto.randomUUID(),expected_session_revision:session.revision,expected_parent_message_id:session.last_message_id,scope:c.view.mode==='all_ready'?{mode:'all_ready'}:{mode:'selected',document_ids:chosen},task:$('compare-sources').checked?{type:'comparison',required_document_ids:chosen}:{type:'qa',required_document_ids:[]},answer_mode:$('answer-mode').value};const pending={body,localRevision:vm.localRevision};const submittedVM=vm,submittedSession=session.id;vm.pending=pending;persist();scopeChanged();$('chat-status').textContent='正在确认提交…';try{const run=await o.api('/sessions/'+session.id+'/messages',{method:'POST',body});if(!valid(k,run.session_id))return;confirm(run,pending);await refreshMessages();if(vm.activeRunId)subscribe(vm.activeRunId);}catch(error){if(!valid(k,submittedSession)){if(error.status)submittedVM.pending=null;return;}if(error.status){vm.pending=null;persist();if(error.status===409)await refreshMessages();}else $('chat-status').textContent='尚未确认服务器是否接收。请查询提交状态，系统不会自动重发问题。';scopeChanged();throw error;}}

 async function send(){if(sending||$('send-chat').disabled)return;sending=true;scopeChanged();try{await sendInner();}finally{sending=false;scopeChanged();}}
 async function recover(){
  if(!vm?.pending||!session)return;
  const t=owner(),pending=t.model.pending;
  const current=()=>owns(t)&&vm.pending===pending;
  async function accepted(run){
   if(!current()||run.session_id!==t.sessionId)return;
   confirm(run,pending);
   try{await refreshMessages();}finally{if(owns(t)){scopeChanged();if(vm.activeRunId)subscribe(vm.activeRunId);}}
  }
  try{
   const run=await o.api('/sessions/'+t.sessionId+'/submissions/'+encodeURIComponent(pending.body.request_key));
   await accepted(run);
  }catch(error){
   if(!owns(t))return;
   if(!current()||error.status!==404)throw error;
   $('chat-status').textContent='服务器暂未找到这次提交，草稿已保留。';
   o.action('确认原提交',[o.el('p','可以继续查询状态，或明确重试原提交。重试沿用同一提交标识，服务器不会重复执行已接收的任务。')],async()=>{
    if(!current())throw Error('对话已切换，请回到原对话查询提交状态。');
    const run=await o.api('/sessions/'+t.sessionId+'/messages',{method:'POST',body:pending.body});
    await accepted(run);
   },'重试原提交');
  }
  if(owns(t))scopeChanged();
 }
 async function stop(){const id=vm?.activeRunId;if(!id)return;vm.cancelUnknown=true;$('chat-status').textContent='正在确认停止状态…';persist();try{const run=await o.api('/runs/'+id+'/cancel',{method:'POST'});if(!valid(kb,run.session_id))return;if(done.has(run.status)){await readRun(id,kb,session.id);}else{vm.cancelUnknown=false;$('chat-status').textContent='停止已受理，等待任务结束。';persist();}}catch(error){$('chat-status').textContent='停止状态尚未确认，正在读取服务器状态。';throw error;}finally{scopeChanged();}}
 async function newSession(){persist();const k=kb,row=await o.api('/knowledge-bases/'+k+'/sessions',{method:'POST',body:{request_key:crypto.randomUUID()}});if(context().kb?.id!==k)return;o.setUrl({kb:k,session:row.id});await loadSession(row);}
 async function history(){
  const t=owner(),token=++historyEpoch,k=kb,search=o.el('input','',{type:'search',placeholder:'搜索对话名称',maxlength:'200','aria-label':'搜索对话名称'}),tabs=o.el('div','',{class:'history-tabs'}),list=o.el('div','',{class:'history-list'}),status=o.el('p','',{class:'hint'}),more=o.button('加载更多对话',()=>loadPage(false));
  let filter='active',offset=0,loading=false,request=0,debounce=null;const seen=new Set();
  const current=()=>owns(t)&&token===historyEpoch&&list.isConnected!==false&&$('action-dialog')?.open!==false;
  const active=o.button('全部对话',()=>switchFilter('active')),archived=o.button('已归档',()=>switchFilter('archived'));tabs.append(active,archived);
  async function switchFilter(value){filter=value;await loadPage(true);}
  function changeState(row,state){
   const key=crypto.randomUUID();
   o.action(state==='archive'?'归档对话':'恢复对话',[o.el('p',state==='archive'?'进行中任务结束后才能归档。资料、消息和已保存笔记保留，可在已归档列表恢复。':'恢复后可继续阅读和提问，不会自动重启旧任务。')],async()=>{
    if(!owns(t))throw Error('知识库或对话已切换，请重新打开历史对话。');
    await o.api('/sessions/'+row.id+'/'+state,{method:'POST',body:{kb_id:k,request_key:key,expected_revision:row.revision}});
    if(!owns(t))return;
    if(state==='archive'&&session?.id===row.id){context().view.draft='';o.setUrl({kb:k});await loadSession(null);}
    o.notice(state==='archive'?'对话已归档，可在历史对话的“已归档”中恢复。':'对话已恢复，旧任务不会自动重启。');
   },state==='archive'?'归档对话':'恢复对话');
  }
  async function loadPage(reset){
   if(!current()||(!reset&&loading))return;
   clearTimeout(debounce);const serial=++request,mode=filter,q=search.value;loading=true;more.disabled=true;
   active.setAttribute?.('aria-pressed',String(mode==='active'));archived.setAttribute?.('aria-pressed',String(mode==='archived'));
   if(reset){offset=0;seen.clear();list.replaceChildren();more.hidden=true;}
   status.textContent='正在读取对话…';
   try{
    const data=await o.api('/knowledge-bases/'+k+'/sessions?'+new URLSearchParams({limit:20,offset:offset||0,state:mode,search:q}));
    if(!current()||serial!==request)return;
    for(const row of data.items){
     if(seen.has(row.id))continue;seen.add(row.id);const item=o.el('div','',{class:'history-row'});
     if(mode==='active')item.append(o.button(row.title,async()=>{if(!current())return;const fresh=await o.api('/sessions/'+row.id+'?kb_id='+k);if(!current())return;o.closeAction();o.setUrl({kb:k,session:row.id});await loadSession(fresh);}));
     else item.append(o.el('span',row.title));
     const b=o.button(mode==='active'?'归档':'恢复',()=>{if(current())changeState(row,mode==='active'?'archive':'restore');});b.disabled=context().kb?.state!=='active';item.append(b);list.append(item);
    }
    offset=data.next_offset;more.hidden=offset===null;status.textContent=seen.size?'已显示 '+seen.size+' 条对话。':'没有匹配的对话。';
   }catch(error){if(current()&&serial===request)status.textContent=error.message;}
   finally{if(current()&&serial===request){loading=false;more.disabled=false;}}
  }
  search.addEventListener('input',()=>{clearTimeout(debounce);debounce=setTimeout(()=>o.guard(()=>loadPage(true)),250);});
  search.addEventListener('keydown',event=>{if(event.key==='Enter'){event.preventDefault();o.guard(()=>loadPage(true));}});
  o.action('历史对话',[tabs,search,list,status,more],async()=>{},'关闭',{menu:true});await loadPage(true);
 }
 function rename(){
  if(!session||context().kb?.state!=='active')return;const t=owner(),input=o.el('input','',{type:'text',maxlength:'100','aria-label':'对话名称'}),label=o.el('label','对话名称',{class:'field'});input.value=session.title||'新对话';input.required=true;label.append(input);let revision=session.revision;
  o.action('重命名对话',[label],async()=>{
   if(!owns(t))throw Error('对话已切换，请回到原对话修改名称。');const title=input.value.trim();if(!title)throw Error('请输入对话名称。');
   try{const row=await o.api('/sessions/'+t.sessionId,{method:'PATCH',body:{title,expected_revision:revision}});if(owns(t)){session.title=row.title;session.revision=Math.max(session.revision,row.revision);render();o.notice('对话名称已更新。');}}
   catch(error){if(error.status===409&&owns(t)){const fresh=await o.api('/sessions/'+t.sessionId+'?kb_id='+t.kb);if(owns(t)){revision=fresh.revision;session.revision=fresh.revision;}throw Error('对话已更新，名称草稿已保留。核对后可再次保存。');}throw error;}
  });
 }
 $('question').addEventListener('input',()=>{if(!vm&&kb)vm=stateFor(kb,session?.id);if(vm){vm.draft=$('question').value;vm.localRevision++;persist();}scopeChanged();});
 $('question').addEventListener('keydown',e=>{if(e.key==='Enter'&&!e.shiftKey&&!e.isComposing&&e.keyCode!==229){e.preventDefault();o.guard(send);}});
 $('send-chat').addEventListener('click',()=>o.guard(send));$('stop-chat').addEventListener('click',()=>o.guard(stop));$('recover-chat').addEventListener('click',()=>o.guard(recover));$('new-chat').addEventListener('click',()=>o.guard(newSession));$('history-chat').addEventListener('click',()=>o.guard(history));$('rename-chat').addEventListener('click',()=>o.guard(rename));$('compare-sources').addEventListener('change',scopeChanged);
 async function olderMessages(){
  if(before===null||!session||!ready||(olderPending&&owns(olderPending.owner)))return;
  const t=owner(),cursor=before,request={owner:t,cursor};olderPending=request;$('older-messages').disabled=true;
  try{
   const data=await o.api('/sessions/'+t.sessionId+'/messages?before_sequence='+cursor);
   if(!owns(t)||olderPending!==request||before!==cursor)return;
   const ids=new Set(messages.map(m=>m.id));messages=[...data.items.filter(m=>!ids.has(m.id)),...messages];
   before=data.next_before;render();
  }finally{
   if(olderPending===request)olderPending=null;
   if(owns(t))$('older-messages').disabled=false;
  }
 }
 $('older-messages').addEventListener('click',()=>o.guard(olderMessages));
 return {load,scopeChanged,refresh:refreshMessages,async focusRun(id){if(!session)return;let card=[...document.querySelectorAll('.chat-message')].find(n=>n.dataset.run===id);if(!card){const k=kb,s=session.id,data=await o.api('/sessions/'+s+'/messages?around_run_id='+encodeURIComponent(id));if(!valid(k,s))return;messages=data.items;before=data.next_before;render();card=[...document.querySelectorAll('.chat-message')].find(n=>n.dataset.run===id);}if(card){card.tabIndex=-1;card.focus({preventScroll:true});card.scrollIntoView({block:'center'});}},invalidateSources(ids){const removed=new Set(ids);for(const m of messages){if(m.role==='assistant'&&(m.run.result?.source_extracts||[]).some(e=>removed.has(e.document_id))){m.run.source_state='revoked';m.run.result=null;}}render();},sessionId:()=>session?.id,detach(){sessionEpoch++;olderPending=null;persist();stopSubscription();stopWait();editButtons=[];ready=false;loadEpoch++;kb=null;session=null;vm=null;messages=[];},recover};
}};
