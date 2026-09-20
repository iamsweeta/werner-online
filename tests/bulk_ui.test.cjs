// Run with the existing jsdom QA environment.
const assert=require('node:assert/strict');
const fs=require('node:fs'),path=require('node:path');
const {JSDOM}=require('jsdom');
const root=path.resolve(__dirname,'..');
const html=fs.readFileSync(path.join(root,'static/index.html'),'utf8').replace(/<script src="\/static\/app.js[^>]*><\/script>/,'');
const script=fs.readFileSync(path.join(root,'static/app.js'),'utf8');
const pause=ms=>new Promise(r=>setTimeout(r,ms));
async function until(fn){for(let i=0;i<400;i++){if(fn())return;await pause(5)}throw Error('UI state timeout');}
(async()=>{
 const dom=new JSDOM(html,{url:'http://localhost:8423',runScripts:'outside-only'}),w=dom.window,$=id=>w.document.getElementById(id);
 w.AbortController=AbortController;w.localStorage.setItem('tariff-auto-refresh-v424','0');
 const originalTimeout=w.setTimeout.bind(w);w.setTimeout=(fn,ms,...args)=>originalTimeout(fn,ms===2500?15:ms,...args);
 const profiles=[{id:'w100',label:'100 кг',weight_kg:100,description:'100 кг',range_weight:'100 кг'}];
 const options={origins:['Москва','Санкт-Петербург','Казань'],destinations:['Москва','Казань'],selected_origin:'Санкт-Петербург',selected_destination:'Москва',companies:[{id:'Werner',label:'Werner'},{id:'ДЛ',label:'ДЛ'}],profiles,integrations:[],integration_status:{}};
 let job={status:'idle'},starts=[],actions=[],plans=[],routeCollect=0;
 const response=data=>({ok:true,json:async()=>data});
 function status(status,extra={}){return {job_id:'bulk-test',status,total_routes:254,completed_routes:0,total_checks:4318,completed_checks:0,companies:['Werner','ДЛ'],outcomes:{},percent:0,export_status:'idle',recent:[],...extra};}
 w.fetch=async(url,request={})=>{
  const u=new URL(url,w.location.href),p=u.pathname;
  if(p==='/api/options')return response(options);
  if(p==='/api/compare')return response({origin:'Санкт-Петербург',destination:'Москва',profile_id:'w100',items:[],range_weight:'100 кг'});
  if(p==='/api/profile-matrix')return response({profiles:[]});
  if(p==='/api/active-collect')return response({status:'idle'});
  if(p==='/api/collect'){routeCollect++;return response({status:'fresh'});}
  if(p==='/api/bulk/plan'){const body=JSON.parse(request.body);plans.push(body);return response({routes:body.scope==='all'?54056:254,companies:17,checks:body.scope==='all'?918952:4318,export_parts:body.scope==='all'?271:1});}
  if(p==='/api/bulk'&&request.method==='POST'){starts.push(JSON.parse(request.body));job=status('running');return response(job);}
  if(p==='/api/bulk'||p==='/api/bulk/bulk-test')return response(job);
  if(p.startsWith('/api/bulk/bulk-test/')){
   const action=p.split('/').pop();actions.push(action);
   if(action==='pause')job=status('paused',{completed_routes:2,completed_checks:36,outcomes:{complete:30,failed:6}});
   else if(action==='resume'||action==='retry')job=status('running',{completed_routes:2,completed_checks:36});
   else if(action==='export')job=status('paused',{completed_routes:2,completed_checks:36,export_status:'running'});
   return response(job);
  }
  throw Error('Unexpected '+url);
 };
 w.eval(script);await until(()=>!$('bulkStartButton').disabled);
 assert.match($('bulkPlan').textContent,/254 маршрутов/);assert.equal(plans[0].scope,'reference');
 $('clearCompaniesButton').click();assert.match($('calculationSelectionMessage').textContent,/0 из/);
 $('bulkStartButton').click();await until(()=>!$('bulkPauseButton').hidden);
 assert.equal(starts.length,1);assert.deepEqual(starts[0],{scope:'reference'});
 assert.equal($('bulkStartButton').disabled,true);assert.equal($('collectButton').disabled,true);
 assert.equal($('autoRefreshToggle'),null,'collection only starts on explicit request');
 await pause(20);assert.equal(routeCollect,0);
 $('bulkPauseButton').click();await until(()=>!$('bulkResumeButton').hidden);
 assert.equal($('bulkPauseButton').hidden,true);assert.equal($('bulkRetryButton').hidden,false);
 $('bulkResumeButton').click();await until(()=>!$('bulkPauseButton').hidden);
 job=status('done',{completed_routes:254,completed_checks:4318,export_status:'running',percent:100,outcomes:{complete:4294,failed:24}});
 await until(()=>$('bulkStatus').textContent.includes('Создаётся Excel'));assert.equal($('bulkStartButton').disabled,true);
 job={...job,export_status:'ready',download_url:'/api/bulk/bulk-test/download',recent:[{company:'ДЛ',origin:'Москва',destination:'Казань',status:'failed',exact_weights:0,message:'<script>alert(1)</script>'}]};
 await until(()=>!$('bulkDownload').hidden);
 assert.equal($('bulkDownload').getAttribute('href'),'/api/bulk/bulk-test/download');
 assert.equal($('bulkRecent').querySelector('script'),null);
 $('bulkScope').value='all';$('bulkScope').dispatchEvent(new w.Event('change'));
 await until(()=>$('bulkPlan').textContent.includes('54'));assert.equal(plans.at(-1).scope,'all');
 $('bulkScope').value='origins';$('bulkScope').dispatchEvent(new w.Event('change'));
 await until(()=>!$('bulkOriginsField').hidden);assert.equal($('bulkDestinationsField').hidden,false);
 await until(()=>!$('bulkStartButton').disabled&&$('bulkPlan').textContent.includes('254'));
 assert.deepEqual(actions.slice(0,2),['pause','resume']);
 dom.window.close();console.log('PASS: all-company scope ignores filters, progress, pause/resume, automatic refresh exclusion, export readiness, route planning, error escaping');
})().catch(e=>{console.error(e);process.exit(1)});
