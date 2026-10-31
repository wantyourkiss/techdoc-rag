window.WorkspaceStudio={create(o){
 'use strict';const $=id=>document.getElementById(id);let kb=null,editor=null,notes=[],epoch=0,openEpoch=0;const drafts=new Map();
 const draftKey=e=>'rag.note.draft.v1:'+e.kb+':'+(e.id||'new');
 function stored(key){try{return JSON.parse(sessionStorage.getItem(key)||'null');}catch{return null;}}
 function persist(e=editor){if(!e||e.source_state==='revoked')return false;try{sessionStorage.setItem(draftKey(e),JSON.stringify(e));if(e.id&&e.draftToken){const key='rag.note.draft.v1:'+e.kb+':new',old=stored(key);if(old?.draftToken===e.draftToken&&old.localRevision<=e.localRevision)try{sessionStorage.removeItem(key);}catch{}}return true;}catch{return false;}}
 function restore(e){
  if(e.source_state==='revoked')return e;
  const saved=stored(draftKey(e));
  if(saved?.dirty&&saved.kb===e.kb){
   const token=saved.draftToken||crypto.randomUUID(),live=drafts.get(token);
   if(live){live.source_state=e.source_state;live.citations=e.citations;return live;}
   e={...e,...saved,kb:e.kb,draftToken:token,citations:e.citations,artifact_id:e.artifact_id,source_state:e.source_state,saving:false};
  }else e={...e,draftToken:crypto.randomUUID(),saving:false};
  drafts.set(e.draftToken,e);return e;
 }
 function clearServerVersion(){$('note-server').hidden=true;$('note-server-body').textContent='';}
 function readonly(e=editor){return !e||o.current().kb?.id!==e.kb||o.current().kb.state!=='active'||e.source_state==='revoked';}
 async function load(){const k=o.current().kb?.id,token=++epoch;if(kb!==k){openEpoch++;if(editor){persist();$('note-dialog').close();editor=null;clearServerVersion();}}kb=k;if(!k)return;const data=await o.api('/knowledge-bases/'+k+'/notes?limit=100');if(token!==epoch||o.current().kb?.id!==k)return;notes=data.items;render();if(editor?.id&&editor.kb===k){const e=editor;const current=await o.api('/notes/'+e.id+'?kb_id='+k);if(token!==epoch||o.current().kb?.id!==k||editor!==e)return;if(current.source_state==='revoked'){persist(e);e.source_state='revoked';e.body='';e.citations=[];e.conflict=false;clearServerVersion();draw();}else if(editor===e&&readonly(e))draw();}}
 function render(){const list=$('studio-notes');list.replaceChildren();$('new-note').disabled=o.current().kb?.state!=='active';if(!notes.length)list.append(o.el('div','还没有笔记。可以手写，也可以保存已经完成的回答。',{class:'studio-empty'}));for(const note of notes){const b=o.button(note.title,()=>open(note.id),'note-card');b.append(o.el('small',note.source_state==='revoked'?'来源已撤销':note.edited?'用户笔记':'已收藏的回答'));list.append(b);}}
 function draw(){if(!editor)return;$('note-title').value=editor.title;$('note-body').value=editor.body;$('note-title').readOnly=$('note-body').readOnly=readonly();$('note-save').disabled=readonly()||!!editor.saving;$('note-copy').disabled=readonly()||!editor.id;$('note-status').textContent=editor.source_state==='revoked'?'关联来源已撤销，笔记正文暂不可查看。':editor.dirty?'有未保存的本地草稿。':editor.edited?'用户编辑的笔记，新增内容未经模型核对。':'已保存。';$('note-load-server').hidden=!editor.conflict;$('note-citations').replaceChildren();for(const cite of editor.citations||[]){$('note-citations').append(o.button('引用 '+cite.evidence_key,()=>o.openCitation(cite.id,{artifactId:editor.artifact_id}),'citation-link'));}}
 async function open(id,fromRoute=false){
  persist();const token=++openEpoch,k=o.current().kb?.id;if(!k)return;let e;
  if(id){const note=await o.api('/notes/'+id+'?kb_id='+encodeURIComponent(k));if(token!==openEpoch||o.current().kb?.id!==k||note.kb_id!==k)return;e={...note,kb:k,baseRevision:note.revision,localRevision:0,dirty:false};}
  else e={kb:k,id:null,artifact_id:null,title:'新笔记',body:'',baseRevision:0,localRevision:0,dirty:true,edited:true,citations:[],source_state:'valid'};
  editor=restore(e);persist();clearServerVersion();draw();if(!$('note-dialog').open)$('note-dialog').showModal();if(!fromRoute&&editor.artifact_id)o.setUrl({kb:editor.kb,session:o.sessionId(),artifact:editor.artifact_id});
 }
 function close(){openEpoch++;persist();clearServerVersion();$('note-dialog').close();if(editor)o.setUrl({kb:editor.kb,session:o.sessionId()},true);editor=null;}
 function edit(){if(!editor||readonly())return;editor.title=$('note-title').value;editor.body=$('note-body').value;editor.localRevision++;editor.dirty=true;persist();$('note-status').textContent='有未保存的本地草稿。';}
 async function save(){
  const e=editor;if(readonly(e)||e.saving)return;if(e.title!==$('note-title').value||e.body!==$('note-body').value)edit();
  // An uncertain acknowledgement is retried with the original payload, even after more typing.
  const body=e.pending?{...e.pending}:{request_key:crypto.randomUUID(),title:e.title,body:e.body,client_revision:e.localRevision};delete body.localRevision;
  if(e.id&&!e.pending)body.expected_revision=e.baseRevision;
  const oldKey=draftKey(e),token=e.draftToken;e.pending={...body,localRevision:body.client_revision};e.saving=true;persist(e);draw();
  try{
   const result=await o.api(e.id?'/notes/'+e.id:'/knowledge-bases/'+e.kb+'/notes',{method:e.id?'PATCH':'POST',body});
   e.id=result.id;e.artifact_id=result.artifact_id;e.baseRevision=result.revision;e.pending=null;e.edited=true;e.conflict=false;
   e.dirty=e.localRevision!==result.client_revision;
   const written=persist(e),saved=stored(oldKey);
   if(written&&oldKey!==draftKey(e)&&saved?.draftToken===token&&saved.localRevision<=e.localRevision){try{sessionStorage.removeItem(oldKey);}catch{}}
   if(!written)o.notice('笔记已提交，但本地草稿存储失败。请保留当前页面；已有恢复副本未清除。');
   if(editor===e){$('note-status').textContent=e.dirty?'旧草稿已保存，当前还有新修改。':'已保存。';o.setUrl({kb:e.kb,session:o.sessionId(),artifact:e.artifact_id},true);}
   if(o.current().kb?.id===e.kb)await load();
  }catch(error){if(error.status){e.pending=null;if(error.status===412)e.conflict=true;persist(e);}if(editor===e)$('note-status').textContent=error.message;throw error;}
  finally{e.saving=false;persist(e);if(editor===e){$('note-save').disabled=readonly();$('note-load-server').hidden=!e.conflict;}}
 }
 async function copy(){
  const e=editor;if(readonly(e)||!e.id)return;
  if(e.title!==$('note-title').value||e.body!==$('note-body').value)edit();
  const visit=openEpoch,revision=e.localRevision,body={request_key:e.copyRevision===revision?e.copyKey:(e.copyKey=crypto.randomUUID()),title:e.title+'（副本）',body:e.body,copy_from_note_id:e.id,client_revision:revision};
  e.copyRevision=revision;persist(e);
  const result=await o.api('/knowledge-bases/'+e.kb+'/notes',{method:'POST',body});
  if(editor!==e||visit!==openEpoch)return;
  if(e.localRevision===revision)e.dirty=false;
  persist(e);await load();if(editor!==e||visit!==openEpoch)return;
  if(e.localRevision!==revision){o.notice('副本已创建，后续输入已保留在原笔记草稿中。');return;}
  await open(result.id);o.notice('已另存为独立笔记。');
 }
 async function serverVersion(){
  const e=editor;if(!e?.id||e.source_state==='revoked')return;
  const note=await o.api('/notes/'+e.id+'?kb_id='+e.kb);if(editor!==e)return;
  if(e.source_state==='revoked'||note.source_state==='revoked'){
   if(e.source_state!=='revoked')persist(e);
   e.source_state='revoked';e.body='';e.citations=[];e.conflict=false;clearServerVersion();draw();return;
  }
  $('note-server').hidden=false;$('note-server-body').textContent=note.title+'\n\n'+note.body;
  $('note-status').textContent='下面显示服务器版本，本地正文保持原样。可手动合并或另存副本。';
 }
 async function collect(run){const key='collect:'+run.id,body={request_key:key,origin_run_id:run.id};await o.api('/knowledge-bases/'+run.kb_id+'/notes',{method:'POST',body});if(o.current().kb?.id===run.kb_id)await load();o.notice('已保存为笔记，没有再次调用模型。');}
 $('new-note').addEventListener('click',()=>o.guard(()=>open(null)));$('note-title').addEventListener('input',edit);$('note-body').addEventListener('input',edit);$('note-save').addEventListener('click',()=>o.guard(save));$('note-copy').addEventListener('click',()=>o.guard(copy));$('note-load-server').addEventListener('click',()=>o.guard(serverVersion));$('close-note').addEventListener('click',close);$('note-dialog').addEventListener('cancel',e=>{e.preventDefault();close();});
  return {load,open,collect,detach(){openEpoch++;persist();clearServerVersion();if($('note-dialog').open)$('note-dialog').close();editor=null;kb=null;notes=[];$('studio-notes').replaceChildren();$('new-note').disabled=true;epoch++;}};
}};
