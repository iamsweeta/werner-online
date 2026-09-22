const assert=require('node:assert/strict');
const fs=require('node:fs'),path=require('node:path');const {JSDOM}=require('jsdom');
const root=path.resolve(__dirname,'..'),html=fs.readFileSync(path.join(root,'static/index.html'),'utf8'),script=fs.readFileSync(path.join(root,'static/app.js'),'utf8');
const pause=ms=>new Promise(r=>setTimeout(r,ms));
async function until(fn){for(let i=0;i<600;i++){if(fn())return;await pause(5);}throw Error('UI timeout');}
async function scenario(mode){
 let posts=0,polls=0,commits=0,jobId=null,ready=false;
 const profile={id:'w100',label:'до 100 кг',weight_kg:100,range_weight:'до 100 кг'};
 const preview={token:'preview-token',company:'Werner',origin:'Казань',destination:'Уфа',rows:[{profile,price:1234}],warnings:['Проверьте оригинал'],missing_profiles:[],meta:{parser:'OCR',ocr:true}};
 function make(saved={}){
  const dom=new JSDOM(html,{url:'http://localhost:8423',runScripts:'outside-only'}),w=dom.window,$=id=>w.document.getElementById(id);
  w.AbortController=AbortController;w.HTMLDialogElement.prototype.showModal=function(){this.open=true;};w.HTMLDialogElement.prototype.close=function(){this.open=false;};
  const timer=w.setTimeout.bind(w);w.setTimeout=(fn,ms,...args)=>timer(fn,[1000,1200,2000,3000,4000,5000].includes(ms)?8:ms,...args);
  w.localStorage.setItem('tariff-route-v44',JSON.stringify({origin:'Казань',destination:'Уфа'}));for(const [k,v] of Object.entries(saved))w.localStorage.setItem(k,v);
  const item={company:'Werner',company_label:'Werner',price:null,comparison_value:null,online:false,refresh_status:'not_run'};
  w.fetch=async(url,opts={})=>{
   const u=new URL(url,w.location.href);let data;
   if(u.pathname==='/api/options')data={origins:['Казань'],destinations:['Уфа'],selected_origin:'Казань',selected_destination:'Уфа',companies:[{id:'Werner',label:'Werner'}],profiles:[profile],integrations:[]};
   else if(u.pathname==='/api/compare')data={origin:'Казань',destination:'Уфа',profile_id:'w100',items:[item]};
   else if(u.pathname==='/api/profile-matrix')data={profiles:[{profile,items:[item]}]};
   else if(u.pathname==='/api/active-collect')data={status:'idle'};
   else if(u.pathname==='/api/imports')data={companies:{}};
   else if(u.pathname==='/api/import/jobs'&&opts.method==='POST'){
    posts++;jobId=opts.body.get('job_id');assert.match(jobId,/^[a-f0-9]{32}$/);assert.equal(opts.body.get('origin'),'Казань');assert.equal(opts.body.get('destination'),'Уфа');
    if(mode==='post502')return {ok:false,status:502,json:async()=>{throw Error('HTML from proxy');}};
    data={job_id:jobId,status:'queued',message:'Файл принят'};
   }else if(u.pathname==='/api/import/jobs/'+jobId){
    polls++;if(mode==='poll502'&&polls===1)return {ok:false,status:502,json:async()=>({})};
    data=ready?{job_id:jobId,status:'ready',preview}:{job_id:jobId,status:'parsing',message:'Распознаю скан: страница 2 из 9'};
   }else if(u.pathname==='/api/import/commit'){commits++;data={company:'Werner',origin:'Казань',destination:'Уфа',rows:1,meta:{}};}
   else throw Error('Unexpected '+url);
   return {ok:true,status:200,json:async()=>data};
  };
  w.eval(script);return {dom,w,$};
 }
 let current=make();await until(()=>current.$('comparisonTable').textContent.includes('Werner'));
 current.$('importButton').click();await until(()=>current.$('importDialog').open);
 Object.defineProperty(current.$('importFile'),'files',{configurable:true,value:[new current.w.File(['scan'],'scan.pdf')]});
 current.$('importPreviewButton').click();await until(()=>polls>=2);
 assert.equal(posts,1);assert.equal(commits,0);assert.equal(current.$('importApplyButton').disabled,true);
 if(mode==='reload'){
  const saved=Object.fromEntries(Object.keys(current.w.localStorage).map(k=>[k,current.w.localStorage.getItem(k)]));
  current.dom.window.close();ready=true;current=make(saved);await until(()=>current.$('comparisonTable').textContent.includes('Werner'));current.$('importButton').click();
 }else ready=true;
 await until(()=>!current.$('importPreview').hidden);
 assert.equal(posts,1,'no second upload on 502 or reload');assert.equal(commits,0,'no automatic commit');assert.equal(current.$('importApplyButton').disabled,true);assert.match(current.$('importRows').textContent,/1\s234/);
 current.dom.window.close();
}
(async()=>{for(const mode of ['poll502','post502','reload'])await scenario(mode);console.log('PASS: short async requests, transient 502 retries, lost POST response recovery, page reload resumes same job, explicit confirmation');})().catch(e=>{console.error(e);process.exit(1);});
