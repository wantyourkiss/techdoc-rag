"""Safe extraction disclosure; counts never imply semantic completeness."""
MESSAGES={
 'DOCX_MERGED_TABLE_EXCLUDED':'合并表格未纳入问答',
 'DOCX_VISUAL_OBJECT_EXCLUDED':'图片或 Office 图形未解释',
 'PDF_TABLE_CONTENT_EXCLUDED':'检测到的表格内容未纳入问答',
 'PDF_IMAGE_CONTENT_EXCLUDED':'图片中的文字及关系未解析',
 'PDF_NO_TEXT_PAGE':'存在无可提取文字的页面，可能需要 OCR',
 'PDF_READING_ORDER_UNVERIFIED':'PDF 阅读顺序未经核验',
 'PDF_VECTOR_DRAWINGS_NOT_INTERPRETED':'矢量绘图关系未解释',
 'PDF_TABLE_DETECTION_UNAVAILABLE':'表格检测未完成',
 'HTML_STRUCTURE_LIMITED':'HTML 仅保留基础文本结构',
 'MARKDOWN_UNCLOSED_FENCE':'Markdown 代码围栏未闭合',
 'MINERU_MACHINE_EXTRACTED':'由 MinerU 云端识别，文字及数值需对照原件核对',
 'MINERU_DISCARDED_REGIONS':'页眉、页脚或其他区域被解析服务排除',
 'MINERU_IMAGE_CONTENT_EXCLUDED':'图片仅保留可用图注，图中关系未解释',
 'MINERU_UNSUPPORTED_CONTENT':'部分内容结构无法安全转换，未纳入问答',
 'MINERU_EMPTY_PAGE':'存在无已索引文字的页面，请核对原件',
}
_PARTIAL={'DOCX_MERGED_TABLE_EXCLUDED','DOCX_VISUAL_OBJECT_EXCLUDED','PDF_TABLE_CONTENT_EXCLUDED',
 'PDF_IMAGE_CONTENT_EXCLUDED','PDF_NO_TEXT_PAGE','MINERU_DISCARDED_REGIONS',
 'MINERU_IMAGE_CONTENT_EXCLUDED','MINERU_UNSUPPORTED_CONTENT','MINERU_EMPTY_PAGE'}
ERRORS={
 'NO_INDEXABLE_CONTENT':'没有可入库文字。扫描件需要 OCR；请检查原文件或解析配置。',
 'MINERU_AUTH_FAILED':'MinerU 凭据无效或已过期，请管理员更新配置。',
 'MINERU_QUOTA_EXCEEDED':'MinerU 额度或队列已达上限，请稍后处理。',
 'MINERU_TIMEOUT':'等待云端解析超时；重试会继续查询原任务。',
 'MINERU_NETWORK_FAILED':'云端连接未完成；重试会继续确认原任务。',
 'MINERU_SUBMISSION_UNCERTAIN':'云端任务创建状态不确定。请管理员核对，暂不重复提交。',
 'MINERU_PARSE_FAILED':'云端解析失败，请检查文件；已有版本保持可读。',
 'MINERU_RESULT_INVALID':'云端结果缺少可信结构或超出安全限制，未发布。',
 'MINERU_UPLOAD_EXPIRED':'云端上传链接已失效，请重新发起导入。',
 'MINERU_NOT_CONFIGURED':'云端解析未配置完整，请管理员检查。',
 'PDF_TOO_MANY_PAGES':'PDF 超过 200 页，请拆分后上传。',
 'ENCRYPTED_PDF_UNSUPPORTED':'不支持加密 PDF，请解密后上传。',
 'DOCX_EXPANSION_LIMIT':'Word 文件解压后过大，未发送至解析服务。',
 'INVALID_DOCX_PACKAGE':'Word 文件格式损坏，请检查原件。',
}

def parsing_report(meta,blocks=None):
 warnings=[w for w in meta.get('warnings',[]) if isinstance(w,str)]
 details=meta.get('parse_details',{})
 report={'contract':'parsing_report_v1','status':'partial' if _PARTIAL&set(warnings) else ('review' if warnings else 'processed'),
  'method':'mineru_api' if meta.get('parser')=='mineru_api_v1' else 'local',
  'coverage_verified':False,'machine_extracted':meta.get('parser')=='mineru_api_v1',
  'indexed_blocks':meta.get('indexed_block_count'),'total_blocks':meta.get('block_count'),
  'total_pages':details.get('total_pages'),'empty_pages':details.get('empty_pages',[]),
  'issues':[{'code':w if w in MESSAGES else 'OTHER_PARSE_WARNING','message':MESSAGES.get(w,'部分文档结构需对照原件核对')} for w in warnings if w!='PDF_DEGENERATE_TABLE_AS_POSITIONED_TEXT'],
  'locations':details.get('locations',[])[:20],
  'locations_truncated':len(details.get('locations',[]))>20,
  'disclosure':'处理成功不代表全文完整或事实已验收；请对照原件核对关键数值。'}
 return report
