"""Prepare privately, rebase, then publish under store -> catalog lock order."""
from hashlib import sha256
import json,shutil
from .catalog import CatalogError
from .core import RetrieverBuildFailed
from .index_staging import IndexStage
from .index_views import freeze
from .inference import inference_slot,checked_lock


def close_fields(fields,current):
    dense=fields.get('dense') if fields else None
    if dense is not None and (current is None or dense is not current.dense):
        close=getattr(dense,'close',None)
        if close:close()


def publish(service,job,source,meta,blocks,data):
    from .imports import MAX_SNAPSHOT_BYTES
    store=service.store;catalog=service.catalog;sid=meta['snapshot_id']
    raw=json.dumps({'metadata':meta,'blocks':blocks},ensure_ascii=False).encode()
    if len(raw)>MAX_SNAPSHOT_BYTES:raise CatalogError('PARSED_SNAPSHOT_BUDGET_EXCEEDED',status=422)
    def check():
        if service.stop_event.is_set():raise CatalogError('IMPORT_CANCELLED')
        with catalog.reader() as conn:service._check_job(conn,job)
    service.queue.renew(job['id'],job['lease_token'],stage='indexing')
    stage=None;lease=None;fields=None;published=False;committed=False
    with inference_slot(check):
        try:
            for iteration in range(3):
                with checked_lock(store.lock,check):
                    with catalog.reader() as conn:known=conn.execute('SELECT * FROM snapshots WHERE snapshot_id=?',(sid,)).fetchone()
                    if known:
                        # Shared snapshots retain their existing owner and never enter cleanup.
                        return service._publish_legacy(job,source,meta,blocks,data)
                    if sid in store.docs:raise CatalogError('SNAPSHOT_CATALOG_INDEX_MISMATCH')
                    store._index_views.check_capacity();lease=store._index_views.pin();base=lease.view
                if stage is None:
                    required=len(raw)+len(data)
                    if service._storage_size()+2*required+16384>service.storage_limit or shutil.disk_usage(catalog.data_dir).free<required+32*1024*1024:
                        raise CatalogError('WORKSPACE_STORAGE_LIMIT',status=507)
                    stage=IndexStage.create(catalog,job,source,meta,raw,data,base.generation_id)
                check()
                # No store or SQLite write lock while tokenizing, encoding or building trees.
                try:fields=store.prepare_addition(meta,blocks,base)
                except Exception as exc:raise RetrieverBuildFailed() from exc
                stage.state('built',base_generation=base.generation_id);check()
                with checked_lock(store.lock,check):
                    if store._index_views.current.generation_id!=base.generation_id:
                        close_fields(fields,store._index_views.current);fields=None;lease.close();lease=None
                        continue
                    stage.validate_blobs()
                    with catalog.transaction() as conn:
                        service._check_job(conn,job)
                        conn.execute('UPDATE import_inputs SET published_snapshot_id=? WHERE id=?',(sid,source['id']))
                    # Durable SQL intent plus journal exists before any root link can appear.
                    stage.install()
                    if service._storage_size()>service.storage_limit:raise CatalogError('WORKSPACE_STORAGE_LIMIT',status=507)
                    service.queue.renew(job['id'],job['lease_token'],stage='publishing')
                    fields['_snapshot_epochs']=freeze({**store._snapshot_epochs,sid:store._next_snapshot_epoch+1})
                    try:
                        with catalog.transaction() as conn:
                            service._bind_import(conn,job,source,meta,stage.snapshot,stage.original,raw,None)
                            stage.validate_installed()
                            store._publish_fields(fields,replace_data=True,prune_cache=False);published=True
                        committed=True
                    except BaseException:
                        # Resolve an ambiguous commit before changing any pointer or owned file.
                        try:
                            with catalog.reader() as conn:
                                row=conn.execute('SELECT snapshot_sha256 FROM snapshots WHERE snapshot_id=?',(sid,)).fetchone()
                                current=conn.execute('SELECT state,lease_token FROM jobs WHERE id=?',(job['id'],)).fetchone()
                        except BaseException:
                            store._recovery_required=True
                            raise CatalogError('INDEX_RECOVERY_REQUIRED',status=503) from None
                        committed=bool(row and row['snapshot_sha256']==sha256(raw).hexdigest() and current['state']=='succeeded' and current['lease_token']==job['lease_token'])
                        if not committed:
                            if store._index_views.current is not base:store._restore_view(base);store._adopt_source_data(base)
                            published=False
                            raise
                        if not published:raise CatalogError('INDEX_RECOVERY_REQUIRED',status=503)
                    break
            else:raise CatalogError('INDEX_REBASE_LIMIT')
        finally:
            # Failed candidates never own shared catalog sources, even after lease loss/retry.
            try:
                if fields and not published:close_fields(fields,store._index_views.current)
            finally:
                try:
                    if lease:lease.close()
                finally:
                    if stage:
                        try:stage.cleanup(catalog)
                        except OSError:
                            # Durable journal remains for startup recovery; committed data is untouched.
                            pass
