const $ = (id) => document.getElementById(id);
const numeric = (v) => v !== null && v !== undefined && v !== '' && Number.isFinite(Number(v));
const escapeHtml = (v) => String(v ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const fmt = (v, unit='₽') => numeric(v) ? `${new Intl.NumberFormat('ru-RU', {maximumFractionDigits: 2}).format(Number(v))} ${unit}` : '—';
const fmtMoney = (v) => numeric(v) ? `${new Intl.NumberFormat('ru-RU', {maximumFractionDigits: 2}).format(Number(v))} ₽` : '—';
const cleanText = (v) => String(v ?? '').replace(/<[^>]*>/g, ' ').replace(/\s+/g, ' ').trim();
const truncate = (v, n=150) => { const t=cleanText(v); return t.length>n ? `${t.slice(0,n-1)}…` : t; };

const state = {
  workspace:typeof initWorkspace==='function'?'bulk':'route',
  options:null, comparison:null, matrix:null, busy:false, bulkActive:false,
  compareSeq:0, compareController:null, optionsSeq:0, optionsController:null,
  refreshingRoutes:new Set(), refreshedRoutes:new Set(),
  companiesInitialized:false, refreshLocked:false, calculationCompanies:new Set(), hiddenGraphCompanies:new Set(), graphScope:'small', unitMode:'total', liveOnly:false, includeImports:true
};

function toast(message){ const n=$('toast'); n.textContent=message; n.classList.add('visible'); clearTimeout(window.__toast); window.__toast=setTimeout(()=>n.classList.remove('visible'),3600); }
async function getJSON(url, options={}){
  const controller=new AbortController();
  const cancel=()=>controller.abort();
  options.signal?.addEventListener('abort',cancel,{once:true});
  const timer=setTimeout(cancel,options.timeout||20000);
  try{
    const r=await fetch(url,{...options,signal:controller.signal,cache:'no-store'});
    const d=await r.json().catch(()=>({}));
    if(!r.ok) throw new Error(typeof d.detail==='string'?d.detail:(d.error||`HTTP ${r.status}`));
    return d;
  }finally{clearTimeout(timer);options.signal?.removeEventListener('abort',cancel);}
}
function setBusy(v){ state.busy=v; document.body.classList.toggle('loading',v); $('compareButton').textContent=v?'Считаем…':'Сравнить'; }
function queryString(){ return new URLSearchParams({origin:$('originSelect').value,destination:$('destinationSelect').value,profile:$('profileSelect').value,companies:[...state.calculationCompanies].join(','),view:state.unitMode,live_only:state.liveOnly?'true':'false',include_imports:state.includeImports?'true':'false'}).toString(); }
function setRefreshRouteLock(locked, origin='', destination=''){
  state.refreshLocked=!!locked;
  document.querySelectorAll('#calculationCompanySelector input').forEach(el=>el.disabled=!!locked);
  syncRetryButtons();
  const ids=['destinationSelect','originSelect','profileSelect','swapRouteButton','selectAllCompaniesButton','clearCompaniesButton'];
  ids.forEach(id=>{const el=$(id); if(el) el.disabled=!!locked;});
  const btn=$('collectButton');
  if(btn){ btn.disabled=!!locked||state.bulkActive; btn.textContent=state.bulkActive?'Идёт общий сбор':locked?`Обновляется ${origin} → ${destination}…`:'Обновить маршрут'; }
}
function rateProfile(profile){return !profile?.is_minimum_profile && Number(profile?.weight_kg)>=100;}
function activeUnit(profile){ return state.unitMode==='per_kg' && !profile?.is_minimum_profile ? '₽/кг' : '₽'; }
function exactViewValue(item, profile){
  if(item?.price_is_minimum)return null;
  let value=item?.comparison_value;
  if(state.unitMode==='per_kg' && !profile?.is_minimum_profile){
    if(!numeric(value) || !(Number(profile?.weight_kg)>0))return null;
    value=Number(value)/Number(profile.weight_kg);
  }
  return numeric(value)?Number(value):null;
}
function updateUnitLabels(){
  const note=$('unitModeNote');if(note)note.textContent=state.unitMode==='per_kg'?'₽/кг = стоимость отправки ÷ контрольный вес. Это расчётная величина, включая минимальную плату. МИН всегда в ₽.':'Показана стоимость всей отправки на контрольном весе. МИН — минимальная стоимость отправления по компании.';
  for(const [key,label] of Object.entries({small:'0–50 кг',medium:'100–1500 кг',heavy:'1500–5000 кг'})){const option=$('graphScopeSelect')?.querySelector(`option[value=${key}]`);if(option)option.textContent=label+' · '+(state.unitMode==='per_kg'?'расчётная стоимость, ₽/кг':'₽ за отправку');}
  const ps=$('profileSelect'); const profiles=state.options?.profiles||[];
  if(ps && profiles.length){ const selected=ps.value; ps.innerHTML=profiles.map(p=>`<option value="${escapeHtml(p.id)}">${escapeHtml(p.description)} · ${escapeHtml(p.is_minimum_profile?'Минимум':state.unitMode==='per_kg'?'Расчётная стоимость':'Стоимость отправки')} · ${escapeHtml(activeUnit(p))}</option>`).join(''); if(profiles.some(p=>p.id===selected)) ps.value=selected; }
}
function rerenderUnitMode(){ updateUnitLabels(); if(state.comparison) renderComparison(state.comparison); if(state.matrix){ renderMatrix(state.matrix); renderTariffGraph(state.matrix); } }


async function loadOptions(origin='Москва',destination=$('destinationSelect').value){
  const seq=++state.optionsSeq;
  if(state.optionsController) state.optionsController.abort();
  const controller=new AbortController(); state.optionsController=controller;
  let d;
  try{ d=await getJSON(`/api/options?${new URLSearchParams({origin,catalog:$('extendedRoutesToggle')?.checked?'all':'main',...(destination?{destination}:{})})}`,{signal:controller.signal}); }
  catch(e){ if(e?.name==='AbortError') return false; throw e; }
  if(seq!==state.optionsSeq) return false;
  state.options=d;
  if($('extendedRoutesToggle'))$('extendedRoutesToggle').checked=d.catalog==='all';
  const valid=new Set(d.companies.map(x=>x.id));
  if(!state.companiesInitialized){d.companies.forEach(x=>state.calculationCompanies.add(x.id));state.companiesInitialized=true;}
  state.calculationCompanies=new Set([...state.calculationCompanies].filter(x=>valid.has(x)));
  $('originSelect').innerHTML=d.origins.map(x=>`<option>${escapeHtml(x)}</option>`).join(''); $('originSelect').value=d.selected_origin||origin;
  $('destinationSelect').innerHTML=d.destinations.map(x=>`<option>${escapeHtml(x)}</option>`).join(''); $('destinationSelect').value=d.selected_destination||d.paired_destination||d.destinations[0]; $('destinationSelect').disabled=state.refreshLocked;
  const oldP=$('profileSelect').value||'w100'; $('profileSelect').innerHTML=d.profiles.map(p=>`<option value="${escapeHtml(p.id)}">${escapeHtml(p.description)} · ${escapeHtml(p.tariff_type||'Тариф')} · ${escapeHtml(p.unit||'')}</option>`).join(''); $('profileSelect').value=d.profiles.some(p=>p.id===oldP)?oldP:'w100';
  renderCompanySelector(); renderIntegrations(d.integration_status); updateUnitLabels();
  return true;
}

function renderCompanySelector(){
  const host=$('calculationCompanySelector');
  host.innerHTML=(state.options?.companies||[]).map(c=>`<label class="company-option ${state.calculationCompanies.has(c.id)?'active':''}"><input type="checkbox" ${state.refreshLocked?'disabled':''} value="${escapeHtml(c.id)}" ${state.calculationCompanies.has(c.id)?'checked':''}><span>${escapeHtml(c.label)}</span></label>`).join('');
  host.querySelectorAll('input').forEach(input=>input.addEventListener('change',()=>{ if(input.checked) state.calculationCompanies.add(input.value); else state.calculationCompanies.delete(input.value); renderCompanySelector(); renderIntegrations(state.options?.integration_status||{}); }));
  $('calculationSelectionMessage').textContent=`${state.calculationCompanies.size} из ${(state.options?.companies||[]).length}`;
}
function setAllCompanies(on){ state.calculationCompanies.clear(); if(on)(state.options?.companies||[]).forEach(c=>state.calculationCompanies.add(c.id)); renderCompanySelector(); renderIntegrations(state.options?.integration_status||{}); }

function visiblePrice(item){return !state.liveOnly || item?.online || (state.includeImports && item?.uploaded);}
function statusLabel(item){
  if(item?.uploaded)return item.document_selected?'Выбранный файл':'Файл пользователя';
  if(item?.online) return item?.price_is_minimum?'Обновлено · цена «от»':'Обновлено · LIVE';
  if(item?.refresh_status==='unavailable' && item?.status!=='ok') return 'Прайс маршрута не опубликован';
  const failed=item?.refresh_status==='failed';
  if(item?.price_is_minimum) return failed?'Не обновилось · сохранённая цена «от»':'Сохранённая цена «от»';
  if(item?.status==='ok') return item?.refresh_status==='running'?'Обновляется · прежняя цена':failed||item?.latest_availability||item?.refresh_status==='partial'?'Не обновилось · прежняя цена':'Сохранённая цена';
  if(item?.status==='document_unavailable'||item?.status==='unavailable') return failed?'Не загрузилось · сохранённой цены нет':'Нет подтверждённой строки';
  return item?.status||'Нет данных';
}
function errorBlock(info, raw=''){
  if(!info&&!raw)return '';
  const title=info?.summary||'Ошибка загрузки';
  const hint=info?.hint||'Откройте источник или повторите загрузку.';
  return `<div class="error-help"><strong>${escapeHtml(title)}</strong><span>${escapeHtml(hint)}</span><details><summary>Технические подробности</summary><pre>${escapeHtml(info?.technical||raw)}</pre></details></div>`;
}
function syncRetryButtons(){
  document.querySelectorAll('[data-retry-company]').forEach(btn=>{btn.disabled=state.refreshLocked;});
  const retry=$('retryFailedButton');
  if(retry){
    retry.disabled=state.refreshLocked;
    retry.hidden=state.refreshLocked||!(state.comparison?.items||[]).some(x=>['failed','partial'].includes(x.refresh_status)&&state.calculationCompanies.has(x.company));
  }
}
function sourceBlock(item){
  const url=item?.source_url; const note=truncate(item?.message||item?.formula||'',180);
  const hasData=numeric(item?.comparison_value)||(item?.price_is_minimum&&numeric(item?.price));
  const mode=item?.uploaded?'ФАЙЛ ПОЛЬЗОВАТЕЛЯ':item?.online?'LIVE':(hasData?'СОХРАНЁННАЯ ЦЕНА':(item?.refresh_status==='failed'?'ОШИБКА ЗАГРУЗКИ':'НЕ ЗАГРУЖЕНО'));
  const file=item?.source_file?`<a href="${item.uploaded?'/api/import-file/':'/api/source-file/'}${encodeURIComponent(item.source_file)}" target="_blank" rel="noopener">${item.uploaded?escapeHtml(item.original_filename||'Загруженный документ'):'Скачанный прайс'}</a>`:'';
  const taxes=item?.tax_basis?`<span>${escapeHtml(item.tax_basis)}</span>`:'';
  const basis=item?.calculation_basis?`<span>${escapeHtml(item.calculation_basis)}</span>`:'';
  const error=item?.refresh_error?errorBlock(item.error_info,item.refresh_error):'';
  const documentDate=item?.uploaded?`<span>Дата в документе: ${escapeHtml(item.document_date||'не распознана')}</span>`:'';
  const time=item?.captured_at?` · ${escapeHtml(new Date(item.captured_at).toLocaleString('ru-RU'))}`:'';
  const transport=item?.origin_terminal?` · Терминал отправления: ${escapeHtml(item.origin_terminal)}`:'';
  return `<div class="source-block"><strong>${escapeHtml(item?.source_type||'Источник')}</strong><span><b>${escapeHtml(mode)}</b>${time}${transport}${note?` · ${escapeHtml(note)}`:''}</span>${url?`<a href="${escapeHtml(url)}" target="_blank" rel="noopener">Открыть источник</a>`:''}${file}${documentDate}${taxes}${basis}${error}</div>`;
}

function graphColor(index){ const light=['#007565','#896000','#3457c4','#ab326f','#067699','#7046bd','#248443','#af5317','#816900','#007e88','#1e795d','#8750a7','#b23f43','#336db0','#567a09','#865baa','#b63c72','#157b73','#996327','#515ba3','#4c596a']; const palette=document.documentElement.dataset.theme==='dark'?['#46F0D2','#FBE2B4','#8BA4FF','#F59AC8','#7DD3FC','#C4B5FD','#86EFAC','#FDBA74','#FDE047','#67E8F9','#A7F3D0','#D8B4FE','#FCA5A5','#93C5FD','#BEF264','#E9D5FF','#F9A8D4','#99F6E4','#FED7AA','#C7D2FE','#E5E7EB']:light; return palette[index % palette.length]; }
function graphSegments(points){
  const segments=[]; let current=[];
  points.forEach(p=>{ if(p){ current.push(p); } else if(current.length){ segments.push(current); current=[]; } });
  if(current.length) segments.push(current);
  return segments;
}
function renderTariffGraph(data){
  const host=$('rateChart');
  const legend=$('graphLegend');
  if(!host || !legend) return;
  const scope=state.graphScope||'small';
  let rows=(data?.profiles||[]).filter(r=>!r.profile?.is_minimum_profile);
  const [minWeight,maxWeight]=({small:[0,50],medium:[100,1500],heavy:[1500,5000]})[scope]||[0,50];
  rows=rows.filter(r=>Number(r.profile?.weight_kg)>0&&Number(r.profile.weight_kg)>=minWeight&&Number(r.profile.weight_kg)<=maxWeight);
  const companies=(state.options?.companies||[]).filter(c=>state.calculationCompanies.has(c.id));
  if(!rows.length || !companies.length){ host.innerHTML='<div class="empty">Нет данных для выбранного масштаба графика.</div>'; legend.innerHTML=''; return; }
  const series=companies.map((company,idx)=>{
    const values=rows.map(row=>{
      const item=(row.items||[]).find(x=>x.company===company.id);
      const v=item && !item.price_is_minimum && visiblePrice(item) ? exactViewValue(item,row.profile) : null;
      return numeric(v) ? Number(v) : null;
    });
    return {company,...company,color:graphColor(idx),values,count:values.filter(numeric).length};
  });
  legend.innerHTML=series.map(s=>`<button type="button" class="graph-legend-item ${s.count?'':'no-data'} ${state.hiddenGraphCompanies.has(s.id)?'hidden-series':''}" data-company="${escapeHtml(s.id)}" ${s.count?'':'disabled'}><i style="--series-color:${s.color}"></i><span>${escapeHtml(s.label)}</span><small>${s.count?`${s.count} точек`:'нет точной линии'}</small></button>`).join('');
  legend.querySelectorAll('button:not([disabled])').forEach(btn=>btn.addEventListener('click',()=>{ const id=btn.dataset.company; if(state.hiddenGraphCompanies.has(id)) state.hiddenGraphCompanies.delete(id); else state.hiddenGraphCompanies.add(id); renderTariffGraph(data); }));

  const visible=series.filter(s=>s.count && !state.hiddenGraphCompanies.has(s.id));
  const allValues=visible.flatMap(s=>s.values.filter(numeric));
  if(!allValues.length){ host.innerHTML=`<div class="empty">${series.some(s=>s.count)?'Все линии скрыты. Нажмите на компанию в легенде, чтобы вернуть её на график.':'Нет подтверждённых точек для выбранного масштаба и фильтра свежести.'}</div>`; return; }
  const width=Math.max(1000, rows.length*72); const height=520;
  const m={left:72,right:28,top:26,bottom:112}; const plotW=width-m.left-m.right; const plotH=height-m.top-m.bottom;
  const rawMax=Math.max(...allValues); const rawMin=Math.min(...allValues);
  const step=rawMax>100000?20000:rawMax>50000?10000:rawMax>10000?2000:rawMax>2000?500:rawMax>500?100:20;
  const yMax=Math.max(step,Math.ceil(rawMax/step)*step);
  const yMin=0;
  const x=(i)=>m.left+(rows.length===1?plotW/2:(i/(rows.length-1))*plotW);
  const y=(v)=>m.top+plotH-((Number(v)-yMin)/(yMax-yMin))*plotH;
  const grid=[]; const ticks=5;
  for(let i=0;i<=ticks;i++){
    const value=yMin+(yMax-yMin)*(i/ticks); const yy=y(value);
    grid.push(`<line x1="${m.left}" x2="${width-m.right}" y1="${yy}" y2="${yy}" class="graph-grid-line"/><text x="${m.left-12}" y="${yy+4}" text-anchor="end" class="graph-y-label">${escapeHtml(new Intl.NumberFormat('ru-RU',{maximumFractionDigits:1}).format(value))}</text>`);
  }
  const selectedId=state.comparison?.profile_id; const selectedIndex=rows.findIndex(r=>r.profile?.id===selectedId);
  const marker=selectedIndex>=0?`<rect x="${Math.max(m.left,x(selectedIndex)-22)}" y="${m.top}" width="44" height="${plotH}" class="graph-selected-band"/><line x1="${x(selectedIndex)}" x2="${x(selectedIndex)}" y1="${m.top}" y2="${m.top+plotH}" class="graph-selected-line"/>`:'';
  const xLabels=rows.map((row,i)=>`<g transform="translate(${x(i)},${m.top+plotH+16}) rotate(48)"><text class="graph-x-label" text-anchor="start">${escapeHtml(row.profile?.range_weight||row.profile?.label||'')}</text></g>`).join('');
  const paths=[];
  visible.forEach(s=>{
    const points=s.values.map((v,i)=>numeric(v)?{x:x(i),y:y(v),v:Number(v),label:rows[i].profile?.range_weight}:null);
    graphSegments(points).forEach(seg=>{ if(seg.length>=2){ const d=seg.map((p,i)=>`${i?'L':'M'} ${p.x.toFixed(1)} ${p.y.toFixed(1)}`).join(' '); paths.push(`<path d="${d}" class="graph-series-line" style="--series-color:${s.color}"/>`); } });
    points.filter(Boolean).forEach(p=>paths.push(`<circle cx="${p.x}" cy="${p.y}" r="4" class="graph-series-point" style="--series-color:${s.color}"><title>${escapeHtml(s.label)} · ${escapeHtml(p.label)} · ${escapeHtml(fmt(p.v,activeUnit(rows[0]?.profile)))}</title></circle>`));
  });
  const graphUnit=activeUnit(rows[0]?.profile);
  const scopeText=`${minWeight}–${maxWeight} кг · ${graphUnit}`;
  host.innerHTML=`<div class="graph-scroll"><svg class="tariff-graph" viewBox="0 0 ${width} ${height}" width="${width}" height="${height}" role="img" aria-label="График тарифов компаний по весовым диапазонам"><text x="18" y="${m.top+plotH/2}" transform="rotate(-90 18 ${m.top+plotH/2})" class="graph-axis-title">${graphUnit}</text>${marker}${grid.join('')}<line x1="${m.left}" x2="${width-m.right}" y1="${m.top+plotH}" y2="${m.top+plotH}" class="graph-axis"/>${paths.join('')}${xLabels}</svg></div><div class="graph-note">Масштаб: ${scopeText}. Минимум по видимым точкам ${fmt(rawMin,graphUnit)}, максимум ${fmt(rawMax,graphUnit)}. Строка «МИН» и цены «от» не строятся. Текстовая пометка означает отсутствие сопоставимой числовой ставки, а не ноль.</div>`;
}
function renderComparison(data){
  state.comparison=data;
  $('updatedAt').textContent=`Расчёт: ${new Date(data.calculated_at).toLocaleString('ru-RU')}`;
  const selectedProfile=(state.options?.profiles||[]).find(p=>p.id===data.profile_id)||{}; const unit=activeUnit(selectedProfile); $('summaryTitle').textContent=`${data.range_weight} · ${selectedProfile.tariff_type||'Тариф'} · ${unit}`;
  $('summaryRoute').textContent=`${data.origin} → ${data.destination}`;
  const items=data.items||[];
  const selected=items.length;
  const visibleItems=items.filter(visiblePrice);
  const exact=visibleItems.filter(x=>numeric(exactViewValue(x,selectedProfile))&&!x.price_is_minimum).length;
  const lower=visibleItems.filter(x=>x.price_is_minimum&&numeric(x.price)).length;
  const online=Number(data.online_count ?? items.filter(x=>x.online&&(numeric(x.comparison_value)||(x.price_is_minimum&&numeric(x.price)))).length);
  const onlineExact=items.filter(x=>x.online&&numeric(exactViewValue(x,selectedProfile))&&!x.price_is_minimum).length;
  const onlineLower=Number(data.online_lower_bound_count ?? items.filter(x=>x.online&&x.price_is_minimum&&numeric(x.price)).length);
  const missing=Math.max(0,selected-exact-lower);
  $('selectedCount').textContent=String(selected);
  $('exactCount').textContent=String(exact);
  if($('onlineCount')) $('onlineCount').textContent=String(online); if($('onlineExactCount')) $('onlineExactCount').textContent=String(onlineExact); if($('onlineLowerCount')) $('onlineLowerCount').textContent=String(onlineLower);
  $('lowerCount').textContent=String(lower);
  $('missingCount').textContent=String(missing);
  if($('importedCount'))$('importedCount').textContent=visibleItems.filter(x=>x.uploaded).length;
  $('missingLabel').textContent=state.liveOnly?(state.includeImports?'нет LIVE / файла':'нет LIVE'):'нет строки';
  $('valueHeader').textContent=unit;
  $('comparisonTable').innerHTML=items.map(item=>{
    const hiddenByFreshness=!visiblePrice(item);
    const exactValue=hiddenByFreshness?null:exactViewValue(item,selectedProfile);
    let value=item.availability==='on_request'?'по запросу':hiddenByFreshness?(numeric(item.price)?'скрыто — не LIVE':'нет данных'):(numeric(exactValue)?fmt(exactValue,unit):(item.display_text||'нет данных'));
    let lowerNote='';
    if(!hiddenByFreshness&&!numeric(exactValue)&&item.price_is_minimum&&numeric(item.price)){
      value=activeUnit(selectedProfile)==='₽/кг'?'нет точной ставки':(item.display_text||'нет данных');
      lowerNote=`<div class="lower-note">от ${fmtMoney(item.price)}${activeUnit(selectedProfile)==='₽/кг'?' · цена «от» не переводится в ₽/кг без точной весовой строки':''}</div>`;
    }
    if(!hiddenByFreshness&&!numeric(exactValue)&&numeric(item.comparison_value)&&activeUnit(selectedProfile)==='₽/кг'){value='нет ставки';lowerNote=`<div class="lower-note">Источник даёт сумму отправки: ${fmtMoney(item.comparison_value)}. Добавьте прайс со ставкой за кг.</div>`;}
    return `<tr><td class="company-cell"><strong>${escapeHtml(item.company_label)}</strong></td><td class="value-cell">${value}${lowerNote}</td><td><span class="status-pill">${escapeHtml(hiddenByFreshness?(item.uploaded?'Файл скрыт фильтром':(numeric(item.price)||numeric(item.comparison_value))?'LAST GOOD скрыт':'нет LIVE'):statusLabel(item))}</span></td><td>${sourceBlock(item)}${['failed','partial'].includes(item.refresh_status)?`<button type="button" class="text-button" data-retry-company="${escapeHtml(item.company)}" ${state.refreshLocked?'disabled':''}>Повторить загрузку</button>`:''}</td></tr>`;
  }).join('')||'<tr><td colspan="4" class="empty">Нет данных.</td></tr>';
  $('comparisonTable').querySelectorAll('[data-retry-company]').forEach(btn=>btn.addEventListener('click',()=>refreshRouteSources(true,[btn.dataset.retryCompany])));
  syncRetryButtons();
}

function renderMatrix(data){
  document.querySelectorAll('#comparisonTable tr').forEach(row=>[...row.children].forEach((td,i)=>td.dataset.label=['Компания','Стоимость','Статус','Источник'][i]||''));
  state.matrix=data; const companies=(state.options?.companies||[]).filter(c=>state.calculationCompanies.has(c.id));
  $('matrixHead').innerHTML=`<tr><th>Диапазон</th><th>Тип тарифа</th><th>Ед.</th>${companies.map(c=>`<th>${escapeHtml(c.label)}</th>`).join('')}</tr>`;
  $('matrixBody').innerHTML=(data.profiles||[]).map(row=>{
    const by=Object.fromEntries((row.items||[]).map(x=>[x.company,x]));
    const rowUnit=activeUnit(row.profile);
    const cells=companies.map(c=>{ const item=by[c.id]||{}; const hiddenByFreshness=!visiblePrice(item); let text=item.availability==='on_request'?'по запросу':hiddenByFreshness?(numeric(item.price)?'—':'нет данных'):(item.display_text||'нет данных'); let cls='empty-cell'; const v=hiddenByFreshness?null:exactViewValue(item,row.profile); if(numeric(v)){ text=fmt(v,rowUnit); cls='value-matrix'; } else if(!hiddenByFreshness&&item.price_is_minimum&&numeric(item.price)){ text=rowUnit==='₽/кг'?'— (от '+fmtMoney(item.price)+')':`от ${fmtMoney(item.price)}`; cls='lower-cell'; } if(!hiddenByFreshness&&!numeric(v)&&numeric(item.comparison_value)&&rowUnit==='₽/кг')text='нет ставки'; return `<td class="${cls}" title="${escapeHtml([statusLabel(item),item.captured_at?new Date(item.captured_at).toLocaleString('ru-RU'):'',cleanText(item.message||'')].filter(Boolean).join(' · '))}">${escapeHtml(text)}</td>`; }).join('');
    return `<tr class="shipment-row"><td class="range-cell"><strong>${escapeHtml(row.profile.range_weight)}</strong></td><td class="type-cell">${escapeHtml(row.profile.is_minimum_profile?'Минимум':state.unitMode==='per_kg'?'Расчётная стоимость':'Стоимость отправки')}</td><td class="unit-cell">${escapeHtml(rowUnit)}</td>${cells}</tr>`;
  }).join('');
}

function renderIntegrations(statuses){
  const items=(state.options?.integrations||[]).filter(x=>state.calculationCompanies.has(x.id));
  $('integrationGrid').innerHTML=items.map(item=>{
    const rows=Number(item.evidence_rows||item.embedded_rows||0); const liveRows=Number(item.collected_rows||0); const updated=item.last_collected_at?new Date(item.last_collected_at).toLocaleString('ru-RU'):'ещё не было';
    const rs=String(item.refresh_status||'not_run'); const stateLabel=rs==='success'?(liveRows?'Обновлено':'Сохранённые цены'):rs==='partial'?'Обновлено частично':rs==='failed'?'Не обновилось':rs==='running'?'Обновляется':rs==='unavailable'?'Источник недоступен':'Ещё не обновляли';
    const err=Array.isArray(item.collection_errors)&&item.collection_errors.length?errorBlock(item.error_info,item.collection_errors[0]):'';
    return `<article class="integration-item"><div class="integration-top"><strong>${escapeHtml(item.label)}</strong><span>${escapeHtml(stateLabel)}</span></div><p>${escapeHtml(item.method||'')}</p><small>Подтверждённых строк: ${rows} · LIVE сейчас: ${liveRows} · последний успех: ${escapeHtml(updated)}</small>${item.source_url?`<a href="${escapeHtml(item.source_url)}" target="_blank" rel="noopener">Официальный источник</a>`:''}${err}</article>`;
  }).join('');
}

async function compare(){
  if(!state.calculationCompanies.size){toast('Выберите хотя бы одну компанию');return;}
  const origin=$('originSelect').value;
  const destination=$('destinationSelect').value;
  const profile=$('profileSelect').value;
  const companies=[...state.calculationCompanies];
  const seq=++state.compareSeq;
  if(state.compareController) state.compareController.abort();
  const controller=new AbortController(); state.compareController=controller;
  setBusy(true);
  try{
    const q=new URLSearchParams({origin,destination,profile,companies:companies.join(',')}).toString();
    const mq=new URLSearchParams({origin,destination,companies:companies.join(',')}).toString();
    const [cmp,matrix]=await Promise.all([
      getJSON(`/api/compare?${q}`,{signal:controller.signal}),
      getJSON(`/api/profile-matrix?${mq}`,{signal:controller.signal})
    ]);
    if(seq!==state.compareSeq) return;
    if($('originSelect').value!==origin || $('destinationSelect').value!==destination || $('profileSelect').value!==profile) return;
    renderComparison(cmp); renderMatrix(matrix); renderTariffGraph(matrix);
    if(typeof refreshRouteDocuments==='function')refreshRouteDocuments(origin,destination);
  }catch(e){
    if(e?.name!=='AbortError' && seq===state.compareSeq) toast(e.message||'Ошибка расчёта');
  }finally{
    if(seq===state.compareSeq) setBusy(false);
  }
}
function renderJob(job){
  state.lastJob=job;
  const results=job.results||[];
  const retry=$('retryFailedButton');
  retry.hidden=!results.some(r=>!r.ok)||['running','queued'].includes(job.status);
  syncRetryButtons();
  const total=(job.requested_companies||[]).length;
  const done=Number(job.completed_companies??results.length);
  $('liveAuditTitle').textContent=`${job.origin} → ${job.destination}: ${job.status==='done'?'проверка завершена':job.status==='error'?'ошибка проверки':'загрузка'} · ${done}/${total} компаний`;
  $('liveAuditText').textContent=job.status==='done'
    ?`Обновлено: ${job.success_count||0}. Ошибок: ${job.failed_count||0}. Нет опубликованного прайса маршрута: ${job.unavailable_count||0}. Неполных таблиц: ${job.partial_count||0}. Прайсы и открытые API заполняют доступные веса; остальные калькуляторы — выбранный вес. Подробности по каждой компании ниже.`
    :'Полученные прайсы появляются сразу. Можно закрыть страницу и вернуться — загрузка продолжится на сервере.';
  $('companyProgress').innerHTML=(job.requested_companies||[]).map(c=>{
    const result=results.find(x=>x.company===c);
    return `<div class="company-progress-row"><strong>${escapeHtml(c)}</strong><span>${result?(result.ok?`Получено диапазонов: ${result.rows}`:(result.error_info?.summary||'Ошибка загрузки')):'Ожидание ответа'}</span>${result&&!result.ok?errorBlock(result.error_info,result.message):''}</div>`;
  }).join('');
}
async function monitorJob(job){
  const origin=job.origin,destination=job.destination,key=`${origin}|${destination}`;
  state.refreshingRoutes.add(key);setRefreshRouteLock(true,origin,destination);
  let revision=-1,errors=0;
  try{
    while(true){
      let d;
      try{d=await getJSON(`/api/collect-status?origin=${encodeURIComponent(origin)}&destination=${encodeURIComponent(destination)}`);errors=0;}
      catch(e){
        errors++;$('liveAuditText').textContent='Связь с приложением прервана. Повторяю проверку состояния…';
        if(errors>=5)throw e;
        await new Promise(resolve=>setTimeout(resolve,3000));continue;
      }
      renderJob(d);
      const next=Number(d.progress_revision||0);
      if(next!==revision||d.status==='done'||d.status==='error'){
        revision=next;
        if($('originSelect').value===origin && $('destinationSelect').value===destination){await loadOptions(origin,destination);await compare();}
      }
      if(!['queued','running'].includes(d.status))break;
      await new Promise(resolve=>setTimeout(resolve,2000));
    }
  }catch(e){$('liveAuditText').textContent=`Не удалось получить состояние: ${e.message}. При восстановлении связи проверка возобновится.`;}
  finally{state.refreshingRoutes.delete(key);setRefreshRouteLock(false);}
}
async function refreshRouteSources(force=false,onlyCompanies=null){
  if(state.bulkActive||state.refreshingRoutes.size||!state.calculationCompanies.size)return;
  const origin=$('originSelect').value,destination=$('destinationSelect').value;
  setRefreshRouteLock(true,origin,destination);
  try{
    const d=await getJSON('/api/collect',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({companies:onlyCompanies||[...state.calculationCompanies],origin,destination,profile:$('profileSelect').value,force})});
    if(d.status==='fresh'){
      const last=await getJSON(`/api/collect-status?origin=${encodeURIComponent(origin)}&destination=${encodeURIComponent(destination)}`);
      if(last.status!=='idle')renderJob(last);
      else {$('liveAuditTitle').textContent='Онлайн-данные уже проверены';$('liveAuditText').textContent=d.message;}
      return;
    }
    if(d.status==='busy'){
      const active=await getJSON('/api/active-collect');
      if(active.mode==='bulk'){setRefreshRouteLock(false);await monitorBulk(active.bulk_job_id);}
      else if(active.status!=='idle')await monitorJob(active);
    }else await monitorJob({...d,origin,destination});
  }catch(e){$('liveAuditTitle').textContent='Не удалось начать загрузку';$('liveAuditText').textContent=e.message;toast(e.message);}
  finally{if(!state.refreshingRoutes.size)setRefreshRouteLock(false);}
}
async function updateSources(){await refreshRouteSources(true);}
async function resumeRefresh(){
  const active=await getJSON('/api/active-collect');
  if(active.mode==='bulk'){await monitorBulk(active.bulk_job_id);return;}
  if(active.status!=='idle'){await loadOptions(active.origin,active.destination);await compare();await monitorJob(active);return;}
  await compare();
}

function exportExcel(){ window.location.href=`/api/export/route?${queryString()}`; }

const bulkState={job:null,monitoring:false,requesting:false,planSeq:0,planReady:false};
function bulkBody(){
  const scope=$('bulkScope').value;
  const origins=[...$('bulkOrigins').selectedOptions].map(x=>x.value);
  const destinations=[...$('bulkDestinations').selectedOptions].map(x=>x.value);
  return {scope,...(scope==='origins'?{origins,...(destinations.length?{destinations}:{})}:{})};
}
function bulkControls(){
  const job=bulkState.job||{};
  const active=['queued','running','pausing'].includes(job.status);
  const exporting=job.export_status==='running';
  state.bulkActive=active;
  $('bulkStartButton').disabled=active||exporting||bulkState.requesting||!bulkState.planReady;
  if($('bulkSavedButton'))$('bulkSavedButton').disabled=$('bulkStartButton').disabled;
  ['bulkScope','bulkOrigins','bulkDestinations'].forEach(id=>$(id).disabled=active||bulkState.requesting);
  $('bulkPauseButton').hidden=!active;$('bulkPauseButton').disabled=job.status==='pausing'||bulkState.requesting;
  $('bulkResumeButton').hidden=!['paused','error'].includes(job.status);$('bulkResumeButton').disabled=exporting||bulkState.requesting;
  $('bulkRetryButton').hidden=active||!(job.outcomes?.failed||job.outcomes?.partial);$('bulkRetryButton').disabled=exporting||bulkState.requesting;
  $('bulkExportButton').hidden=active||!job.completed_checks;$('bulkExportButton').disabled=exporting||bulkState.requesting;
  $('collectButton').disabled=active||state.refreshLocked;
  if(active)$('collectButton').textContent='Идёт общий сбор';else if(!state.refreshLocked)$('collectButton').textContent='Обновить прайсы';
}
async function updateBulkPlan(){
  const seq=++bulkState.planSeq;bulkState.planReady=false;
  const origins=$('bulkScope').value==='origins';
  $('bulkOriginsField').hidden=!origins;$('bulkDestinationsField').hidden=!origins;bulkControls();
  try{
    const plan=await getJSON('/api/bulk/plan',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(bulkBody())});
    if(seq!==bulkState.planSeq)return;
    const n=x=>Number(x).toLocaleString('ru-RU');
    $('bulkPlan').textContent=`${n(plan.routes)} маршрутов · ${plan.companies} компаний · ${n(plan.checks)} проверок компаний. ${plan.export_parts===1?'Один Excel по образцу.':`ZIP с ${plan.export_parts} файлами Excel по образцу.`} Каждое направление проверяется отдельно; наличие города в справочнике не означает наличие тарифа у каждой компании.`;
    bulkState.planReady=true;
  }catch(e){if(seq===bulkState.planSeq)$('bulkPlan').textContent=e.message;}
  finally{if(seq===bulkState.planSeq)bulkControls();}
}
function renderBulk(job){
  bulkState.job=job;bulkControls();if(typeof renderCoverage==='function')renderCoverage(job);
  if(job.status==='idle')return;
  const labels={queued:'В очереди',running:'Идёт сбор',pausing:'Завершаю текущий маршрут перед паузой',paused:'Пауза',done:'Проверки завершены',error:'Сбор прерван'};
  const current=job.current?` Сейчас: ${job.current.origin} → ${job.current.destination}.`:'';
  const outcomes=job.outcomes||{};
  const exportText=job.export_outdated?' Прайс-листы изменились. Пересоберите Excel с прайсами.':job.export_status==='running'?' Создаётся Excel…':job.export_status==='ready'?' Excel готов.':job.export_status==='error'?' Не удалось создать Excel. Можно повторить выгрузку.':'';
  $('bulkStatus').textContent=`${job.mode==='saved'?'Сбор из текущих данных и прайсов':(labels[job.status]||job.status)}. Маршрутов: ${job.completed_routes}/${job.total_routes}. Проверок компаний: ${job.completed_checks}/${job.total_checks}. Полная сетка: ${outcomes.complete||0}; неполные: ${outcomes.partial||0}; ошибки: ${outcomes.failed||0}; без публичного тарифа: ${outcomes.unavailable||0}. ${outcomes.saved?'Обработано из текущих данных: '+outcomes.saved+'. ':''}${current}${exportText} ${job.message||''}`;
  $('bulkProgress').hidden=false;$('bulkProgress').value=job.percent||0;
  const download=$('bulkDownload');download.hidden=!job.download_url;
  if(job.download_url){download.href=job.download_url;download.textContent=job.total_routes>400?'Скачать Excel-файлы в ZIP':'Скачать большую таблицу';}
  $('bulkResults').hidden=!job.recent?.length;
  const statuses={complete:'Все 28 весов',partial:'Неполная сетка',failed:'Ошибка',unavailable:'Нет публичного тарифа',saved:'Текущие данные и прайсы'};
  $('bulkRecent').innerHTML=(job.recent||[]).map(r=>`<div class="company-progress-row"><strong>${escapeHtml(r.company)} · ${escapeHtml(statuses[r.status]||r.status)}</strong><span>${escapeHtml(r.origin)} → ${escapeHtml(r.destination)}</span><span>Точных весов: ${Number(r.exact_weights)||0}/28 · по запросу: ${Number(r.on_request)||0}</span><small>${escapeHtml(r.checked_at||'')}</small><details><summary>Ответ источника</summary>${escapeHtml(r.message||'')}</details></div>`).join('');
}
async function monitorBulk(id){
  if(bulkState.monitoring)return;
  bulkState.monitoring=true;let errors=0,revision=-1;
  try{
    while(true){
      let job;
      try{job=await getJSON('/api/bulk/'+encodeURIComponent(id));errors=0;}
      catch(e){if(++errors>=5)throw e;$('bulkStatus').textContent='Связь с приложением прервана. Повторяю запрос состояния…';await new Promise(r=>setTimeout(r,3000));continue;}
      renderBulk(job);
      if(job.completed_routes!==revision){revision=job.completed_routes;await compare();}
      if(!['queued','running','pausing'].includes(job.status)&&job.export_status!=='running')break;
      await new Promise(r=>setTimeout(r,2500));
    }
  }catch(e){$('bulkStatus').textContent=`Не удалось прочитать состояние: ${e.message}. Сбор продолжится, если приложение запущено. Обновите страницу для подключения.`;}
  finally{bulkState.monitoring=false;}
}
async function bulkAction(action){
  if(bulkState.requesting)return;
  bulkState.requesting=true;bulkControls();
  try{
    const url=['start','saved'].includes(action)?'/api/bulk':`/api/bulk/${encodeURIComponent(bulkState.job.job_id)}/${action}`;
    const job=await getJSON(url,{method:'POST',headers:{'Content-Type':'application/json'},...(['start','saved'].includes(action)?{body:JSON.stringify({...bulkBody(),...(action==='saved'?{mode:'saved'}:{})})}:{})});
    renderBulk(job);monitorBulk(job.job_id);
  }catch(e){$('bulkStatus').textContent=e.message;toast(e.message);}
  finally{bulkState.requesting=false;bulkControls();}
}
async function initBulk(){
  const options=(state.options?.all_origins||state.options?.origins||[]).map(c=>`<option value="${escapeHtml(c)}">${escapeHtml(c)}</option>`).join('');
  $('bulkOrigins').innerHTML=options;$('bulkDestinations').innerHTML=options;
  [...$('bulkOrigins').options].forEach(o=>o.selected=o.value===$('originSelect').value);
  await updateBulkPlan();
  try{const job=await getJSON('/api/bulk');renderBulk(job);if(job.status!=='idle'&&(['queued','running','pausing'].includes(job.status)||job.export_status==='running'))monitorBulk(job.job_id);}
  catch(e){$('bulkStatus').textContent=e.message;}
}

async function openSettings(){
  const dialog=$('settingsDialog'); $('settingsMessage').textContent='';
  try{ const configured=await getJSON('/api/settings'); $('browserCalculatorsToggle').checked=configured.public_browser_enabled===true; $('publicBrowserStatus').textContent='Онлайн-загрузка включена по умолчанию. LIVE означает успешное получение за последние 30 минут. Старые значения остаются LAST GOOD. Некоторые точные калькуляторы требуют API-ключ; цена «от» показывается отдельно.'; const map={dellin_appkey:'dellinAppKey',vozovoz_api_key:'vozovozKey',baikal_api_key:'baikalApiKey',baikal_api_url:'baikalApiUrl'}; Object.entries(map).forEach(([k,id])=>{$(id).value='';$(id).placeholder=configured[k]?'Сохранено — введите новое значение':'';}); }catch(e){$('settingsMessage').textContent=e.message;}
  if(!dialog.open) dialog.showModal();
}
function closeSettings(){if($('settingsDialog').open)$('settingsDialog').close();}
const importsState={seq:0,route:null,token:null,busy:false,companies:{}};
function resetImportPreview(){
  importsState.seq++;importsState.token=null;$('importPreview').hidden=true;
  $('importConfirmed').checked=false;$('importApplyButton').disabled=true;$('importMessage').textContent='';
}
function importBusy(busy){
  importsState.busy=busy;
  ['importCompany','importFile','importPreviewButton','importRemoveButton'].forEach(id=>$(id).disabled=busy);
  $('importApplyButton').disabled=busy||!importsState.token||!$('importConfirmed').checked;
}
function showCurrentImport(){
  const saved=importsState.companies[$('importCompany').value];
  const guide=state.options?.import_guide?.[$('importCompany').value];
  $('importCompanyGuide').innerHTML=guide?`<a href="${escapeHtml(guide.page_url)}" target="_blank" rel="noopener">Где скачать прайс ${escapeHtml($('importCompany').value)}</a><span>${escapeHtml(guide.instruction)}</span>`:'';
  $('importCurrent').textContent=saved?`Документ в библиотеке: ${saved.original_filename}. Импортирован: ${saved.uploaded_at}. Дата в документе: ${saved.document_date||'не распознана'}.`:'Для этой компании и направления пользовательский файл ещё не применён.';
  $('importRemoveButton').hidden=!saved;
}
async function openImport(){
  resetImportPreview();importBusy(false);$('importFile').value='';
  importsState.route={origin:$('originSelect').value,destination:$('destinationSelect').value};
  $('importRoute').textContent=`${importsState.route.origin} → ${importsState.route.destination}`;
  $('importCompany').innerHTML=(state.options?.companies||[]).map(c=>`<option value="${escapeHtml(c.id)}">${escapeHtml(c.label)}</option>`).join('');
  if(state.calculationCompanies.size===1)$('importCompany').value=[...state.calculationCompanies][0];
  importsState.companies={};showCurrentImport();$('importDialog').showModal();
  const seq=importsState.seq;
  try{
    const data=await getJSON('/api/imports?'+new URLSearchParams(importsState.route));
    if(seq!==importsState.seq||!$('importDialog').open)return;
    importsState.companies=data.companies||{};showCurrentImport();
  }catch(e){if(seq===importsState.seq)$('importMessage').textContent=e.message;}
}
function closeImport(){resetImportPreview();$('importDialog').close();}
async function previewImport(){
  const file=$('importFile').files[0];resetImportPreview();
  if(!file){$('importMessage').textContent='Выберите документ на компьютере.';return;}
  if(file.size>20*1024*1024){$('importMessage').textContent='Файл превышает 20 МБ.';return;}
  const seq=importsState.seq;const form=new FormData();
  Object.entries({...importsState.route,company:$('importCompany').value}).forEach(([k,v])=>form.append(k,v));form.append('file',file);
  importBusy(true);$('importMessage').textContent='Читаю таблицы и проверяю направление…';
  try{
    const data=await getJSON('/api/import/preview',{method:'POST',body:form,timeout:120000});
    if(seq!==importsState.seq||!$('importDialog').open)return;
    importsState.token=data.token;
    $('importMessage').textContent=`Распознано ${data.rows.length} из ${state.options.profiles.length} строк. Цены ещё не применены.`;
    const m=data.meta;
    $('importEvidence').textContent=`${data.company} · ${data.origin} → ${data.destination}. ${m.parser}. Дата в документе: ${m.document_date||'не распознана'}. ${m.source_page?'Страница '+m.source_page+'. ':''}${m.source_row?'Строка '+m.source_row+'. ':''}${m.archive_member?'Файл в архиве: '+m.archive_member+'. ':''}${m.calculation_basis||''}`;
    $('importWarnings').innerHTML=(data.warnings||[]).map(x=>`<li>${escapeHtml(x)}</li>`).join('');
    $('importRows').innerHTML=data.rows.map(r=>`<tr><td>${escapeHtml(r.profile.label)}</td><td>${r.profile.is_minimum_profile?'—':escapeHtml(r.profile.weight_kg)+' кг'}</td><td>${escapeHtml(fmt(r.price))}</td><td>${r.rate_per_kg?escapeHtml(fmt(r.rate_per_kg,'₽/кг')):'сумма отправки; ставка не указана'}${r.minimum?' · минимум '+escapeHtml(fmt(r.minimum)):''}</td></tr>`).join('');
    $('importMissing').textContent=data.missing_profiles.length?'Не распознаны: '+data.missing_profiles.join(', '):'Все весовые строки распознаны.';
    $('importPreview').hidden=false;
  }catch(e){if(seq===importsState.seq)$('importMessage').textContent=e.message;}
  finally{if(seq===importsState.seq)importBusy(false);}
}
async function applyImport(){
  if(importsState.busy||!importsState.token||!$('importConfirmed').checked)return;
  importBusy(true);
  try{
    const data=await getJSON('/api/import/commit',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({token:importsState.token})});
    importsState.token=null;importsState.companies[data.company]=data.meta;showCurrentImport();
    $('importMessage').textContent=`Применено ${data.rows} строк. Источник: файл пользователя.`;
    state.calculationCompanies.add(data.company);
    // Make the explicitly applied document visible immediately, including export.
    state.liveOnly=false;state.includeImports=true;$('liveModeSelect').value='all';
    await loadOptions(data.origin,data.destination);await compare();
    if(typeof refreshDocuments==='function'){await refreshDocuments();await refreshBulkAfterDocuments();}
  }catch(e){$('importMessage').textContent=e.message;}
  finally{importBusy(false);}
}
async function removeImport(){
  if(importsState.busy)return;
  const company=$('importCompany').value;importBusy(true);
  try{
    await getJSON('/api/import?'+new URLSearchParams({...importsState.route,company}),{method:'DELETE'});
    delete importsState.companies[company];resetImportPreview();showCurrentImport();
    $('importMessage').textContent='Файл отключён для этого направления. Снова используются результаты онлайн-загрузки.';
    await loadOptions(importsState.route.origin,importsState.route.destination);await compare();
  }catch(e){$('importMessage').textContent=e.message;}
  finally{importBusy(false);}
}
$('importButton').addEventListener('click',openImport);
$('closeImportButton').addEventListener('click',closeImport);
$('importDialog').addEventListener('cancel',()=>resetImportPreview());
$('importCompany').addEventListener('change',()=>{resetImportPreview();$('importFile').value='';showCurrentImport();});
$('importFile').addEventListener('change',resetImportPreview);
$('importConfirmed').addEventListener('change',()=>importBusy(importsState.busy));
$('importPreviewButton').addEventListener('click',previewImport);
$('importApplyButton').addEventListener('click',applyImport);
$('importRemoveButton').addEventListener('click',removeImport);
$('importTemplateButton').addEventListener('click',()=>{window.location.href='/api/import/template?'+new URLSearchParams({...importsState.route,company:$('importCompany').value});});
async function saveSettings(){ const map={dellin_appkey:'dellinAppKey',vozovoz_api_key:'vozovozKey',baikal_api_key:'baikalApiKey',baikal_api_url:'baikalApiUrl'}; const payload={public_browser_enabled:$('browserCalculatorsToggle').checked}; Object.entries(map).forEach(([k,id])=>{const v=$(id).value.trim();if(v)payload[k]=v;}); if(!Object.keys(payload).length){$('settingsMessage').textContent='Введите хотя бы одно значение.';return;} try{await getJSON('/api/settings',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});$('settingsMessage').textContent='Сохранено локально.';setTimeout(closeSettings,500);}catch(e){$('settingsMessage').textContent=e.message;} }
function setTheme(next){
  next=next==='dark'?'dark':'light';
  document.documentElement.dataset.theme=next;
  document.documentElement.style.colorScheme=next==='dark'?'dark':'only light';
  try{localStorage.setItem('tariff-theme-v51',next);}catch{}
  const url=new URL(location.href);if(url.searchParams.has('theme')){url.searchParams.delete('theme');history.replaceState(null,'',url);}
  if(typeof updateThemeButton==='function')updateThemeButton();
  if(state.matrix)renderTariffGraph(state.matrix);
}
function toggleTheme(){setTheme(document.documentElement.dataset.theme==='dark'?'light':'dark');}
async function init(){ if(typeof updateThemeButton==='function')updateThemeButton(); state.unitMode=localStorage.getItem('tariff-unit-mode-v50')==='per_kg'?'per_kg':'total'; const sourceMode='all'; state.liveOnly=sourceMode!=='all'; state.includeImports=sourceMode!=='live'; if($('unitModeSelect')) $('unitModeSelect').value=state.unitMode; if($('liveModeSelect')) $('liveModeSelect').value=sourceMode; updateUnitLabels(); let route={origin:'Санкт-Петербург',destination:'Москва'}; try{route={...route,...JSON.parse(localStorage.getItem('tariff-route-v44')||'{}')};}catch{} try{await loadOptions(route.origin,route.destination);}catch{await loadOptions('Санкт-Петербург','Москва');} $('profileSelect').value='w100'; await compare(); if(typeof initWorkspace==='function')initWorkspace(); initBulk(); resumeRefresh().catch(e=>toast(e.message)); }


async function changeRoute(origin,destination){
  try { if(await loadOptions(origin,destination)){localStorage.setItem('tariff-route-v44',JSON.stringify({origin:$('originSelect').value,destination:$('destinationSelect').value}));await compare();} }
  catch(e){toast(e.message);}
}
if($('swapRouteButton')) $('swapRouteButton').addEventListener('click',()=>changeRoute($('destinationSelect').value,$('originSelect').value));
$('originSelect').addEventListener('change',()=>changeRoute($('originSelect').value,$('destinationSelect').value));
$('destinationSelect').addEventListener('change',()=>changeRoute($('originSelect').value,$('destinationSelect').value)); $('profileSelect').addEventListener('change',()=>{ const p=(state.options?.profiles||[]).find(x=>x.id===$('profileSelect').value); if(state.graphScope!=='all' && p && !p.is_minimum_profile){ state.graphScope=Number(p.weight_kg||0)<=50?'small':Number(p.weight_kg||0)<=1500?'medium':'heavy'; $('graphScopeSelect').value=state.graphScope; } compare(); });
$('graphScopeSelect').addEventListener('change',()=>{ state.graphScope=$('graphScopeSelect').value; if(state.matrix) renderTariffGraph(state.matrix); }); if($('unitModeSelect')) $('unitModeSelect').addEventListener('change',()=>{ state.unitMode=$('unitModeSelect').value==='per_kg'?'per_kg':'total'; localStorage.setItem('tariff-unit-mode-v50',state.unitMode); rerenderUnitMode(); }); if($('liveModeSelect')) $('liveModeSelect').addEventListener('change',()=>{ state.liveOnly=$('liveModeSelect').value!=='all'; state.includeImports=$('liveModeSelect').value!=='live'; localStorage.setItem('tariff-source-mode-v450',$('liveModeSelect').value); if(state.comparison) renderComparison(state.comparison); if(state.matrix){renderMatrix(state.matrix);renderTariffGraph(state.matrix);} }); $('compareButton').addEventListener('click',compare);
$('selectAllCompaniesButton').addEventListener('click',()=>setAllCompanies(true)); $('clearCompaniesButton').addEventListener('click',()=>setAllCompanies(false));
$('retryFailedButton').addEventListener('click',()=>{const failed=(state.comparison?.items||[]).filter(r=>['failed','partial'].includes(r.refresh_status)&&state.calculationCompanies.has(r.company)).map(r=>r.company);if(failed.length)refreshRouteSources(true,failed);});
$('diagnosticsButton').addEventListener('click',()=>{window.location.href='/api/diagnostics/download?'+new URLSearchParams({origin:$('originSelect').value,destination:$('destinationSelect').value,profile:$('profileSelect').value});});

['bulkScope','bulkOrigins','bulkDestinations'].forEach(id=>$(id).addEventListener('change',updateBulkPlan));
[['bulkStartButton','start'],['bulkPauseButton','pause'],['bulkResumeButton','resume'],['bulkRetryButton','retry'],['bulkExportButton','export']].forEach(([id,action])=>$(id).addEventListener('click',()=>bulkAction(action)));
$('collectButton').addEventListener('click',updateSources); $('exportButton').addEventListener('click',exportExcel); $('themeButton').addEventListener('click',toggleTheme);
$('settingsButton').addEventListener('click',openSettings); $('closeSettingsButton').addEventListener('click',closeSettings); $('closeSettingsIcon').addEventListener('click',closeSettings); $('saveSettingsButton').addEventListener('click',saveSettings);
$('settingsDialog').addEventListener('click',e=>{if(e.target===$('settingsDialog'))closeSettings();});
setInterval(()=>{if(!document.hidden&&!state.refreshingRoutes.size)resumeRefresh().catch(()=>{});},60000);
init().catch(e=>toast(e.message));

document.addEventListener('visibilitychange',()=>{if(!document.hidden&&!state.refreshingRoutes.size)resumeRefresh().catch(()=>{});});

$('extendedRoutesToggle')?.addEventListener('change',async()=>{
  const expanded=$('extendedRoutesToggle').checked;
  try{await loadOptions(expanded?$('originSelect').value:'Москва',expanded?$('destinationSelect').value:'Санкт-Петербург');await compare();}
  catch(e){toast(e.message);}
});

$('lightThemeButton').addEventListener('click',()=>setTheme('light'));
$('darkThemeButton').addEventListener('click',()=>setTheme('dark'));
