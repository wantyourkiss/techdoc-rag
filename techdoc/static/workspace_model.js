(function(root,factory){if(typeof module==='object'&&module.exports)module.exports=factory();else root.WorkspaceModel=factory();})(typeof globalThis!=='undefined'?globalThis:this,function(){
 'use strict';
 function ready(documents){return documents.filter(d=>d.state==='active'&&d.active_version_id&&d.processing_state==='ready');}
 function selected(view,documents){const ids=new Set(ready(documents).map(d=>d.id));return view.mode==='all_ready'?[...ids]:[...view.selected].filter(id=>ids.has(id));}
 function toggle(view,documents,id,checked){const ids=new Set(selected(view,documents));if(checked)ids.add(id);else ids.delete(id);return {...view,mode:'selected',selected:ids};}
 function selectAll(checked){return {mode:checked?'all_ready':'selected',selected:new Set()};}
 function selectVisible(view,documents,visible){return {mode:'selected',selected:new Set([...selected(view,documents),...ready(visible).map(d=>d.id)])};}
 function clean(view,documents){return view.mode==='all_ready'?view:{...view,selected:new Set(selected(view,documents))};}
 function summarize(view,documents,visible){const chosen=selected(view,documents),shown=new Set(visible.map(d=>d.id)),total=ready(documents).length;return {chosen,total,hidden:chosen.filter(id=>!shown.has(id)).length,checked:total>0&&chosen.length===total,indeterminate:chosen.length>0&&chosen.length<total};}
 return {ready,selected,toggle,selectAll,selectVisible,clean,summarize};
});
