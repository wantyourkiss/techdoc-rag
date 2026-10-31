const test=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
class Element{constructor(text='',attrs={}){this.textContent=text;this.attrs=attrs;this.children=[];this.dataset={};this.options=[{},{}];this.value='';}append(...nodes){this.children.push(...nodes);}replaceChildren(...nodes){this.children=nodes;}addEventListener(){}setAttribute(){}focus(){}}
const all=node=>[node,...node.children.flatMap(all)];
async function fixture({source_state='valid',status='succeeded',result_status='EVIDENCE_ONLY',verified=false}={}){
 const nodes=new Map(),node=id=>{if(!nodes.has(id))nodes.set(id,new Element());return nodes.get(id);},opened=[];
 const session={id:'s',kb_id:'kb',revision:1,last_message_id:'m'},run={id:'r',session_id:'s',status,source_state,allowed_document_count:1,
  result:{status:result_status,reason:'处理结束',source_extracts:[{citation_id:'c1',evidence_key:'E1',document_name:'Guide'}],question_evidence:{contract:'question_evidence_v1',method:'shared_terms_v1',semantic_coverage_verified:verified,parts:[
    {question:'<script>literal question</script>',state:'related_excerpt',citation_ids:['c1','PRIVATE_OUTSIDE']},
    {question:'Second question',state:'related_excerpt',citation_ids:['PRIVATE_OUTSIDE']} ]}}};
 const sandbox={window:{WorkspaceModel:{selected:()=>['doc']}},document:{getElementById:node},sessionStorage:{getItem:()=>null,setItem:()=>{}},requestAnimationFrame:fn=>fn()};
 vm.runInNewContext(fs.readFileSync(path.join(__dirname,'../techdoc/static/workspace_chat.js'),'utf8'),sandbox);
 const controller=sandbox.window.WorkspaceConversation.create({current:()=>({kb:{id:'kb',state:'active'},view:{draft:''},documents:[],capabilities:{generative_answer_enabled:true}}),
  api:async url=>url.endsWith('/sessions')?{items:[session]}:url.includes('/messages')?{items:[{id:'m',role:'assistant',run_id:'r',run}],next_before:null,session_revision:1,last_message_id:'m'}:session,
  el:(_tag,text='',attrs={})=>new Element(text,attrs),button:(text,invoke,cls)=>Object.assign(new Element(text),{invoke,cls}),setUrl:()=>{},openCitation:(...args)=>opened.push(args)});
 await controller.load(new URLSearchParams('session=s'));return {nodes:all(node('messages')),opened};
}
test('question evidence is disclosed as unverified and only links this runs actual citations',async()=>{
 const f=await fixture(),panel=f.nodes.find(n=>n.attrs.class==='question-evidence-map');assert.ok(panel);
 const rows=all(panel);assert.ok(rows.some(n=>n.textContent.includes('不代表每部分已回答正确')));
 assert.ok(rows.some(n=>n.textContent==='<script>literal question</script>'));
 assert.ok(rows.some(n=>n.textContent==='未自动关联到引用'));
 const buttons=rows.filter(n=>n.cls==='source-citation');assert.equal(buttons.length,1);buttons[0].invoke();
 assert.equal(f.opened[0][0],'c1');assert.equal(f.opened[0][1].sessionId,'s');assert.ok(!rows.some(n=>n.textContent.includes('PRIVATE_OUTSIDE')));
});
for(const opts of [{source_state:'revoked'},{status:'failed'},{result_status:'ABSTAIN_PROVIDER_FAILURE',status:'abstained'},{verified:true}])test('invalid, revoked or unapproved evidence map stays hidden '+JSON.stringify(opts),async()=>{
 const f=await fixture(opts);assert.ok(!f.nodes.some(n=>n.attrs.class==='question-evidence-map'));
});
