from __future__ import annotations

import json, os, threading, time
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .v40_engine import (
    BASE_DIR, DATA_DIR, RUNTIME_DIR, COMPANIES, COMPANY_LABELS, COMMON_PROFILES,
    PROFILE_BY_ID, normalize_city, is_target_route, quote, matrix, coverage,
)
from .v40_collectors import collect_selected, LOG_PATH

VERSION="40.2"
STATIC_DIR=BASE_DIR/"static"
SETTINGS_PATH=RUNTIME_DIR/"v40_settings.json"
RUNTIME_DIR.mkdir(parents=True,exist_ok=True)

app=FastAPI(title="Tariff Comparison v40",version=VERSION)
app.mount("/static",StaticFiles(directory=str(STATIC_DIR)),name="static")

DESTINATIONS=json.loads((DATA_DIR/"customer_destinations.json").read_text(encoding="utf-8"))
ORIGINS=["Москва","Санкт-Петербург"]

COLLECT_LOCK=threading.Lock()
COLLECT_JOBS: dict[str, dict[str,Any]]={}
EXACT_REVISION=0

def _now()->str: return datetime.now().astimezone().isoformat(timespec="seconds")
def _companies(raw: str|None)->list[str]:
    if not raw:return list(COMPANIES)
    requested=[x.strip() for x in raw.split(",") if x.strip()]
    return [x for x in COMPANIES if x in requested]
def _route_key(origin:str,destination:str)->str: return f"{normalize_city(origin)}|{normalize_city(destination)}"

def _settings()->dict[str,Any]:
    try:return json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
    except:return {}
def _save_settings(data:dict[str,Any]):
    old=_settings(); old.update({k:v for k,v in data.items() if v not in (None,"")})
    SETTINGS_PATH.write_text(json.dumps(old,ensure_ascii=False,indent=2),encoding="utf-8")

@app.get("/")
def root(): return FileResponse(STATIC_DIR/"index.html",headers={"Cache-Control":"no-store"})

@app.get("/health")
def health():
    return {"ok":True,"version":VERSION,"engine":"v40_clean_route_engine","target_route":"Санкт-Петербург → Москва","port_hint":8402,"collect_log":str(LOG_PATH)}

@app.get("/api/options")
def options(origin: str="Москва"):
    origin=normalize_city(origin)
    integrations=[coverage(origin,"Москва" if origin=="Санкт-Петербург" else "Санкт-Петербург",c) for c in COMPANIES]
    return {
        "version":VERSION,"origins":ORIGINS,"destinations":[d for d in DESTINATIONS if d!=origin],
        "profiles":COMMON_PROFILES,
        "companies":[{"id":c,"label":COMPANY_LABELS[c]} for c in COMPANIES],
        "integrations":integrations,"integration_status":{x["id"]:x["coverage_label"] for x in integrations},
    }

@app.get("/api/compare")
def compare(origin:str,destination:str,profile:str="w100",companies:str|None=None):
    origin=normalize_city(origin); destination=normalize_city(destination)
    if profile not in PROFILE_BY_ID: raise HTTPException(400,"Неизвестный диапазон")
    selected=_companies(companies); p=PROFILE_BY_ID[profile]
    items=[quote(c,origin,destination,profile) for c in selected]
    exact=sum(1 for x in items if x.get("comparison_value") is not None and not x.get("price_is_minimum"))
    lower=sum(1 for x in items if x.get("price_is_minimum") and x.get("price") is not None)
    return {"version":VERSION,"calculated_at":_now(),"origin":origin,"destination":destination,"profile_id":profile,
            "range_weight":p["range_weight"],"comparison_unit":"₽","items":items,"exact_count":exact,"lower_bound_count":lower}

@app.get("/api/profile-matrix")
def profile_matrix(origin:str,destination:str,companies:str|None=None):
    origin=normalize_city(origin); destination=normalize_city(destination); selected=_companies(companies)
    return {"version":VERSION,"origin":origin,"destination":destination,"profiles":matrix(origin,destination,selected)}

class CollectRequest(BaseModel):
    companies:list[str]|None=None
    origin:str="Санкт-Петербург"
    destination:str="Москва"
    profile:str|None=None
    strict_only:bool=True
    background:bool=True

def _run_collect(key:str,selected:list[str],origin:str,destination:str):
    global EXACT_REVISION
    job=COLLECT_JOBS[key]
    job.update({"status":"running","started_at":_now(),"message":"Обновляю независимые адаптеры…"})
    try:
        if is_target_route(origin,destination):
            # Only companies whose source genuinely needs live route-form extraction.
            live=[c for c in selected if c in {"CTSgroup","Новая Линия"}]
            job["targets"]=live; job["progress_rows"]=0
            results=collect_selected(live)
            job["results"]=results; job["progress_rows"]=sum(int(x.get("rows") or 0) for x in results)
            success=[x for x in results if x.get("ok")]
            with COLLECT_LOCK:
                EXACT_REVISION+=len(success)
                job["exact_revision"]=EXACT_REVISION
            failed=[x for x in results if not x.get("ok")]
            if failed:
                job["message"]="Обновление завершено. Рабочие значения сохранены; неуспешные источники не затронули таблицу. " + " | ".join(x.get("message","") for x in failed)
            else:
                job["message"]="Обновление завершено: валидированные маршруты сохранены."
        else:
            job["message"]="Для маршрутов вне Санкт-Петербург → Москва используется режим совместимости; v40 не запускает старый массовый сбор автоматически."
            job["results"]=[]
        job["status"]="done"; job["finished_at"]=_now()
    except Exception as exc:
        job.update({"status":"error","finished_at":_now(),"message":f"Ошибка обновления: {type(exc).__name__}: {exc}"})

@app.post("/api/collect")
def collect(body:CollectRequest):
    origin=normalize_city(body.origin); destination=normalize_city(body.destination); key=_route_key(origin,destination)
    selected=[c for c in (body.companies or COMPANIES) if c in COMPANIES]
    with COLLECT_LOCK:
        current=COLLECT_JOBS.get(key)
        if current and current.get("status") in {"queued","running"}:
            return {"ok":True,"status":current["status"],"message":"Обновление уже выполняется"}
        COLLECT_JOBS[key]={"status":"queued","created_at":_now(),"origin":origin,"destination":destination,"progress_rows":0,"exact_revision":EXACT_REVISION,"message":"Поставлено в очередь"}
    t=threading.Thread(target=_run_collect,args=(key,selected,origin,destination),daemon=True); t.start()
    return {"ok":True,"status":"queued","message":"Запущено независимое обновление источников"}

@app.get("/api/collect-status")
def collect_status(origin:str,destination:str):
    key=_route_key(origin,destination)
    job=COLLECT_JOBS.get(key)
    if not job:return {"status":"idle","progress_rows":0,"exact_revision":EXACT_REVISION,"message":"Обновление ещё не запускалось"}
    return job

@app.get("/api/diagnostics")
def diagnostics(origin:str="Санкт-Петербург",destination:str="Москва"):
    origin=normalize_city(origin); destination=normalize_city(destination)
    items=[quote(c,origin,destination,"w100") for c in COMPANIES]
    exact=[x["company"] for x in items if x.get("comparison_value") is not None and not x.get("price_is_minimum")]
    lower=[x["company"] for x in items if x.get("price_is_minimum") and x.get("price") is not None]
    missing=[x["company"] for x in items if x.get("comparison_value") is None and not (x.get("price_is_minimum") and x.get("price") is not None)]
    tail=[]
    try:
        tail=LOG_PATH.read_text(encoding="utf-8",errors="replace").splitlines()[-30:]
    except Exception:
        pass
    live_path=RUNTIME_DIR/"v40_spb_moscow_live.json"
    return {
        "ok":True,"version":VERSION,"engine":"v40_clean_route_engine",
        "route":f"{origin} → {destination}","profile":"w100",
        "summary":{"exact":len(exact),"lower_bound":len(lower),"missing":len(missing)},
        "exact_companies":exact,"lower_bound_companies":lower,"missing_companies":missing,
        "target_route_clean_engine":is_target_route(origin,destination),
        "live_update_exists":live_path.exists(),
        "collector_policy":{"CTSgroup":"HTML/form → curl/PowerShell/WinHTTP → validated official SPB XLSX → headless","Новая Линия":"direction-specific NL002 PDF → tolerant Moscow row parser","all_others":"validated route pack; failed refresh cannot erase it"},
        "collect_log_tail":tail,
    }

@app.get("/api/settings")
def settings_get():
    cfg=_settings()
    return {"vozovoz_api_key":bool(cfg.get("vozovoz_api_key")),"baikal_api_key":bool(cfg.get("baikal_api_key")),"baikal_api_url":cfg.get("baikal_api_url") or ""}

@app.post("/api/settings")
async def settings_post(request:Request):
    data=await request.json(); allowed={k:data.get(k) for k in ("vozovoz_api_key","baikal_api_key","baikal_api_url") if data.get(k)}
    _save_settings(allowed); return {"ok":True}

@app.get("/api/export/excel")
def export_excel(origin:str,destination:str,profile:str="w100",companies:str|None=None,view:str="total"):
    from io import BytesIO
    from openpyxl import Workbook
    selected=_companies(companies); rows=matrix(normalize_city(origin),normalize_city(destination),selected)
    per_kg=str(view).lower()=="per_kg"
    wb=Workbook(); ws=wb.active; ws.title="Тарифы"
    ws.append(["Маршрут",f"{normalize_city(origin)} → {normalize_city(destination)}"])
    ws.append(["Версия",VERSION])
    ws.append(["Режим","Эффективные ₽/кг = стоимость отправки / контрольный вес" if per_kg else "Стоимость отправки, ₽"])
    if per_kg: ws.append(["Примечание","МИН и официальные цены «от» остаются в ₽ и не превращаются в выдуманную ставку ₽/кг."])
    ws.append([])
    ws.append(["Диапазон","Тип","Ед."]+[COMPANY_LABELS[c] for c in selected])
    for row in rows:
        p=row["profile"]; row_per_kg=per_kg and not p.get("is_minimum_profile"); unit="₽/кг" if row_per_kg else "₽"
        vals=[]
        for item in row["items"]:
            if item.get("comparison_value") is not None:
                if row_per_kg:
                    v=item.get("effective_rate_per_kg")
                    if v is None and float(p.get("weight_kg") or 0)>0: v=float(item["comparison_value"])/float(p["weight_kg"])
                    vals.append(round(float(v),4) if v is not None else "нет данных")
                else: vals.append(item["comparison_value"])
            elif item.get("price_is_minimum") and item.get("price") is not None:
                vals.append(f"от {item['price']:.0f} ₽")
            else: vals.append("нет данных")
        ws.append([p["range_weight"],p["tariff_type"],unit]+vals)
    ws.freeze_panes="D6" if per_kg else "D5"
    out=BytesIO(); wb.save(out)
    suffix="perkg" if per_kg else "total"
    return Response(out.getvalue(),media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",headers={"Content-Disposition":f"attachment; filename=tariffs_v402_{suffix}.xlsx"})
