"""Bounded cache of verified OCR derived from the exact uploaded bytes.

This never confirms a document or writes tariff prices into the user's library.
"""
import hashlib
import json
import os
import tempfile
import time
from pathlib import Path

REVISION='58.0-1'
TTL=7*24*3600
MAX_FILES=64
MAX_BYTES=32*1024*1024
MAX_ENTRY_BYTES=8*1024*1024


def root():
    from . import v42_engine as e
    return e.RUNTIME_DIR/'imports'/'ocr_cache'


def identity(raw,company,origin=None,destination=None):
    return {'sha256':hashlib.sha256(raw).hexdigest(),'company':company,'revision':REVISION,
            'scope':[origin,destination] if destination else 'full'}


def filename(key):
    return hashlib.sha256(json.dumps(key,sort_keys=True,ensure_ascii=False).encode()).hexdigest()+'.json'


def cleanup():
    entries=[];stamp=time.time()
    for p in root().glob('*'):
        try:
            if p.is_symlink() or not p.is_file():continue
            st=p.stat()
            if (p.suffix=='.json' and stamp-st.st_mtime>TTL) or (p.suffix=='.tmp' and stamp-st.st_mtime>3600):
                p.unlink(missing_ok=True);continue
            if p.suffix=='.json':entries.append((st.st_mtime,st.st_size,p))
        except OSError:continue
    size=0
    for i,(_,n,p) in enumerate(sorted(entries,reverse=True)):
        size+=n
        if i>=MAX_FILES or size>MAX_BYTES:
            try:p.unlink(missing_ok=True)
            except OSError:pass


def get(raw,company,origin=None,destination=None):
    cleanup();keys=[identity(raw,company)]
    if destination:keys.append(identity(raw,company,origin,destination))
    for key in keys:
        path=root()/filename(key)
        try:
            if path.is_symlink() or path.stat().st_size>MAX_ENTRY_BYTES:continue
            record=json.loads(path.read_text(encoding='utf-8'))
            if record['identity']!=key or not 0<=time.time()-record['created']<TTL:continue
            value=record['result']
            if not isinstance(value,dict) or not value.get('rows') or value.get('errors'):continue
            if destination:
                value={**value,'rows':[r for r in value['rows'] if r['destination']==destination],
                       'ocr_scope':'route'}
                value['ocr_pages']=sorted({r['source_page'] for r in value['rows']})
            return {**value,'ocr_cache_hit':True}
        except (OSError,ValueError,KeyError,TypeError):continue
    return None


def put(raw,company,result,origin=None,destination=None):
    if not result.get('rows') or result.get('errors') or result.get('error'):return
    key=identity(raw,company,origin,destination);temp=None
    try:
        content=json.dumps({'identity':key,'created':time.time(),'result':result},ensure_ascii=False,allow_nan=False).encode()
        if len(content)>MAX_ENTRY_BYTES:return
        folder=root();folder.mkdir(parents=True,exist_ok=True)
        fd,name=tempfile.mkstemp(suffix='.tmp',dir=folder);temp=Path(name)
        with os.fdopen(fd,'wb') as file:file.write(content)
        os.replace(temp,folder/filename(key));cleanup()
    except (OSError,ValueError):pass
    finally:
        if temp is not None:temp.unlink(missing_ok=True)
