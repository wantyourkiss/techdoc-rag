"""Start an isolated BM25 workspace, import synthetic text, verify auth and reader.

Uses only runtime requirements and the standard library. No models or production
data. The subprocess is stopped before its owned temporary directory is removed.
"""
import base64,json,os,socket,subprocess,sys,tempfile,time,urllib.error,urllib.request,uuid
from pathlib import Path

def main():
    root=Path(__file__).resolve().parents[1]
    env={k:v for k,v in os.environ.items() if not k.startswith(('TECHDOC_','LLM_'))}
    env['PYTHONPATH']=str(root)+os.pathsep+env.get('PYTHONPATH','')
    user='smoke';password=uuid.uuid4().hex
    token=base64.b64encode((user+':'+password).encode()).decode()
    opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with socket.socket() as sock:sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
    def request(path,body=None,authenticated=True):
        headers={'Content-Type':'application/json'}
        if authenticated:headers['Authorization']='Basic '+token
        req=urllib.request.Request(f'http://127.0.0.1:{port}'+path,headers=headers,
              data=json.dumps(body).encode() if body is not None else None)
        with opener.open(req,timeout=3) as response:return json.load(response)
    with tempfile.TemporaryDirectory(prefix='techdoc-minimal-smoke-') as temporary:
        env.update(TECHDOC_DATA_DIR=temporary,TECHDOC_KNOWLEDGE_MODE='1',TECHDOC_DEPLOYMENT_MODE='public',
          TECHDOC_BROWSER_USER=user,TECHDOC_BROWSER_PASSWORD=password,TECHDOC_RETRIEVER='bm25',
          TECHDOC_INFERENCE_POLICY='isolated_v1',TECHDOC_INDEX_POLICY='staged_v1')
        with (Path(temporary)/'server.log').open('w',encoding='utf-8') as log:
            process=subprocess.Popen([sys.executable,'-m','uvicorn','techdoc.api:app','--host','127.0.0.1','--port',str(port),'--workers','1'],
                cwd=root,env=env,stdout=log,stderr=subprocess.STDOUT,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
            try:
                for _ in range(100):
                    try:request('/health');break
                    except (OSError,urllib.error.URLError):
                        if process.poll() is not None:raise RuntimeError('Isolated server failed to start')
                        time.sleep(.1)
                else:raise RuntimeError('Startup timed out')
                try:request('/api/v1/capabilities',authenticated=False)
                except urllib.error.HTTPError as error:assert error.code==401
                else:raise AssertionError('Anonymous access was permitted')
                assert request('/api/v1/capabilities')['knowledge_mode']
                kb=request('/api/v1/knowledge-bases',{'name':'Isolated smoke'})
                job=request('/api/v1/knowledge-bases/'+kb['id']+'/text-sources',{
                    'title':'Smoke source','text':'Connection timeout is 30 seconds.',
                    'expected_revision':kb['catalog_revision'],'request_key':uuid.uuid4().hex})
                for _ in range(100):
                    status=request('/api/v1/jobs/'+job['id'])
                    if status['state'] in ('succeeded','failed','cancelled','interrupted'):break
                    time.sleep(.1)
                assert status['state']=='succeeded',status.get('error_code')
                content=request('/api/v1/versions/'+status['result']['version_id']+'/content?kb_id='+kb['id'])
                assert any('30 seconds' in b['text'] for b in content['blocks'])
                print(json.dumps({'passed':True,'auth_required':True,'knowledge_mode':True,
                    'synthetic_import_and_reader':True,'actual_model_calls':0}))
            finally:
                process.terminate()
                try:process.wait(timeout=10)
                except subprocess.TimeoutExpired:process.kill();process.wait(timeout=5)

if __name__=='__main__':main()
