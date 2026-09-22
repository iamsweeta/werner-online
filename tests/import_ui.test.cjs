// Run with jsdom@26.1.0. The API parser/lifecycle is covered by Python tests.
const assert=require('node:assert/strict');
const fs=require('node:fs');const path=require('node:path');const {JSDOM}=require('jsdom');
const root=path.resolve(__dirname,'..');
const html=fs.readFileSync(path.join(root,'static/index.html'),'utf8').replace(/<script src="\/static\/app.js[^>]*><\/script>/,'');
const script=fs.readFileSync(path.join(root,'static/app.js'),'utf8');
const pause=ms=>new Promise(r=>setTimeout(r,ms));
async function until(fn){for(let i=0;i<250;i++){if(fn())return;await pause(5)}throw Error('UI state timeout')}
(async()=>{
 const dom=new JSDOM(html,{url:'http://localhost:8423',runScripts:'outside-only'});const w=dom.window,$=id=>w.document.getElementById(id);
 w.HTMLDialogElement.prototype.showModal=function(){this.open=true};w.HTMLDialogElement.prototype.close=function(){this.open=false};
 w.AbortController=AbortController;w.localStorage.setItem('tariff-auto-refresh-v424','0');
 w.localStorage.setItem('tariff-route-v44',JSON.stringify({origin:'Казань',destination:'Екатеринбург'}));
 const profiles=[{id:'w100',label:'до 100 кг',weight_kg:100,description:'до 100 кг',range_weight:'до 100 кг',tariff_type:'Стоимость отправки'}];
 const opts={origins:['Казань','Екатеринбург'],destinations:['Екатеринбург'],selected_origin:'Казань',selected_destination:'Екатеринбург',companies:[{id:'Werner',label:'Werner'}],profiles,integrations:[],integration_status:{}};
 let applied=false,previewCount=0,commitCount=0;
 const item=()=>({company:'Werner',company_label:'Werner',price:applied?1234:null,comparison_value:applied?1234:null,status:applied?'ok':'document_unavailable',online:false,uploaded:applied,source_type:applied?'Файл пользователя':'Источник',source_file:applied?'abc.xlsx':null,original_filename:'Прайс.xlsx',document_date:null,refresh_status:'not_run'});
 w.fetch=async(url,request={})=>{
  const u=new URL(url,w.location.href);let data;
  if(u.pathname==='/api/options')data=opts;
  else if(u.pathname==='/api/compare')data={origin:'Казань',destination:'Екатеринбург',profile_id:'w100',items:[item()],range_weight:'до 100 кг'};
  else if(u.pathname==='/api/profile-matrix')data={profiles:[{profile:profiles[0],items:[item()]}]};
  else if(u.pathname==='/api/active-collect')data={status:'idle'};
  else if(u.pathname==='/api/imports')data={companies:{}};
  else if(u.pathname==='/api/import/jobs'){
   previewCount++;assert.equal(request.body.get('company'),'Werner');assert.equal(request.body.get('origin'),'Казань');assert.equal(request.body.get('destination'),'Екатеринбург');
   data={token:'token',company:'Werner',origin:'Казань',destination:'Екатеринбург',rows:[{profile:profiles[0],price:1234}],missing_profiles:[],warnings:['Актуальность проверьте по оригиналу'],meta:{parser:'Тестовая таблица'}};data={job_id:'job',status:'ready',preview:data};
  }else if(u.pathname==='/api/import/commit'){commitCount++;assert.equal(JSON.parse(request.body).token,'token');applied=true;data={company:'Werner',origin:'Казань',destination:'Екатеринбург',rows:1,meta:{original_filename:'Прайс.xlsx',uploaded_at:'now'}}}
  else if(u.pathname==='/api/import'&&request.method==='DELETE'){applied=false;data={ok:true}}
  else throw Error('Unexpected '+url);
  return {ok:true,json:async()=>data};
 };
 w.eval(script);await until(()=>$('originSelect').value==='Казань'&&$('comparisonTable').textContent.includes('Werner'));
 $('importButton').click();await until(()=>$('importDialog').open);
 Object.defineProperty($('importFile'),'files',{configurable:true,value:[new w.File(['test'],'Прайс.xlsx')]});
 $('importPreviewButton').click();await until(()=>!$('importPreview').hidden);
 assert.equal(previewCount,1);assert.equal(applied,false);assert.equal($('importApplyButton').disabled,true);
 assert.match($('importRows').textContent,/1\s234/);assert.match($('importRoute').textContent,/Казань → Екатеринбург/);
 $('importConfirmed').checked=true;$('importConfirmed').dispatchEvent(new w.Event('change'));$('importApplyButton').click();
 await until(()=>$('importedCount').textContent==='1'&&!$('importCompany').disabled);
 assert.equal(commitCount,1);assert.equal($('onlineCount').textContent,'0');assert.equal($('liveModeSelect').value,'all');
 assert.match($('comparisonTable').textContent,/Файл пользователя/);
 assert.match($('comparisonTable').innerHTML,/\/api\/import-file\/abc.xlsx/);
 assert.match($('matrixBody').textContent,/1\s234 ₽/);
 $('liveModeSelect').value='live';$('liveModeSelect').dispatchEvent(new w.Event('change'));
 assert.equal($('exactCount').textContent,'0');assert.equal($('importedCount').textContent,'0');assert.match(w.queryString(),/include_imports=false/);
 $('importRemoveButton').click();await until(()=>!applied&&!$('importCompany').disabled);
 assert.equal($('importRemoveButton').hidden,true);assert.equal($('importApplyButton').disabled,true);
 dom.window.close();console.log('PASS: upload preview, route binding, explicit apply, document provenance, filters, matrix, export query and removal');
})().catch(e=>{console.error(e);process.exit(1)});
