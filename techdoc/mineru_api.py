"""Bounded MinerU cloud parsing with durable submission receipts, never URL logs."""
from hashlib import sha256
from html.parser import HTMLParser
from io import BytesIO
import inspect,json,math,os,re,socket,time,zipfile
from ipaddress import ip_address
from pathlib import Path,PurePosixPath
from urllib.parse import urlsplit
import httpx
from .catalog import CatalogError,encoded
from .core import MAX_BYTES,MAX_BLOCKS,MAX_CHARS,block,digest

API='https://mineru.net/api/v4'
OBJECT_HOSTS={'mineru.oss-cn-shanghai.aliyuncs.com','cdn-mineru.openxlab.org.cn'}
MAX_ZIP=64*1024*1024

def parser_mode():
 mode=os.getenv('TECHDOC_DOCUMENT_PARSER','local')
 if mode not in ('local','mineru_api'):raise RuntimeError('INVALID_DOCUMENT_PARSER')
 return mode

def _save(path,value):
 from .imports import atomic_bytes
 raw=encoded(value).encode()
 atomic_bytes(path,raw);os.chmod(path,0o600)

def _object_url(url):
 try:
  p=urlsplit(url)
  if p.scheme!='https' or p.hostname not in OBJECT_HOSTS or p.port not in (None,443) or p.username or p.password or p.fragment:raise ValueError()
  addresses=socket.getaddrinfo(p.hostname,443,type=socket.SOCK_STREAM)
  if not addresses or any(not ip_address(a[4][0]).is_global for a in addresses):raise ValueError()
 except (ValueError,OSError,TypeError):raise CatalogError('MINERU_RESULT_INVALID',status=502) from None
 return url

def _api(client,method,path,token,**kwargs):
 try:
  with client.stream(method,API+path,headers={'Authorization':'Bearer '+token},**kwargs) as response:
   if response.status_code in (401,403):raise CatalogError('MINERU_AUTH_FAILED',status=502)
   if response.status_code==429:raise CatalogError('MINERU_QUOTA_EXCEEDED',status=503)
   if response.status_code!=200:raise CatalogError('MINERU_NETWORK_FAILED',status=503)
   raw=bytearray()
   for part in response.iter_bytes():
    raw.extend(part)
    if len(raw)>1024*1024:raise CatalogError('MINERU_RESULT_INVALID',status=502)
  value=json.loads(raw)
  if not isinstance(value,dict):raise ValueError()
  code=value.get('code')
  if code in ('A0202','A0211'):raise CatalogError('MINERU_AUTH_FAILED',status=502)
  if code in (-60018,-60019,-60009):raise CatalogError('MINERU_QUOTA_EXCEEDED',status=503)
  if code!=0:raise CatalogError('MINERU_PARSE_FAILED',status=502)
  if not isinstance(value.get('data'),dict):raise ValueError()
  return value['data']
 except CatalogError:raise
 except httpx.HTTPError:raise CatalogError('MINERU_NETWORK_FAILED',status=503) from None
 except (ValueError,TypeError):raise CatalogError('MINERU_RESULT_INVALID',status=502) from None

def validate_input(name,data):
 if not data or len(data)>MAX_BYTES:raise CatalogError('EMPTY_OR_OVERSIZED_UPLOAD',status=422)
 ext=Path(name).suffix.lower()
 if ext=='.pdf':
  import pymupdf
  try:
   with pymupdf.open(stream=data,filetype='pdf') as doc:
    if doc.needs_pass:raise CatalogError('ENCRYPTED_PDF_UNSUPPORTED',status=422)
    if doc.page_count>200:raise CatalogError('PDF_TOO_MANY_PAGES',status=422)
    return {'total_pages':doc.page_count}
  except CatalogError:raise
  except Exception:raise CatalogError('DOCUMENT_IMPORT_FAILED',status=422) from None
 if ext=='.docx':
  try:
   with zipfile.ZipFile(BytesIO(data)) as z:
    files=z.infolist()
    if len(files)>2000 or sum(i.file_size for i in files)>32*1024*1024:raise CatalogError('DOCX_EXPANSION_LIMIT',status=422)
    if 'word/document.xml' not in z.namelist():raise ValueError()
  except CatalogError:raise
  except (ValueError,zipfile.BadZipFile):raise CatalogError('INVALID_DOCX_PACKAGE',status=422) from None
  return {}
 raise CatalogError('INVALID_UPLOAD_FILENAME',status=422)

def fetch_result(name,data,directory,check=lambda:None,progress=lambda stage:None,*,client=None):
 """Resume a known batch. An ambiguous creation is never silently resubmitted."""
 validate_input(name,data)
 token=os.getenv('TECHDOC_MINERU_API_TOKEN','');model=os.getenv('TECHDOC_MINERU_MODEL','vlm')
 if not token.strip() or model not in ('vlm','pipeline'):raise CatalogError('MINERU_NOT_CONFIGURED',status=503)
 directory=Path(directory);receipt=directory/'mineru-request.json';cache=directory/'mineru-result.zip'
 identity=digest([sha256(data).hexdigest(),Path(name).suffix.lower(),model])
 state=None
 if receipt.exists():
  try:state=json.loads(receipt.read_text())
  except (ValueError,OSError):raise CatalogError('MINERU_RESULT_INVALID',status=502) from None
  if state.get('identity')!=identity:raise CatalogError('MINERU_RESULT_INVALID',status=502)
  if state.get('phase')=='creating':raise CatalogError('MINERU_SUBMISSION_UNCERTAIN',status=409)
 if cache.exists() and state and state.get('result_sha256'):
  raw=cache.read_bytes()
  if len(raw)>MAX_ZIP or sha256(raw).hexdigest()!=state['result_sha256']:raise CatalogError('MINERU_RESULT_INVALID',status=502)
  check();return raw,model
 owns=client is None
 client=client or httpx.Client(timeout=30,trust_env=False,follow_redirects=False)
 try:
  check()
  if state is None:
   state={'identity':identity,'phase':'creating'};_save(receipt,state)
   try:
    value=_api(client,'POST','/file-urls/batch',token,json={'files':[{'name':'source-'+identity[:12]+Path(name).suffix.lower(),'data_id':identity}],
        'model_version':model,'enable_table':True,'enable_formula':True})
   except CatalogError as error:
    if error.code in ('MINERU_AUTH_FAILED','MINERU_QUOTA_EXCEEDED','MINERU_PARSE_FAILED'):
     # Definite rejection is safe for an explicit future retry.
     receipt.unlink(missing_ok=True);raise
    raise CatalogError('MINERU_SUBMISSION_UNCERTAIN',status=409) from None
   batch=value.get('batch_id');urls=value.get('file_urls')
   if not isinstance(batch,str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}',batch) or not isinstance(urls,list) or len(urls)!=1 or not isinstance(urls[0],str):
    raise CatalogError('MINERU_SUBMISSION_UNCERTAIN',status=409)
   state.update(phase='upload_pending',batch_id=batch,upload_url=urls[0]);_save(receipt,state)
  batch=state.get('batch_id')
  if not isinstance(batch,str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}',batch):raise CatalogError('MINERU_RESULT_INVALID',status=502)
  if state['phase']=='upload_pending':
   check();progress('cloud_upload')
   try:
    response=client.put(_object_url(state['upload_url']),content=data)
    if response.status_code in (401,403):raise CatalogError('MINERU_UPLOAD_EXPIRED',status=502)
    response.raise_for_status()
   except httpx.HTTPError:raise CatalogError('MINERU_NETWORK_FAILED',status=503) from None
   state.update(phase='uploaded');state.pop('upload_url',None);_save(receipt,state)
  try:timeout=float(os.getenv('TECHDOC_MINERU_TIMEOUT_S','600'))
  except ValueError:raise CatalogError('MINERU_NOT_CONFIGURED',status=503) from None
  if not math.isfinite(timeout) or not 30<=timeout<=900:raise CatalogError('MINERU_NOT_CONFIGURED',status=503)
  end=time.monotonic()+timeout;progress('cloud_parsing')
  while True:
   check();value=_api(client,'GET','/extract-results/batch/'+batch,token)
   rows=value.get('extract_result')
   if not isinstance(rows,list) or len(rows)>1:raise CatalogError('MINERU_RESULT_INVALID',status=502)
   row=rows[0] if rows else {}
   if not isinstance(row,dict):raise CatalogError('MINERU_RESULT_INVALID',status=502)
   if row.get('state')=='failed':raise CatalogError('MINERU_PARSE_FAILED',status=502)
   if row.get('state')=='done':break
   if time.monotonic()>=end:raise CatalogError('MINERU_TIMEOUT',status=504)
   for _ in range(6):check();time.sleep(.5)
  progress('cloud_download');raw=bytearray()
  try:
   with client.stream('GET',_object_url(row.get('full_zip_url'))) as response:
    response.raise_for_status()
    for part in response.iter_bytes(chunk_size=65536):
     check();raw.extend(part)
     if len(raw)>MAX_ZIP:raise CatalogError('MINERU_RESULT_INVALID',status=502)
  except httpx.HTTPError:raise CatalogError('MINERU_NETWORK_FAILED',status=503) from None
  from .imports import atomic_bytes
  atomic_bytes(cache,raw);os.chmod(cache,0o600)
  state.update(result_sha256=sha256(raw).hexdigest(),phase='done');_save(receipt,state)
  return bytes(raw),model
 finally:
  if owns:client.close()

class _Table(HTMLParser):
 _BLOCKS={'p','div','li','ul','ol','dl','dt','dd','section','article','blockquote','pre','h1','h2','h3','h4','h5','h6'}
 def __init__(self):super().__init__(convert_charrefs=True);self.rows=[];self.cell=None;self.depth=0
 def handle_starttag(self,tag,attrs):
  if tag in ('script','style','iframe','object'):raise ValueError()
  if tag=='table':
   self.depth+=1
   if self.depth>1:raise ValueError()
  if tag=='tr':self.rows.append([])
  if tag in ('td','th'):
   if self.cell is not None or not self.rows:raise ValueError()
   attrs=dict(attrs);rowspan=int(attrs.get('rowspan','1'));colspan=int(attrs.get('colspan','1'))
   if not 1<=rowspan<=200 or not 1<=colspan<=100:raise ValueError()
   self.cell={'text':'','rowspan':rowspan,'colspan':colspan,'header':tag=='th'}
  if (tag=='br' or tag in self._BLOCKS) and self.cell is not None:self.cell['text']+='\n'
 def handle_endtag(self,tag):
  if tag in ('td','th'):
   if self.cell is None:raise ValueError()
   paragraphs=[' '.join(line.split()) for line in self.cell['text'].splitlines() if line.strip()]
   self.cell['paragraphs']=paragraphs
   self.cell['text']=' ; '.join(paragraphs);self.rows[-1].append(self.cell);self.cell=None
  if tag in self._BLOCKS and self.cell is not None:self.cell['text']+='\n'
  if tag=='table':self.depth-=1
 def handle_data(self,text):
  if self.cell is not None:self.cell['text']+=text
 def result(self):
  if self.cell is not None or self.depth or not self.rows or len(self.rows)>200 or sum(map(len,self.rows))>1000:raise ValueError()
  cells=[];used=set()
  for r,row in enumerate(self.rows):
   c=0
   for cell in row:
    while (r,c) in used:c+=1
    for y in range(r,r+cell['rowspan']):
     for x in range(c,c+cell['colspan']):
      if (y,x) in used or x>=100 or y>=200:raise ValueError()
      used.add((y,x))
    cells.append(dict(cell,row=r,column=c));c+=cell['colspan']
  return '\n'.join(' | '.join(c['text'] for c in row) for row in self.rows),cells

def convert_result(name,data,raw,model='vlm',version='',origin='file'):
 """Map content-list positions to immutable machine-extracted evidence blocks."""
 limits=validate_input(name,data)
 try:
  with zipfile.ZipFile(BytesIO(raw)) as z:
   info=z.infolist();names=[i.filename for i in info]
   if len(info)>4000 or len(set(names))!=len(names) or sum(i.file_size for i in info)>128*1024*1024:raise ValueError()
   if any(PurePosixPath(n).is_absolute() or '..' in PurePosixPath(n).parts or '\\' in n or ':' in n for n in names):raise ValueError()
   if any(i.flag_bits&1 or (i.external_attr>>16)&0o170000==0o120000 for i in info):raise ValueError()
   content=[n for n in names if n.endswith('_content_list.json')]
   if len(content)!=1 or 'layout.json' not in names:raise ValueError()
   if z.getinfo(content[0]).file_size>16*1024*1024 or z.getinfo('layout.json').file_size>32*1024*1024:raise ValueError()
   items=json.loads(z.read(content[0]));layout=json.loads(z.read('layout.json'))
  if not isinstance(items,list) or len(items)>MAX_BLOCKS or not isinstance(layout,dict):raise ValueError()
  pages=layout.get('pdf_info')
  if not isinstance(pages,list) or not 1<=len(pages)<=200:raise ValueError()
  page_ids=[p['page_idx'] for p in pages]
  if page_ids!=list(range(len(pages))) or limits.get('total_pages',len(pages))!=len(pages):raise ValueError()
  identity={'profile':'mineru_api_v1','model_requested':model,'cloud_weights_pinned':False,
   'result_sha256':digest([items,pages]),'adapter_sha256':sha256(inspect.getsource(inspect.getmodule(convert_result)).encode()).hexdigest()}
  source_sha=sha256(data).hexdigest();basename=Path(name).name
  office=Path(name).suffix.lower()=='.docx' and layout.get('_backend')=='office'
  sid=digest(['snapshot_mineru_v1',basename,version,source_sha,identity,origin])[:28]
  blocks=[];warnings={'MINERU_MACHINE_EXTRACTED'};locations=[];text_pages=set()
  def strings(value):
   if not isinstance(value,list) or any(not isinstance(v,str) for v in value) or sum(map(len,value))>MAX_CHARS:raise ValueError()
   return value
  def add(kind,text,locator,indexed=True,**extra):
   if not isinstance(text,str) or len(text)>MAX_CHARS:raise ValueError()
   text=text.strip()
   if not text:return
   blocks.append(dict(block(sid,len(blocks),kind,text,basename+' · '+str(locator.get('page',locator.get('converted_page',''))),locator,indexable=indexed,verified=False),**extra))
   if indexed:text_pages.add(locator.get('page',locator.get('converted_page',locator.get('section_index'))))
  for position,item in enumerate(items):
   if not isinstance(item,dict):raise ValueError()
   pi=item.get('page_idx');bbox=item.get('bbox')
   if type(pi) is not int or pi not in page_ids:raise ValueError()
   if office:
    loc={'document_block':position+1,'section_index':pi+1,'locator_basis':'office_structure','extraction':'mineru_api','source_kind':'machine_extracted'}
   else:
    if not isinstance(bbox,list) or len(bbox)!=4 or any(type(v) not in (float,int) or not math.isfinite(v) or not 0<=v<=1000 for v in bbox) or bbox[0]>bbox[2] or bbox[1]>bbox[3]:raise ValueError()
    loc={'page' if Path(name).suffix.lower()=='.pdf' else 'converted_page':pi+1,'bbox':bbox,'coordinate_system':'normalized_1000','extraction':'mineru_api','source_kind':'machine_extracted'}
   kind=item.get('type')
   if not isinstance(kind,str):raise ValueError()
   if kind=='text':add('heading' if item.get('text_level') else 'paragraph',item['text'],loc)
   elif kind=='equation':add('formula',item['text'],loc)
   elif kind=='list':
    values=strings(item['list_items'])
    add('list','\n'.join(values),loc)
   elif kind=='table':
    body=item.get('table_body','')
    if not isinstance(body,str):raise ValueError()
    caption=strings(item.get('table_caption',[]));foot=strings(item.get('table_footnote',[]))
    try:
     table=_Table();table.feed(body);table.close();text,cells=table.result()
     add('table','\n'.join(caption+[text]+foot),loc,table_structure=cells)
    except (ValueError,TypeError):
     add('table_unverified','[Table structure unavailable]',loc,False);warnings.add('MINERU_UNSUPPORTED_CONTENT');locations.append(dict(loc,kind='table_unverified'))
   elif kind=='image':
    add('figure_unverified','[Image relations not interpreted]',loc,False);warnings.add('MINERU_IMAGE_CONTENT_EXCLUDED');locations.append(dict(loc,kind='figure_unverified'))
    captions=strings(item.get('image_caption',[]))+strings(item.get('image_footnote',[]))
    add('caption','\n'.join(captions),loc)
   else:
    add('unparsed','[Unsupported extracted structure]',loc,False);warnings.add('MINERU_UNSUPPORTED_CONTENT');locations.append(dict(loc,kind='unparsed'))
  for page in pages:
   for region in page.get('discarded_blocks',[]):
    warnings.add('MINERU_DISCARDED_REGIONS');locations.append({'section_index' if office else ('page' if Path(name).suffix.lower()=='.pdf' else 'converted_page'):page['page_idx']+1,'kind':'discarded_region'})
  if len(blocks)>MAX_BLOCKS or sum(len(b['text']) for b in blocks)>MAX_CHARS:raise ValueError()
  if not any(b['indexable'] for b in blocks):raise CatalogError('NO_INDEXABLE_CONTENT',status=422)
  empty=sorted(set(range(1,len(pages)+1))-text_pages)
  if empty:warnings.add('MINERU_EMPTY_PAGE')
  meta={'snapshot_id':sid,'filename':basename,'product_version':version,'source_sha256':source_sha,
   'parser':'mineru_api_v1','snapshot_scheme':'snapshot_mineru_v1','parser_identity':identity,'origin':origin,
   'block_count':len(blocks),'indexed_block_count':sum(b['indexable'] for b in blocks),'warnings':sorted(warnings),
   'parse_details':{'total_pages':None if office else len(pages),'section_count':len(pages) if office else None,'page_basis':'office_structure' if office else ('original_pdf' if Path(name).suffix.lower()=='.pdf' else 'converted_document'),'empty_pages':[] if office else empty,'locations':locations[:200]}}
  return meta,blocks
 except CatalogError:raise
 except (ValueError,KeyError,TypeError,IndexError,AttributeError,zipfile.BadZipFile):raise CatalogError('MINERU_RESULT_INVALID',status=502) from None
