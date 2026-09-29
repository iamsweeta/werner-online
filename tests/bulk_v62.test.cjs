const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),{JSDOM}=require('jsdom');
const root=path.resolve(__dirname,'..');
const dom=new JSDOM(fs.readFileSync(path.join(root,'static/index.html'),'utf8'),{url:'http://localhost:8423',runScripts:'outside-only'}),w=dom.window,$=id=>w.document.getElementById(id);
w.eval(fs.readFileSync(path.join(root,'static/app.js'),'utf8').replace('init().catch(e=>toast(e.message));','')+'\n'+fs.readFileSync(path.join(root,'static/workspace.js'),'utf8')+'\nwindow.bs=bulkState;');
w.compare=async()=>{};w.loadBulkHistory=async()=>{};
const job=id=>({job_id:id,status:'done',mode:'saved',total_routes:1,completed_routes:1,total_checks:17,completed_checks:17,percent:100,outcomes:{saved:17},export_status:'ready',download_url:'/api/bulk/'+id+'/download',recent:[]});
const deferred=()=>{let resolve;const promise=new Promise(r=>resolve=r);return{promise,resolve}};
(async()=>{
 // A late response to an old monitor must not replace a newly selected job.
 const old=deferred();w.getJSON=async url=>url.endsWith('/old')?old.promise:job('new');
 const first=w.monitorBulk('old');await w.monitorBulk('new');old.resolve(job('old'));await first;
 assert.equal(w.bs.job.job_id,'new');assert.equal($('bulkDownload').getAttribute('href'),'/api/bulk/new/download');
 // Applying a document refreshes the selected historical job, not the newest job.
 let requested;w.getJSON=async url=>{requested=url;return{...job('selected'),export_outdated:true,download_url:null}};
 w.renderBulk(job('selected'));await w.refreshBulkAfterDocuments();
 assert.equal(requested,'/api/bulk/selected');assert.equal(w.bs.job.job_id,'selected');assert.equal($('bulkDownload').hidden,true);
 // An in-flight refresh is ignored after another view has been selected.
 const slow=deferred();w.getJSON=async()=>slow.promise;
 const refresh=w.refreshBulkAfterDocuments();w.bs.viewSeq++;w.renderBulk(job('other'));
 slow.resolve(job('selected'));await refresh;assert.equal(w.bs.job.job_id,'other');
 w.renderBulk({status:'idle'});assert.equal($('bulkDownload').hidden,true);assert.equal($('bulkDownload').hasAttribute('href'),false);assert.equal($('bulkProgress').hidden,true);
 dom.window.close();console.log('PASS: stale monitor responses, selected history refresh, stale refresh exclusion and idle controls');
})().catch(e=>{console.error(e);process.exit(1)});
