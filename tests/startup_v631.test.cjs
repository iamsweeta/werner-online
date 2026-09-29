const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const {JSDOM}=require('jsdom');
const root=path.resolve(__dirname,'..');
const html=fs.readFileSync(path.join(root,'static/index.html'),'utf8');
const script=fs.readFileSync(path.join(root,'static/app.js'),'utf8');
const wait=ms=>new Promise(r=>setTimeout(r,ms));
async function until(fn){for(let i=0;i<200;i++){if(fn())return;await wait(5);}throw Error('UI did not recover');}
const profiles=[{id:'w100',weight_kg:100,description:'до 100 кг',range_weight:'до 100 кг'}];
function catalog(){return {origins:['Москва','Санкт-Петербург'],all_origins:['Москва','Санкт-Петербург'],destinations:['Москва'],selected_origin:'Санкт-Петербург',selected_destination:'Москва',companies:[{id:'Werner',label:'Werner'}],profiles,integrations:[],integration_status:{}};}

async function recovery(status){
 const dom=new JSDOM(html,{url:'https://tariffs.example/',runScripts:'outside-only'});
 const w=dom.window,doc=w.document;w.AbortController=AbortController;
 let failed=true,optionCalls=0;const requests=[];
 w.fetch=async(url)=>{
  const u=new URL(url,w.location.href);requests.push(u);
  if(u.pathname==='/api/options'){
   optionCalls++;
   if(failed)return {ok:false,status,json:async()=>({})};
   return {ok:true,json:async()=>catalog()};
  }
  let data={status:'idle'};
  if(u.pathname==='/api/compare')data={origin:'Санкт-Петербург',destination:'Москва',profile_id:'w100',items:[],range_weight:'до 100 кг'};
  if(u.pathname==='/api/profile-matrix')data={profiles:profiles.map(profile=>({profile,items:[]}))};
  return {ok:true,json:async()=>data};
 };
 w.eval(script+'\nwindow.testState=state;');
 await until(()=>!doc.querySelector('#connectionWarning').hidden&&!doc.querySelector('#connectionRetry').disabled);
 assert.equal(optionCalls,1,'401/503 must not trigger a blind second catalog request');
 assert.equal(doc.querySelector('#connectionLogin').hidden,status!==401);
 if(status===401)assert.match(doc.querySelector('#connectionWarningText').textContent,/включён пароль/);
 assert.equal(doc.querySelector('#originSelect').options.length,0);
 failed=false;doc.querySelector('#connectionRetry').click();
 await until(()=>doc.querySelector('#originSelect').options.length===2&&!w.testState.initializing);
 assert.equal(doc.querySelector('#connectionWarning').hidden,true);
 assert.equal(doc.querySelector('#destinationSelect').value,'Москва');
 assert.equal(doc.querySelector('#destinationSelect').disabled,false);
 assert.equal(doc.querySelector('#bulkOrigins').options.length,2);
 assert.equal(w.testState.calculationCompanies.size,1);
 assert.ok(requests.filter(u=>u.pathname==='/api/options').every(u=>u.searchParams.get('include_status')==='false'),'catalog does not read prices on startup');
 dom.window.close();
}

async function timeout(){
 const dom=new JSDOM(html,{url:'https://tariffs.example/',runScripts:'outside-only'});
 const w=dom.window;w.AbortController=AbortController;
 w.fetch=async(url,options)=>new Promise((resolve,reject)=>options.signal.addEventListener('abort',()=>reject(new w.DOMException('Aborted','AbortError'))));
 w.eval(script.replace('init().catch(e=>toast(e.message));',''));
 await assert.rejects(w.getJSON('/api/options',{timeout:5}),error=>error.code==='request_timeout'&&/не ответил вовремя/.test(error.message));
 // A deliberate route switch must remain a cancellation, not a timeout.
 const c=new AbortController();const request=w.getJSON('/api/options',{signal:c.signal,timeout:1000});c.abort();
 await assert.rejects(request,error=>error.name==='AbortError');
 dom.window.close();
}

(async()=>{await recovery(401);await recovery(503);await timeout();console.log('PASS: persistent login/server error, retry fills route and bulk lists, independent catalog, timeout vs route cancellation');})().catch(error=>{console.error(error);process.exit(1);});
