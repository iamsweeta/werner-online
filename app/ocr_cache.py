"""Bounded cache of recognition results; never commits a document or a price."""
import hashlib,json,os,tempfile,time
from pathlib import Path
from . import v42_engine as e
REVISION='59.0-1'
TTL=7*86400
MAX_FILES=64
MAX_BYTES=32*1024*1024
MAX_ENTRY=8*1024*1024

def root():return e.RUNTIME_DIR/'imports'/'ocr_cache'
def identity(raw,company,origin=None,destination=None):
    return {'sha256':hashlib.sha256(raw).hexdigest(),'company':company,'revision':REVISION,'scope':[origin,destination] if destination else 'full'}
def path(key):return root()/(hashlib.sha256(json.dumps(key,sort_keys=True,ensure_ascii=False).encode()).hexdigest()+'.json')
def cleanup():
    files=[]
    for p in root().glob('*'):
        try:
            if p.is_symlink() or not p.is_file():continue
            stat=p.stat()
            if time.time()-stat.st_mtime>(TTL if p.suffix=='.json' else 3600):p.unlink(missing_ok=True)
            elif p.suffix=='.json':files.append((stat.st_mtime,stat.st_size,p))
        except OSError:pass
    size=0
    for i,(_,n,p) in enumerate(sorted(files,reverse=True)):
        size+=n
        if i>=MAX_FILES or size>MAX_BYTES:
            try:p.unlink(missing_ok=True)
            except OSError:pass

def get(raw,company,origin=None,destination=None):
    cleanup()
    keys=[identity(raw,company)]
    if destination:keys.append(identity(raw,company,origin,destination))
    for key in keys:
        try:
            p=path(key)
            if p.is_symlink() or p.stat().st_size>MAX_ENTRY:continue
            data=json.loads(p.read_text(encoding='utf-8'));r=data['result']
            if data['identity']!=key or not 0<=time.time()-data['created_at']<TTL or not r.get('rows') or r.get('errors'):continue
            if destination:
                r={**r,'rows':[row for row in r['rows'] if row['destination']==destination],'ocr_scope':'route'}
                r['ocr_pages']=sorted({row['source_page'] for row in r['rows']})
            return {**r,'ocr_cache_hit':True}
        except (OSError,ValueError,KeyError,TypeError):continue
    return None

def put(raw,company,result,origin=None,destination=None):
    if not result.get('rows') or result.get('errors') or result.get('error'):return
    key=identity(raw,company,origin,destination)
    try:
        content=json.dumps({'identity':key,'created_at':time.time(),'result':result},ensure_ascii=False,allow_nan=False).encode()
        if len(content)>MAX_ENTRY:return
        root().mkdir(parents=True,exist_ok=True)
        fd,name=tempfile.mkstemp(dir=root(),suffix='.tmp')
        try:
            with os.fdopen(fd,'wb') as f:f.write(content)
            Path(name).replace(path(key))
        finally:Path(name).unlink(missing_ok=True)
        cleanup()
    except (OSError,ValueError):pass
