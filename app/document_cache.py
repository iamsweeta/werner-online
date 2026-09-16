"""Reuse a downloaded public document within one bulk run, never across runs."""
from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
import threading
import time

_ACTIVE=ContextVar('bulk_document_cache',default=None)
TTL=300
MAX_BYTES=64*1024*1024


@contextmanager
def session():
    token=_ACTIVE.set({'rows':{},'bytes':0,'lock':threading.RLock()})
    try:yield
    finally:_ACTIVE.reset(token)


def get(key):
    cache=_ACTIVE.get()
    if cache is None:return None
    with cache['lock']:
        row=cache['rows'].get(key)
        if row is None:return None
        stamp,raw,meta=row
        if time.monotonic()-stamp>=TTL:
            cache['bytes']-=len(raw);del cache['rows'][key];return None
        return raw,{**deepcopy(meta),'document_reused_in_run':True}


def put(key,raw,meta):
    cache=_ACTIVE.get()
    if cache is None or len(raw)>MAX_BYTES:return
    with cache['lock']:
        old=cache['rows'].pop(key,None)
        if old:cache['bytes']-=len(old[1])
        while cache['rows'] and cache['bytes']+len(raw)>MAX_BYTES:
            first=next(iter(cache['rows']));cache['bytes']-=len(cache['rows'].pop(first)[1])
        cache['rows'][key]=(time.monotonic(),raw,deepcopy(meta));cache['bytes']+=len(raw)
