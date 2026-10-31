/* View decisions shared by the UI and contract tests. No network or browser state. */
(function(root){
'use strict';
const tutorialNames={rag:'01-大模型RAG技术从小白到深入理解.pdf',agent:'02-大模型Agent知识从0-1笔记-万字详解版本！.pdf'};
function docName(doc){return doc?.filename===tutorialNames.rag?'RAG 教程':doc?.filename===tutorialNames.agent?'Agent 教程':(doc?.filename||'来源文档')}
function versionName(doc){return ({'tutorial-assisted-v1':'教程版 v1','tutorial-assisted-v2':'教程版 v2','deployment-check':'部署检查'})[doc?.product_version]||doc?.product_version||'未标注版本'}
function groupDocuments(docs){const groups=new Map();for(const doc of docs){if(!groups.has(doc.filename))groups.set(doc.filename,[]);groups.get(doc.filename).push(doc)}return Array.from(groups,([name,versions])=>{const superseded=new Set(versions.map(d=>d.supersedes_snapshot_id).filter(Boolean));return {name,versions:versions.slice().sort((a,b)=>Number(superseded.has(a.snapshot_id))-Number(superseded.has(b.snapshot_id))||String(b.product_version||'').localeCompare(String(a.product_version||''),'zh-CN',{numeric:true}))}}).sort((a,b)=>a.name.localeCompare(b.name,'zh-CN',{numeric:true}));}
function selectionBody(question,ids,docs,mode='answer'){
 if(!question.trim())throw Error('请先输入问题。');if(!ids.length)throw Error('请至少选择一份回答依据。');if(ids.length>4||new Set(ids).size!==ids.length)throw Error('最多选择 4 份不同的资料。');
 const selected=ids.map(id=>docs.find(d=>d.snapshot_id===id));if(selected.some(d=>!d))throw Error('资料已更新，请刷新后重新选择。');if(new Set(selected.map(d=>d.filename)).size!==ids.length)throw Error('同一份文档只能选择一个版本。');if(!['answer','evidence_only','llm'].includes(mode))throw Error('请选择有效的回答方式。');return {question:question.trim(),snapshot_ids:ids.slice(),mode};
}
function statusInfo(status){const known={GENERATED_ANSWER:['回答完成','success'],EVIDENCE_ONLY:['已找到原文','neutral'],MODEL_ABSTAIN:['暂未生成答案','notice'],ABSTAIN_SUPPORT_REVIEW:['回答未通过核对','notice'],ABSTAIN_SOURCE_CHANGED:['资料已更新，请重新提问','notice'],ABSTAIN_PROVIDER_FAILURE:['模型服务暂时不可用','error'],LLM_NOT_CONFIGURED:['回答服务尚未就绪','error'],RETRIEVAL_UNAVAILABLE:['资料检索暂时不可用','error'],ABSTAIN_DOCUMENT_SCOPE:['请先选择回答依据','notice'],ABSTAIN_VERSION_CONFLICT:['请选择同一文档的一个版本','notice'],ABSTAIN_NO_DOCUMENTS:['请先导入文档','notice'],ABSTAIN_UNKNOWN_DOCUMENT:['资料已变更，请重新选择','notice'],ABSTAIN_NO_EVIDENCE:['没有找到可用原文','notice']};const value=known[status]||(String(status).startsWith('ABSTAIN_')?['本次回答未能完成核对','notice']:['暂时无法完成回答','error']);return {label:value[0],tone:value[1]};}
function presentation(response,scope){
 const status=response.status,info=statusInfo(status),allowed=new Set(scope||response.snapshot_ids||[]);const valid=e=>e&&allowed.has(e.snapshot_id)&&/^E\d+$/.test(e.evidence_key)&&Array.isArray(e.quotes)&&e.quotes.length&&e.quotes.every(q=>typeof q==='string'&&q.trim());
 const approved=(response.source_extracts||response.extracts||[]).filter(valid);const answer=status==='GENERATED_ANSWER'&&response.model_support_review_passed===true&&approved.length>0;const evidence=status==='EVIDENCE_ONLY'&&approved.length>0;
 if(status==='GENERATED_ANSWER'&&!answer){info.label='回答未能确认';info.tone='notice'}
 const text=(answer||evidence)&&typeof response.safe_output==='string'?response.safe_output:'';const sources=answer||evidence?approved:(response.retrieved_evidence_kind==='source_excerpts_not_an_answer'?(response.retrieved_evidence||[]).filter(valid):[]);return {...info,kind:answer?'answer':evidence?'evidence':'refusal',text,sources,reason:typeof response.reason==='string'&&/[\u4e00-\u9fff]/.test(response.reason)?response.reason:''};
}
function stageIndex(stage){return stage==='support_review'||stage==='source_revalidation'?2:stage==='generation'?1:0}
const api={tutorialNames,docName,versionName,groupDocuments,selectionBody,statusInfo,presentation,stageIndex};root.RagUI=api;if(typeof module!=='undefined')module.exports=api;
})(typeof window==='undefined'?globalThis:window);
