/* The two answer views share source rendering and publish only final API results. */
(function(){
'use strict';
const U=window.RagUI,$=id=>document.getElementById(id);
const state={docs:[],selected:new Set(),versions:new Map(),managerId:null,cases:[],casesLoaded:false,lesson:0,view:'query',busy:null,result:null,lessonResults:new Map(),loaded:false};
const lessonTitles=['RAG 的四个阶段','分块与重叠','Embedding 的作用','混合搜索与重排','Agent 如何执行任务','ReAct 循环','短期记忆与长期记忆','工具的使用说明','两种检索的共同点','资料不足时怎么办'];
function el(tag,text,cls){const node=document.createElement(tag);if(text!==undefined)node.textContent=String(text);if(cls)node.className=cls;return node}
function button(text,cls,action){const node=el('button',text,cls);node.type='button';if(action)node.addEventListener('click',action);return node}
function detail(title){const node=el('details');node.append(el('summary',title));return node}
let toastTimer;
function toast(text){$('toast').textContent=text;$('toast').hidden=false;clearTimeout(toastTimer);toastTimer=setTimeout(()=>$('toast').hidden=true,3800)}
function feedback(node,text,error=false){node.textContent=text;node.dataset.error=String(error)}
function friendlyError(error){
 const mapping={REVISION_VERSION_REQUIRED:'请填写新版本名称。',REVISION_FILENAME_MISMATCH:'新版本的文件名需与当前文档一致。',REVISION_UNCHANGED:'文件和版本名称没有变化。',UNKNOWN_REVISION_PARENT:'原版本已不在文档库中，请刷新。',EMPTY_OR_OVERSIZED_UPLOAD:'请选择非空且不超过 8 MB 的文件。',UNKNOWN_DOCUMENT:'文档已不存在，请刷新列表。',DOCUMENT_PARSE_FAILED:'文档未能解析，请检查格式后重试。',DOCUMENT_DELETE_FAILED:'文档删除失败，请刷新列表确认状态。',RETRIEVER_INDEX_BUILD_FAILED:'文档索引建立失败，请稍后重试。',QUERY_CAPACITY_REACHED:'已有问题正在处理，请稍后再试。'};
 return mapping[error.message]||(/[\u4e00-\u9fff]/.test(error.message)?error.message:'暂时无法完成，请检查连接后重试。');
}
async function request(path,options={}){
 const response=await fetch(path,options);if(response.status===401)throw Error('登录已失效，请刷新页面后重新登录。');if(response.status===204)return {};
 const data=await response.json();if(!response.ok)throw Error(typeof data.detail==='string'?data.detail:'服务暂时不可用，请稍后再试。');return data;
}
function currentManaged(){return state.docs.find(doc=>doc.snapshot_id===state.managerId)}
function renderSelection(){
 const root=$('documentChoices');root.replaceChildren();
 if(!state.docs.length)root.append(el('p','还没有可用资料，请先到文档管理导入。','empty-small'));
 for(const group of U.groupDocuments(state.docs)){
  const chosen=group.versions.find(d=>d.snapshot_id===state.versions.get(group.name))||group.versions[0];state.versions.set(group.name,chosen.snapshot_id);
  const checked=state.selected.has(chosen.snapshot_id),row=el('div',undefined,'document-choice'+(checked?' is-selected':''));
  const label=el('label',undefined,'checkbox-row'),box=el('input');box.type='checkbox';box.checked=checked;box.setAttribute('aria-label','参考 '+U.docName(chosen));
  label.title=chosen.filename;label.append(box,el('span',U.docName(chosen)));row.append(label);
  box.addEventListener('change',()=>{if(box.checked&&state.selected.size>=4){box.checked=false;toast('最多选择 4 份回答依据。');return}if(box.checked)state.selected.add(chosen.snapshot_id);else state.selected.delete(chosen.snapshot_id);renderSelection()});
  const select=el('select',undefined,'version-select');select.setAttribute('aria-label',U.docName(chosen)+'的版本');
  for(const doc of group.versions){const option=el('option',U.versionName(doc));option.value=doc.snapshot_id;option.selected=doc.snapshot_id===chosen.snapshot_id;select.append(option)}
  select.addEventListener('change',()=>{if(state.selected.delete(chosen.snapshot_id))state.selected.add(select.value);state.versions.set(group.name,select.value);renderSelection()});row.append(select);root.append(row);
 }
 $('selectedCount').textContent='已选 '+state.selected.size+' 份';$('scopeSummary').replaceChildren();
 const selected=state.docs.filter(d=>state.selected.has(d.snapshot_id));
 if(!selected.length)$('scopeSummary').textContent='请先选择回答依据';
 for(const doc of selected){const chip=el('span',U.docName(doc)+' · '+U.versionName(doc),'scope-chip');chip.title=doc.filename;$('scopeSummary').append(chip)}
 $('selectTutorials').disabled=!Object.values(U.tutorialNames).every(name=>state.docs.some(d=>d.filename===name));
 syncBusy();
}
function renderManager(){
 const list=$('managedDocuments');list.replaceChildren();
 if(!state.docs.length)list.append(el('p','还没有文档，可以在右侧导入。','empty-small'));
 for(const group of U.groupDocuments(state.docs))for(const doc of group.versions){const item=button(U.docName(doc),'managed-document',()=>{state.managerId=doc.snapshot_id;renderManager()});item.title=doc.filename;item.setAttribute('aria-pressed',String(doc.snapshot_id===state.managerId));item.append(el('small',U.versionName(doc)+(doc.original_stored?' · 原文件已保存':'')));list.append(item)}
 const root=$('documentDetail'),doc=currentManaged();root.replaceChildren();
 if(!doc){root.append(el('h2','资料详情'),el('p','选择一份文档，查看来源与版本。','empty-small'));$('asRevision').disabled=true;return}
 root.append(el('h2','资料详情'),el('p',doc.filename,'file-name'),el('p','版本：'+U.versionName(doc),'muted'));
 if(doc.warnings?.length)root.append(el('p','部分内容可能未完整解析，建议对照原文件核对。','result-note'));
 const info=detail('技术详情');info.className='technical-detail';info.append(el('pre',JSON.stringify(doc,null,2)));root.append(info);
 const actions=el('div',undefined,'document-actions');
 if(doc.original_stored){const link=el('a','下载原文件');link.href='/documents/'+encodeURIComponent(doc.snapshot_id)+'/source';actions.append(link)}
 actions.append(button('删除所选版本','danger',()=>openDelete(doc)));root.append(actions);$('asRevision').disabled=false;updateRevisionTarget();
}
function updateRevisionTarget(){const doc=currentManaged();$('productVersion').required=$('asRevision').checked;$('revisionTarget').textContent=$('asRevision').checked&&doc?'当前文档：'+doc.filename+' · '+U.versionName(doc):''}
async function refreshDocuments(preferred,initial=false){
 const data=await request('/documents');state.docs=data.documents;const active=new Set(state.docs.map(d=>d.snapshot_id));state.selected=new Set([...state.selected].filter(id=>active.has(id)));
 if(initial&&!state.loaded&&state.docs.length){const group=U.groupDocuments(state.docs).find(g=>g.name===U.tutorialNames.rag)||U.groupDocuments(state.docs)[0];state.selected.add(group.versions[0].snapshot_id)}
 if(preferred&&active.has(preferred)){const doc=state.docs.find(d=>d.snapshot_id===preferred);for(const old of state.docs)if(old.filename===doc.filename)state.selected.delete(old.snapshot_id);if(state.selected.size<4)state.selected.add(preferred);state.versions.set(doc.filename,preferred);state.managerId=preferred}
 if(!active.has(state.managerId))state.managerId=[...state.selected][0]||state.docs[0]?.snapshot_id||null;
 state.loaded=true;renderSelection();renderManager();
}
function sourceLabel(source){const doc=state.docs.find(d=>d.snapshot_id===source.snapshot_id);const page=source.page??source.locator?.page;return U.docName(doc)+(page!=null?' · PDF 第 '+page+' 页':' · 原文片段')}
function renderResult(root,record,prefix){
 root.replaceChildren();const response=record.response,view=U.presentation(response,record.scope),top=el('div',undefined,'result-topline');
 const badge=el('span',view.label,'status-badge');badge.dataset.tone=view.tone;top.append(badge,el('span','本次耗时 '+(record.elapsed/1000).toFixed(1)+' 秒','elapsed'));root.append(top);
 if(prefix==='query'&&record.question)root.append(el('p','问题：'+record.question,'muted'));
 root.append(el('p','本次参考：'+record.scope.map(id=>U.docName(state.docs.find(d=>d.snapshot_id===id))).join('、'),'result-note'));
 const targets=new Map();for(const source of view.sources){if(!targets.has(source.evidence_key))targets.set(source.evidence_key,'source-'+prefix+'-'+source.evidence_key)}
 if(view.text){
  const text=el('div',undefined,'result-text');
  for(const part of view.text.split(/(\[E\d+\])/g)){const key=part.slice(1,-1);if(/^\[E\d+\]$/.test(part)&&targets.has(key)){const link=el('a',part,'citation-link');link.href='#'+targets.get(key);link.setAttribute('aria-label','查看来源 '+key);link.addEventListener('click',event=>{event.preventDefault();const target=document.getElementById(targets.get(key));if(target){target.open=true;target.scrollIntoView({block:'nearest',behavior:matchMedia('(prefers-reduced-motion: reduce)').matches?'auto':'smooth'});target.focus({preventScroll:true})}});text.append(link)}else text.append(document.createTextNode(part))}
  root.append(text);const copy=button('复制'+(view.kind==='answer'?'答案':'摘录'),'secondary',async()=>{try{await navigator.clipboard.writeText(view.text);copy.textContent='已复制';setTimeout(()=>copy.textContent='复制'+(view.kind==='answer'?'答案':'摘录'),1800)}catch(_){toast('未能复制，请选中文本后手动复制。')}});root.append(copy);
 }
 root.append(el('p',view.kind==='answer'?'答案由模型生成并核对来源，请结合原文判断。':view.kind==='evidence'?'以下是原文摘录，尚未生成答案。':'本次未能生成完整且可核对的答案。可查看原文，或调整问题再试。','result-note'));
 if(view.kind==='refusal'&&view.reason)root.append(el('p','系统给出的说明：'+view.reason,'result-reason'));
 if(view.sources.length){root.append(el('p',view.kind==='refusal'?'检索到的原文 · 可能相关，不代表已回答问题':'来源依据 · 点击引用可跳转','source-heading'));const seen=new Set();for(const source of view.sources){if(seen.has(source.evidence_key))continue;seen.add(source.evidence_key);const block=detail('['+source.evidence_key+'] '+sourceLabel(source));block.className='source-item';block.id=targets.get(source.evidence_key);block.tabIndex=-1;block.append(el('div',source.quotes.join('\n'),'source-quote'));const doc=state.docs.find(d=>d.snapshot_id===source.snapshot_id);if(doc?.original_stored){const link=el('a','下载来源原文件','source-download');link.href='/documents/'+encodeURIComponent(source.snapshot_id)+'/source';block.append(link)}root.append(block)}}
 if(response.parser_warnings?.length)root.append(el('p','部分资料可能存在解析缺失，请核对原文件。','result-note'));
 const tech=detail('技术详情');tech.className='technical-detail';tech.append(el('pre',JSON.stringify({状态代码:response.status,系统说明:response.reason,请求编号:response.diagnostics?.request_id,查询版本:response.snapshot_ids,检索方式:response.retriever,来源位置:view.sources.map(s=>({引用:s.evidence_key,快照:s.snapshot_id,位置:s.locator})),阶段耗时:response.diagnostics?.timings_ms},null,2)));root.append(tech);
}
function makeProgress(root,job){
 root.replaceChildren();const box=el('div',undefined,'progress-box'),label=el('p',undefined,'progress-label'),spin=el('span',undefined,'spinner');spin.setAttribute('aria-hidden','true');const labelText=el('span',job.label);label.append(spin,labelText);const steps=el('ol',undefined,'progress-steps');
 for(const text of job.mode==='evidence_only'?['查找资料']:job.mode==='llm'?['查找资料','选择摘录','核对版本']:['查找资料','生成回答','核对来源'])steps.append(el('li',text));const time=el('p',undefined,'progress-time'),stop=button('停止等待','secondary',()=>job.controller.abort());box.append(label,steps,time,stop);root.append(box);
 return ()=>{labelText.textContent=job.label;time.textContent='已等待 '+Math.floor((performance.now()-job.started)/1000)+' 秒';Array.from(steps.children).forEach((item,i)=>{item.className=i<job.stage?'done':i===job.stage?'active':'';if(i===job.stage)item.setAttribute('aria-current','step');else item.removeAttribute('aria-current')})};
}
function syncBusy(){document.querySelectorAll('[data-run]').forEach(node=>node.disabled=Boolean(state.busy)||!state.loaded);$('activeRequest').hidden=!state.busy;if(state.busy)$('activeRequestLabel').textContent=(state.busy.lessonId?state.busy.lessonId+' · ':'')+state.busy.label}
function showQueryResult(){if(state.busy&&!state.busy.lessonId)state.busy.update=makeProgress($('queryResult'),state.busy);else if(state.result)renderResult($('queryResult'),state.result,'query')}
async function runQuestion(body,lessonId=null){
 if(state.busy){toast('请等待当前问题完成，或先停止。');return}
 const job={controller:new AbortController(),lessonId,mode:body.mode,label:'等待处理',stage:0,started:performance.now(),scope:body.snapshot_ids.slice(),update:null};state.busy=job;if(lessonId)state.lessonResults.delete(lessonId);
 const root=lessonId?$('lessonOutput'):$('queryResult');job.update=makeProgress(root,job);syncBusy();job.update();const timer=setInterval(()=>{job.update?.();syncBusy()},1000);
 try{
  const response=await queryWithProgress(body,event=>{job.label=body.mode==='llm'&&event.stage==='generation'?'选择原文摘录':event.label;job.stage=Math.max(job.stage,U.stageIndex(event.stage));job.update?.();syncBusy()},job.controller.signal);
  const record={response,scope:job.scope,question:body.question,elapsed:performance.now()-job.started};if(lessonId)state.lessonResults.set(lessonId,record);else state.result=record;
  if(lessonId&&state.cases[state.lesson]?.id===lessonId)renderResult($('lessonOutput'),record,lessonId);else if(!lessonId)renderResult($('queryResult'),record,'query');
 }catch(error){const message=error.name==='AbortError'?'已停止等待。服务器将在当前阶段结束后停止后续处理。':friendlyError(error);const target=lessonId?(state.cases[state.lesson]?.id===lessonId?$('lessonOutput'):null):$('queryResult');if(target){target.replaceChildren(el('p',message,'result-reason'));if(!lessonId)state.result=null}else toast(message)}
 finally{clearInterval(timer);state.busy=null;syncBusy()}
}
function learningValue(id){try{return localStorage.getItem('techdoc.tutorial.assisted.v1.'+id)||''}catch(_){return ''}}
function renderLearningProgress(){const complete=state.cases.filter(c=>learningValue(c.id)==='已看懂').length;$('learningProgressLabel').textContent='已看懂 '+complete+' / '+state.cases.length+' 题';$('learningProgress').max=state.cases.length||10;$('learningProgress').value=complete;const list=$('lessonList');list.replaceChildren();state.cases.forEach((item,index)=>{const row=el('li'),nav=button('','',()=>selectLesson(index,true));nav.append(el('span',item.id,'lesson-number'),el('span',lessonTitles[index]||item.question));if(learningValue(item.id)==='已看懂')nav.append(el('span','✓','lesson-check'));if(index===state.lesson)nav.setAttribute('aria-current','step');row.append(nav);list.append(row)})}
function renderLesson(){
 const item=state.cases[state.lesson],root=$('lessonCard');root.replaceChildren();if(!item){root.append(el('h2',state.casesLoaded?'练习暂时未能加载':'正在加载练习…'),el('p',state.casesLoaded?'请刷新页面后重试。':'正在读取教程内容。','muted'));return}
 root.append(el('p',item.id+' · '+(item.scope_documents.length>1?'两份教程对照':'单份教程'),'lesson-kicker'));const heading=el('h2',item.question);heading.tabIndex=-1;heading.id='lessonQuestion';root.append(heading);
 const explanation=el('div',undefined,'lesson-explanation');explanation.append(el('strong','这道题在问什么'),document.createTextNode(item.plain_explanation));root.append(explanation,el('p','打个比方：'+item.analogy,'lesson-analogy'));
 const original=detail('查看原文和页码');for(const source of item.sources){original.append(el('p',(source.document==='rag'?'RAG 教程':'Agent 教程')+' · PDF 第 '+source.pdf_page+' 页','muted'),el('blockquote',source.quote,'source-quote'))}root.append(original);
 const draft=detail('查看答案草稿与判断要点');draft.append(el('p',item.answer_draft));const points=el('ul');for(const point of item.checklist)points.append(el('li',point));draft.append(points);root.append(draft);
 const run=button('让 RAG 回答这题','primary',()=>runLesson(item));run.dataset.run='';root.append(run);
 const output=el('div',undefined,'lesson-output');output.id='lessonOutput';output.setAttribute('aria-live','polite');root.append(output);
 if(state.busy?.lessonId===item.id)state.busy.update=makeProgress(output,state.busy);else if(state.lessonResults.has(item.id))renderResult(output,state.lessonResults.get(item.id),item.id);else output.append(el('p','先读解释，再运行回答，与原文对照。','muted'));
 const field=el('div',undefined,'lesson-state'),label=el('label','我的学习状态');label.htmlFor='lessonState';const select=el('select');select.id='lessonState';for(const value of ['','已看懂','还需讲解','原文与答案有疑问']){const option=el('option',value||'尚未确认');option.value=value;select.append(option)}select.value=learningValue(item.id);select.addEventListener('change',()=>{try{localStorage.setItem('techdoc.tutorial.assisted.v1.'+item.id,select.value)}catch(_){toast('浏览器未允许保存学习状态。')}renderLearningProgress()});field.append(label,select);root.append(field);
 $('previousLesson').disabled=state.lesson===0;$('nextLesson').disabled=state.lesson===state.cases.length-1;$('lessonPosition').textContent=(state.lesson+1)+' / '+state.cases.length;renderLearningProgress();syncBusy();state.busy?.update?.();
}
function runLesson(item){try{const groups=U.groupDocuments(state.docs);const ids=item.scope_documents.map(key=>{const group=groups.find(g=>g.name===U.tutorialNames[key]);const doc=group?.versions.find(d=>d.product_version==='tutorial-assisted-v2')||group?.versions.find(d=>d.product_version==='tutorial-assisted-v1');if(!doc)throw Error('缺少这道题需要的教程，请先到文档管理导入。');return doc.snapshot_id});runQuestion(U.selectionBody(item.question,ids,state.docs),item.id)}catch(error){toast(friendlyError(error))}}
function selectLesson(index,focus=false){state.lesson=Math.max(0,Math.min(state.cases.length-1,index));renderLesson();if(state.view==='learn')setView('learn',false);if(focus){if(matchMedia('(max-width:720px)').matches)$('lessonDirectory').open=false;$('lessonQuestion')?.focus({preventScroll:true});$('lessonCard').scrollIntoView({block:'start',behavior:'auto'})}}
function setView(view,push=true,writeURL=true){
 if(!['query','documents','learn'].includes(view))view='query';state.view=view;for(const name of ['query','documents','learn'])$('view-'+name).hidden=name!==view;
 document.querySelectorAll('.top-nav [data-view]').forEach(link=>{if(link.dataset.view===view)link.setAttribute('aria-current','page');else link.removeAttribute('aria-current')});
 const path=view==='learn'?'/?learn=1'+(state.cases[state.lesson]?'&lesson='+state.cases[state.lesson].id:''):view==='documents'?'/?view=documents':'/';if(writeURL&&location.pathname+location.search!==path){if(push)history.pushState(null,'',path);else history.replaceState(null,'',path)}
 document.title='TechDoc RAG · '+({query:'资料问答',documents:'文档管理',learn:'教程学习'})[view];if(view==='learn')renderLesson();if(view==='query')showQueryResult();
}
function restoreView(){const params=new URLSearchParams(location.search),id=params.get('lesson');if(id){const index=state.cases.findIndex(c=>c.id===id);if(index>=0)state.lesson=index}setView(params.get('learn')==='1'?'learn':params.get('view')==='documents'?'documents':'query',false,false)}
function openDelete(doc){state.deletingId=doc.snapshot_id;$('deleteTarget').textContent=doc.filename+' · '+U.versionName(doc);feedback($('deleteError'),'');$('deleteDialog').showModal()}
function clearDeletedResults(id){const affected=record=>record.scope.includes(id);if(state.result&&affected(state.result)){state.result=null;$('queryResult').replaceChildren(el('p','资料已删除，请重新选择依据后提问。','result-note'))}for(const [key,record]of state.lessonResults)if(affected(record))state.lessonResults.delete(key);if(state.view==='learn')renderLesson()}
$('confirmDelete').addEventListener('click',async()=>{const id=state.deletingId;if(!id)return;$('confirmDelete').disabled=true;try{await request('/documents/'+encodeURIComponent(id),{method:'DELETE'});$('deleteDialog').close();state.selected.delete(id);clearDeletedResults(id);await refreshDocuments();toast('所选版本已删除。')}catch(error){feedback($('deleteError'),friendlyError(error),true)}finally{$('confirmDelete').disabled=false}});
$('cancelDelete').addEventListener('click',()=>$('deleteDialog').close());
$('uploadForm').addEventListener('submit',async event=>{event.preventDefault();const form=event.currentTarget,file=$('uploadFile').files[0],doc=currentManaged(),revision=$('asRevision').checked;
 if(!file||!file.size||file.size>8*1024*1024){feedback($('uploadStatus'),'请选择非空且不超过 8 MB 的文件。',true);return}if(revision&&(!doc||doc.filename!==file.name)){feedback($('uploadStatus'),'请选中同名文档，再上传它的新版本。',true);return}
 $('uploadButton').disabled=true;feedback($('uploadStatus'),'正在导入和建立索引，较大文档可能需要等待。');try{const data=await request(revision?'/documents/'+encodeURIComponent(doc.snapshot_id)+'/revisions':'/documents',{method:'POST',body:new FormData(form)});await refreshDocuments(data.snapshot_id);feedback($('uploadStatus'),data.duplicate?'这份文件与版本已在文档库中。':state.selected.has(data.snapshot_id)?'导入完成，已选为回答依据。':'导入完成。回答依据已选满，可在资料问答中调整。');$('uploadFile').value=''}catch(error){feedback($('uploadStatus'),friendlyError(error),true)}finally{$('uploadButton').disabled=false}});
$('asRevision').addEventListener('change',updateRevisionTarget);
$('refreshDocuments').addEventListener('click',async()=>{try{await refreshDocuments();toast('资料列表已刷新。')}catch(error){toast(friendlyError(error))}});
$('selectTutorials').addEventListener('click',()=>{const groups=U.groupDocuments(state.docs),ids=Object.values(U.tutorialNames).map(name=>groups.find(g=>g.name===name)?.versions[0]?.snapshot_id).filter(Boolean);state.selected=new Set(ids);for(const id of ids){const doc=state.docs.find(d=>d.snapshot_id===id);state.versions.set(doc.filename,id)}renderSelection()});
$('queryForm').addEventListener('submit',event=>{event.preventDefault();try{runQuestion(U.selectionBody($('question').value,[...state.selected],state.docs,$('answerMode').value))}catch(error){toast(friendlyError(error))}});
$('question').addEventListener('input',()=>$('questionCount').textContent=$('question').value.length+' / 2000');
$('clearResult').addEventListener('click',()=>{if(state.busy&&!state.busy.lessonId){toast('请先停止当前问题，再清除显示。');return}state.result=null;$('queryResult').replaceChildren(el('p','显示已清除，可以输入新问题。','empty-small'))});
$('previousLesson').addEventListener('click',()=>selectLesson(state.lesson-1,true));$('nextLesson').addEventListener('click',()=>selectLesson(state.lesson+1,true));
$('stopActive').addEventListener('click',()=>state.busy?.controller.abort());$('returnToRequest').addEventListener('click',()=>{if(state.busy?.lessonId){state.lesson=state.cases.findIndex(c=>c.id===state.busy.lessonId);setView('learn')}else setView('query')});
document.addEventListener('click',event=>{const link=event.target.closest('a[data-view]');if(link&&!event.ctrlKey&&!event.metaKey&&!event.shiftKey&&!event.altKey){event.preventDefault();setView(link.dataset.view)}});window.addEventListener('popstate',restoreView);
if(matchMedia('(max-width:720px)').matches)$('lessonDirectory').open=false;
restoreView();
Promise.allSettled([refreshDocuments(null,true),request('/assets/learning-cases.json').then(data=>{state.cases=data;state.casesLoaded=true;restoreView();renderLearningProgress()}),request('/health').then(health=>{$('serviceState').textContent=health.status==='local_demo_ready'?'服务可用':'资料索引尚未就绪';$('serviceState').dataset.state=health.status==='local_demo_ready'?'ready':'error'})]).then(results=>{if(results[0].status==='rejected'){$('documentChoices').replaceChildren(el('p',friendlyError(results[0].reason),'feedback'));toast(friendlyError(results[0].reason))}if(results[1].status==='rejected'){state.casesLoaded=true;if(state.view==='learn')renderLesson()}if(results[2].status==='rejected'){$('serviceState').textContent='暂时无法连接';$('serviceState').dataset.state='error'}});
})();
