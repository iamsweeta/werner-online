const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),{JSDOM}=require('jsdom');
const root=path.resolve(__dirname,'..'),html=fs.readFileSync(path.join(root,'static/index.html'),'utf8');
const scripts=['workspace.js','app.js'].map(f=>fs.readFileSync(path.join(root,'static',f),'utf8')).join('\n');
const pause=ms=>new Promise(r=>setTimeout(r,ms));async function until(fn){for(let i=0;i<500;i++){if(fn())return;await pause(5);}throw Error('UI timeout');}
(async()=>{
 const dom=new JSDOM(html,{url:'http://localhost:8423',runScripts:'outside-only'}),w=dom.window,$=id=>w.document.getElementById(id);
 w.AbortController=AbortController;w.HTMLDialogElement.prototype.showModal=function(){this.open=true};w.HTMLDialogElement.prototype.close=function(){this.open=false};
 w.localStorage.setItem('tariff-route-v44',JSON.stringify({origin:'Москва',destination:'Казань'}));
 let extractions=0,uploads=0,commits=0,selections=0,applied=false;const errors=[];w.addEventListener('error',e=>errors.push(e.message));
 const id='b'.repeat(32),profiles=[{id:'w100',label:'до 100 кг',range_weight:'до 100 кг',description:'100 кг',weight_kg:100}];
 const files=[{id,company:'ДЛ',original_filename:'Прайс из Москвы.pdf',source_file:id+'.pdf',extension:'.pdf',uploaded_at:'2026-09-01T00:00:00Z',route_count:1,values_count:1}];
 const item=()=>({company:'ДЛ',company_label:'ДЛ',price:applied?3210:null,comparison_value:applied?3210:null,uploaded:applied,document_selected:applied,online:false,status:applied?'ok':'document_unavailable'});
 w.fetch=async(url,request={})=>{
  const u=new URL(url,w.location.href),p=u.pathname;let data;
  if(p==='/api/options')data={origins:['Москва'],destinations:['Казань'],selected_origin:'Москва',selected_destination:'Казань',companies:[{id:'ДЛ',label:'ДЛ'},{id:'Werner',label:'Werner'}],profiles,integrations:[]};
  else if(p==='/api/compare')data={origin:'Москва',destination:'Казань',profile_id:'w100',items:[item()]};
  else if(p==='/api/profile-matrix')data={profiles:[{profile:profiles[0],items:[item()]}]};
  else if(p==='/api/active-collect'||p==='/api/bulk')data={status:'idle'};
  else if(p==='/api/bulk/history')data={jobs:[]};
  else if(p==='/api/bulk/plan')data={routes:254,companies:17,checks:4318};
  else if(p==='/api/storage')data={files:1,bytes:100};
  else if(p==='/api/price-documents')data={files};
  else if(p==='/api/imports')data={companies:{}};
  else if(p==='/api/route-documents')data={files:applied?files.map(f=>({...f,selected:true,route_values_count:1})):[],total_files:1};
  else if(p==='/api/price-documents/'+id+'/extract'){
   extractions++;assert.equal(request.method,'POST');const b=JSON.parse(request.body);
   assert.deepEqual([b.origin,b.destination],['Москва','Казань']);assert.match(b.job_id,/^[a-f0-9]{32}$/);
   assert.equal('file' in b,false);
   data={job_id:b.job_id,status:'ready',preview:{token:'new-route',company:'ДЛ',origin:'Москва',destination:'Казань',rows:[{profile:profiles[0],price:3210}],warnings:[],missing_profiles:[],meta:{parser:'PDF',document_id:id}}};
  }else if(p==='/api/import/jobs'){uploads++;throw Error('No upload expected');}
  else if(p==='/api/import/commit'){commits++;applied=true;data={company:'ДЛ',origin:'Москва',destination:'Казань',rows:1,meta:{original_filename:files[0].original_filename,document_id:id}};}
  else if(p==='/api/route-documents/select'){selections++;assert.equal(JSON.parse(request.body).document_id,id);data={company:'ДЛ',origin:'Москва',destination:'Казань',filename:files[0].original_filename,applied_prices:1,document_id:id};}
  else throw Error('Unexpected '+url);
  return {ok:true,status:200,json:async()=>data};
 };
 w.eval(scripts);await until(()=>$('originSelect').value==='Москва'&&$('mobileMatrixBody').rows.length===1);
 $('importButton').click();await until(()=>$('importSavedFile').value===id);
 assert.equal($('importUploadPanel').hidden,true);assert.equal($('importLibraryMode').getAttribute('aria-pressed'),'true');
 $('importPreviewButton').click();await until(()=>!$('importPreview').hidden);
 assert.equal(extractions,1);assert.equal(uploads,0);assert.equal(commits,0);assert.equal($('importApplyButton').disabled,true);
 $('importConfirmed').checked=true;$('importConfirmed').dispatchEvent(new w.Event('change'));$('importApplyButton').click();
 await until(()=>commits===1&&!$('importCompany').disabled);
 assert.match($('importApplyFeedback').textContent,/применён/);assert.match($('mobileMatrixBody').textContent,/3\s210 ₽/);
 $('mobileAllCompanies').click();assert.equal($('mobileAllCompanies').getAttribute('aria-pressed'),'true');
 $('mobileAllCompanies').click();assert.equal($('mobileAllCompanies').getAttribute('aria-pressed'),'false');
 $('importDoneButton').click();$('importButton').click();await until(()=>$('importPreviewButton').textContent==='Применить сохранённые цены');
 $('importPreviewButton').click();await until(()=>selections===1&&!$('importCompany').disabled);
 assert.match($('importApplyFeedback').textContent,/Цены применены/);assert.equal(extractions,1);assert.equal(uploads,0);
 $('importCompany').value='Werner';$('importCompany').dispatchEvent(new w.Event('change'));
 assert.equal($('importSavedFile').options.length,1);assert.match($('importSavedHint').textContent,/пока нет/);
 $('importUploadMode').click();assert.equal($('importUploadPanel').hidden,false);
 assert.deepEqual(errors,[]);dom.window.close();console.log('PASS: saved document extracts a new route without upload, explicit apply, shared table, ready prices skip OCR, company isolation and mobile table controls');
})().catch(e=>{console.error(e);process.exit(1)});
