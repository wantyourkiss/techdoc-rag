"""Single-user FastAPI integration with explicit authenticated/local deployment."""
from __future__ import annotations
from contextlib import asynccontextmanager
from pathlib import Path
import os
import base64
import binascii
import secrets
import json
import logging
import asyncio
import threading
from logging.handlers import RotatingFileHandler
from time import perf_counter, time

from fastapi import FastAPI, File, Form, HTTPException, UploadFile, Request as HttpRequest, Response
from fastapi.responses import HTMLResponse, FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from .core import DocumentStore, MAX_BYTES, RetrieverBuildFailed
from .query_plan import PLAN_VERSION


class QueryBody(BaseModel):
    question: str = Field(min_length=1,max_length=2000)
    snapshot_id: str | None = None
    snapshot_ids: list[str] | None = Field(default=None, min_length=1, max_length=4)
    mode: str = 'evidence_only'


class PlanInspectionBody(QueryBody):
    # A diagnostic A/B control only; ordinary answer requests always reuse.
    query_embedding_reuse: bool = True
    context_policy: str | None = None
    generation_policy: str | None = None


def create_app(data_dir: str | Path | None = None, *, knowledge_mode=None, import_worker=True,query_worker=None) -> FastAPI:
    from .auth_config import browser_access
    access=browser_access()  # Fail before acquiring the owner, touching files or loading models.
    root=Path(data_dir or os.getenv('TECHDOC_DATA_DIR','./runtime_data')).resolve()
    flag=os.getenv('TECHDOC_KNOWLEDGE_MODE','0')
    if flag not in ('0','1'):raise RuntimeError('INVALID_KNOWLEDGE_MODE')
    knowledge_mode=flag=='1' if knowledge_mode is None else knowledge_mode
    owner=catalog=imports=workspace=runs=studio=None
    query_worker=import_worker if query_worker is None else query_worker
    try:
        if knowledge_mode:
            from .catalog import Catalog
            from .catalog_owner import CatalogOwner
            from .imports import ImportService,reconcile_import_files
            from .workspace import WorkspaceService
            # Never silently migrate a populated legacy directory on startup.
            if not (root/'catalog/app.sqlite3').exists() and any(root.glob('*.json')):
                raise RuntimeError('OFFLINE_KNOWLEDGE_MIGRATION_REQUIRED')
            owner=CatalogOwner(root)
            catalog=Catalog(root)
            (root/'originals').mkdir(exist_ok=True)
            reconcile_import_files(catalog)
            from scripts.audit_catalog import audit
            if not audit(root)['ok']:raise RuntimeError('KNOWLEDGE_SOURCE_AUDIT_FAILED')
            workspace=WorkspaceService(catalog)
        store=DocumentStore(root)
        if knowledge_mode:
            budget=int(os.getenv('TECHDOC_WORKSPACE_MAX_BYTES',str(512*1024*1024)))
            if budget<32*1024*1024:raise RuntimeError('WORKSPACE_BUDGET_TOO_SMALL')
            imports=ImportService(catalog,store,storage_limit=budget)
            from .runs import RunService
            runs=RunService(catalog,store,workspace)
            from .studio import StudioService
            studio=StudioService(catalog,runs,workspace)
    except BaseException:
        if owner:owner.close()
        raise

    @asynccontextmanager
    async def lifespan(app):
        if imports:imports.queue.recover_after_restart()
        if runs:runs.recover()
        if imports and import_worker:imports.start()
        if runs and query_worker:runs.start()
        try:yield
        finally:
            if imports:imports.stop_event.set()
            if runs:runs.stop()
            if imports:imports.stop()
            # Keep ownership while a non-interruptible model kernel still holds the index.
            if owner and not ((imports.thread and imports.thread.is_alive()) or runs.alive()):
                store.close();owner.close()

    app=FastAPI(title='TechDoc RAG Final Local Demo', version='1.0.0',lifespan=lifespan)
    app.state.store=store
    app.state.knowledge_mode=knowledge_mode
    app.state.catalog=catalog
    app.state.workspace=workspace
    app.state.imports=imports
    app.state.runs=runs
    app.state.studio=studio
    if knowledge_mode:
        from .knowledge_api import install_knowledge_api
        install_knowledge_api(app,workspace,imports,runs,studio)
        from .upload_limit import KnowledgeUploadLimit
        app.add_middleware(KnowledgeUploadLimit)
    query_slots=threading.BoundedSemaphore(max(1,min(8,int(os.getenv('TECHDOC_QUERY_CAPACITY','2')))))
    from .diagnostics import QueryTrace,QueryCancelled
    query_logger=logging.getLogger('techdoc.query')
    query_logger.setLevel(logging.INFO)
    if os.getenv('TECHDOC_QUERY_LOG') and not query_logger.handlers:
        path=Path(os.environ['TECHDOC_QUERY_LOG']);path.parent.mkdir(parents=True,exist_ok=True)
        qhandler=RotatingFileHandler(path,maxBytes=5_000_000,backupCount=3,encoding='utf-8')
        qhandler.setFormatter(logging.Formatter('%(message)s'));query_logger.addHandler(qhandler)
        query_logger.propagate=False
    access_logger=logging.getLogger('techdoc.access.'+str(id(app)))
    access_logger.setLevel(logging.INFO)
    access_logger.propagate=False
    if os.getenv('TECHDOC_ACCESS_LOG'):
        log_path=Path(os.environ['TECHDOC_ACCESS_LOG'])
        log_path.parent.mkdir(parents=True,exist_ok=True)
        handler=RotatingFileHandler(log_path,maxBytes=5_000_000,backupCount=3,encoding='utf-8')
        handler.setFormatter(logging.Formatter('%(message)s'))
        access_logger.addHandler(handler)
    browser_user,browser_password=access.username,access.password

    @app.middleware('http')
    async def browser_auth(request, call_next):
        if not browser_password and not access.permits_anonymous(request):
            return JSONResponse({'code':'LOCAL_ACCESS_ONLY','message':'匿名模式仅允许本机直接访问。',
                                 'retryable':False},status_code=403,headers={'Cache-Control':'no-store'})
        if browser_password:
            valid = False
            scheme, _, token = request.headers.get('authorization', '').partition(' ')
            if scheme.lower() == 'basic':
                try:
                    decoded = base64.b64decode(token, validate=True).decode('utf-8')
                    username, _, password = decoded.partition(':')
                    valid = (secrets.compare_digest(username.encode(), browser_user.encode())
                             & secrets.compare_digest(password.encode(), browser_password.encode()))
                except (ValueError, UnicodeError, binascii.Error):
                    pass
            if not valid:
                payload=({'code':'LOGIN_REQUIRED','message':'请登录后继续。','retryable':False,
                          'request_id':secrets.token_hex(16)} if knowledge_mode and request.url.path.startswith('/api/v1')
                         else {'detail':'Login required'})
                return JSONResponse(payload, status_code=401,
                    headers={'WWW-Authenticate': 'Basic realm="TechDoc RAG", charset="UTF-8"',
                             'Cache-Control': 'no-store'})
        return await call_next(request)

    @app.middleware('http')
    async def local_security_headers(request, call_next):
        started=perf_counter()
        try:
            response=await call_next(request)
        except Exception:
            access_logger.info(json.dumps({'timestamp':time(),'method':request.method,
                'path':request.url.path,'status':500,'elapsed_ms':round((perf_counter()-started)*1000,3)}))
            raise
        access_logger.info(json.dumps({'timestamp':time(),'method':request.method,
            'path':request.url.path,'status':response.status_code,
            'elapsed_ms':round((perf_counter()-started)*1000,3)}))
        response.headers['Cache-Control']='no-store'
        response.headers['X-Content-Type-Options']='nosniff'
        response.headers['X-Frame-Options']='DENY'
        response.headers['Referrer-Policy']='no-referrer'
        return response

    @app.get('/health')
    def health():
        return {'status':'local_demo_ready' if store.retrieval_ready else 'index_unavailable',
                'retriever':{'bm25':'BM25_LOCAL_DEMO','dense':'DENSE_QDRANT_LOCAL','hybrid':'HYBRID_RRF_LOCAL'}[store.retriever_mode],
                'dense_runtime_enabled':store.retriever_mode in ('dense','hybrid'),
                'dense_quality_verified':False,'dense_available':store.retriever_mode in ('dense','hybrid'),
                'dense_model_id':store.dense.model_id if store.dense is not None else None,
                'dense_indexed_chunks':store.dense.indexed_chunks if store.dense is not None else 0,
                'embedding_cache':{'hits':getattr(store.dense,'cache_hits',0),'misses':getattr(store.dense,'cache_misses',0)},
                'reranker_enabled':store.reranker is not None,
                'reranker_model':store.reranker.model_id if store.reranker else None,
                'rerank_cache':(store.reranker.score_cache.stats() if getattr(store.reranker,'score_cache',None) else
                    store.reranker.cache_stats() if callable(getattr(store.reranker,'cache_stats',None)) else {'enabled':False}),
                'rerank_cpu_threads':getattr(store.reranker,'cpu_threads',None),
                'rerank_quantized_engine':getattr(store.reranker,'quantized_engine',None),
                'streaming_progress':True,
                'query_plan_policy':store.query_plan_policy,
                'query_plan_version':PLAN_VERSION,
                'context_policy':store.context_policy,'structure':store.structure.stats(),
                'generation_policy':store.generation_policy,
                'request_local_query_embedding_reuse':True,
                'generation_outside_index_lock':True,
                'cpu_dynamic_int8':os.getenv('TECHDOC_MODEL_INT8') == '1',
                'real_llm_verified':False,
                'query_modes':['evidence_only','llm','answer'],
                'generative_answer_enabled':True,
                'snapshot_count':len(store.docs),
                'document_formats':['md','txt','html','pdf','docx'],
                'single_user_no_auth':not bool(browser_password),'explicit_multi_doc_scope':True,'max_snapshots_per_query':4,'version':'1.0.0'}

    @app.get('/documents')
    def documents():
        with store.lock:
            docs=[dict(store.docs[k]) for k in sorted(store.docs)]
            return {'documents':docs,'count':len(docs)}

    @app.post('/documents',status_code=201)
    async def upload_document(file: UploadFile = File(...), product_version: str = Form('')):
        # Stream / limit before parsing; no storage of the original upload.
        try:
            data=await file.read(MAX_BYTES+1)
            if len(data)>MAX_BYTES:raise ValueError('EMPTY_OR_OVERSIZED_UPLOAD')
            return store.ingest(file.filename or '',data,product_version)
        except ValueError as e:
            raise HTTPException(status_code=422,detail=str(e)) from None
        except RetrieverBuildFailed:
            raise HTTPException(status_code=503,detail='RETRIEVER_INDEX_BUILD_FAILED') from None
        except Exception:
            # Parsing libraries sometimes throw format-specific exceptions.
            raise HTTPException(status_code=422,detail='DOCUMENT_PARSE_FAILED') from None
        finally:
            await file.close()

    @app.post('/documents/{snapshot_id}/revisions',status_code=201)
    async def upload_revision(snapshot_id: str, file: UploadFile = File(...), product_version: str = Form('')):
        try:
            data=await file.read(MAX_BYTES+1)
            if len(data)>MAX_BYTES:raise ValueError('EMPTY_OR_OVERSIZED_UPLOAD')
            if not product_version.strip():raise ValueError('REVISION_VERSION_REQUIRED')
            return store.ingest(file.filename or '',data,product_version,replaces_snapshot_id=snapshot_id)
        except ValueError as e:
            raise HTTPException(status_code=422,detail=str(e)) from None
        except RetrieverBuildFailed:
            raise HTTPException(status_code=503,detail='RETRIEVER_INDEX_BUILD_FAILED') from None
        except Exception:
            raise HTTPException(status_code=422,detail='DOCUMENT_PARSE_FAILED') from None
        finally:
            await file.close()

    @app.delete('/documents/{snapshot_id}',status_code=204)
    def delete_document(snapshot_id: str):
        try:
            deleted=store.delete(snapshot_id)
        except (OSError,RuntimeError):
            raise HTTPException(status_code=500,detail='DOCUMENT_DELETE_FAILED') from None
        if not deleted:
            raise HTTPException(status_code=404,detail='UNKNOWN_DOCUMENT')

    @app.get('/documents/{snapshot_id}/source')
    def download_source(snapshot_id: str):
        p=store.source_path(snapshot_id)
        if p is None:raise HTTPException(status_code=404,detail='ORIGINAL_SOURCE_NOT_AVAILABLE')
        # Attachment disposition: uploaded HTML/PDF never executes as this app's origin.
        return FileResponse(p,filename=store.docs[snapshot_id]['filename'],media_type='application/octet-stream')

    def validate_query(body):
        if body.mode not in ('evidence_only','llm','answer'):
            raise HTTPException(status_code=422,detail='MODE_MUST_BE_EVIDENCE_ONLY_OR_LLM_OR_ANSWER')
        if body.snapshot_id is not None and len(body.snapshot_id)>64:
            raise HTTPException(status_code=422,detail='BAD_SNAPSHOT_ID')

    @app.post('/query')
    def query(body: QueryBody, response: Response):
        validate_query(body)
        if not query_slots.acquire(blocking=False):
            raise HTTPException(status_code=429,detail='QUERY_CAPACITY_REACHED')
        trace=QueryTrace();response.headers['X-Request-ID']=trace.request_id
        try:
            return store.query(body.question,body.snapshot_id,body.mode,snapshot_ids=body.snapshot_ids,trace=trace)
        except (RuntimeError, ValueError):
            raise HTTPException(status_code=503,detail='RETRIEVER_QUERY_FAILED') from None
        finally:query_slots.release()

    @app.post('/query/stream')
    async def query_stream(body: QueryBody, request: HttpRequest):
        validate_query(body)
        if not query_slots.acquire(blocking=False):
            raise HTTPException(status_code=429,detail='QUERY_CAPACITY_REACHED')
        cancel=threading.Event();loop=asyncio.get_running_loop();events=asyncio.Queue(maxsize=128)
        def put_event(event):
            if not events.full():events.put_nowait(event)
        def progress(event):
            if not cancel.is_set():
                try:loop.call_soon_threadsafe(put_event,event)
                except RuntimeError:cancel.set()
        trace=QueryTrace(callback=progress,cancel_event=cancel)
        def work():
            try:return store.query(body.question,body.snapshot_id,body.mode,snapshot_ids=body.snapshot_ids,trace=trace)
            finally:query_slots.release()  # Cancellation does not stop an in-flight model kernel.
        def frame(kind,data):
            return 'event: '+kind+'\ndata: '+json.dumps(data,ensure_ascii=False)+'\n\n'
        task=asyncio.create_task(asyncio.to_thread(work))
        def consume(done):
            if not done.cancelled():done.exception()
        task.add_done_callback(consume)
        async def stream():
            last_heartbeat=loop.time()
            try:
                while not task.done() or not events.empty():
                    if await request.is_disconnected():cancel.set();return
                    try:
                        event=await asyncio.wait_for(events.get(),timeout=0.5)
                        yield frame('progress',event)
                    except asyncio.TimeoutError:
                        if not task.done() and loop.time()-last_heartbeat>=10:
                            last_heartbeat=loop.time();yield ': keep-alive\n\n'
                try:result=await task
                except QueryCancelled:
                    yield frame('error',{'code':'QUERY_CANCELLED','request_id':trace.request_id})
                except Exception:
                    trace.outcome='ERROR'
                    yield frame('error',{'code':'RETRIEVER_QUERY_FAILED','request_id':trace.request_id})
                else:yield frame('result',result)  # Never stream proposed/unreviewed claims.
            finally:
                cancel.set()
                # Capacity is released only when work actually exits.
        return StreamingResponse(stream(),media_type='text/event-stream',headers={
            'X-Accel-Buffering':'no','X-Request-ID':trace.request_id,'Cache-Control':'no-store'})

    @app.post('/diagnostics/query-plan')
    def inspect_query_plan(body: PlanInspectionBody, response: Response):
        """Authenticated retrieval probe; shares query capacity and scope checks.

        It runs the answer retrieval/context path but never calls the LLM or
        returns a natural-language answer. Used for paired shadow evaluation.
        """
        validate_query(body)
        if body.mode!='answer':raise HTTPException(status_code=422,detail='INSPECTION_REQUIRES_ANSWER_MODE')
        if body.context_policy not in (None,'legacy','structured'):raise HTTPException(status_code=422,detail='BAD_CONTEXT_POLICY')
        if body.generation_policy is not None:raise HTTPException(status_code=422,detail='INSPECTION_HAS_NO_GENERATION_POLICY')
        if not query_slots.acquire(blocking=False):
            raise HTTPException(status_code=429,detail='QUERY_CAPACITY_REACHED')
        trace=QueryTrace();response.headers['X-Request-ID']=trace.request_id
        try:
            return store.query(body.question,body.snapshot_id,'answer',snapshot_ids=body.snapshot_ids,
                               trace=trace,inspect_only=True,embedding_reuse=body.query_embedding_reuse,context_policy=body.context_policy)
        except (RuntimeError,ValueError):
            raise HTTPException(status_code=503,detail='RETRIEVER_QUERY_FAILED') from None
        finally:query_slots.release()

    @app.get('/assets/{asset_name}')
    def ui_asset(asset_name: str):
        assets={'query-stream.js':('query_stream.js','application/javascript'),
                'workspace.js':('workspace.js','application/javascript'),
                'workspace-chat.js':('workspace_chat.js','application/javascript'),
                'workspace-studio.js':('workspace_studio.js','application/javascript'),
                'workspace-overview.js':('workspace_overview.js','application/javascript'),
                'workspace-uploads.js':('workspace_uploads.js','application/javascript'),
                'workspace-layout.js':('workspace_layout.js','application/javascript'),
                'workspace-model.js':('workspace_model.js','application/javascript'),
                'workspace.css':('workspace.css','text/css'),
                'ui-model.js':('ui_model.js','application/javascript'),
                'app.js':('app.js','application/javascript'),
                'ui.css':('ui.css','text/css'),
                'learning-cases.json':('learning_cases.json','application/json')}
        if asset_name not in assets:raise HTTPException(status_code=404,detail='ASSET_NOT_FOUND')
        filename,media_type=assets[asset_name]
        return FileResponse(Path(__file__).parent/'static'/filename,media_type=media_type)

    @app.post('/diagnostics/answer')
    def evaluate_answer(body: PlanInspectionBody,response: Response):
        """Explicit evaluation route: full answer guards, alternate context policy.

        This is separate from the model-free retrieval inspection route. It
        allows a candidate to be evaluated before switching ordinary queries.
        """
        validate_query(body)
        if body.mode!='answer' or body.context_policy not in ('legacy','structured'):
            raise HTTPException(status_code=422,detail='EXPLICIT_ANSWER_CONTEXT_POLICY_REQUIRED')
        if body.generation_policy not in (None,'legacy','citation_v2','anchored_v3','anchored_v4'):
            raise HTTPException(status_code=422,detail='BAD_GENERATION_POLICY')
        if not query_slots.acquire(blocking=False):raise HTTPException(status_code=429,detail='QUERY_CAPACITY_REACHED')
        trace=QueryTrace();response.headers['X-Request-ID']=trace.request_id
        try:
            result=store.query(body.question,body.snapshot_id,'answer',snapshot_ids=body.snapshot_ids,
                               trace=trace,context_policy=body.context_policy,generation_policy=body.generation_policy)
            return {**result,'evaluation_only':True}
        except (RuntimeError,ValueError):raise HTTPException(status_code=503,detail='RETRIEVER_QUERY_FAILED') from None
        finally:query_slots.release()

    @app.get('/',response_class=HTMLResponse)
    def home():
        path=Path(__file__).parent/'static'/('workspace.html' if knowledge_mode else 'index.html')
        return HTMLResponse(path.read_text(encoding='utf-8'),headers={
            'Content-Security-Policy':"default-src 'none'; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; connect-src 'self'; img-src 'self'; base-uri 'none'; form-action 'self'"})

    return app


# Anonymous local CLI: TECHDOC_DEPLOYMENT_MODE=local uvicorn techdoc.api:app --host 127.0.0.1 --port 8765
app=create_app()
