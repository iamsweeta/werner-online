// Run: npm install --no-save jsdom@26.1.0; node tests/ui.test.cjs
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const {JSDOM}=require('jsdom');
const root=path.resolve(__dirname,'..');
const html=fs.readFileSync(path.join(root,'static/index.html'),'utf8').replace(/<script src="\/static\/app.js[^>]*><\/script>/,'');
const script=fs.readFileSync(path.join(root,'static/app.js'),'utf8');
const companies=['Werner','Мейджик'];
const profiles=[{id:'w100',weight_kg:100,description:'до 100 кг',range_weight:'до 100 кг',tariff_type:'Стоимость отправки'},{id:'w200',weight_kg:200,description:'до 200 кг',range_weight:'до 200 кг',tariff_type:'Стоимость отправки'}];
const wait=ms=>new Promise(r=>setTimeout(r,ms));
async function until(fn){for(let n=0;n<200;n++){if(fn())return;await wait(5);}throw new Error('UI did not reach expected state');}
async function scenario(resume=false){
 const calls=[];let expired=false,polls=0,job=resume?{job_id:'existing',status:'running',origin:'Санкт-Петербург',destination:'Москва',requested_companies:companies}:null;
 const errors=[];
 const dom=new JSDOM(html,{url:'http://localhost:8423',runScripts:'outside-only'});
 const w=dom.window;
 const nativeTimeout=w.setTimeout.bind(w);w.setTimeout=(fn,ms,...args)=>nativeTimeout(fn,ms===2000||ms===3000?5:ms,...args);
 w.AbortController=AbortController;w.addEventListener('error',e=>errors.push(e.message));
 function items(q,p='w100'){return companies.map(c=>({company:c,company_label:c,profile_id:p,status:'ok',comparison_value:q.get('origin')==='Москва'?1400:1300,price:1300,published_rate_per_kg:q.get('origin')==='Москва'?14:13,online:c==='Werner'&&!expired,price_is_minimum:false,source_type:'Official',source_url:'https://example.test/',captured_at:new Date().toISOString(),refresh_status:c==='Werner'?'success':'failed',refresh_error:c==='Werner'?null:'HTTP 503',calculation_basis:'Тариф по весу'}));}
 w.fetch=async(url,opts={})=>{
  const u=new URL(url,w.location.href),q=u.searchParams;let result;
  if(u.pathname==='/api/options'){
   const cities=['Санкт-Петербург','Москва','Казань','Екатеринбург'];const o=q.get('origin');let d=q.get('destination')||(o==='Москва'?'Санкт-Петербург':'Москва');if(d===o)d=o==='Москва'?'Санкт-Петербург':'Москва';
   result={origins:cities,destinations:cities.filter(x=>x!==o),selected_origin:o,selected_destination:d,paired_destination:d,companies:companies.map(c=>({id:c,label:c})),profiles,integrations:[],integration_status:{}};
  }else if(u.pathname==='/api/compare') result={origin:q.get('origin'),destination:q.get('destination'),profile_id:q.get('profile'),range_weight:'до 100 кг',calculated_at:new Date().toISOString(),items:items(q),exact_count:2,online_count:expired?0:1};
  else if(u.pathname==='/api/profile-matrix')result={profiles:profiles.map(p=>({profile:p,items:items(q,p.id)}))};
  else if(u.pathname==='/api/active-collect')result=job&&job.status==='running'?job:{status:'idle'};
  else if(u.pathname==='/api/collect'){
   const body=JSON.parse(opts.body);calls.push(body);polls=0;
   job={...body,job_id:'test-'+calls.length,status:'running',requested_companies:body.companies};result={...job,status:'queued'};
  }else if(u.pathname==='/api/collect-status'){
   polls++;const done=polls>=2;
   result={...job,status:done?'done':'running',progress_revision:polls,progress_rows:1,completed_companies:done?2:1,results:[{company:'Werner',ok:true,rows:1,message:'ready'},...(done?[{company:'Мейджик',ok:false,rows:0,message:'HTTP 503'}]:[])],success_count:1,failed_count:1};
   if(done)job={...result};
  }else throw new Error('Unexpected fetch '+url);
  return {ok:true,json:async()=>result};
 };
 w.localStorage.setItem('tariff-auto-refresh-v424','1');
 w.localStorage.setItem('tariff-source-mode-v450','mixed');
 w.eval(script+'\nwindow.testState=state;');
 if(resume)await until(()=>w.document.querySelector('#liveAuditTitle').textContent.includes('проверка завершена'));
 else await until(()=>w.document.querySelector('#comparisonTable').textContent.includes('1'));
 await until(()=>!w.document.querySelector('#collectButton').disabled);
 assert.equal(calls.length,0,'startup reads saved prices and only resumes monitoring an existing job');
 if(resume)assert.match(w.document.querySelector('#companyProgress').textContent,/HTTP 503/);
 assert.match(w.document.querySelector('#comparisonTable').textContent,/1\s*300\s*₽/);

 assert.equal(w.document.querySelector('#liveModeSelect').value,'all','opening shows last saved prices, including expired values');
 assert.equal(w.document.querySelector('#exactCount').textContent,'2','counter includes saved values');
 assert.equal(w.document.querySelector('#missingCount').textContent,'0');
 w.document.querySelector('#liveModeSelect').value='all';w.document.querySelector('#liveModeSelect').dispatchEvent(new w.Event('change'));
 assert.equal(w.document.querySelector('#exactCount').textContent,'2');
 const beforeRetry=calls.length;
 w.document.querySelector('#retryFailedButton').click();await until(()=>calls.length===beforeRetry+1);
 assert.deepEqual(calls.at(-1).companies,['Мейджик'],'retry only the failed company');
 await until(()=>!w.document.querySelector('#collectButton').disabled);
 const beforeOne=calls.length;
 w.document.querySelector('[data-retry-company="Мейджик"]').click();await until(()=>calls.length===beforeOne+1);
 assert.deepEqual(calls.at(-1).companies,['Мейджик']);
 await until(()=>!w.document.querySelector('#collectButton').disabled);
 const beforeManual=calls.length;

 w.document.querySelector('#collectButton').click();await until(()=>calls.length===beforeManual+1);
 assert.equal(calls.at(-1).force,true);
 await until(()=>!w.document.querySelector('#collectButton').disabled);
 w.document.querySelector('#swapRouteButton').click();await until(()=>w.document.querySelector('#summaryRoute').textContent==='Москва → Санкт-Петербург');w.document.querySelector('#collectButton').click();await until(()=>calls.at(-1).origin==='Москва');
 assert.equal(calls.at(-1).destination,'Санкт-Петербург');
 await until(()=>!w.document.querySelector('#collectButton').disabled);
 assert.match(w.document.querySelector('#comparisonTable').textContent,/1\s*400\s*₽/);
 w.document.querySelector('#profileSelect').value='w200';w.document.querySelector('#profileSelect').dispatchEvent(new w.Event('change'));
 await until(()=>w.testState.comparison.profile_id==='w200');
 w.document.querySelector('#collectButton').click();await until(()=>calls.at(-1).profile==='w200');
 await until(()=>!w.document.querySelector('#collectButton').disabled);
 assert.equal(w.document.querySelector('#destinationSelect').disabled,false,'destination is selectable after a job');
 w.document.querySelector('#destinationSelect').value='Казань';w.document.querySelector('#destinationSelect').dispatchEvent(new w.Event('change'));
 await until(()=>w.document.querySelector('#summaryRoute').textContent==='Москва → Казань');w.document.querySelector('#collectButton').click();await until(()=>calls.at(-1).destination==='Казань');await until(()=>!w.document.querySelector('#collectButton').disabled);
 assert.equal(w.document.querySelector('#destinationSelect').value,'Казань','progress refresh preserves new destination');
 w.document.querySelector('#originSelect').value='Екатеринбург';w.document.querySelector('#originSelect').dispatchEvent(new w.Event('change'));
 await until(()=>w.document.querySelector('#summaryRoute').textContent==='Екатеринбург → Казань');w.document.querySelector('#collectButton').click();await until(()=>calls.at(-1).origin==='Екатеринбург');await until(()=>!w.document.querySelector('#collectButton').disabled);
 assert.equal(calls.at(-1).destination,'Казань');
 w.document.querySelector('#swapRouteButton').click();await until(()=>w.document.querySelector('#summaryRoute').textContent==='Казань → Екатеринбург');w.document.querySelector('#collectButton').click();await until(()=>calls.at(-1).origin==='Казань');await until(()=>!w.document.querySelector('#collectButton').disabled);
 assert.equal(calls.at(-1).destination,'Екатеринбург','swap works for regional cities');
 assert.deepEqual(JSON.parse(w.localStorage.getItem('tariff-route-v44')),{origin:'Казань',destination:'Екатеринбург'});
 const beforeExpired=calls.length;
 expired=true;await w.resumeRefresh();
 assert.equal(calls.length,beforeExpired,'idle refresh never starts a collection');
 assert.equal(w.document.querySelector('#exactCount').textContent,'2','expired saved values remain visible');
 assert.equal(w.document.querySelector('#onlineCount').textContent,'0','expired prices stop being LIVE even with automatic loading off');
 w.document.querySelector('#liveModeSelect').value='live';w.document.querySelector('#liveModeSelect').dispatchEvent(new w.Event('change'));
 assert.equal(w.document.querySelector('#exactCount').textContent,'0');
 w.document.querySelector('#clearCompaniesButton').click();
 assert.equal(w.document.querySelectorAll('#calculationCompanySelector input:checked').length,0);
 assert.deepEqual(errors,[]);
 dom.window.close();
}
(async()=>{await scenario();await scenario(true);console.log('PASS: saved default and manual loading, progress and errors, retry failed and single company, LIVE counters, manual reload, route and profile changes, empty selection, resume without duplicate job');})().catch(e=>{console.error(e);process.exit(1);});
