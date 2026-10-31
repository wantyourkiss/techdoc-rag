"""Versioned parsing identity for new knowledge sources; legacy IDs stay unchanged."""
from __future__ import annotations

from hashlib import sha256
from importlib import metadata as packages
import inspect
from pathlib import Path
import platform

from . import core
from .catalog import encoded


def parser_identity(filename):
    extension = Path(filename).suffix.lower()
    dependencies = {}
    for name in ({'.pdf':['PyMuPDF'],'.docx':['python-docx','lxml']}.get(extension,[])):
        dependencies[name] = packages.version(name)
    sources = '\n'.join(inspect.getsource(obj) for obj in
                        (core.DocParser,core.block,core.digest,core._BasicHTML,core._intersect))
    return {'profile':'conservative_snapshot_v2','extension':extension,
            'parser_code_sha256':sha256(sources.encode()).hexdigest(),
            'dependencies':dependencies,'python':platform.python_version(),
            'limits':{'max_bytes':core.MAX_BYTES,'max_blocks':core.MAX_BLOCKS,'max_chars':core.MAX_CHARS}}


def parse_snapshot_v2(filename, data, version='', *, origin='file'):
    if origin not in ('file','pasted_text'):
        raise ValueError('INVALID_SOURCE_ORIGIN')
    meta, blocks = core.DocParser.parse(filename,data,version)
    identity = dict(parser_identity(meta['filename']),origin=origin)
    sid = sha256(encoded(['snapshot_v2',meta['filename'],version,meta['source_sha256'],identity]).encode()).hexdigest()[:28]
    new_blocks = [dict(block,snapshot_id=sid,
                       block_id=core.digest([sid,i,block['kind'],block['text'],block['locator']])[:20])
                  for i,block in enumerate(blocks)]
    return dict(meta,snapshot_id=sid,snapshot_scheme='snapshot_v2',parser_identity=identity,origin=origin), new_blocks
