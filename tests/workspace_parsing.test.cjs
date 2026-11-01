const test=require('node:test'),assert=require('node:assert/strict');
require('../techdoc/static/workspace_parsing.js');
test('partial extraction exposes location and honest counts, never a completeness score',()=>{
 const v=WorkspaceParsing.describe({contract:'parsing_report_v1',coverage_verified:false,status:'partial',machine_extracted:true,total_pages:2,indexed_blocks:1,total_blocks:3,issues:[{message:'表格未纳入问答'}],locations:[{page:2}],empty_pages:[2]});
 assert.equal(v.label,'部分内容未解析');assert(v.details.some(x=>x.includes('第 2 页')));assert(v.details.some(x=>x.includes('不是解析完整率')));
});
test('uncertain submission and scan failures have actionable fixed messages',()=>{
 assert(WorkspaceParsing.error('MINERU_SUBMISSION_UNCERTAIN').includes('避免重复提交'));
 assert(WorkspaceParsing.error('NO_INDEXABLE_CONTENT').includes('OCR'));
 assert(!WorkspaceParsing.error('private provider secret').includes('private'));
 assert.equal(WorkspaceParsing.describe({coverage_verified:true}),null);
});
