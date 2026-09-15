const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path');
const {JSDOM}=require('jsdom'),root=path.resolve(__dirname,'..');
const html=fs.readFileSync(path.join(root,'static/index.html'),'utf8');
for(const saved of [null,'dark','light','invalid']){
 const dom=new JSDOM(html,{url:'http://localhost:8423',runScripts:'outside-only'}),w=dom.window;
 w.localStorage.setItem('tariff-theme-v42','dark');w.matchMedia=()=>({matches:true});
 if(saved!==null)w.localStorage.setItem('tariff-theme-v501',saved);
 w.eval(w.document.querySelector('head script').textContent);
 assert.equal(w.document.documentElement.dataset.theme,saved==='dark'?'dark':'light');
 assert.equal(w.document.querySelector('meta[name=theme-color]').content,saved==='dark'?'#090a0c':'#ffffff');
 assert.equal(w.document.querySelector('.sidebar-note'),null);
 w.eval(fs.readFileSync(path.join(root,'static/workspace.js'),'utf8'));
 w.eval(fs.readFileSync(path.join(root,'static/app.js'),'utf8').replace('init().catch(e=>toast(e.message));',''));
 w.updateThemeButton();w.document.getElementById('themeButton').click();
 const next=saved==='dark'?'light':'dark';assert.equal(w.document.documentElement.dataset.theme,next);
 assert.equal(w.localStorage.getItem('tariff-theme-v501'),next);
 assert.equal(w.document.getElementById('themeButton').textContent,next==='dark'?'Светлая тема':'Тёмная тема');
 assert.match(w.exportExcel.toString(),/\/api\/export\/route\?/);
 dom.window.close();
}
console.log('PASS: prepaint light default, saved dark/light, invalid legacy values, theme toggle and separate route export');
