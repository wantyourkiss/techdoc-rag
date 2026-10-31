/* Per-file durable identity in a bounded, explicit, in-memory upload queue. */
window.WorkspaceUploads={create(o){
 'use strict';const batches=new Map(),slot=kb=>'rag.upload.queue.v1:'+kb;
 function save(b){try{sessionStorage.setItem(slot(b.kb),JSON.stringify({id:b.id,kb:b.kb,entries:b.entries.map(({file,...e})=>e)}));return true;}catch{return false;}}
 const changed=b=>{save(b);try{o.changed?.(b);}catch{}};
 function get(kb){
  if(!kb||batches.has(kb))return batches.get(kb);
  try{
   const b=JSON.parse(sessionStorage.getItem(slot(kb))||'null');
   if(!b||b.kb!==kb||!Array.isArray(b.entries)||b.entries.length>20||b.entries.some(e=>typeof e.key!=='string'||e.key.length>128||typeof e.name!=='string'||!Number.isFinite(e.size)))return;
   b.busy=false;b.paused=true;b.reason='已恢复提交标识。可查询原提交；未提交文件需重新选择相同原件。';
   for(const e of b.entries){e.file=null;if(['uploading','unknown'].includes(e.state))e.state='unknown';else if(e.state==='pending')e.state='needs_file';}
   batches.set(kb,b);return b;
  }catch{}
 }
 async function fingerprint(file){if(o.fingerprint)return o.fingerprint(file);return [...new Uint8Array(await crypto.subtle.digest('SHA-256',await file.arrayBuffer()))].map(b=>b.toString(16).padStart(2,'0')).join('');}
 function create(kb,files,{version_label='',display_name=null}={}){
  const old=get(kb);
  if(old&&(old.busy||old.entries.some(e=>['pending','needs_file','unknown','uploading'].includes(e.state))))throw Error('请先继续确认或移除当前批次的待提交文件。');
  if(!files.length||files.length>20)throw Error('每批请选择 1～20 个文件。');
  if(files.reduce((n,f)=>n+f.size,0)>64*1024*1024)throw Error('每批文件合计不能超过 64 MiB。');
  const b={id:crypto.randomUUID(),kb,busy:false,paused:false,reason:'',entries:files.map(file=>({file,name:file.name,size:file.size,sha256:null,key:crypto.randomUUID(),revision:null,sent:false,
   version_label,display_name:files.length===1?display_name:null,state:!file.size||file.size>8*1024*1024||! /\.(pdf|docx|md|txt|html|htm)$/i.test(file.name)?'invalid':'pending',error:'',retryable:false}))};
  batches.set(kb,b);changed(b);return b;
 }
 function live(b,epoch){const c=o.current();return c.kb?.id===b.kb&&c.epoch===epoch&&c.kb.state==='active'&&!c.authPaused;}
 async function run(b){
  if(batches.get(b.kb)!==b||b.busy)return;
  const epoch=o.current().epoch;if(!live(b,epoch))return;
  b.busy=true;b.paused=false;b.reason='';changed(b);
  try{
   for(const e of b.entries){
    if(!['pending','needs_file','unknown'].includes(e.state))continue;
    if(b.paused||!live(b,epoch)){b.paused=true;b.reason='页面或权限状态已变化，剩余文件暂停。';break;}
     if(e.sent){
     try{const result=await o.find(b.kb,e.key);e.state='accepted';e.job_id=result.job_id;e.error='';try{await o.accepted?.(b);}catch{}changed(b);continue;}
      catch(error){if(error.status!==404||!e.file){b.paused=true;b.reason=error.status===404?'服务器暂未找到原提交；可以稍后再查询，或重新选择相同原件以沿用原标识确认。':error.message;break;}}
    }
    if(!e.file){if(!e.sent)e.state='needs_file';b.paused=true;b.reason='请重新选择相同的原文件后继续。';break;}
    if(!e.sha256){try{e.sha256=await fingerprint(e.file);}catch{b.paused=true;b.reason='无法计算文件校验，请重新选择文件。';break;}}
    if(!e.sent){
     try{e.revision=await o.revision(b.kb);}catch(error){b.paused=true;b.reason=error.message;break;}
     if(b.paused||!live(b,epoch)){b.paused=true;break;}
    }
    const wasSent=e.sent;e.state='uploading';e.sent=true;
    if(!save(b)){e.sent=wasSent;e.state=wasSent?'unknown':'pending';b.paused=true;b.reason='无法保存提交标识，已暂停上传。请释放浏览器存储后继续。';break;}
    changed(b);
    try{
     const result=await o.send(b.kb,e);e.state='accepted';e.job_id=result.job_id;e.file=null;e.error='';
     // A later refresh failure must never turn an acknowledged POST into a retry.
     try{await o.accepted?.(b);}catch{}
    }catch(error){
     e.error=error.message||'提交未完成';
     if(!error.status||error.status>=500||error.status===408){e.state='unknown';b.paused=true;b.reason='尚未确认该文件是否已接收；继续确认会先查询回执，并沿用原提交标识和原目录版本。';}
     else{
      e.state='failed';e.retryable=error.code==='CATALOG_REVISION_CONFLICT';e.sent=false;
      if(error.status===429&&error.code==='JOB_QUEUE_FULL'){e.state='pending';e.revision=null;b.paused=true;b.reason='服务器任务队列已满。请等待已有任务完成后继续上传；原提交标识保留。';}
      if([401,403,404,410].includes(error.status)){b.paused=true;b.reason='权限或知识库状态已变化，后续文件暂停。';}
     }
    }
    changed(b);if(b.paused)break;
   }
  }finally{b.busy=false;changed(b);}
 }
 return {create,run,get,pause(b){b.paused=true;b.reason='剩余文件已暂停；正在提交的文件需等待确认。';changed(b);},
  async attach(b,files){
   if(b.busy)throw Error('请先等待当前提交确认。');
   for(const file of files){const hash=await fingerprint(file),e=b.entries.find(e=>!e.file&&e.name===file.name&&e.size===file.size&&(!e.sha256||e.sha256===hash)&&['unknown','needs_file','failed'].includes(e.state));if(!e)throw Error('文件与待确认的原件不匹配：'+file.name);e.file=file;e.sha256=hash;if(e.state==='needs_file')e.state='pending';changed(b);}
  },
  retry(b,e){if(e.state==='failed'&&e.retryable){e.state='pending';e.revision=null;}return run(b);},
  clear(b){if(b.busy||b.entries.some(e=>e.state==='unknown'))return false;for(const e of b.entries)e.file=null;batches.delete(b.kb);try{sessionStorage.removeItem(slot(b.kb));}catch{}try{o.changed?.(b);}catch{}return true;}};
}};
