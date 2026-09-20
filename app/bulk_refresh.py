"""Durable all-carrier collection, with immutable evidence for each online run.

SQLite checkpoints after each carrier. Closing the browser does not interrupt
the worker. A server restart pauses the job; resume retries only unfinished
carriers. Exports combine this job's evidence with confirmed documents for missing cells.
"""
from __future__ import annotations

import csv
import io
import json
import sqlite3
import threading
import time
import uuid
import zipfile
import zlib
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime
from pathlib import Path

from . import v42_engine as e
from .cities import city_names, MAIN_ORIGINS
from .tariff_model import tariff_value

ACTIVE={'queued','running','pausing'}
WEIGHTS=[p['id'] for p in e.COMMON_PROFILES if not p.get('is_minimum_profile')]
PART_SIZE=200
SINGLE_WORKBOOK_LIMIT=400


def now():return datetime.now().astimezone().isoformat(timespec='seconds')


def plan_routes(scope='reference', origins=None, destinations=None):
    if scope in {'reference','extended_reference'}:
        rows=json.loads((e.DATA_DIR/'customer_routes.json').read_text(encoding='utf-8'))['routes']
        if scope=='reference':rows=[r for r in rows if e.normalize_city(r[0]) in MAIN_ORIGINS]
    elif scope in {'origins','all'}:
        def valid(raw):
            if not raw:raise ValueError('Выберите хотя бы один город отправления')
            names=list(dict.fromkeys(e.normalize_city(c) for c in raw))
            if any(c not in city_names() for c in names):raise ValueError('Неизвестный город')
            return names
        source=city_names() if scope=='all' else valid(origins)
        target=valid(destinations) if destinations else city_names()
        rows=[(o,d) for o in source for d in target if o!=d]
    else:raise ValueError('Неизвестный список маршрутов')
    result=list(dict.fromkeys(tuple(e.route_pair(*r)) for r in rows if e.is_supported_route(*r)))
    if not result:raise ValueError('Нет направлений: отправление и назначение должны различаться')
    return result


def route_plan(scope='reference', origins=None, destinations=None):
    routes=plan_routes(scope,origins,destinations)
    return {'scope':scope,'routes':len(routes),'companies':len(e.COMPANIES),
            'checks':len(routes)*len(e.COMPANIES),'weight_points':len(routes)*len(e.COMPANIES)*len(WEIGHTS),
            'cities':len(city_names()),'company_names':e.COMPANIES,
            'sample':[{'origin':o,'destination':d} for o,d in routes[:6]],
            'export_parts':1 if len(routes)<=SINGLE_WORKBOOK_LIMIT else (len(routes)+PART_SIZE-1)//PART_SIZE}


def capture_company(company, origin, destination, result):
    """Freeze the last known prices, keeping failed refreshes visibly distinct."""
    with e.STATE_LOCK:
        live=e._live_pack(origin,destination)
    meta=(live.get('companies') or {}).get(company,{})
    aid=meta.get('current_attempt_id')
    clean={'companies':{company:deepcopy(meta)},'profiles':{}}
    if not result.get('ok') and not result.get('snapshot'):
        clean['companies'][company].update(attempt_status='failed',last_error=result.get('message'))
    for pid,companies in live.get('profiles',{}).items():
        row=companies.get(company)
        if row:
            clean['profiles'][pid]={company:deepcopy(row)}
    packs=({},clean,{})
    items=[e._route_quote(company,origin,destination,p['id'],packs) for p in e.COMMON_PROFILES]
    for item in items:
        item['collected_online']=bool(item.get('online'))
        item['checked_at']=meta.get('last_finished_at') or now()
    exact=sum(tariff_value(i,e.PROFILE_BY_ID[i['profile_id']]) is not None for i in items if i['profile_id']!='min')
    confirmed=sum(i.get('collected_online') and tariff_value(i,e.PROFILE_BY_ID[i['profile_id']]) is not None for i in items if i['profile_id']!='min')
    lower=sum(bool(i.get('price_is_minimum')) for i in items)
    request=sum(i.get('availability')=='on_request' for i in items if i['profile_id']!='min')
    error=result.get('message') if not result.get('ok') else meta.get('last_error')
    if not result.get('ok'):
        status='unavailable' if (result.get('error_info') or {}).get('code')=='route_unpublished' else 'failed'
    elif confirmed==len(WEIGHTS):status='complete'
    else:status='partial'
    if result.get('snapshot'):status='saved'
    return {'company':company,'origin':origin,'destination':destination,'status':status,
            'checked_at':now(),'exact_weights':exact,'on_request':request,'lower_bounds':lower,
            'message':error or result.get('message',''),'items':items}


class BusyError(ValueError):pass


class BulkManager:
    def __init__(self, directory=None, *, guard=None, route_busy=None, collector=None):
        self.directory=Path(directory or e.RUNTIME_DIR/'bulk')
        self.directory.mkdir(parents=True,exist_ok=True)
        self.path=self.directory/'jobs.sqlite3'
        self.guard=guard or threading.RLock()
        self.route_busy=route_busy or (lambda:False)
        self.collector=collector
        self.worker=None;self.export_worker=None
        with self.db() as db:
            db.executescript('''
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS jobs (
                  id TEXT PRIMARY KEY,status TEXT,created_at TEXT,updated_at TEXT,
                  config TEXT,message TEXT,export_status TEXT,export_file TEXT);
                CREATE TABLE IF NOT EXISTS routes (
                  job TEXT,idx INTEGER,origin TEXT,destination TEXT,state TEXT DEFAULT 'pending',
                  PRIMARY KEY(job,idx));
                CREATE INDEX IF NOT EXISTS route_state ON routes(job,state,idx);
                CREATE TABLE IF NOT EXISTS results (
                  job TEXT,idx INTEGER,company TEXT,status TEXT,payload BLOB,
                  PRIMARY KEY(job,idx,company));
                CREATE INDEX IF NOT EXISTS result_status ON results(job,status);
            ''')
            db.execute("UPDATE jobs SET status='paused',message='Приложение перезапущено. Нажмите «Продолжить». ' WHERE status IN ('queued','running','pausing')")
            db.execute("UPDATE routes SET state='pending' WHERE state='running'")
            db.execute("UPDATE jobs SET export_status='error',message='Выгрузка прервана перезапуском. Повторите создание Excel.' WHERE export_status='running'")
            # Old work is preserved, but an export with old placeholders must
            # never remain downloadable after the comparison semantics change.
            for job in db.execute('SELECT id,config FROM jobs').fetchall():
                config=json.loads(job['config'])
                if config.get('tariff_schema')==52:continue
                convert_weights=config.get('tariff_schema') not in {50,51}
                config.update(companies=[c for c in config['companies'] if c in e.COMPANIES],tariff_schema=52)
                for result in (db.execute('SELECT idx,company,payload FROM results WHERE job=?',(job['id'],)).fetchall() if convert_weights else []):
                    if result['company'] not in e.COMPANIES:
                        db.execute('DELETE FROM results WHERE job=? AND idx=? AND company=?',(job['id'],result['idx'],result['company']));continue
                    payload=json.loads(zlib.decompress(result['payload']))
                    payload['exact_weights']=sum(tariff_value(i,e.PROFILE_BY_ID[i['profile_id']]) is not None for i in payload['items'] if i['profile_id']!='min')
                    if payload['status'] in {'complete','partial'}:payload['status']='complete' if payload['exact_weights']==len(WEIGHTS) else 'partial'
                    db.execute('UPDATE results SET status=?,payload=? WHERE job=? AND idx=? AND company=?',
                        (payload['status'],zlib.compress(json.dumps(payload,ensure_ascii=False).encode()),job['id'],result['idx'],result['company']))
                db.execute("UPDATE jobs SET config=?,export_status='idle',export_file=NULL,message='Формат выгрузки обновлён. Пересоберите Excel: отсутствующие цены будут пустыми.' WHERE id=?",(json.dumps(config,ensure_ascii=False),job['id']))

    @contextmanager
    def db(self):
        db=sqlite3.connect(self.path,timeout=30)
        db.row_factory=sqlite3.Row
        try:
            with db:yield db
        finally:db.close()

    def active(self):
        with self.db() as db:
            row=db.execute("SELECT id FROM jobs WHERE status IN ('queued','running','pausing') LIMIT 1").fetchone()
        return row['id'] if row else None

    def _job(self,job_id=None):
        with self.db() as db:
            row=db.execute('SELECT * FROM jobs WHERE id=?',(job_id,)).fetchone() if job_id else db.execute('SELECT * FROM jobs ORDER BY created_at DESC,rowid DESC LIMIT 1').fetchone()
        if row is None and job_id:raise ValueError('Сбор не найден')
        return dict(row) if row else None

    def status(self,job_id=None):
        job=self._job(job_id)
        if job is None:return {'status':'idle'}
        ident=job['id']
        with self.db() as db:
            counts={r['state']:r['n'] for r in db.execute('SELECT state,COUNT(*) n FROM routes WHERE job=? GROUP BY state',(ident,))}
            outcomes={r['status']:r['n'] for r in db.execute('SELECT status,COUNT(*) n FROM results WHERE job=? GROUP BY status',(ident,))}
            current=db.execute("SELECT idx,origin,destination FROM routes WHERE job=? AND state='running' ORDER BY idx LIMIT 1",(ident,)).fetchone()
            last=db.execute('SELECT idx,company,status,payload FROM results WHERE job=? ORDER BY rowid DESC LIMIT 18',(ident,)).fetchall()
        config=json.loads(job['config'])
        from .document_imports import revision
        outdated=bool(config.get('include_imports',True) and job['export_status']=='ready' and config.get('export_document_revision')!=revision())
        job.pop('config');job.pop('export_file')
        total=sum(counts.values());done=counts.get('done',0)
        job.update({'job_id':ident,'scope':config['scope'],'companies':config['companies'],
                    'coverage':config.get('coverage') if job['export_status']=='ready' and not outdated else None,
                    'mode':config.get('mode','online'),'include_imports':config.get('include_imports',True),
                    'total_routes':total,'completed_routes':done,'total_checks':total*len(config['companies']),
                    'completed_checks':sum(outcomes.values()),'outcomes':outcomes,'current':dict(current) if current else None,
                    'percent':round(100*sum(outcomes.values())/(total*len(config['companies'])),2) if total else 0,
                    'recent':[],'export_outdated':outdated,'download_url':f'/api/bulk/{ident}/download' if job['export_status']=='ready' and not outdated else None})
        for row in last:
            value=json.loads(zlib.decompress(row['payload']))
            job['recent'].append({k:v for k,v in value.items() if k!='items'})
        return job

    def start(self,scope='reference',origins=None,destinations=None,*,mode='online',include_imports=True):
        if mode not in {'online','saved'}:raise ValueError('Неизвестный режим отчёта')
        routes=plan_routes(scope,origins,destinations)
        with self.guard:
            if self.active() or self.route_busy():raise BusyError('Дождитесь текущего обновления или приостановите общий сбор')
            if self.export_worker and self.export_worker.is_alive():raise BusyError('Дождитесь завершения создания Excel')
            ident='bulk-'+uuid.uuid4().hex
            config={'scope':scope,'origins':origins,'destinations':destinations,'companies':list(e.COMPANIES),'tariff_schema':52,
                    'mode':mode,'include_imports':bool(include_imports)}
            with self.db() as db:
                db.execute('INSERT INTO jobs VALUES (?,?,?,?,?,?,?,?)',
                           (ident,'queued',now(),now(),json.dumps(config,ensure_ascii=False),'В очереди','idle',None))
                db.executemany('INSERT INTO routes(job,idx,origin,destination) VALUES (?,?,?,?)',
                               ((ident,i,o,d) for i,(o,d) in enumerate(routes)))
            self._launch(ident)
        return self.status(ident)

    def _launch(self,ident):
        self.worker=threading.Thread(target=self._run,args=(ident,),daemon=True,name='tariffs-bulk')
        self.worker.start()

    def pause(self,ident):
        with self.guard,self.db() as db:
            self._job(ident)
            db.execute("UPDATE jobs SET status='pausing',message='Пауза после текущего маршрута',updated_at=? WHERE id=? AND status IN ('queued','running')",(now(),ident))
        return self.status(ident)

    def resume(self,ident,retry=False):
        with self.guard:
            job=self._job(ident)
            if self.active() or self.route_busy():raise BusyError('Обновление уже выполняется')
            if self.export_worker and self.export_worker.is_alive():raise BusyError('Дождитесь создания Excel')
            if retry:
                with self.db() as db:
                    db.execute("UPDATE routes SET state='pending' WHERE job=? AND idx IN (SELECT idx FROM results WHERE job=? AND status IN ('failed','partial'))",(ident,ident))
                    db.execute("DELETE FROM results WHERE job=? AND status IN ('failed','partial')",(ident,))
            elif job['status'] not in {'paused','error'}:raise ValueError('Этот сбор уже завершён. Запустите новое обновление.')
            with self.db() as db:
                db.execute("UPDATE jobs SET status='queued',export_status='idle',export_file=NULL,message='Продолжаю сбор',updated_at=? WHERE id=?",(now(),ident))
            self._launch(ident)
        return self.status(ident)

    def _save_result(self,ident,idx,o,d,result):
        company=result['company']
        payload=capture_company(company,o,d,result)
        raw=zlib.compress(json.dumps(payload,ensure_ascii=False).encode('utf-8'))
        with self.db() as db:
            db.execute('INSERT OR REPLACE INTO results VALUES (?,?,?,?,?)',(ident,idx,company,payload['status'],raw))
            db.execute('UPDATE jobs SET updated_at=? WHERE id=?',(now(),ident))

    def _run(self,ident):
        from .document_cache import session
        with session():self._run_with_documents(ident)

    def _run_with_documents(self,ident):
        from .v42_collectors import collect_selected
        collector=self.collector or collect_selected
        try:
            with self.db() as db:
                db.execute("UPDATE jobs SET status=CASE WHEN status='pausing' THEN status ELSE 'running' END WHERE id=?",(ident,))
            config=json.loads(self._job(ident)['config']);companies=config['companies']
            while True:
                if self._job(ident)['status']=='pausing':
                    with self.db() as db:
                        db.execute("UPDATE jobs SET status='paused',message='Сбор приостановлен. Результаты сохранены.',updated_at=? WHERE id=?",(now(),ident))
                    return
                with self.db() as db:
                    route=db.execute("SELECT * FROM routes WHERE job=? AND state!='done' ORDER BY idx LIMIT 1",(ident,)).fetchone()
                    if route is None:break
                    idx,o,d=route['idx'],route['origin'],route['destination']
                    completed={r['company'] for r in db.execute('SELECT company FROM results WHERE job=? AND idx=?',(ident,idx))}
                    db.execute("UPDATE routes SET state='running' WHERE job=? AND idx=?",(ident,idx))
                    db.execute('UPDATE jobs SET message=?,updated_at=? WHERE id=?',(f'{o} → {d}',now(),ident))
                selected=[c for c in companies if c not in completed]
                if selected:
                    seen=set();callback_lock=threading.Lock()
                    def progress(result):
                        with callback_lock:
                            if result['company'] in selected and result['company'] not in seen:
                                self._save_result(ident,idx,o,d,result);seen.add(result['company'])
                    if config.get('mode')=='saved':
                        results=[]
                        for company in selected:
                            meta=e.live_company_state(o,d,company)
                            result={'company':company,'ok':meta.get('attempt_status') in {'success','partial'},
                                    'snapshot':True,
                                    'message':meta.get('last_error') or 'Отчёт из текущих данных и подтверждённых документов'}
                            progress(result);results.append(result)
                    else:results=collector(selected,o,d,'w100',on_progress=progress,full_grid=True)
                    for result in results:progress(result)
                    if seen!=set(selected):raise RuntimeError('Загрузчик не вернул результат для всех компаний')
                with self.db() as db:
                    db.execute("UPDATE routes SET state='done' WHERE job=? AND idx=?",(ident,idx))
                # Bound route-to-route request pressure; carrier jobs have their
                # own deadlines and a fixed maximum number of worker threads.
                if config.get('mode')!='saved':time.sleep(.2)
            with self.guard:
                with self.db() as db:
                    db.execute("UPDATE jobs SET status='done',export_status='running',message='Данные обработаны. Создаю Excel.',updated_at=? WHERE id=?",(now(),ident))
                self.prepare_export(ident)
        except Exception as exc:
            with self.db() as db:
                db.execute("UPDATE jobs SET status='error',export_status='idle',message=?,updated_at=? WHERE id=?",(str(exc)[:1500],now(),ident))

    def _route_rows(self,ident,idx,o,d,imports=None):
        with self.db() as db:
            stored={r['company']:json.loads(zlib.decompress(r['payload'])) for r in db.execute('SELECT company,payload FROM results WHERE job=? AND idx=?',(ident,idx))}
        by={c:{i['profile_id']:i for i in row['items']} for c,row in stored.items()}
        output=[]
        for p in e.COMMON_PROFILES:
            items=[]
            for company in e.COMPANIES:
                item=deepcopy(by.get(company,{}).get(p['id']))
                if item is None:
                    item={'company':company,'company_label':e.COMPANY_LABELS[company],'profile_id':p['id'],
                          'price':None,'comparison_value':None,'online':False,'uploaded':False,
                          'message':'Не проверено в этом сборе','bulk_status':'pending'}
                else:
                    item['bulk_status']=stored[company]['status']
                    item['message']=item.get('message') or stored[company]['message']
                    # Keep collection-time evidence accessible after the LIVE
                    # window. Never pretend a multi-day run is simultaneous.
                    item['online']=bool(item.get('collected_online') and e.age_seconds(item.get('captured_at'))<e.LIVE_TTL_SECONDS)
                items.append(item)
            output.append({'profile':deepcopy(p),'items':items})
        # Old jobs may contain a "minimum" derived from one heavy quote.
        first=next(r for r in output if r['profile']['id']=='w001')
        minimum=next(r for r in output if r['profile']['id']=='min')
        for pos,item in enumerate(minimum['items']):
            if (item.get('minimum_basis')=='lowest_available_shipment'
                and item.get('minimum_source_profile') not in {'min','w001'}
                and first['items'][pos].get('comparison_value') is None):
                minimum['items'][pos]={**item,'price':None,'comparison_value':None,'tariff_value':None,
                    'online':False,'collected_online':False,'message':'Минимум не подтверждён: нужен первый весовой диапазон или опубликованный минимум.'}
        if imports is not None:
            output=fill_document_gaps(output,imports,o,d)
        return output

    def prepare_export(self,ident):
        with self.guard:
            job=self._job(ident)
            if job['status'] in ACTIVE:raise BusyError('Сначала дождитесь окончания сбора или нажмите «Пауза»')
            if self.export_worker and self.export_worker.is_alive():raise BusyError('Excel уже создаётся')
            with self.db() as db:
                count=db.execute('SELECT COUNT(*) FROM results WHERE job=?',(ident,)).fetchone()[0]
                if not count:raise ValueError('Ещё нет результатов для выгрузки')
                db.execute("UPDATE jobs SET export_status='running',export_file=NULL WHERE id=?",(ident,))
            self.export_worker=threading.Thread(target=self._export,args=(ident,),daemon=True,name='tariffs-excel')
            self.export_worker.start()
        return self.status(ident)

    def _export(self,ident):
        from .excel_export import export_bytes
        try:
            with self.db() as db:
                all_routes=[dict(r) for r in db.execute('SELECT * FROM routes WHERE job=? ORDER BY idx',(ident,))]
            job=self.status(ident)
            config=json.loads(self._job(ident)['config'])
            from .document_imports import revision
            document_revision=revision()
            # A partial export contains attempted routes only; the manifest
            # lists all planned pairs and explicitly marks those not checked.
            if job['status']!='done':
                with self.db() as db:attempted={r[0] for r in db.execute('SELECT DISTINCT idx FROM results WHERE job=?',(ident,))}
                routes=[r for r in all_routes if r['idx'] in attempted]
            else:routes=all_routes
            if not routes:raise ValueError('Нет проверенных маршрутов для Excel')
            part_size=len(routes) if len(routes)<=SINGLE_WORKBOOK_LIMIT else PART_SIZE
            parts=[routes[i:i+part_size] for i in range(0,len(routes),part_size)]
            target=self.directory/ident;target.mkdir(exist_ok=True)
            info={k:job[k] for k in ('job_id','created_at','updated_at','status','total_routes','completed_routes','completed_checks','total_checks','outcomes')}
            info['include_imports']=config.get('include_imports',True);info['mode']=config.get('mode','online')
            files=[]
            coverage={c:{'company':c,'online':0,'saved':0,'document':0,'missing':0} for c in e.COMPANIES}
            for number,part in enumerate(parts,1):
                index={(r['origin'],r['destination']):r['idx'] for r in part}
                # One version of user documents for the whole workbook. Online
                # replies are already frozen in this collection's checkpoints.
                from .document_imports import pack as imported_pack
                with e.STATE_LOCK:
                    documents={route:imported_pack(*route) for route in index} if info['include_imports'] else {}
                def matrix_for(o,d):
                    rows=self._route_rows(ident,index[(o,d)],o,d,documents.get((o,d)))
                    for row in rows:
                        if row['profile']['id']=='min':continue
                        for item in row['items']:
                            key=('document' if item.get('uploaded') else 'online' if item.get('collected_online') else 'saved') if tariff_value(item,row['profile']) is not None else 'missing'
                            coverage[item['company']][key]+=1
                    return rows
                content=export_bytes(*next(iter(index)),list(e.COMPANIES),live_only=False,include_imports=False,
                                     routes_override=list(index),matrix_provider=matrix_for,
                                     collection_info={**info,'part':number,'parts':len(parts)})
                file=target/f'tariffs_{number:03d}.xlsx'
                temp=file.with_suffix('.tmp');temp.write_bytes(content);temp.replace(file);files.append(file)
            if len(files)==1:
                output=files[0]
            else:
                output=target/'tariffs_all_routes.zip';temp=output.with_suffix('.tmp')
                manifest=io.StringIO();writer=csv.writer(manifest,delimiter=';')
                writer.writerow(['Откуда','Куда','Проверка','Excel'])
                part_by_route={r['idx']:files[i].name for i,part in enumerate(parts) for r in part}
                for route in all_routes:
                    writer.writerow([route['origin'],route['destination'],'Завершена' if route['state']=='done' else 'Не завершена',part_by_route.get(route['idx'],'Не проверено')])
                with zipfile.ZipFile(temp,'w',zipfile.ZIP_DEFLATED) as out:
                    for file in files:out.write(file,file.name)
                    out.writestr('routes.csv',manifest.getvalue().encode('utf-8-sig'))
                    out.writestr('collection.json',json.dumps(info,ensure_ascii=False,indent=2).encode())
                    out.writestr('README.txt','Онлайн-данные на время проверки; пропуски дополнены подтверждёнными прайсами, если включены документы. Даты и источники — на листе Источники. Для обновления запустите новый общий сбор в приложении. Маршруты перечислены в routes.csv.'.encode('utf-8'))
                temp.replace(output)
            with self.db() as db:
                config['export_document_revision']=document_revision
                config['coverage']=list(coverage.values())
                db.execute("UPDATE jobs SET export_status='ready',export_file=?,config=?,message='Excel готов. Даты проверки указаны в файле.' WHERE id=?",(str(output.resolve()),json.dumps(config,ensure_ascii=False),ident))
        except Exception as exc:
            with self.db() as db:
                db.execute("UPDATE jobs SET export_status='error',message=? WHERE id=?",('Ошибка Excel: '+str(exc)[:1500],ident))

    def download(self,ident):
        job=self._job(ident)
        if self.status(ident).get('export_outdated'):raise ValueError('Прайс-листы изменились. Пересоберите Excel, чтобы включить актуальные файлы.')
        if job['export_status']!='ready' or not job['export_file']:raise ValueError('Excel ещё не готов')
        file=Path(job['export_file']).resolve()
        if not file.is_relative_to(self.directory.resolve()) or not file.is_file():raise ValueError('Файл выгрузки не найден')
        return file


def fill_document_gaps(rows,imports,origin,destination):
    """Confirmed online weights win. Exact files fill missing and 'from' cells."""
    rows=deepcopy(rows)
    for row in rows:
        pid=row['profile']['id']
        if pid=='min':continue
        for pos,item in enumerate(row['items']):
            company=item['company'];value=imports.get('profiles',{}).get(pid,{}).get(company)
            if item.get('collected_online') and tariff_value(item,row['profile']) is not None:continue
            if not value:continue
            imported=e._route_quote(company,origin,destination,pid,({}, {}, imports))
            if imported.get('comparison_value') is not None and (tariff_value(imported,row['profile']) is not None or item.get('comparison_value') is None):
                row['items'][pos]={**imported,'bulk_status':'document',
                                  'refresh_error':item.get('refresh_error'),'checked_at':item.get('checked_at')}
    minimum=next(r for r in rows if r['profile']['id']=='min')
    for pos,item in enumerate(minimum['items']):
        company=item['company']
        candidates=[r['items'][pos] for r in rows if r['profile']['id']!='min' and r['items'][pos].get('comparison_value') is not None and not r['items'][pos].get('price_is_minimum')]
        if item.get('comparison_value') is not None and not item.get('price_is_minimum'):candidates.append(item)
        explicit=imports.get('profiles',{}).get('min',{}).get(company)
        if explicit and not item.get('collected_online'):
            candidates.append(e._route_quote(company,origin,destination,'min',({}, {}, imports),derive_minimum=False))
        pools=([c for c in candidates if c.get('collected_online')],
               [c for c in candidates if c.get('uploaded')],
               [c for c in candidates if not c.get('collected_online') and not c.get('uploaded')])
        pool=next((group for group in pools if any(c.get('profile_id') in {'min','w001'} for c in group)),[])
        if pool:
            chosen=min(pool,key=lambda x:x['price'])
            minimum['items'][pos]={**chosen,'profile_id':'min','comparison_value':chosen['price'],
                'tariff_value':chosen['price'],'tariff_unit':'₽',
                'effective_rate_per_kg':None,'published_rate_per_kg':None,'minimum_source_profile':chosen['profile_id'],
                'minimum_basis':'published_or_first_weight','price_is_minimum':False}
    return rows
