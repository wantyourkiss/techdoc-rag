"""Commit verified release records with exception rollback; no service mutation."""
import json,os,tempfile
from pathlib import Path
from .entrypoints import require_verified


def _atomic(path,raw):
    temporary=None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent,prefix='.release-',delete=False) as f:
            temporary=Path(f.name);f.write(raw);f.flush();os.fsync(f.fileno())
        os.replace(temporary,path)
    finally:
        if temporary is not None:temporary.unlink(missing_ok=True)


def publish_verified_release(pointer_path,result_path,pointer,result):
    if result.get('deployed') is not True or result.get('entrypoints_verified') is not True:raise ValueError('UNVERIFIED_RELEASE')
    require_verified(result.get('entrypoint_health'))
    paths=[Path(pointer_path),Path(result_path)]
    if paths[0].resolve()==paths[1].resolve() or any(p.is_symlink() for p in paths):raise ValueError('INVALID_RELEASE_RECORD_PATH')
    previous=[p.read_bytes() if p.exists() else None for p in paths]
    # Serialize both before changing either file (including NaN/type errors).
    records=[json.dumps(value,ensure_ascii=False,allow_nan=False,indent=2).encode() for value in (pointer,result)]
    try:
        for path,raw in zip(paths,records):_atomic(path,raw)
    except BaseException:
        for path,raw in zip(paths,previous):
            if raw is None:path.unlink(missing_ok=True)
            else:_atomic(path,raw)
        raise
