"""Opt-in candidate API. All data endpoints share the app's authentication."""
from __future__ import annotations

from pathlib import Path
from uuid import uuid4
from urllib.parse import urlsplit
import json
import os

from fastapi import APIRouter, File, Form, UploadFile, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, FileResponse
from pydantic import BaseModel, Field, ConfigDict
from typing import Literal

from .catalog import CatalogError
from .core import MAX_BYTES


MESSAGES = {
    'OVERVIEW_MODEL_NOT_CONFIGURED':'大模型尚未配置，暂时无法生成资料概览。',
    'KNOWLEDGE_BASE_NOT_FOUND':'知识库不存在。', 'KNOWLEDGE_BASE_TRASHED':'知识库已在回收站。',
    'KNOWLEDGE_BASE_READ_ONLY':'此知识库已归档，请恢复使用后再修改。',
    'KNOWLEDGE_BASE_NAME_CONFLICT':'已有同名知识库，请换一个名称。',
    'CATALOG_REVISION_CONFLICT':'资料已在其他页面变更，请刷新后重新确认。',
    'DOCUMENT_NOT_FOUND':'此知识库中没有该资料。','DOCUMENT_TRASHED':'资料已被移入回收站。',
    'VERSION_NOT_FOUND':'资料版本不存在或不属于此知识库。','SOURCE_UNAVAILABLE':'原文暂时不可读取，请检查服务状态。',
    'JOB_REQUEST_KEY_CONFLICT':'该提交标识已用于另一份内容，请重新提交。',
    'JOB_QUEUE_FULL':'后台任务较多，请稍后再试。','IMPORT_NOT_FOUND':'未找到这次导入记录。',
    'SAME_NAME_REQUIRES_EXPLICIT_CHOICE':'已有同名资料，请选择上传新版本，或填写另建资料的显示名。',
    'WORKSPACE_STORAGE_LIMIT':'资料存储空间不足，请清理后再上传。',
    'REVISION_PARENT_CHANGED':'资料版本已经变化，请重新选择要更新的版本。',
    'EMPTY_OR_OVERSIZED_UPLOAD':'请选择非空文件，单个文件最大 8 MiB。',
    'REMOVAL_PREVIEW_CHANGED':'受影响的内容已变化，请重新预览后再确认移除。',
    'LEGACY_ENDPOINT_DISABLED':'知识库模式已启用，请使用新版知识库入口。',
    'CROSS_ORIGIN_WRITE_DENIED':'请从本站页面执行此操作。',
    'INVALID_REQUEST':'请求内容或格式不正确，请检查后重试。',
    'SESSION_REVISION_CONFLICT':'对话已在其他页面更新，请加载最新消息后再发送。',
    'SESSION_RUN_PENDING':'此对话仍有任务未结束，请先等待或确认停止状态。',
    'SESSION_NOT_FOUND':'没有找到这个对话。','RUN_NOT_FOUND':'没有找到这次任务。',
    'SUBMISSION_NOT_FOUND':'服务器尚未找到这次提交，请确认网络恢复后再决定是否发送。',
    'REQUEST_KEY_CONFLICT':'提交标识已用于不同内容，请重新发起操作。',
    'INVALID_SOURCE_SCOPE':'资料范围不正确或过大，请重新选择资料。',
    'EMPTY_OR_DUPLICATE_SCOPE':'请选择至少一份可用资料。','NO_READY_SOURCES':'请先添加可用资料。',
    'SOURCE_SCOPE_CHANGED':'所选资料已变化，请刷新资料列表后重新选择。',
    'NOTE_REVISION_CONFLICT':'笔记已在其他页面修改。本地草稿已保留，请对照服务器版本或另存副本。',
    'NOTE_SOURCE_REVOKED':'笔记关联的来源已撤销，暂时不能编辑或复制。',
    'ANSWER_NOT_COLLECTABLE':'此回答尚未通过来源检查，或来源已移除，无法收藏。',
    'CITATION_SOURCE_REVOKED':'此引用的来源已移除，无法继续展开。',
    'CITATION_RELATION_MISMATCH':'这条引用不属于当前回答或成果。',
    'NOTE_NOT_FOUND':'未找到这篇笔记。','CITATION_NOT_FOUND':'未找到这条引用。',
    'INVALID_NOTE':'请填写笔记标题，正文最多 10 万字符。',
}


def error_response(request, code, status):
    return JSONResponse({'code':code,'message':MESSAGES.get(code,'操作未完成，请刷新状态后检查输入或重试。'),
        'retryable':status in (429,503),'request_id':getattr(request.state,'knowledge_request_id',uuid4().hex)},status_code=status)


class StrictBody(BaseModel):
    model_config = ConfigDict(extra='forbid')


class CreateLibrary(StrictBody):
    name: str | None = Field(default=None,max_length=60)


class RevisionBody(StrictBody):
    expected_revision: int = Field(ge=1)


class UpdateLibrary(RevisionBody):
    name: str | None = Field(default=None,max_length=60)
    description: str | None = Field(default=None,max_length=1000)


class RestoreBody(RevisionBody):
    name: str | None = Field(default=None,max_length=60)


class RenameDocument(RevisionBody):
    display_name: str = Field(min_length=1,max_length=200)


class ActivateVersion(RevisionBody):
    version_id: str = Field(min_length=1,max_length=80)


class ManagementSelection(StrictBody):
    document_ids: list[str] = Field(min_length=1,max_length=100)


class RemoveDocuments(ManagementSelection,RevisionBody):
    preview_token: str = Field(pattern='^[0-9a-f]{64}$')


class TextSource(RevisionBody):
    title: str = Field(min_length=1,max_length=180)
    text: str = Field(min_length=1,max_length=200000)
    request_key: str = Field(min_length=1,max_length=128)


class CreateSession(StrictBody):
    request_key: str = Field(min_length=1,max_length=128)
    title: str = Field(default='新对话',min_length=1,max_length=100)


class RenameSession(RevisionBody):
    title: str = Field(min_length=1,max_length=100)


class SessionStateChange(RevisionBody):
    kb_id: str = Field(min_length=1,max_length=80)
    request_key: str = Field(min_length=1,max_length=128)


class SourceScope(StrictBody):
    mode: Literal['all_ready','selected']='all_ready'
    document_ids: list[str] | None = Field(default=None,max_length=2048)


class QueryTask(StrictBody):
    type: Literal['qa','comparison']='qa'
    required_document_ids: list[str] = Field(default_factory=list,max_length=4)


class SubmitMessage(StrictBody):
    question: str = Field(min_length=1,max_length=2000)
    request_key: str = Field(min_length=1,max_length=128)
    expected_session_revision: int = Field(ge=1)
    expected_parent_message_id: str | None = Field(default=None,max_length=80)
    scope: SourceScope = Field(default_factory=SourceScope)
    task: QueryTask = Field(default_factory=QueryTask)
    answer_mode: Literal['answer','evidence_only']='answer'


class CreateNote(StrictBody):
    request_key: str = Field(min_length=1,max_length=128)
    title: str | None = Field(default=None,max_length=200)
    body: str = Field(default='',max_length=100000)
    origin_run_id: str | None = Field(default=None,max_length=80)
    copy_from_note_id: str | None = Field(default=None,max_length=80)
    client_revision: int = Field(default=0,ge=0)


class UpdateNote(RevisionBody):
    request_key: str = Field(min_length=1,max_length=128)
    title: str = Field(min_length=1,max_length=200)
    body: str = Field(max_length=100000)
    client_revision: int = Field(default=0,ge=0)


class OverviewLookup(StrictBody):
    scope: SourceScope = Field(default_factory=SourceScope)


class RequestOverview(OverviewLookup):
    request_key: str = Field(min_length=1,max_length=128)
    regenerate: bool = False


def install_knowledge_api(app,service,imports,runs,studio):
    router = APIRouter(prefix='/api/v1')

    @app.middleware('http')
    async def knowledge_boundary(request,call_next):
        request.state.knowledge_request_id = uuid4().hex
        path = request.url.path
        # Legacy readers/query endpoints have no KB relationship to validate.
        # Block them as well as writes; no global snapshot bypass in this mode.
        if (path=='/documents' or path.startswith('/documents/') or path in ('/query','/query/stream')
                or path.startswith('/diagnostics/') or path=='/assets/learning-cases.json'):
            return error_response(request,'LEGACY_ENDPOINT_DISABLED',410)
        if path.startswith('/api/v1') and request.method not in ('GET','HEAD','OPTIONS'):
            origin = request.headers.get('origin')
            host = request.headers.get('host','').lower()
            parsed = urlsplit(origin) if origin else None
            if (request.headers.get('sec-fetch-site')=='cross-site'
                    or (parsed and (parsed.scheme not in ('http','https') or parsed.netloc.lower()!=host))):
                return error_response(request,'CROSS_ORIGIN_WRITE_DENIED',403)
        response = await call_next(request)
        if path.startswith('/api/v1'): response.headers['X-Request-ID']=request.state.knowledge_request_id
        return response

    @app.exception_handler(CatalogError)
    async def catalog_error(request,exc):
        return error_response(request,exc.code,exc.status)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request,exc):
        return error_response(request,'INVALID_REQUEST',422)

    @router.get('/capabilities')
    def capabilities():
        from .mineru_api import parser_mode
        return {'knowledge_mode':True,'phase':'workspace_release_candidate',
                'document_parser':parser_mode(),'cloud_parser_formats':['pdf','docx'] if parser_mode()=='mineru_api' else [],
                'public_release_ready':os.getenv('TECHDOC_WORKSPACE_RELEASED')=='1',
                'studio_tools':['notes','overview'],'tutorial_enabled':False,'chat_enabled':True,'durable_imports':True,
                'generative_answer_enabled':bool(os.getenv('LLM_BASE_URL') and os.getenv('LLM_MODEL') and os.getenv('LLM_API_KEY')),
                'scope_contract_version':'kb_scope_v1','max_allowed_sources':2048,'max_comparison_sources':4,
                'max_upload_bytes':MAX_BYTES,'workspace_storage_limit_bytes':imports.storage_limit,
                'source_formats':['pdf','docx','md','txt','html','htm'],
                'parsed_source_reader':True,'physical_purge':False,
                'rerank_runtime':{'quantized_engine':getattr(runs.store.reranker,'quantized_engine',None),
                    'cpu_threads':getattr(runs.store.reranker,'cpu_threads',None),
                    'score_cache':runs.store.reranker.cache_stats() if callable(getattr(runs.store.reranker,'cache_stats',None)) else
                        runs.store.reranker.score_cache.stats() if getattr(runs.store.reranker,'score_cache',None) else {'enabled':False}}}

    @router.get('/knowledge-bases')
    def list_libraries(state: str='active', search: str=Query('',max_length=200), sort: str='updated',
                       offset: int=Query(0,ge=0),limit: int=Query(50,ge=1,le=100)):
        return service.libraries(state=state,search=search,sort=sort,offset=offset,limit=limit)

    @router.post('/knowledge-bases',status_code=201)
    def create_library(body: CreateLibrary):
        return service.catalog.create_knowledge_base(body.name,workspace_id=service.workspace_id)

    @router.get('/knowledge-bases/{kb_id}')
    def get_library(kb_id: str): return service.library(kb_id)

    @router.patch('/knowledge-bases/{kb_id}')
    def update_library(kb_id: str,body: UpdateLibrary):
        return service.update_library(kb_id,revision=body.expected_revision,name=body.name,description=body.description)

    @router.post('/knowledge-bases/{kb_id}/archive')
    def archive(kb_id: str,body: RevisionBody):
        return service.set_library_state(kb_id,'archived',revision=body.expected_revision)

    @router.post('/knowledge-bases/{kb_id}/unarchive')
    def unarchive(kb_id: str,body: RestoreBody):
        return service.set_library_state(kb_id,'active',revision=body.expected_revision,name=body.name)

    @router.post('/knowledge-bases/{kb_id}/trash')
    def trash(kb_id: str,body: RevisionBody):
        return service.set_library_state(kb_id,'trashed',revision=body.expected_revision)

    @router.post('/knowledge-bases/{kb_id}/restore')
    def restore(kb_id: str,body: RestoreBody):
        return service.restore_library(kb_id,name=body.name,revision=body.expected_revision)

    @router.get('/knowledge-bases/{kb_id}/documents')
    def documents(kb_id: str,state: str='active',search: str=Query('',max_length=200),
                  offset: int=Query(0,ge=0),limit: int=Query(50,ge=1,le=100)):
        return service.documents(kb_id,state=state,search=search,offset=offset,limit=limit)

    @router.post('/knowledge-bases/{kb_id}/documents',status_code=202)
    async def upload(kb_id: str,file: UploadFile=File(...),request_key: str=Form(...),
                     expected_revision: int=Form(...,ge=1),version_label: str=Form('',max_length=120),
                     display_name: str | None=Form(None,max_length=200)):
        try:
            data = await file.read(MAX_BYTES+1)
            # submit only stages bytes; parsing/indexing happen in the worker.
            import asyncio
            return await asyncio.to_thread(imports.submit,kb_id,file.filename or '',data,request_key=request_key,
                revision=expected_revision,version_label=version_label,display_name=display_name)
        finally: await file.close()

    @router.post('/knowledge-bases/{kb_id}/text-sources',status_code=202)
    def text_source(kb_id: str,body: TextSource):
        title = body.title.strip()
        if '/' in title or '\\' in title or any(ord(c)<32 for c in title):
            raise CatalogError('INVALID_DOCUMENT_NAME',status=422)
        return imports.submit(kb_id,title+'.txt',body.text.encode(),request_key=body.request_key,
                              revision=body.expected_revision,display_name=title,origin='pasted_text')

    @router.get('/knowledge-bases/{kb_id}/imports/by-request/{request_key}')
    def import_by_key(kb_id: str,request_key: str): return imports.by_request(kb_id,request_key)

    @router.get('/knowledge-bases/{kb_id}/documents/{document_id}/versions')
    def versions(kb_id: str,document_id: str): return service.versions(kb_id,document_id)

    @router.post('/knowledge-bases/{kb_id}/documents/{document_id}/versions',status_code=202)
    async def revision(kb_id: str,document_id: str,file: UploadFile=File(...),request_key: str=Form(...),
                       expected_revision: int=Form(...,ge=1),version_label: str=Form(...,min_length=1,max_length=120)):
        try:
            data = await file.read(MAX_BYTES+1)
            import asyncio
            return await asyncio.to_thread(imports.submit,kb_id,file.filename or '',data,request_key=request_key,
                revision=expected_revision,version_label=version_label,document_id=document_id)
        finally: await file.close()

    @router.patch('/knowledge-bases/{kb_id}/documents/{document_id}')
    def rename_document(kb_id: str,document_id: str,body: RenameDocument):
        return service.rename_document(kb_id,document_id,body.display_name,revision=body.expected_revision)

    @router.post('/knowledge-bases/{kb_id}/documents/{document_id}/active-version')
    def activate(kb_id: str,document_id: str,body: ActivateVersion):
        return service.activate_version(kb_id,document_id,body.version_id,revision=body.expected_revision)

    @router.post('/knowledge-bases/{kb_id}/documents/{document_id}/restore')
    def restore_document(kb_id: str,document_id: str,body: RevisionBody):
        return service.restore_document(kb_id,document_id,revision=body.expected_revision)

    @router.post('/knowledge-bases/{kb_id}/documents/removal-preview')
    def removal_preview(kb_id: str,body: ManagementSelection):
        return service.removal_preview(kb_id,body.document_ids)

    @router.post('/knowledge-bases/{kb_id}/documents/trash')
    def remove(kb_id: str,body: RemoveDocuments):
        return service.remove_documents(kb_id,body.document_ids,revision=body.expected_revision,preview_token=body.preview_token)

    @router.get('/versions/{version_id}/content')
    def content(version_id: str,kb_id: str,offset: int=Query(0,ge=0),limit: int=Query(40,ge=1,le=100)):
        return service.content(version_id,kb_id=kb_id,offset=offset,limit=limit)

    @router.get('/versions/{version_id}/source')
    def source(version_id: str,kb_id: str):
        row = service.version(version_id,kb_id=kb_id)
        path = service.checked_path(row,original=True)
        service.version(version_id,kb_id=kb_id)
        return FileResponse(path,filename=json.loads(row['metadata_json'])['filename'],media_type='application/octet-stream')

    @router.get('/jobs/{job_id}')
    def job(job_id: str): return imports.get(job_id)

    @router.post('/jobs/{job_id}/cancel')
    def cancel(job_id: str): return imports.cancel(job_id)

    @router.post('/jobs/{job_id}/retry')
    def retry(job_id: str): return imports.retry(job_id)

    @router.get('/knowledge-bases/{kb_id}/sessions')
    def sessions(kb_id: str,offset: int=Query(0,ge=0),limit: int=Query(30,ge=1,le=100),state: str='active',search: str=Query('',max_length=200)):
        return runs.sessions(kb_id,offset=offset,limit=limit,state=state,search=search)

    @router.get('/knowledge-bases/{kb_id}/imports/submissions/{request_key}')
    def import_submission(kb_id: str,request_key: str):
        return imports.by_request(kb_id,request_key)

    @router.post('/sessions/{session_id}/archive')
    def archive_session(session_id: str,body: SessionStateChange):
        return runs.set_session_state(session_id,kb_id=body.kb_id,revision=body.expected_revision,request_key=body.request_key,state='archived')

    @router.post('/sessions/{session_id}/restore')
    def restore_session(session_id: str,body: SessionStateChange):
        return runs.set_session_state(session_id,kb_id=body.kb_id,revision=body.expected_revision,request_key=body.request_key,state='active')

    @router.post('/knowledge-bases/{kb_id}/sessions',status_code=201)
    def create_session(kb_id: str,body: CreateSession):
        return runs.create_session(kb_id,request_key=body.request_key,title=body.title)

    @router.get('/sessions/{session_id}')
    def session(session_id: str,kb_id: str):
        row=runs.session(session_id)
        if row['kb_id']!=kb_id:raise CatalogError('SESSION_NOT_FOUND',status=404)
        return row

    @router.patch('/sessions/{session_id}')
    def rename_session(session_id: str,body: RenameSession):
        return runs.rename_session(session_id,body.title,body.expected_revision)

    @router.get('/sessions/{session_id}/messages')
    def messages(session_id: str,after_sequence: int | None=Query(None,ge=0),
                 before_sequence: int | None=Query(None,ge=1),limit: int=Query(40,ge=1,le=100),around_run_id: str | None=None):
        if after_sequence is not None and before_sequence is not None:raise CatalogError('INVALID_REQUEST',status=422)
        return runs.messages(session_id,after=after_sequence,before=before_sequence,limit=limit,around_run_id=around_run_id)

    @router.post('/sessions/{session_id}/messages',status_code=202)
    def submit_message(session_id: str,body: SubmitMessage):
        if ((body.task.type=='comparison' and len(body.task.required_document_ids)<2)
                or (body.task.type=='qa' and body.task.required_document_ids)):
            raise CatalogError('INVALID_SOURCE_SCOPE',status=422)
        return runs.submit(session_id,question=body.question,request_key=body.request_key,
            expected_session_revision=body.expected_session_revision,expected_parent_message_id=body.expected_parent_message_id,
            scope_mode=body.scope.mode,document_ids=body.scope.document_ids,
            required_document_ids=body.task.required_document_ids,answer_mode=body.answer_mode)

    @router.get('/sessions/{session_id}/submissions/{request_key}')
    def submission(session_id: str,request_key: str):return runs.by_request(session_id,request_key)

    @router.get('/runs/{run_id}')
    def run(run_id: str,kb_id: str | None=None):
        row=runs.get(run_id)
        if kb_id and row['kb_id']!=kb_id:raise CatalogError('RUN_NOT_FOUND',status=404)
        return row

    @router.post('/runs/{run_id}/cancel')
    def cancel_run(run_id: str):return runs.cancel(run_id)

    @router.get('/knowledge-bases/{kb_id}/notes')
    def notes(kb_id: str,offset: int=Query(0,ge=0),limit: int=Query(50,ge=1,le=100)):
        return studio.notes(kb_id,offset=offset,limit=limit)

    @router.post('/knowledge-bases/{kb_id}/notes',status_code=201)
    def create_note(kb_id: str,body: CreateNote):
        return studio.create_note(kb_id,request_key=body.request_key,title=body.title,body=body.body,
            origin_run_id=body.origin_run_id,copy_from_note_id=body.copy_from_note_id,client_revision=body.client_revision)

    @router.get('/notes/{note_id}')
    def note(note_id: str,kb_id: str):
        row=studio.get_note(note_id)
        if row['kb_id']!=kb_id:raise CatalogError('NOTE_NOT_FOUND',status=404)
        return row

    @router.patch('/notes/{note_id}')
    def update_note(note_id: str,body: UpdateNote):
        return studio.update_note(note_id,request_key=body.request_key,expected_revision=body.expected_revision,
            title=body.title,body=body.body,client_revision=body.client_revision)

    @router.get('/citations/{citation_id}')
    def citation(citation_id: str,kb_id: str,session_id: str | None=None,artifact_id: str | None=None):
        return studio.citation(citation_id,kb_id=kb_id,session_id=session_id,artifact_id=artifact_id)

    @router.get('/knowledge-bases/{kb_id}/artifacts')
    def artifacts(kb_id: str,offset: int=Query(0,ge=0),limit: int=Query(30,ge=1,le=100)):
        return studio.artifacts(kb_id,offset=offset,limit=limit)

    @router.get('/artifacts/{artifact_id}')
    def artifact(artifact_id: str,kb_id: str):return studio.artifact(artifact_id,kb_id)

    @router.get('/knowledge-bases/{kb_id}/overview')
    def overview(kb_id: str):return studio.overview(kb_id)

    @router.post('/knowledge-bases/{kb_id}/overview/resolve')
    def resolve_overview(kb_id: str,body: OverviewLookup):
        return studio.overview(kb_id,mode=body.scope.mode,document_ids=body.scope.document_ids)

    @router.post('/knowledge-bases/{kb_id}/overview',status_code=202)
    def create_overview(kb_id: str,body: RequestOverview):
        return studio.request_overview(kb_id,request_key=body.request_key,mode=body.scope.mode,
                                       document_ids=body.scope.document_ids,regenerate=body.regenerate)

    @router.get('/runs/{run_id}/events')
    async def run_events(run_id: str,request: Request,after_seq: int=Query(0,ge=0)):
        runs.get(run_id)
        from fastapi.responses import StreamingResponse
        import asyncio
        async def stream():
            sequence=after_seq;heartbeat=asyncio.get_running_loop().time()
            while True:
                if await request.is_disconnected():return
                try:data=await asyncio.to_thread(runs.events,run_id,sequence)
                except CatalogError:
                    yield 'event: unavailable\ndata: '+json.dumps({'run_id':run_id})+'\n\n';return
                for event in data['events']:
                    sequence=event['sequence']
                    yield 'id: '+str(sequence)+'\nevent: '+event['kind']+'\ndata: '+json.dumps(event['payload'],ensure_ascii=False)+'\n\n'
                if data['terminal'] and len(data['events'])<100:return
                now=asyncio.get_running_loop().time()
                if now-heartbeat>10:heartbeat=now;yield ': keep-alive\n\n'
                await asyncio.sleep(.5)
        return StreamingResponse(stream(),media_type='text/event-stream',headers={'X-Accel-Buffering':'no','Cache-Control':'no-store'})

    app.include_router(router)
