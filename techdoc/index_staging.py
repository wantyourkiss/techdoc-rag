"""Attempt-owned durable staging; catalog commits remain the authority."""
from hashlib import sha256
import json,os,re,tempfile
from pathlib import Path
from uuid import uuid4
from .catalog import CatalogError


def safe_path(root,relative):
    root=Path(root).resolve();rel=Path(relative)
    if rel.is_absolute() or '..' in rel.parts:raise CatalogError('INDEX_STAGE_INVALID')
    path=root
    for part in rel.parts:
        path=path/part
        if path.is_symlink():raise CatalogError('INDEX_STAGE_INVALID')
    if not path.resolve().is_relative_to(root):raise CatalogError('INDEX_STAGE_INVALID')
    return path


def sync_directory(path):
    if os.name=='nt':return
    fd=os.open(path,os.O_RDONLY|os.O_DIRECTORY)
    try:os.fsync(fd)
    finally:os.close(fd)


def atomic_file(path,raw):
    path.parent.mkdir(parents=True,exist_ok=True)
    fd,name=tempfile.mkstemp(prefix='.tmp_index_',dir=path.parent)
    try:
        with os.fdopen(fd,'wb') as stream:stream.write(raw);stream.flush();os.fsync(stream.fileno())
        os.chmod(name,0o600);os.replace(name,path);sync_directory(path.parent)
    finally:Path(name).unlink(missing_ok=True)


class IndexStage:
    def __init__(self,root,path,record):self.root,self.path,self.record=Path(root).resolve(),Path(path),record

    @classmethod
    def create(cls,catalog,job,source,metadata,raw,data,generation):
        record={'format':'index_stage_v1','job_id':job['id'],'attempt_id':'attempt_'+str(job['attempt'])+'_'+uuid4().hex,
            'lease_hash':sha256(job['lease_token'].encode()).hexdigest(),'import_id':source['id'],
            'snapshot_id':metadata['snapshot_id'],'extension':Path(metadata['filename']).suffix.lower(),
            'source_sha256':sha256(data).hexdigest(),'snapshot_sha256':sha256(raw).hexdigest(),
            'parser_identity_sha256':sha256(json.dumps(metadata['parser_identity'],sort_keys=True).encode()).hexdigest(),
            'base_generation':generation,'state':'prepared'}
        path=safe_path(catalog.data_dir,'index_staging/'+record['job_id']+'/'+record['attempt_id'])
        path.mkdir(parents=True,exist_ok=False);stage=cls(catalog.data_dir,path,record)
        try:stage.save();atomic_file(stage.original_blob,data);atomic_file(stage.snapshot_blob,raw)
        except BaseException:
            try:stage.cleanup(catalog)
            except OSError:pass
            raise
        return stage

    @property
    def original_blob(self):return self.path/('original'+self.record['extension'])
    @property
    def snapshot_blob(self):return self.path/'snapshot.json'
    @property
    def original(self):return safe_path(self.root,'originals/'+self.record['snapshot_id']+self.record['extension'])
    @property
    def snapshot(self):return safe_path(self.root,self.record['snapshot_id']+'.json')

    def save(self):atomic_file(self.path/'journal.json',json.dumps(self.record,sort_keys=True).encode())
    def state(self,value,**fields):self.record.update(state=value,**fields);self.save()

    def validate_blobs(self):
        for path,field in ((self.original_blob,'source_sha256'),(self.snapshot_blob,'snapshot_sha256')):
            checked=safe_path(self.root,path.relative_to(self.root))
            if not checked.is_file() or sha256(checked.read_bytes()).hexdigest()!=self.record[field]:
                raise CatalogError('INDEX_STAGE_CORRUPT')

    def install(self):
        self.validate_blobs();self.state('publishing')
        # Exclusive hard links both avoid overwrites and prove the attempt's inode ownership.
        for blob,target in ((self.original_blob,self.original),(self.snapshot_blob,self.snapshot)):
            os.link(blob,target);sync_directory(target.parent)

    def validate_installed(self):
        self.validate_blobs()
        for blob,target in ((self.original_blob,self.original),(self.snapshot_blob,self.snapshot)):
            if not target.is_file() or not target.samefile(blob):raise CatalogError('INDEX_STAGE_OWNERSHIP_CHANGED')

    def validate_namespace(self):
        safe_path(self.root,self.path.relative_to(self.root))
        for child in self.path.iterdir():
            if child.is_symlink() or not child.is_file() or child.name not in ('journal.json','snapshot.json','original'+self.record['extension']) and not child.name.startswith('.tmp_index_'):
                raise CatalogError('INDEX_STAGE_INVALID')

    def cleanup(self,catalog):
        self.validate_namespace()
        with catalog.reader() as conn:registered=conn.execute('SELECT 1 FROM snapshots WHERE snapshot_id=?',(self.record['snapshot_id'],)).fetchone()
        if not registered:
            for blob,target in ((self.original_blob,self.original),(self.snapshot_blob,self.snapshot)):
                if target.exists() and blob.exists() and target.samefile(blob):
                    target.unlink();sync_directory(target.parent)
        # Only this attempt's fixed filenames may be reclaimed; never recurse across namespaces.
        for child in list(self.path.iterdir()):child.unlink()
        self.path.rmdir();sync_directory(self.path.parent)
        try:self.path.parent.rmdir()
        except OSError:pass


def recover_stages(catalog):
    """Only under CatalogOwner, before loading snapshots or starting workers."""
    root=safe_path(catalog.data_dir,'index_staging')
    if not root.exists():return []
    stages=[];scratch=[]
    for job_dir in root.iterdir():
        if job_dir.is_symlink() or not job_dir.is_dir() or not re.fullmatch(r'job_[0-9a-f]{32}',job_dir.name):raise CatalogError('INDEX_STAGE_INVALID')
        for attempt_dir in job_dir.iterdir():
            if attempt_dir.is_symlink() or not attempt_dir.is_dir() or not re.fullmatch(r'attempt_[1-9][0-9]*_[0-9a-f]{32}',attempt_dir.name):raise CatalogError('INDEX_STAGE_INVALID')
            path=safe_path(catalog.data_dir,(attempt_dir/'journal.json').relative_to(catalog.data_dir))
            if not path.is_file():
                # Crash before the first journal replace, or after cleanup removed it.
                for child in attempt_dir.iterdir():
                    if child.is_symlink() or not child.is_file() or not (child.name.startswith('.tmp_index_') or child.name=='snapshot.json' or re.fullmatch(r'original\.(md|txt|pdf|docx|html|htm)',child.name)):
                        raise CatalogError('INDEX_STAGE_INVALID')
                scratch.append(attempt_dir);continue
            if path.stat().st_size>16384:raise CatalogError('INDEX_STAGE_INVALID')
            try:record=json.loads(path.read_bytes())
            except (ValueError,OSError):raise CatalogError('INDEX_STAGE_INVALID') from None
            if (record.get('format')!='index_stage_v1' or record.get('job_id')!=job_dir.name or record.get('attempt_id')!=attempt_dir.name
                or not re.fullmatch(r'import_[0-9a-f]{32}',record.get('import_id',''))
                or not re.fullmatch(r'[0-9a-f]{28}',record.get('snapshot_id',''))
                or record.get('extension') not in ('.md','.txt','.pdf','.docx','.html','.htm')
                or record.get('state') not in ('prepared','built','publishing','committed','aborted')
                or any(not re.fullmatch(r'[0-9a-f]{64}',record.get(k,'')) for k in ('lease_hash','source_sha256','snapshot_sha256','parser_identity_sha256'))):
                raise CatalogError('INDEX_STAGE_INVALID')
            with catalog.reader() as conn:
                source=conn.execute('SELECT source_sha256 FROM import_inputs WHERE id=?',(record['import_id'],)).fetchone()
                job=conn.execute('SELECT target_id FROM jobs WHERE id=?',(record['job_id'],)).fetchone()
            if not source or not job or job['target_id']!=record['import_id'] or source['source_sha256']!=record['source_sha256']:raise CatalogError('INDEX_STAGE_INVALID')
            stages.append(IndexStage(catalog.data_dir,attempt_dir,record))
    for stage in stages:stage.validate_namespace()
    # Validate every namespace before deleting anything, and protect roots owned by another attempt.
    with catalog.reader() as conn:registered={row[0] for row in conn.execute('SELECT snapshot_id FROM snapshots')}
    for stage in stages:
        if stage.record['snapshot_id'] in registered:continue
        for target,kind in ((stage.snapshot,'snapshot_blob'),(stage.original,'original_blob')):
            if target.exists() and not any(getattr(other,kind).exists() and target.samefile(getattr(other,kind)) for other in stages if other.record['snapshot_id']==stage.record['snapshot_id']):
                raise CatalogError('INDEX_STAGE_UNOWNED_FILE')
    result=[]
    for stage in stages:
        result.append({'job_id':stage.record['job_id'],'attempt_id':stage.record['attempt_id'],'registered':stage.record['snapshot_id'] in registered})
        stage.cleanup(catalog)
    for directory in scratch:
        for child in directory.iterdir():child.unlink()
        directory.rmdir()
    return result
