"""Conservative event retention. Default read-only; application requires stop+backup.

Never deletes jobs, receipts, answers, citations, messages, notes or source files.
Terminal SSE events are retained so an old cursor can still resolve the run.
"""
import argparse,json
from datetime import datetime,timedelta,timezone
from pathlib import Path
from techdoc.catalog import Catalog
from techdoc.catalog_owner import CatalogOwner

TABLES=('jobs','run_events','mutation_receipts','messages','artifacts','runs','citations')
ELIGIBLE="""SELECT r.id FROM runs r WHERE r.finished_at<?
 AND r.status IN ('succeeded','abstained','failed','cancelled','interrupted')
 AND EXISTS(SELECT 1 FROM run_events e WHERE e.run_id=r.id AND e.event_kind='terminal')
 AND NOT EXISTS(SELECT 1 FROM jobs j WHERE j.id=r.worker_job_id AND j.state IN ('queued','running','cancel_requested'))"""


def maintain(data_dir,*,retention_days=30,apply=False,backup=None,now=None):
    if type(retention_days) is not int or not 7<=retention_days<=3650:raise ValueError('RETENTION_DAYS_OUT_OF_RANGE')
    root=Path(data_dir).resolve();catalog=Catalog(root,initialize=False)
    cutoff=((now or datetime.now(timezone.utc))-timedelta(days=retention_days)).isoformat(timespec='milliseconds')
    def report():
        with catalog.reader() as conn:
            counts={table:conn.execute('SELECT count(*) FROM '+table).fetchone()[0] for table in TABLES}
            candidates=conn.execute("SELECT count(*) FROM run_events WHERE event_kind='progress' AND run_id IN ("+ELIGIBLE+')',(cutoff,)).fetchone()[0]
            pages=conn.execute('PRAGMA freelist_count').fetchone()[0]
        return {'table_counts':counts,'eligible_progress_events':candidates,'database_bytes':catalog.path.stat().st_size,'free_pages':pages}
    before=report();value={'mode':'apply' if apply else 'dry_run','retention_days':retention_days,'before':before,
        'deleted_progress_events':0,'receipts_and_business_history_preserved':True,'physical_files_deleted':0,'schema_migrated':False}
    if not apply:return value
    if backup is None:raise ValueError('BACKUP_PATH_REQUIRED')
    owner=CatalogOwner(root)
    try:
        before=report();value['before']=before
        with catalog.reader() as conn:
            if conn.execute("SELECT 1 FROM jobs WHERE state IN ('queued','running','cancel_requested') LIMIT 1").fetchone():raise ValueError('QUIESCE_JOBS_BEFORE_MAINTENANCE')
        from scripts.audit_catalog import audit
        if not audit(root)['ok']:raise ValueError('SOURCE_AUDIT_FAILED')
        from scripts.backup_workspace import _backup_workspace
        _backup_workspace(root,backup)
        with catalog.transaction() as conn:
            value['deleted_progress_events']=conn.execute("DELETE FROM run_events WHERE event_kind='progress' AND run_id IN ("+ELIGIBLE+')',(cutoff,)).rowcount
        after=report();value['after']=after;value['backup_created']=True
        assert all(after['table_counts'][t]==before['table_counts'][t] for t in TABLES if t!='run_events')
        assert audit(root)['ok']
        return value
    finally:owner.close()


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('data_dir');p.add_argument('--retention-days',type=int,default=30)
    p.add_argument('--apply',action='store_true');p.add_argument('--backup');args=p.parse_args()
    print(json.dumps(maintain(args.data_dir,retention_days=args.retention_days,apply=args.apply,backup=args.backup),ensure_ascii=False,indent=2))


if __name__=='__main__':main()
