const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path');
const {JSDOM}=require('jsdom'),root=path.resolve(__dirname,'..');
const dom=new JSDOM(fs.readFileSync(path.join(root,'static/index.html'),'utf8'),{url:'http://localhost:8423',runScripts:'outside-only'});
const w=dom.window,$=id=>w.document.getElementById(id);
w.eval(fs.readFileSync(path.join(root,'static/app.js'),'utf8').replace('init().catch(e=>toast(e.message));','')+'\nwindow.testState=state;');
const s=w.testState,weights=[1,50,100,1000,1500,2000,5000,10000,20000];
const profiles=weights.map(weight=>({id:'w'+weight,weight_kg:weight,label:weight+' кг',range_weight:weight+' кг'}));
s.options={profiles,companies:[{id:'ДЛ',label:'ДЛ'}]};s.calculationCompanies.add('ДЛ');
const data={profiles:profiles.map(p=>({profile:p,items:[{company:'ДЛ',comparison_value:p.weight_kg*10,price:p.weight_kg*10,status:'ok',online:false,refresh_status:'failed',captured_at:'2020-01-01T00:00:00Z'}]}))};
assert.deepEqual([...$('graphScopeSelect').options].map(x=>x.value),['small','medium','heavy','custom']);
for(const [scope,expected] of [['small',[1,50]],['medium',[100,1000,1500]],['heavy',[1500,2000,5000]]]){
 s.graphScope=scope;w.renderTariffGraph(data);
 const titles=[...$('rateChart').querySelectorAll('circle title')].map(x=>x.textContent);
 assert.equal(titles.length,expected.length);
 expected.forEach(n=>assert.ok(titles.some(t=>t.includes('· '+n+' кг ·'))));
 assert.ok(titles.every(t=>!t.includes('10000 кг')&&!t.includes('20000 кг')));
 assert.doesNotMatch($('rateChart').innerHTML,/NaN|Infinity/);
}
assert.match(w.statusLabel(data.profiles[0].items[0]),/Не обновилось/);
assert.match(w.statusLabel({...data.profiles[0].items[0],online:true,refresh_status:'success'}),/Обновлено/);
assert.match(w.sourceBlock(data.profiles[0].items[0]),/СОХРАНЁННАЯ ЦЕНА/);
assert.equal($('autoRefreshToggle'),null);
assert.equal($('documentFile').hasAttribute('accept'),false,'browser chooser must allow quoted PDF names');
assert.equal($('importFile').hasAttribute('accept'),false);
assert.match(w.document.querySelector('.source-help').textContent,/Не обновилось/);
assert.match(w.document.querySelector('.dellin-help').textContent,/pricelist.pdf'/);
dom.window.close();console.log('PASS: three bounded graph scales, honest retained-price badges and PDF chooser/help');
