"""Small idempotency receipts contain identities/revisions, never answer copies."""
from hashlib import sha256
import json
from .catalog import CatalogError,encoded,utc_now


def request_digest(value): return sha256(encoded(value).encode()).hexdigest()


def check_key(key):
    if not isinstance(key,str) or not 1<=len(key)<=128 or any(ord(c)<32 for c in key):
        raise CatalogError('INVALID_REQUEST_KEY',status=422)


def replay(conn,owner,key,operation,value):
    check_key(key)
    row=conn.execute('SELECT * FROM mutation_receipts WHERE owner_id=? AND request_key=?',(owner,key)).fetchone()
    if row:
        if row['operation']!=operation or row['input_sha256']!=request_digest(value):
            raise CatalogError('REQUEST_KEY_CONFLICT')
        return json.loads(row['result_json'])


def remember(conn,owner,key,operation,value,result):
    conn.execute('INSERT INTO mutation_receipts VALUES(?,?,?,?,?,?)',
        (owner,key,operation,request_digest(value),encoded(result),utc_now()))
