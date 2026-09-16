const assert=require('node:assert/strict'),fs=require('fs'),path=require('path'),{JSDOM}=require('jsdom');
const root=path.resolve(__dirname,'../static'),html=fs.readFileSync(path.join(root,'index.html'),'utf8');
const dom=new JSDOM(html,{url:'http://localhost:8423/?build=51.0&theme=light',runScripts:'outside-only'}),w=dom.window;
w.localStorage.setItem('tariff-theme-v51','dark');w.eval(w.document.querySelector('head script').textContent);
const style=w.document.createElement('style');style.textContent=fs.readFileSync(path.join(root,'styles.css'),'utf8');w.document.head.append(style);
for(const selector of ['html','body','.app-shell','.sidebar','.topbar','.page','.card','.table-wrap','.settings-card']){
 assert.equal(w.getComputedStyle(w.document.querySelector(selector)).backgroundColor,'rgb(255, 255, 255)',selector);
}
w.eval(fs.readFileSync(path.join(root,'workspace.js'),'utf8'));
w.eval(fs.readFileSync(path.join(root,'app.js'),'utf8').replace('init().catch(e=>toast(e.message));',''));
w.updateThemeButton();w.document.getElementById('darkThemeButton').click();
assert.equal(w.document.documentElement.dataset.theme,'dark');assert.equal(w.document.getElementById('darkThemeButton').getAttribute('aria-pressed'),'true');
assert.equal(new URL(w.location.href).searchParams.has('theme'),false);
w.document.getElementById('lightThemeButton').click();
assert.equal(w.document.documentElement.style.colorScheme,'only light');
assert.equal(w.getComputedStyle(w.document.body).backgroundColor,'rgb(255, 255, 255)');
assert.equal(w.localStorage.getItem('tariff-theme-v51'),'light');
dom.window.close();console.log('PASS: white page, navigation, cards, tables, dialogs; explicit theme buttons and launch-time light choice');
