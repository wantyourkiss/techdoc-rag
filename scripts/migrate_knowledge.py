"""Offline catalog migration: dry-run by default; never rewrites source snapshots."""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess

from techdoc.catalog import Catalog
from techdoc.knowledge_migration import plan_migration, summarize_plan, migrate_legacy


def require_not_live_data(data_dir):
    if not shutil.which('systemctl'):
        return
    probe = subprocess.run(['systemctl','--user','show','techdoc-rag','-p','MainPID','--value'],
                           capture_output=True,text=True,timeout=10)
    if probe.returncode != 0 or not probe.stdout.strip().isdigit() or int(probe.stdout.strip()) == 0:
        return
    pid = int(probe.stdout.strip())
    try:
        env = dict(x.split(b'=',1) for x in Path(f'/proc/{pid}/environ').read_bytes().split(b'\0') if b'=' in x)
        configured = env.get(b'TECHDOC_DATA_DIR')
        if configured is None:
            live = Path(f'/proc/{pid}/cwd').resolve()/'runtime_data'
        else:
            live = Path(os.fsdecode(configured))
            if not live.is_absolute():
                live = Path(f'/proc/{pid}/cwd').resolve()/live
    except OSError as exc:
        raise RuntimeError('Cannot verify live-service data directory; stop service before migration') from exc
    if live.resolve() == Path(data_dir).resolve():
        raise RuntimeError('Stop techdoc-rag before migrating its production data directory')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-dir',required=True)
    parser.add_argument('--course-config',type=Path)
    parser.add_argument('--cases',type=Path)
    parser.add_argument('--apply',action='store_true')
    args = parser.parse_args()
    if bool(args.course_config) != bool(args.cases):
        parser.error('--course-config and --cases must be supplied together')
    config = json.loads(args.course_config.read_text(encoding='utf-8-sig')) if args.course_config else None
    cases = json.loads(args.cases.read_text(encoding='utf-8-sig')) if args.cases else []
    ids = [case['id'] for case in cases]
    if len(ids) != len(set(ids)):
        raise ValueError('Duplicate course case IDs')
    if args.apply:
        require_not_live_data(args.data_dir)
        result = migrate_legacy(args.data_dir,config,ids)
        result['catalog_audit'] = Catalog(args.data_dir).audit()
    else:
        result = summarize_plan(plan_migration(args.data_dir,config,ids))
    print(json.dumps(result,ensure_ascii=False,indent=2))


if __name__ == '__main__':
    main()
