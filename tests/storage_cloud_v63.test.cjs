const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),{JSDOM}=require('jsdom');
const root=path.resolve(__dirname,'..');
const html=fs.readFileSync(path.join(root,'static/storage-help.html'),'utf8');
const code=fs.readFileSync(path.join(root,'static/storage-help.js'),'utf8');
const pause=ms=>new Promise(resolve=>setTimeout(resolve,ms));
async function until(check){for(let i=0;i<100;i++){if(check())return;await pause(5);}throw Error('UI timed out');}
(async()=>{
 const dom=new JSDOM(html,{url:'https://tariffs.example/storage-guide',runScripts:'outside-only'}),w=dom.window,$=id=>w.document.getElementById(id);
 let state='idle',posts=0,checks=0,failCheck=false,polls=0;
 const timer=w.setTimeout.bind(w);w.setTimeout=(fn,ms)=>timer(fn,ms===2000?5:ms);
 w.fetch=async(url,opts={})=>{
  let data;
  if(url==='/api/storage')data={persistence:{persistence_status:'cloud',message:'Облачное хранение'}};
  else if(url==='/api/storage/check'){
   checks++;if(failCheck)return {ok:false,json:async()=>({detail:'Хранилище недоступно'})};
   data={ok:true,message:'Файл сохранён и прочитан'};
  }else if(url==='/api/storage/migration'&&opts.method==='POST'){
   posts++;assert.equal(opts.body.get('file').name,'backup.zip');state='running';data={status:state,message:'Архив принят'};
  }else if(url==='/api/storage/migration'){
   if(state==='running'&&++polls>1)state='done';
   data={status:state,message:state==='done'?'Перенос завершён':state==='running'?'Архив принят':'Выберите архив'};
  }else throw Error('Unexpected '+url);
  return {ok:true,json:async()=>data};
 };
 w.eval(code);await until(()=>$('migrationStatus').textContent==='Выберите архив');
 assert.equal($('cloudControls').hidden,false);
 $('checkButton').click();await until(()=>checks===1&&!$('checkButton').disabled);assert.match($('checkStatus').textContent,/✓/);
 failCheck=true;$('checkButton').click();await until(()=>checks===2&&!$('checkButton').disabled);assert.match($('checkStatus').textContent,/недоступно/);
 Object.defineProperty($('archive'),'files',{value:[new w.File(['zip'],'backup.zip')]});
 $('restoreForm').dispatchEvent(new w.Event('submit',{bubbles:true,cancelable:true}));
 await until(()=>$('migrationStatus').textContent==='Перенос завершён');
 assert.equal(posts,1);assert.equal($('restoreButton').disabled,true);assert.equal($('archive').disabled,true);assert.equal($('retryButton').hidden,true);
 assert.equal($('migrationStatus').getAttribute('role'),'status');dom.window.close();
 console.log('PASS: cloud connection feedback, error recovery, accepted backup polling and completed migration controls');
})().catch(error=>{console.error(error);process.exit(1)});
