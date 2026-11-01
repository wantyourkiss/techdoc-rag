import io,json,socket,zipfile
from pathlib import Path
from types import SimpleNamespace
import httpx,pymupdf,pytest
from fastapi.testclient import TestClient
from techdoc import mineru_api as m
from techdoc.catalog import CatalogError
from techdoc.parse_quality import parsing_report

def pdf():
 d=pymupdf.open();p=d.new_page();p.insert_text((50,80),'Connection timeout is at most 30 seconds.');return d.tobytes()

def result(items=None,**extra):
 if items is None:items=[{'type':'text','text':'Connection timeout is at most 30 seconds.','page_idx':0,'bbox':[10,20,90,50]}]
 b=io.BytesIO()
 with zipfile.ZipFile(b,'w') as z:
  z.writestr('source_content_list.json',json.dumps(items));z.writestr('layout.json',json.dumps({'pdf_info':[{'page_idx':0,'discarded_blocks':[]}]}))
  for n,v in extra.items():z.writestr(n,v)
 return b.getvalue()

@pytest.fixture
def cloud(monkeypatch):
 monkeypatch.setenv('TECHDOC_MINERU_API_TOKEN','test-only-token')
 monkeypatch.setattr(socket,'getaddrinfo',lambda *a,**k:[(socket.AF_INET,socket.SOCK_STREAM,6,'',('8.8.8.8',443))])

def transport(zip_data,calls,*,put_error=False,bad_api=False):
 def handle(r):
  calls.append(r)
  if r.url.host=='mineru.net':
   assert r.headers['authorization']=='Bearer test-only-token'
   if r.method=='POST':
    if bad_api:return httpx.Response(200,json={'code':'A0202','msg':'private upstream error'})
    return httpx.Response(200,json={'code':0,'data':{'batch_id':'batch-1','file_urls':['https://mineru.oss-cn-shanghai.aliyuncs.com/file?signature=private']}})
   return httpx.Response(200,json={'code':0,'data':{'extract_result':[{'state':'done','full_zip_url':'https://cdn-mineru.openxlab.org.cn/result.zip'}]}})
  assert 'authorization' not in r.headers
  if r.method=='PUT':
   if put_error:raise httpx.ReadTimeout('https://private/url?signature=secret',request=r)
   return httpx.Response(200)
  return httpx.Response(200,content=zip_data)
 return httpx.MockTransport(handle)

def test_cloud_upload_poll_download_and_cached_reuse(cloud,tmp_path):
 calls=[];raw=pdf();z=result()
 with httpx.Client(transport=transport(z,calls)) as c:
  downloaded,model=m.fetch_result('confidential-title.pdf',raw,tmp_path,client=c)
  assert downloaded==z and model=='vlm'
  assert m.fetch_result('confidential-title.pdf',raw,tmp_path,client=c)==(z,'vlm')
 assert len(calls)==4 and len([r for r in calls if r.method=='POST'])==1
 assert 'confidential-title' not in calls[0].content.decode()
 meta,blocks=m.convert_result('guide.pdf',raw,z)
 assert blocks[0]['locator']['page']==1 and blocks[0]['locator']['coordinate_system']=='normalized_1000'
 assert blocks[0]['verified'] is False and meta['parser_identity']['cloud_weights_pinned'] is False
 report=parsing_report(meta);assert report['machine_extracted'] and not report['coverage_verified']

def test_lost_creation_response_never_automatically_resubmits(cloud,tmp_path):
 calls=[];raw=pdf()
 def handle(r):calls.append(r);raise httpx.ReadTimeout('private upstream')
 with httpx.Client(transport=httpx.MockTransport(handle)) as c:
  for _ in range(2):
   with pytest.raises(CatalogError,match='MINERU_SUBMISSION_UNCERTAIN'):m.fetch_result('x.pdf',raw,tmp_path,client=c)
 assert len(calls)==1

def test_definite_auth_rejection_has_safe_code_and_no_stale_intent(cloud,tmp_path):
 calls=[]
 with httpx.Client(transport=transport(result(),calls,bad_api=True)) as c:
  with pytest.raises(CatalogError,match='MINERU_AUTH_FAILED'):m.fetch_result('x.pdf',pdf(),tmp_path,client=c)
 assert not (tmp_path/'mineru-request.json').exists()

def test_lost_upload_response_resumes_same_batch(cloud,tmp_path):
 calls=[];raw=pdf()
 with httpx.Client(transport=transport(result(),calls,put_error=True)) as c:
  with pytest.raises(CatalogError,match='MINERU_NETWORK_FAILED'):m.fetch_result('x.pdf',raw,tmp_path,client=c)
 with httpx.Client(transport=transport(result(),calls)) as c:m.fetch_result('x.pdf',raw,tmp_path,client=c)
 assert len([r for r in calls if r.method=='POST'])==1

@pytest.mark.parametrize('url',['http://cdn-mineru.openxlab.org.cn/x','https://127.0.0.1/x','https://example.com/x','https://user@cdn-mineru.openxlab.org.cn/x','https://cdn-mineru.openxlab.org.cn:8443/x'])
def test_untrusted_result_locations_rejected(cloud,url):
 with pytest.raises(CatalogError,match='MINERU_RESULT_INVALID'):m._object_url(url)

def test_private_dns_is_rejected(cloud,monkeypatch):
 monkeypatch.setattr(socket,'getaddrinfo',lambda *a,**k:[(2,1,6,'',('127.0.0.1',443))])
 with pytest.raises(CatalogError,match='MINERU_RESULT_INVALID'):m._object_url('https://cdn-mineru.openxlab.org.cn/x')

def test_zip_traversal_and_missing_pages_never_become_evidence():
 with pytest.raises(CatalogError,match='MINERU_RESULT_INVALID'):m.convert_result('x.pdf',pdf(),result(**{'../escape':'bad'}))
 items=[{'type':'text','text':'secret','page_idx':2,'bbox':[1,2,3,4]}]
 with pytest.raises(CatalogError,match='MINERU_RESULT_INVALID'):m.convert_result('x.pdf',pdf(),result(items))

def test_table_cells_and_spans_survive_without_rendering_provider_html():
 items=[{'type':'table','table_body':'<table><tr><th colspan="2">Limits</th></tr><tr><td>Retry</td><td>3 attempts</td></tr></table>','page_idx':0,'bbox':[1,2,30,40]}]
 meta,blocks=m.convert_result('x.pdf',pdf(),result(items))
 assert blocks[0]['text']=='Limits\nRetry | 3 attempts'
 assert blocks[0]['table_structure'][0]['colspan']==2
 assert blocks[0]['table_structure'][2]['column']==1
 assert '<table>' not in blocks[0]['text']

def test_visual_and_unconvertible_content_disclosed_not_indexed():
 items=[{'type':'text','text':'Visible body','page_idx':0,'bbox':[1,2,3,4]},
        {'type':'image','image_caption':['Figure A'],'image_footnote':[],'page_idx':0,'bbox':[1,2,3,4]},
        {'type':'table','table_body':'<script>secret</script>','page_idx':0,'bbox':[1,2,3,4]}]
 meta,blocks=m.convert_result('x.pdf',pdf(),result(items))
 report=parsing_report(meta)
 assert report['status']=='partial' and len(report['locations'])==2
 assert all('secret' not in b['text'] for b in blocks)
 assert {b['kind'] for b in blocks if not b['indexable']}=={'figure_unverified','table_unverified'}

def test_remote_result_content_changes_snapshot_identity():
 raw=pdf();a,_=m.convert_result('x.pdf',raw,result())
 b,_=m.convert_result('x.pdf',raw,result([{'type':'text','text':'Different text','page_idx':0,'bbox':[1,2,3,4]}]))
 assert a['snapshot_id']!=b['snapshot_id'] and a['source_sha256']==b['source_sha256']

def test_native_office_structure_does_not_fabricate_pdf_page_or_bbox():
 from docx import Document
 word=Document();word.add_paragraph('Timeout');raw=io.BytesIO();word.save(raw)
 b=io.BytesIO()
 with zipfile.ZipFile(b,'w') as z:
  z.writestr('source_content_list.json',json.dumps([{'type':'text','text':'Timeout is 30 seconds.','page_idx':0}]))
  z.writestr('layout.json',json.dumps({'_backend':'office','pdf_info':[{'page_idx':0,'discarded_blocks':[]}]}))
 meta,blocks=m.convert_result('source.docx',raw.getvalue(),b.getvalue())
 assert blocks[0]['locator']['document_block']==1
 assert 'page' not in blocks[0]['locator'] and 'bbox' not in blocks[0]['locator'] and 'converted_page' not in blocks[0]['locator']
 assert meta['parse_details']['total_pages'] is None and meta['parse_details']['page_basis']=='office_structure'

def test_cancel_before_network_does_not_submit(cloud,tmp_path):
 calls=[]
 def cancel():raise CatalogError('IMPORT_CANCELLED')
 with httpx.Client(transport=transport(result(),calls)) as c:
  with pytest.raises(CatalogError,match='IMPORT_CANCELLED'):m.fetch_result('x.pdf',pdf(),tmp_path,check=cancel,client=c)
 assert not calls and not (tmp_path/'mineru-request.json').exists()

def test_cloud_snapshot_publishes_with_report_and_immutable_original(tmp_path,monkeypatch):
 from techdoc.api import create_app
 monkeypatch.setenv('TECHDOC_RETRIEVER','bm25');monkeypatch.setenv('TECHDOC_DEPLOYMENT_MODE','local')
 monkeypatch.setenv('TECHDOC_DOCUMENT_PARSER','mineru_api');monkeypatch.setenv('TECHDOC_MINERU_API_TOKEN','test-only-token')
 monkeypatch.delenv('TECHDOC_RERANKER_MODEL',raising=False)
 monkeypatch.setattr(m,'fetch_result',lambda *a,**k:(result(),'vlm'))
 with TestClient(create_app(tmp_path/'data',knowledge_mode=True,import_worker=False),client=('127.0.0.1',50000)) as c:
  kb=c.post('/api/v1/knowledge-bases',json={'name':'Cloud sample'}).json();raw=pdf()
  r=c.post('/api/v1/knowledge-bases/'+kb['id']+'/documents',files={'file':('x.pdf',raw,'application/pdf')},data={'request_key':'cloud-1','expected_revision':kb['catalog_revision']})
  assert r.status_code==202;c.app.state.imports.process_next()
  job=c.get('/api/v1/jobs/'+r.json()['id']).json();assert job['state']=='succeeded'
  docs=c.get('/api/v1/knowledge-bases/'+kb['id']+'/documents').json()['items'];report=docs[0]['parsing_report']
  assert report['method']=='mineru_api' and report['machine_extracted'] and not report['coverage_verified']
  version=docs[0]['active_version_id'];content=c.get('/api/v1/versions/'+version+'/content',params={'kb_id':kb['id']}).json()
  assert content['parsing_report']==report and content['blocks'][0]['locator']['page']==1
  original=c.get('/api/v1/versions/'+version+'/source',params={'kb_id':kb['id']})
  assert original.content==raw

def test_local_scan_failure_is_actionable_and_partial_import_has_details(tmp_path,monkeypatch):
 from techdoc.api import create_app
 monkeypatch.setenv('TECHDOC_RETRIEVER','bm25');monkeypatch.setenv('TECHDOC_DEPLOYMENT_MODE','local');monkeypatch.setenv('TECHDOC_DOCUMENT_PARSER','local')
 monkeypatch.delenv('TECHDOC_RERANKER_MODEL',raising=False)
 with TestClient(create_app(tmp_path/'data',knowledge_mode=True,import_worker=False),client=('127.0.0.1',50000)) as c:
  kb=c.post('/api/v1/knowledge-bases',json={'name':'Local'}).json()
  d=pymupdf.open();p=d.new_page();pix=pymupdf.Pixmap(pymupdf.csRGB,(0,0,20,20),False);p.insert_image((50,50,100,100),pixmap=pix)
  r=c.post('/api/v1/knowledge-bases/'+kb['id']+'/documents',files={'file':('scan.pdf',d.tobytes(),'application/pdf')},data={'request_key':'scan','expected_revision':kb['catalog_revision']})
  c.app.state.imports.process_next();job=c.get('/api/v1/jobs/'+r.json()['id']).json()
  assert job['state']=='failed' and job['error_code']=='NO_INDEXABLE_CONTENT'
