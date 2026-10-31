/* Verified result only: progress contains no answer text or source excerpts. */
(function(root){
'use strict';
function createSSEParser(onEvent){
 let pending='';
 return {
  push(text){
   pending+=text;
   if(pending.length>2000000)throw Error('响应过大');
   let match;
   while((match=/\r?\n\r?\n/.exec(pending))){
    const block=pending.slice(0,match.index);pending=pending.slice(match.index+match[0].length);
    let kind='message',id=null;const data=[];
    for(const line of block.split(/\r?\n/)){
     if(line.startsWith('event:'))kind=line.slice(6).trim();
     else if(line.startsWith('id:'))id=line.slice(3).trim();
     else if(line.startsWith('data:'))data.push(line.slice(5).replace(/^ /,''));
    }
    if(data.length)onEvent(kind,JSON.parse(data.join('\n')),{id});
   }
  },
  finish(){if(pending.trim()&&!pending.trim().startsWith(':'))throw Error('响应意外中断')}
 };
}
async function queryWithProgress(body,onProgress,signal){
 const response=await fetch('/query/stream',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body),signal});
 if(!response.ok){const x=await response.json();throw Error(x.detail==='QUERY_CAPACITY_REACHED'?'已有请求正在处理，请稍后重试':(typeof x.detail==='string'?x.detail:'查询失败'))}
 if(!response.body)throw Error('浏览器无法读取进度，请刷新后重试');
 let result,received=false;const reader=response.body.getReader(),decoder=new TextDecoder();
 const parser=createSSEParser((kind,data)=>{
  if(kind==='progress'){if(onProgress)onProgress(data)}
  else if(kind==='result'){result=data;received=true}
  else if(kind==='error')throw Error(data.code==='QUERY_CANCELLED'?'查询已取消':'查询失败，请凭请求编号 '+data.request_id+' 排查');
 });
 try{
  while(true){const {value,done}=await reader.read();if(done)break;parser.push(decoder.decode(value,{stream:true}))}
  parser.push(decoder.decode());parser.finish();if(!received)throw Error('结果尚未完成，连接已中断');return result;
 }catch(error){try{await reader.cancel()}catch(e){}throw error}
 finally{reader.releaseLock()}
}
root.queryWithProgress=queryWithProgress;
root.TechDocStream={createSSEParser};
if(typeof module!=='undefined')module.exports={createSSEParser,queryWithProgress};
})(typeof window!=='undefined'?window:globalThis);
