// Client workflows. Test prices are never bundled as application prices.
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path');
const {JSDOM}=require('jsdom');const root=path.resolve(__dirname,'..');
const html=fs.readFileSync(path.join(root,'static/index.html'),'utf8');
const script=['workspace.js','app.js'].map(f=>fs.readFileSync(path.join(root,'static',f),'utf8')).join('\n');
const pause=ms=>new Promise(r=>setTimeout(r,ms));
async function until(fn){for(let i=0;i<400;i++){if(fn())return;await pause(5)}throw Error('UI state timeout');}
(async()=>{
 const dom=new JSDOM(html,{url:'http://localhost:8423',runScripts:'outside-only'}),w=dom.window,$=id=>w.document.getElementById(id);
 w.AbortController=AbortController;const errors=[];w.addEventListener('error',e=>errors.push(e.message));
 const profiles=[{id:'w100',label:'до 100 кг',weight_kg:100,description:'100 кг',range_weight:'100 кг'}];
 const options={origins:['Москва','Санкт-Петербург','Казань'],destinations:['Москва','Казань'],selected_origin:'Санкт-Петербург',selected_destination:'Москва',companies:[{id:'Werner',label:'Werner'},{id:'ДЛ',label:'ДЛ'}],profiles,integrations:[],integration_status:{},import_guide:{Werner:{page_url:'https://example.invalid/tariffs',instruction:'Скачать прайс компании'}}};
 let job={status:'idle'},files=[],routeCollect=0,starts=[],commits=0,previewPolls=0;
 const response=data=>({ok:true,json:async()=>data});
 const originalTimeout=w.setTimeout.bind(w);w.setTimeout=(fn,ms,...args)=>originalTimeout(fn,[1200,2500].includes(ms)?10:ms,...args);
 const documentJob={token:'a'.repeat(32),status:'ready',company:'Werner',filename:'test.csv',values_count:2,warnings:['Проверьте дату'],errors:[],routes:[{origin:'Казань',destination:'Москва',meta:{source_row:2},values:{w100:{price:1234.5}}},{origin:'Москва',destination:'Казань',meta:{},values:{w100:{price:2200}}}]};
 w.fetch=async(url,request={})=>{
  const p=new URL(url,w.location.href).pathname;
  if(p==='/api/options')return response(options);
  if(p==='/api/compare')return response({origin:'Санкт-Петербург',destination:'Москва',profile_id:'w100',items:[],range_weight:'100 кг'});
  if(p==='/api/profile-matrix')return response({profiles:[]});
  if(p==='/api/active-collect')return response({status:'idle'});
  if(p==='/api/collect'){routeCollect++;return response({status:'fresh'});}
  if(p==='/api/bulk/plan')return response({routes:254,companies:17,checks:4318,export_parts:1});
  if(p==='/api/bulk'&&request.method==='POST'){
   starts.push(JSON.parse(request.body));job={job_id:'bulk-test',mode:starts.at(-1).mode,status:'done',total_routes:254,completed_routes:254,total_checks:4318,completed_checks:4318,companies:['Werner','ДЛ'],outcomes:{saved:4318},percent:100,export_status:'ready',recent:[],download_url:'/api/bulk/bulk-test/download'};return response(job);
  }
  if(p==='/api/bulk'||p==='/api/bulk/bulk-test')return response(job);
  if(p==='/api/price-documents')return response({files});
  if(p==='/api/price-documents/preview'){
   assert.equal(request.body.get('company'),'Werner');return response({...documentJob,status:'parsing',done:0,total:2,matched:0});
  }
  if(p.startsWith('/api/price-documents/preview/')){previewPolls++;return response(documentJob);}
  if(p==='/api/price-documents/commit'){
   commits++;assert.equal(JSON.parse(request.body).token,documentJob.token);assert.deepEqual(JSON.parse(request.body).resolutions,{'0':1});
   files=[{id:documentJob.token,company:'Werner',original_filename:'test.csv',extension:'.csv',route_count:2,values_count:2,uploaded_at:new Date().toISOString(),source_file:documentJob.token+'.csv'}];
   job={...job,export_outdated:true,download_url:null};return response({route_count:2,values_count:2});
  }
  if(p.startsWith('/api/price-documents/')&&request.method==='DELETE'){files=[];return response({ok:true});}
  throw Error('Unexpected '+url);
 };
 w.eval(script);await until(()=>!$('bulkStartButton').disabled&&$('documentCompany').options.length===2);
 assert.equal($('bulkPanel').hidden,true);assert.equal($('routeWorkspace').hidden,false);assert.equal($('documentsPanel').hidden,true);$('bulkOpenButton').click();
 await pause(25);assert.equal(routeCollect,0,'primary report must not launch hidden route refresh');
 assert.equal(w.document.documentElement.dataset.theme,'light');$('themeButton').click();assert.equal(w.document.documentElement.dataset.theme,'dark');assert.match($('themeButton').textContent,/Светлая/);
 $('themeButton').click();assert.equal(w.document.documentElement.dataset.theme,'light');
 $('bulkSavedButton').click();await until(()=>starts.length===1&&!$('bulkDownload').hidden);assert.equal(starts[0].mode,'saved');
 $('bulkDocumentsButton').click();assert.equal($('documentsPanel').hidden,false);assert.equal($('bulkPanel').hidden,true);
 assert.match($('documentGuide').textContent,/Скачать прайс компании/);assert.equal($('documentGuide').querySelector('a').getAttribute('href'),'https://example.invalid/tariffs');
 w.selectDocument(new w.File(['fixture'],'test.csv',{type:'text/csv'}));$('documentParseButton').click();
 await until(()=>!$('documentPreview').hidden&&!$('documentParseButton').disabled);assert.equal(previewPolls,1);assert.equal(commits,0);
 assert.equal($('documentCommitButton').disabled,true);assert.equal($('documentRoutePreview').options.length,2);
 assert.match($('documentPriceRows').textContent,/1\s234,5/,'preview preserves kopecks');
 $('documentRoutePreview').value='1';$('documentRoutePreview').dispatchEvent(new w.Event('change'));assert.match($('documentPriceRows').textContent,/2\s200/);
 documentJob.conflicts=[{id:'0',origin:'Казань',destination:'Москва',profile_id:'w100',options:[{price:1234.5,original_filename:'first.csv'},{price:777.7,original_filename:'second.csv'}]}];
 w.selectDocuments([new w.File(['fixture'],'first.csv'),new w.File(['fixture'],'second.csv')]);$('documentParseButton').click();
 await until(()=>!$('documentPreview').hidden&&!$('documentParseButton').disabled);assert.equal($('documentConflicts').hidden,false);assert.match($('documentPriceRows').textContent,/Выберите цену/);
 $('documentConfirmed').checked=true;$('documentConfirmed').dispatchEvent(new w.Event('change'));assert.equal($('documentCommitButton').disabled,true);
 const choice=$('documentConflictChoices').querySelector('select');choice.value='1';choice.dispatchEvent(new w.Event('change'));
 assert.match($('documentPriceRows').textContent,/777,7/);assert.equal($('documentCommitButton').disabled,false);
 $('documentCommitButton').click();await until(()=>commits===1&&$('documentPreview').hidden&&$('documentsList').textContent.includes('test.csv'));
 await until(()=>$('bulkDownload').hidden);assert.match($('bulkStatus').textContent,/Пересоберите/);assert.equal($('bulkExportButton').hidden,false);
 assert.equal($('documentsList').querySelector('a').getAttribute('href'),'/api/import-file/'+documentJob.token+'.csv');
 $('routeTab').click();assert.equal($('routeWorkspace').hidden,false);await pause(20);assert.equal(routeCollect,0,'opening the route never starts a collection');
 assert.match(w.exportExcel.toString(),/\/api\/export\/route\?/);
 $('documentsTab').click();$('documentsTab').dispatchEvent(new w.KeyboardEvent('keydown',{key:'ArrowLeft',bubbles:true}));assert.equal($('bulkPanel').hidden,false);
 assert.equal($('bulkOpenButton').getAttribute('aria-selected'),'true');assert.equal($('routeTab').getAttribute('aria-selected'),'false');
 assert.deepEqual(errors,[]);
 const css=fs.readFileSync(path.join(root,'static/styles.css'),'utf8');assert.match(css,/max-width:\s*520px/);assert.match(css,/prefers-reduced-motion/);assert.match(css,/\[hidden\]/);
 await pause(30);dom.window.close();console.log('PASS: primary workspace, route isolation, light/dark theme, multi-file upload with conflict choices and explicit confirmation, decimal prices, invalidated export, source files, keyboard tabs, responsive rules');
})().catch(e=>{console.error(e);process.exit(1)});
