const $=id=>document.getElementById(id);
let polling=false;
async function api(path,options={}){
  const response=await fetch(path,options);
  let data;try{data=await response.json();}catch{throw Error('Сервер недоступен. Попробуйте ещё раз через минуту.');}
  if(!response.ok)throw Error(data.detail||'Не удалось выполнить действие.');
  return data;
}
function busy(value){$('restoreButton').disabled=value;$('retryButton').disabled=value;}
async function watch(){
  if(polling)return;polling=true;
  try{
    let job;
    do{
      job=await api('/api/storage/migration');
      $('migrationStatus').textContent=job.message||'Выберите ZIP резервной копии.';
      $('retryButton').hidden=!['interrupted','error'].includes(job.status);
      busy(job.status==='running');
      if(job.status==='done'){$('restoreButton').disabled=true;$('archive').disabled=true;}
      if(job.status==='running')await new Promise(resolve=>setTimeout(resolve,2000));
    }while(job.status==='running');
  }catch(error){$('migrationStatus').textContent=error.message+' Обновите страницу для проверки статуса принятого архива.';busy(false);}
  finally{polling=false;}
}
$('checkButton').addEventListener('click',async()=>{
  $('checkButton').disabled=true;$('checkStatus').textContent='Проверяю запись и чтение…';
  try{const result=await api('/api/storage/check',{method:'POST'});$('checkStatus').textContent='✓ '+result.message;}
  catch(error){$('checkStatus').textContent=error.message;}
  finally{$('checkButton').disabled=false;}
});
$('restoreForm').addEventListener('submit',async event=>{
  event.preventDefault();const file=$('archive').files[0];if(!file)return;
  if(file.size>512*1024*1024){$('migrationStatus').textContent='Архив больше 512 МБ. Используйте cloud_setup.py по инструкции.';return;}
  busy(true);$('migrationStatus').textContent='Передаю резервную копию. Не закрывайте страницу до сообщения «Архив принят».';
  const form=new FormData();form.append('file',file);
  try{const job=await api('/api/storage/migration',{method:'POST',body:form});$('migrationStatus').textContent=job.message;await watch();}
  catch(error){$('migrationStatus').textContent=error.message+' Если связь оборвалась, обновите страницу: перенос мог уже начаться.';busy(false);}
});
$('retryButton').addEventListener('click',async()=>{
  busy(true);
  try{await api('/api/storage/migration/retry',{method:'POST'});await watch();}
  catch(error){$('migrationStatus').textContent=error.message;busy(false);}
});
(async()=>{
  try{
    const info=await api('/api/storage');$('storageStatus').textContent=info.persistence.message;
    const cloud=info.persistence.persistence_status==='cloud';$('cloudControls').hidden=!cloud;
    if(cloud)watch();
  }catch(error){$('storageStatus').textContent=error.message;}
})();
