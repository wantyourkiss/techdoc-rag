"""Durable bounded job metadata. Recovery never repeats an unconfirmed model call."""
from __future__ import annotations

import re
import time
from .catalog import CatalogError, new_id, utc_now
from .inference import enabled


class JobQueue:
    def __init__(self, catalog, *, queue_limit=10, workspace_id='ws_personal'):
        self.catalog, self.queue_limit, self.workspace_id = catalog, queue_limit, workspace_id

    def enqueue(self, kb_id, kind, target_id, request_key, input_sha256, *, conn=None):
        if (kind not in ('import','overview','artifact','query') or not 1 <= len(request_key) <= 128
                or not re.fullmatch('[0-9a-f]{64}',input_sha256)):
            raise CatalogError('INVALID_JOB_SUBMISSION', status=422)
        if conn is None:
            with self.catalog.transaction() as transaction:
                return self.enqueue(kb_id,kind,target_id,request_key,input_sha256,conn=transaction)
        self.catalog.require_kb(conn,kb_id,active=True,workspace_id=self.workspace_id)
        old = conn.execute('SELECT * FROM jobs WHERE kb_id=? AND kind=? AND request_key=?',
                           (kb_id,kind,request_key)).fetchone()
        if old:
            if old['input_sha256'] != input_sha256 or old['target_id'] != target_id:
                raise CatalogError('JOB_REQUEST_KEY_CONFLICT')
            return dict(old)
        table = 'document_versions' if kind == 'import' else ('runs' if kind=='query' else 'artifacts')
        target = conn.execute('SELECT 1 FROM '+table+' WHERE id=? AND kb_id=?', (target_id,kb_id)).fetchone()
        if not target and kind == 'import':
            target = conn.execute('SELECT 1 FROM import_inputs WHERE id=? AND kb_id=?',(target_id,kb_id)).fetchone()
        if not target:
            raise CatalogError('JOB_TARGET_NOT_FOUND', status=404)
        count = conn.execute("SELECT count(*) FROM jobs WHERE state IN ('queued','running','cancel_requested')").fetchone()[0]
        if count >= self.queue_limit:
            raise CatalogError('JOB_QUEUE_FULL', status=429)
        ident, now = new_id('job'), utc_now()
        conn.execute('''INSERT INTO jobs(id,kb_id,kind,target_id,request_key,input_sha256,state,created_at,updated_at)
                        VALUES(?,?,?,?,?,?,'queued',?,?)''',
                     (ident,kb_id,kind,target_id,request_key,input_sha256,now,now))
        return dict(conn.execute('SELECT * FROM jobs WHERE id=?', (ident,)).fetchone())

    def claim(self, *, lease_seconds=60, kind=None,parallel_query_limit=1):
        if not 1 <= lease_seconds <= 3600:
            raise ValueError('Invalid lease duration')
        with self.catalog.reader() as conn:
            if self._claimable(conn,kind,parallel_query_limit) is None:return None
        with self.catalog.transaction() as conn:
            job=self._claimable(conn,kind,parallel_query_limit)
            if job is None:return None
            token = new_id('lease')
            conn.execute("UPDATE jobs SET state='running',stage='starting',lease_token=?,lease_until=?,updated_at=? WHERE id=?",
                         (token,time.time()+lease_seconds,utc_now(),job['id']))
            return dict(conn.execute('SELECT * FROM jobs WHERE id=?', (job['id'],)).fetchone())

    def _claimable(self,conn,kind,parallel_query_limit):
            active=conn.execute("SELECT kind FROM jobs WHERE state IN ('running','cancel_requested')").fetchall()
            shared=enabled() and kind in ('import','query')
            if shared:
                limit=1 if kind=='import' else min(2,max(1,parallel_query_limit))
                if any(r['kind'] not in ('import','query') for r in active) or sum(r['kind']==kind for r in active)>=limit:return None
            if not shared and active and not (kind=='query' and all(r['kind']=='query' for r in active)
                               and len(active)<min(2,max(1,parallel_query_limit))):
                return None
            job = conn.execute('''SELECT j.* FROM jobs j JOIN knowledge_bases k ON k.id=j.kb_id
                WHERE j.state='queued' AND j.cancel_requested=0 AND k.state='active' AND k.workspace_id=?
                ORDER BY j.created_at,j.id LIMIT 1''', (self.workspace_id,)).fetchone()
            if not job or (kind is not None and job['kind']!=kind):
                return None
            return job

    def renew(self, job_id, lease_token, *, stage=None, lease_seconds=120):
        if not 1<=lease_seconds<=3600:
            raise ValueError('Invalid lease duration')
        with self.catalog.transaction() as conn:
            job = self._owned(conn,job_id)
            if (job['lease_token']!=lease_token or job['state'] not in ('running','cancel_requested')
                    or job['lease_until']<time.time()):
                raise CatalogError('JOB_LEASE_LOST')
            conn.execute('UPDATE jobs SET lease_until=?,stage=?,updated_at=? WHERE id=?',
                         (time.time()+lease_seconds,stage or job['stage'],utc_now(),job_id))
            return bool(job['cancel_requested'])

    def _owned(self, conn, job_id):
        row = conn.execute('''SELECT j.* FROM jobs j JOIN knowledge_bases k ON k.id=j.kb_id
                              WHERE j.id=? AND k.workspace_id=?''', (job_id,self.workspace_id)).fetchone()
        if row is None:
            raise CatalogError('JOB_NOT_FOUND',status=404)
        return row

    def cancel(self, job_id):
        with self.catalog.transaction() as conn:
            job = self._owned(conn,job_id)
            if job['state'] in ('queued','running'):
                state = 'cancelled' if job['state'] == 'queued' else 'cancel_requested'
                conn.execute('UPDATE jobs SET cancel_requested=1,state=?,updated_at=? WHERE id=?', (state,utc_now(),job_id))
            return dict(conn.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone())

    def finish(self, job_id, lease_token, *, error_code=None):
        with self.catalog.transaction() as conn:
            job = self._owned(conn,job_id)
            if job['lease_token'] != lease_token:
                raise CatalogError('JOB_LEASE_LOST')
            if job['state'] not in ('running','cancel_requested'):
                return dict(job)
            kb = conn.execute('SELECT state FROM knowledge_bases WHERE id=?', (job['kb_id'],)).fetchone()
            if job['cancel_requested'] or kb['state'] != 'active':
                state = 'cancelled'
            elif job['lease_until'] < time.time():
                raise CatalogError('JOB_LEASE_EXPIRED')
            else:
                state = 'failed' if error_code else 'succeeded'
            conn.execute('UPDATE jobs SET state=?,stage=?,error_code=?,updated_at=? WHERE id=?',
                         (state,state,error_code,utc_now(),job_id))
            return dict(conn.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone())

    def recover_expired(self):
        query="""SELECT j.* FROM jobs j JOIN knowledge_bases k ON k.id=j.kb_id
                WHERE j.state IN ('running','cancel_requested') AND j.lease_until<? AND k.workspace_id=?"""
        with self.catalog.reader() as conn:
            if conn.execute(query,(time.time(),self.workspace_id)).fetchone() is None:return 0
        with self.catalog.transaction() as conn:
            rows = conn.execute(query,(time.time(),self.workspace_id)).fetchall()
            for row in rows:
                state = 'cancelled' if row['cancel_requested'] else 'interrupted'
                conn.execute('UPDATE jobs SET state=?,stage=?,updated_at=? WHERE id=?', (state,state,utc_now(),row['id']))
            return len(rows)

    def recover_after_restart(self):
        """Only call before workers start while holding the exclusive catalog owner."""
        with self.catalog.transaction() as conn:
            conn.execute('''UPDATE jobs SET state=CASE WHEN cancel_requested=1 THEN 'cancelled'
                ELSE 'interrupted' END,stage=CASE WHEN cancel_requested=1 THEN 'cancelled'
                ELSE 'interrupted' END,updated_at=? WHERE state IN ('running','cancel_requested')
                AND kb_id IN (SELECT id FROM knowledge_bases WHERE workspace_id=?)''',
                (utc_now(),self.workspace_id))

    def retry(self, job_id):
        with self.catalog.transaction() as conn:
            job = self._owned(conn,job_id)
            self.catalog.require_kb(conn,job['kb_id'],active=True,workspace_id=self.workspace_id)
            if job['state'] not in ('failed','cancelled','interrupted'):
                raise CatalogError('JOB_NOT_RETRYABLE')
            if job['kind'] == 'import':
                alive = conn.execute('''SELECT 1 FROM document_versions v JOIN documents d ON d.id=v.document_id
                    WHERE v.id=? AND d.state='active' ''', (job['target_id'],)).fetchone()
                if not alive:
                    alive = conn.execute('''SELECT 1 FROM import_inputs i LEFT JOIN documents d ON d.id=i.document_id
                        WHERE i.id=? AND (i.document_id IS NULL OR d.state='active')''',(job['target_id'],)).fetchone()
                if not alive:
                    raise CatalogError('JOB_SOURCE_REMOVED')
            count = conn.execute("SELECT count(*) FROM jobs WHERE state IN ('queued','running','cancel_requested')").fetchone()[0]
            if count >= self.queue_limit:
                raise CatalogError('JOB_QUEUE_FULL',status=429)
            conn.execute('''UPDATE jobs SET state='queued',stage='queued',attempt=attempt+1,lease_token=NULL,
                lease_until=NULL,cancel_requested=0,error_code=NULL,updated_at=? WHERE id=?''', (utc_now(),job_id))
            return dict(conn.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone())
