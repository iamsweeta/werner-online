// Three client workflows: the full reference table, one route, and price files.
const wsEl=id=>document.getElementById(id);
const documentState={job:null,busy:false,file:null,files:[],resolutions:{},seq:0};
function setWorkspace(name,{focus=false}={}){
  if(!['bulk','route','documents'].includes(name))name='bulk';
  state.workspace=name;localStorage.setItem('tariff-workspace-v48',name);
  const panels={bulk:'bulkPanel',route:'routeWorkspace',documents:'documentsPanel'};
  Object.entries(panels).forEach(([key,id])=>wsEl(id).hidden=key!==name);
  document.querySelectorAll('[data-workspace]').forEach(btn=>{const on=btn.dataset.workspace===name;btn.classList.toggle('active',on);btn.setAttribute('aria-selected',String(on));btn.tabIndex=on?0:-1;});
  const labels={bulk:['Большая таблица','Обновление тарифов в формате заказчика'],route:['Один маршрут','Сравнение компаний и отдельный Excel'],documents:['Прайс-листы','Документы для заполнения недостающих цен']};
  wsEl('workspaceTitle').textContent=labels[name][0];wsEl('workspaceSubtitle').textContent=labels[name][1];
  if(focus)wsEl('workspaceTitle').focus({preventScroll:true});
  if(name==='documents')refreshDocuments();
  if(name==='bulk')refreshBulkAfterDocuments();
  if(name==='route'&&state.options){compare();}
}
function updateThemeButton(){
  const dark=document.documentElement.dataset.theme==='dark';
  wsEl('lightThemeButton')?.setAttribute('aria-pressed',String(!dark));
  wsEl('darkThemeButton')?.setAttribute('aria-pressed',String(dark));
  wsEl('themeButton').textContent=dark?'Светлая тема':'Тёмная тема';
  wsEl('themeButton').setAttribute('aria-label',dark?'Включить светлую тему':'Включить тёмную тему');
  document.querySelector('meta[name=theme-color]').content=dark?'#090a0c':'#ffffff';
}
async function initWorkspace(){
  const cities=(state.options.all_origins||state.options.origins).map(c=>`<option value="${escapeHtml(c)}">${escapeHtml(c)}</option>`).join('');
  wsEl('documentOrigin').innerHTML='<option value="">Определить из файла</option>'+cities;
  wsEl('documentCompany').innerHTML=state.options.companies.map(c=>`<option value="${escapeHtml(c.id)}">${escapeHtml(c.label)}</option>`).join('');
  documentGuide();updateThemeButton();
  setWorkspace(localStorage.getItem('tariff-workspace-v48')||'bulk');
  await refreshDocuments();
  getJSON('/api/storage').then(info=>{if(wsEl('storageInfo'))wsEl('storageInfo').textContent=`${info.files} оригиналов · ${(info.bytes/1024/1024).toFixed(1)} МБ. ${info.confirmed_retention} ${info.disabled_retention} Предпросмотр действует 30 минут. Папка: ${info.location}`;}).catch(()=>{});
}
function documentGuide(){
  const guide=state.options?.import_guide?.[wsEl('documentCompany').value];
  wsEl('documentGuide').textContent='';
  if(!guide)return;
  const entries=Array.isArray(guide)?guide:[guide];
  for(const entry of entries){
    const p=document.createElement('p');p.textContent=typeof entry==='string'?entry:(entry.instruction||entry.description||entry.instructions||entry.note||'');wsEl('documentGuide').append(p);if(entry.page_url&&/^https?:\/\//i.test(entry.page_url)){const a=document.createElement('a');a.href=entry.page_url;a.target='_blank';a.rel='noopener';a.textContent='Открыть страницу с прайсами';wsEl('documentGuide').append(a);}
  }
}
function documentBusy(value){
  documentState.busy=value;
  ['documentCompany','documentOrigin','documentDate','documentFile','documentParseButton'].forEach(id=>wsEl(id).disabled=value);
  wsEl('documentConfirmed').disabled=value;
  wsEl('documentConflictChoices').querySelectorAll('select').forEach(el=>el.disabled=value);
  wsEl('documentCommitButton').disabled=value||documentState.job?.status!=='ready'||!wsEl('documentConfirmed').checked||(documentState.job?.conflicts||[]).some(c=>documentState.resolutions[c.id]===undefined);
}
function resetDocumentPreview(){
  if(documentState.busy)return;
  documentState.seq++;documentState.job=null;documentState.resolutions={};wsEl('documentPreview').hidden=true;wsEl('documentConfirmed').checked=false;
  wsEl('documentProgress').hidden=true;wsEl('documentMessage').textContent='';documentBusy(false);
}
function selectDocument(file){selectDocuments(file?[file]:[]);}
function selectDocuments(files){
  if(documentState.busy)return;
  resetDocumentPreview();documentState.files=Array.from(files||[]);documentState.file=documentState.files[0]||null;
  const size=documentState.files.reduce((n,f)=>n+f.size,0);
  wsEl('documentFileName').textContent=documentState.files.length?`${documentState.files.length} файлов · ${(size/1024/1024).toFixed(1)} МБ · ${documentState.files.map(f=>f.name).join(', ')}`:'Текстовый PDF, Excel, CSV или архив с документами';
}
function renderConflicts(){
  const conflicts=documentState.job?.conflicts||[];wsEl('documentConflicts').hidden=!conflicts.length;
  wsEl('documentConflictChoices').innerHTML=conflicts.map(c=>{
    const profile=state.options.profiles.find(p=>p.id===c.profile_id);
    return `<label class="conflict-field field"><span>${escapeHtml(c.origin)} → ${escapeHtml(c.destination)} · ${escapeHtml(profile?.label||c.profile_id)}</span><select data-price-conflict="${escapeHtml(c.id)}"><option value="">Выберите подтверждённую цену</option>${c.options.map((v,i)=>`<option value="${i}">${escapeHtml(fmt(v.price))}${numeric(v.rate_per_kg)?' · ставка '+escapeHtml(fmt(v.rate_per_kg,'₽/кг')):''}${v.minimum?' · минимум '+escapeHtml(fmt(v.minimum)):''} · ${escapeHtml(v.original_filename||v.archive_member||'Файл')} · ${escapeHtml(v.document_date||'дата не определена')}</option>`).join('')}</select></label>`;
  }).join('');
  wsEl('documentConflictChoices').querySelectorAll('[data-price-conflict]').forEach(select=>select.addEventListener('change',()=>{
    const id=select.dataset.priceConflict;
    if(select.value==='')delete documentState.resolutions[id];else documentState.resolutions[id]=Number(select.value);
    renderDocumentRoute();documentBusy(documentState.busy);
  }));
}
function renderDocumentRoute(){
  const route=documentState.job?.routes?.[Number(wsEl('documentRoutePreview').value)];
  wsEl('documentPriceRows').innerHTML='';if(!route)return;
  wsEl('documentPriceRows').innerHTML=(state.options.profiles||[]).filter(p=>route.values[p.id]).map(p=>{
    const conflict=(documentState.job.conflicts||[]).find(c=>c.origin===route.origin&&c.destination===route.destination&&c.profile_id===p.id);
    const unresolved=conflict&&documentState.resolutions[conflict.id]===undefined;
    const item=conflict&&!unresolved?conflict.options[documentState.resolutions[conflict.id]]:route.values[p.id],proof=item.source_page||route.meta?.source_page||item.source_row||route.meta?.source_row||'—';
    return `<tr><td>${escapeHtml(p.label||p.range_weight)}</td><td class="value-cell">${unresolved?'Выберите цену':escapeHtml(rateProfile(p)?(numeric(item.rate_per_kg)?fmt(item.rate_per_kg,'₽/кг'):'нет ставки · сумма '+fmt(item.price)):fmt(item.price))}</td><td>${escapeHtml(item.original_filename||item.archive_member||'')} ${escapeHtml(proof)}</td></tr>`;
  }).join('');
}
function renderDocumentPreview(job){
  documentState.job=job;wsEl('documentPreview').hidden=false;
  wsEl('documentPreviewTitle').textContent=job.filename;
  wsEl('documentPreviewSummary').textContent=`${job.company} · маршрутов: ${job.routes.length} · цен: ${job.values_count}. Цены ещё не сохранены.`;
  wsEl('documentWarnings').innerHTML=(job.warnings||[]).map(w=>`<li>${escapeHtml(w)}</li>`).join('');
  wsEl('documentRoutePreview').innerHTML=job.routes.map((r,i)=>`<option value="${i}">${escapeHtml(r.origin)} → ${escapeHtml(r.destination)} · ${Object.keys(r.values).length} цен</option>`).join('');
  wsEl('documentSkipped').hidden=!job.errors?.length;
  wsEl('documentErrors').innerHTML=(job.errors||[]).map(r=>`<p>${escapeHtml(r.file||[r.origin,r.destination].filter(Boolean).join(' → '))}: ${escapeHtml(r.message)}</p>`).join('');
  renderConflicts();renderDocumentRoute();
}
async function parsePriceDocument(){
  if(documentState.busy)return;
  const files=documentState.files.length?documentState.files:Array.from(wsEl('documentFile').files||[]);
  if(!files.length){wsEl('documentMessage').textContent='Выберите прайс-лист.';return;}
  if(files.length>30||files.reduce((n,f)=>n+f.size,0)>20*1024*1024){wsEl('documentMessage').textContent='Выберите до 30 файлов общим размером не более 20 МБ.';return;}
  resetDocumentPreview();const seq=++documentState.seq;documentBusy(true);wsEl('documentProgress').hidden=false;
  try{
    const form=new FormData();files.forEach(file=>form.append(files.length===1?'file':'files',file));form.append('company',wsEl('documentCompany').value);
    form.append('origin',wsEl('documentOrigin').value);form.append('document_date',wsEl('documentDate').value);
    let job=await getJSON('/api/price-documents/preview',{method:'POST',body:form});
    while(job.status==='parsing'){
      if(seq!==documentState.seq)return;
      wsEl('documentMessage').textContent=job.total?`Проверено направлений: ${job.done}/${job.total}. Распознано: ${job.matched}.`:'Читаю документ и определяю маршруты…';
      wsEl('documentProgress').value=job.total?100*job.done/job.total:0;
      await new Promise(r=>setTimeout(r,1200));job=await getJSON('/api/price-documents/preview/'+encodeURIComponent(job.token));
    }
    if(job.status!=='ready')throw Error(job.message||'Не удалось распознать прайс');
    renderDocumentPreview(job);wsEl('documentMessage').textContent='Распознавание завершено. Проверьте маршруты и цены ниже.';wsEl('documentProgress').value=100;
  }catch(e){wsEl('documentMessage').textContent=e.message;}
  finally{documentBusy(false);}
}
async function commitPriceDocument(){
  if(documentState.busy||documentState.job?.status!=='ready'||!wsEl('documentConfirmed').checked||(documentState.job.conflicts||[]).some(c=>documentState.resolutions[c.id]===undefined))return;
  documentBusy(true);
  try{
    const result=await getJSON('/api/price-documents/commit',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({token:documentState.job.token,resolutions:documentState.resolutions})});
    documentState.job.status='committed';wsEl('documentPreview').hidden=true;
    wsEl('documentMessage').textContent=`Сохранено: ${result.route_count} маршрутов, ${result.values_count} цен. Теперь соберите большую таблицу.`;
    await refreshDocuments();await refreshBulkAfterDocuments();await compare();
  }catch(e){wsEl('documentMessage').textContent=e.message;}
  finally{documentBusy(false);}
}
function renderCoverage(job){
  const rows=job.coverage||[];wsEl('bulkCoverage').hidden=!rows.length;
  wsEl('bulkCoverageRows').innerHTML=rows.map(r=>`<tr><td><strong>${escapeHtml(r.company)}</strong></td><td>${Number(r.online).toLocaleString('ru-RU')}</td><td>${Number(r.saved||0).toLocaleString('ru-RU')}</td><td>${Number(r.document).toLocaleString('ru-RU')}</td><td class="${r.missing?'coverage-missing':''}">${Number(r.missing).toLocaleString('ru-RU')}</td><td>${r.missing?`<button class="text-button" data-add-price-company="${escapeHtml(r.company)}" type="button">Добавить прайс</button>`:'Заполнено'}</td></tr>`).join('');
  wsEl('bulkCoverageRows').querySelectorAll('[data-add-price-company]').forEach(button=>button.addEventListener('click',()=>{
    if(!documentState.busy){resetDocumentPreview();wsEl('documentCompany').value=button.dataset.addPriceCompany;documentGuide();}
    setWorkspace('documents',{focus:true});
  }));
}
async function refreshBulkAfterDocuments(){
  try{const job=await getJSON('/api/bulk');renderBulk(job);}catch{}
}
async function refreshDocuments(){
  try{
    const data=await getJSON('/api/price-documents');const files=data.files||[];
    wsEl('documentCountBadge').textContent=files.length?`(${files.length})`:'';
    if(!files.length){wsEl('documentsList').innerHTML='<p class="empty">Пока нет прайс-листов. Добавьте первый документ выше.</p>';return;}
    wsEl('documentsList').innerHTML=files.map(f=>`<article class="document-item"><span class="document-icon">${escapeHtml((f.extension||'').slice(1).toUpperCase())}</span><div><h3>${escapeHtml(f.original_filename)}</h3><p>${escapeHtml(f.company)} · ${Number(f.route_count)} маршрутов · ${Number(f.values_count)} цен</p><small>Дата тарифов: ${escapeHtml(f.document_date||'не определена')} · загружен ${escapeHtml(new Date(f.uploaded_at).toLocaleDateString('ru-RU'))}</small></div><div class="file-actions"><a href="/api/import-file/${encodeURIComponent(f.source_file)}" target="_blank" rel="noopener">Оригинал</a><button class="text-button" type="button" data-disable-document="${escapeHtml(f.id)}">Отключить</button></div></article>`).join('');
    wsEl('documentsList').querySelectorAll('[data-disable-document]').forEach(btn=>btn.addEventListener('click',async()=>{
      btn.disabled=true;
      try{await getJSON('/api/price-documents/'+encodeURIComponent(btn.dataset.disableDocument),{method:'DELETE'});await refreshDocuments();await refreshBulkAfterDocuments();await compare();toast('Прайс отключён. Пересоберите Excel, чтобы обновить файл.');}
      catch(e){toast(e.message);btn.disabled=false;}
    }));
  }catch(e){wsEl('documentsList').textContent=e.message;}
}
function downloadCSVTemplate(){
  const raw='\ufeffКомпания;Откуда;Куда;Вес от, кг;Вес до, кг;Тариф;Единица;Минимум, руб\r\n';
  const url=URL.createObjectURL(new Blob([raw],{type:'text/csv;charset=utf-8'}));
  const link=document.createElement('a');link.href=url;link.download='price_import_template.csv';link.click();setTimeout(()=>URL.revokeObjectURL(url),1000);
}
document.querySelectorAll('[data-workspace]').forEach(btn=>{
  btn.addEventListener('click',()=>setWorkspace(btn.dataset.workspace,{focus:true}));
  btn.addEventListener('keydown',event=>{
    if(!['ArrowLeft','ArrowRight','ArrowUp','ArrowDown'].includes(event.key))return;
    event.preventDefault();const tabs=[...document.querySelectorAll('[data-workspace]')];
    const next=tabs[(tabs.indexOf(btn)+(['ArrowLeft','ArrowUp'].includes(event.key)?-1:1)+tabs.length)%tabs.length];next.click();next.focus();
  });
});
document.querySelector('[data-open-workspace]').addEventListener('click',e=>{e.preventDefault();setWorkspace('bulk');});
wsEl('bulkDocumentsButton').addEventListener('click',()=>setWorkspace('documents',{focus:true}));
wsEl('documentsExportButton').addEventListener('click',()=>setWorkspace('bulk',{focus:true}));
wsEl('documentFile').addEventListener('change',()=>selectDocuments(wsEl('documentFile').files));
['documentCompany','documentOrigin','documentDate'].forEach(id=>wsEl(id).addEventListener('change',()=>{resetDocumentPreview();if(id==='documentCompany')documentGuide();}));
wsEl('documentRoutePreview').addEventListener('change',renderDocumentRoute);
wsEl('documentParseButton').addEventListener('click',parsePriceDocument);
wsEl('documentConfirmed').addEventListener('change',()=>documentBusy(documentState.busy));
wsEl('documentCommitButton').addEventListener('click',commitPriceDocument);
wsEl('documentWorkbookButton').addEventListener('click',()=>{window.location.href='/api/price-documents/template?'+new URLSearchParams({company:wsEl('documentCompany').value,origin:wsEl('documentOrigin').value});});
wsEl('documentTemplateButton').addEventListener('click',downloadCSVTemplate);
wsEl('bulkSavedButton').addEventListener('click',()=>bulkAction('saved'));
const dropzone=wsEl('documentDropzone');
['dragenter','dragover'].forEach(type=>dropzone.addEventListener(type,event=>{event.preventDefault();if(!documentState.busy)dropzone.classList.add('drag-over');}));
['dragleave','drop'].forEach(type=>dropzone.addEventListener(type,event=>{event.preventDefault();dropzone.classList.remove('drag-over');}));
dropzone.addEventListener('drop',event=>{if(!documentState.busy)selectDocuments(event.dataTransfer.files);});

wsEl('documentPointsButton')?.addEventListener('click',()=>{window.location.href='/api/price-documents/template?'+new URLSearchParams({company:wsEl('documentCompany').value,origin:wsEl('documentOrigin').value,kind:'points'});});
