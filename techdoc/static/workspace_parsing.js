/* Plain-text disclosure of extraction limits; never a completeness score. */
(function(root){'use strict';
 const labels={partial:'部分内容未解析',review:'解析结果需核对',processed:'已处理'};
 const errors={NO_INDEXABLE_CONTENT:'没有可入库文字。扫描件需要 OCR，请检查原件或解析配置。',
 MINERU_AUTH_FAILED:'云端解析凭据无效或过期，请管理员更新。',MINERU_QUOTA_EXCEEDED:'云端额度或队列已达上限，请稍后处理。',
 MINERU_TIMEOUT:'云端解析等待超时；重试将继续查询原任务。',MINERU_NETWORK_FAILED:'云端连接未完成；重试将继续确认原任务。',
 MINERU_SUBMISSION_UNCERTAIN:'云端任务创建状态不确定，请管理员核对，避免重复提交。',
 MINERU_PARSE_FAILED:'云端解析失败，请检查原文件后重新发起导入。',MINERU_RESULT_INVALID:'解析结果结构异常或超出安全限制，未发布。',
 MINERU_UPLOAD_EXPIRED:'云端上传链接已失效，请重新发起导入。',MINERU_NOT_CONFIGURED:'云端解析配置不完整。',
 PDF_TOO_MANY_PAGES:'PDF 超过 200 页，请拆分后上传。',ENCRYPTED_PDF_UNSUPPORTED:'请解密 PDF 后上传。',
 DOCX_EXPANSION_LIMIT:'Word 解压后过大，未发送至解析服务。',INVALID_DOCX_PACKAGE:'Word 文件损坏，请检查原件。'};
 function describe(report){
  if(report?.contract!=='parsing_report_v1'||report.coverage_verified!==false)return null;
  const issues=Array.isArray(report.issues)?report.issues.filter(v=>typeof v?.message==='string'&&v.code!=='MINERU_MACHINE_EXTRACTED').map(v=>v.message):[];
  const locations=Array.isArray(report.locations)?report.locations:[];
  const pages=[...new Set(locations.map(v=>v.page||v.converted_page).filter(v=>Number.isInteger(v)&&v>0))];
  const details=[report.machine_extracted?'MinerU 云端解析；识别文字及数值请对照原件核对。':'本地解析。',...issues];
  if(Number.isInteger(report.total_pages))details.push(`共 ${report.total_pages} 页。`);
  if(pages.length)details.push('需核对的位置：第 '+pages.join('、')+' 页'+(report.locations_truncated?'等':'')+'。');
  if(Array.isArray(report.empty_pages)&&report.empty_pages.length)details.push('无已索引文字：第 '+report.empty_pages.join('、')+' 页。');
  if(Number.isInteger(report.indexed_blocks)&&Number.isInteger(report.total_blocks))details.push(`已索引 ${report.indexed_blocks}/${report.total_blocks} 个检测到的文本或结构块；这不是解析完整率。`);
  details.push('处理成功不代表全文完整或事实已验收。');
  return {label:labels[report.status]||'解析情况待核对',details};
 }
 root.WorkspaceParsing={describe,error:code=>errors[code]||'这份文件未能发布，已有可用版本保持可读。'};
})(typeof window==='undefined'?globalThis:window);
