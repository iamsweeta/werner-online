const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),{JSDOM}=require('jsdom');
const root=path.resolve(__dirname,'..'),dom=new JSDOM(fs.readFileSync(path.join(root,'static/index.html'),'utf8'),{url:'http://localhost:8423',runScripts:'outside-only'}),w=dom.window,$=id=>w.document.getElementById(id);
w.eval(fs.readFileSync(path.join(root,'static/app.js'),'utf8').replace('init().catch(e=>toast(e.message));','')+'\n'+fs.readFileSync(path.join(root,'static/workspace.js'),'utf8'));
(async()=>{
 for(const id of ['unitModeSelect','graphUnitSelect']){
  assert.equal($(id).closest('details'),null,id+' must be visible with every details panel collapsed');
  assert.equal($(id).closest('[hidden]'),null);
 }
 w.getJSON=async()=>({files:2,bytes:1024,disabled_retention:'Originals kept',data_directory:'/var/data/app',persistence:{needs_attention:true,message:'Persistent disk is missing'}});
 await w.refreshStorageInfo();assert.equal($('storageWarning').hidden,false);assert.match($('storageWarningText').textContent,/Persistent disk/);
 assert.match($('storageInfo').textContent,/2 оригиналов/);
 w.getJSON=async()=>({files:2,bytes:1024,persistence:{needs_attention:false,message:'Disk detected'}});
 await w.refreshStorageInfo();assert.equal($('storageWarning').hidden,true);
 dom.window.close();console.log('PASS: visible price-unit controls and actual storage-status warning');
})().catch(e=>{console.error(e);process.exit(1)});
