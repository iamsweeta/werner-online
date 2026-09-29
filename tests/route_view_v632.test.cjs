const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path');
const {JSDOM}=require('jsdom');
const root=path.resolve(__dirname,'..');
const wait=ms=>new Promise(resolve=>setTimeout(resolve,ms));
async function until(fn){for(let i=0;i<300;i++){if(fn())return;await wait(5);}throw Error('UI not ready');}
const dom=new JSDOM(fs.readFileSync(path.join(root,'static/index.html'),'utf8'),{url:'https://tariffs.example',runScripts:'outside-only'});
const w=dom.window;w.AbortController=AbortController;
const timeout=w.setTimeout.bind(w);w.setTimeout=(fn,ms,...args)=>timeout(fn,[2000,3000].includes(ms)?5:ms,...args);
const profile={id:'w100',weight_kg:100,description:'до 100 кг',range_weight:'до 100 кг'};
let views=0,catalogs=0,polls=0,hold=null;
w.fetch=async(url)=>{
 const u=new URL(url,w.location.href);let data={status:'idle'};
 if(u.pathname==='/api/options'){
  catalogs++;
  data={route_view:true,origins:['Москва','Санкт-Петербург'],all_origins:['Москва','Санкт-Петербург'],destinations:['Москва'],selected_origin:'Санкт-Петербург',selected_destination:'Москва',companies:[{id:'Werner',label:'Werner'}],profiles:[profile],integrations:[],integration_status:{}};
 }else if(u.pathname==='/api/route-view'){
  views++;if(hold)await hold;
  const items=[{company:'Werner',company_label:'Werner',profile_id:'w100',status:'ok',comparison_value:1300,price:1300,online:false,published_rate_per_kg:13}];
  data={comparison:{origin:'Санкт-Петербург',destination:'Москва',profile_id:'w100',items,range_weight:'до 100 кг'},matrix:{profiles:[{profile,items}]}};
 }else if(['/api/compare','/api/profile-matrix'].includes(u.pathname))throw Error('Legacy double request used');
 else if(u.pathname==='/api/price-documents')data={files:[]};
 else if(u.pathname==='/api/route-documents')data={files:[],total_files:0};
 else if(u.pathname==='/api/storage')data={files:0,bytes:0,persistence:{needs_attention:false,persistence_status:'cloud',message:'Cloud'}};
 else if(u.pathname==='/api/collect-status')data={job_id:'job',status:++polls>1?'done':'running',origin:'Санкт-Петербург',destination:'Москва',requested_companies:['Werner'],results:[],progress_revision:polls};
 return {ok:true,json:async()=>data};
};
w.eval(fs.readFileSync(path.join(root,'static/workspace.js'),'utf8'));
w.eval(fs.readFileSync(path.join(root,'static/app.js'),'utf8')+'\nwindow.s=state;');
(async()=>{
 await until(()=>w.s.matrix&&!w.s.initializing);await wait(25);
 assert.equal(views,1,'opening the page must not calculate three times');
 assert.equal(catalogs,1);
 let release;hold=new Promise(resolve=>{release=resolve;});
 const first=w.compare(),second=w.compare();await wait(5);
 assert.equal(views,2,'identical concurrent refresh shares the in-flight request');
 release();await Promise.all([first,second]);hold=null;
 await w.monitorJob({origin:'Санкт-Петербург',destination:'Москва'});
 assert.equal(polls,2);
 assert.equal(catalogs,1,'progress refreshes prices, not the unchanged city catalog');
 assert.equal(w.document.querySelector('#originSelect').disabled,false);
 assert.equal(w.document.querySelector('#connectionWarning').hidden,true);
 dom.window.close();console.log('PASS: one route snapshot at startup, shared concurrent request, progress without repeated catalog loads');
})().catch(error=>{console.error(error);dom.window.close();process.exit(1);});
