// Actual UI in Chromium with an isolated HTTP fixture; no CNC or HA credentials.
const {chromium} = require('playwright');
const assert = require('node:assert/strict');
const http = require('node:http');
const fs = require('node:fs');
const path = require('node:path');
const machine = name => ({id:name,name,controller:{model:'Fixture'},host:'192.0.2.1',port:8082,online:true,enabled:true,signals:{},interval:1,fast_interval:.25,diagnostic_interval:60});
const status = {machines:['Haas 0i-10','alpha','haas 0i-2'].map(machine),ui_settings:{title:'Your machine floor',revision:0},schedule:{shifts:[]},mode:'haas',mqtt_connected:true,scan:{results:[]}};
let apiWrites = 0;
const server = http.createServer((req,res)=>{
  if (req.method !== 'GET') apiWrites++;
  if (req.url === '/api/ui-settings' && req.method === 'PUT') {let body='';req.on('data',c=>body+=c);req.on('end',()=>{const value=JSON.parse(body);status.ui_settings={title:value.title.trim(),revision:status.ui_settings.revision+1};res.setHeader('Content-Type','application/json');res.end(JSON.stringify(status.ui_settings));});return;}
  if (req.url === '/parent') {res.setHeader('Content-Type','text/html');return res.end('<home-assistant></home-assistant><script>document.querySelector("home-assistant").hass={themes:{darkMode:false}};</script><iframe src="/" style="width:100%;height:95vh;border:0"></iframe>');}
  if (req.url === '/api/status') {res.setHeader('Content-Type','application/json');return res.end(JSON.stringify(status));}
  const name = req.url === '/' ? 'index.html' : req.url.replace('/assets/','');
  if (!['index.html','app.js','preferences.js','style.css'].includes(name)) {res.statusCode=404;return res.end();}
  res.setHeader('Content-Type',name.endsWith('.js')?'text/javascript':name.endsWith('.css')?'text/css':'text/html');
  res.end(fs.readFileSync(path.join(__dirname,'../../haas_monitor/web',name)));
});
(async()=>{
  await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
  const browser=await chromium.launch({headless:true,
    executablePath:process.env.UI_CHROMIUM_EXECUTABLE || undefined,
    args:process.env.UI_CHROMIUM_ARGS ? JSON.parse(process.env.UI_CHROMIUM_ARGS) : undefined});
  try {
    const context=await browser.newContext({colorScheme:'dark'}), page=await context.newPage();
    const errors=[];page.on('pageerror',e=>errors.push(e.message));
    const base=`http://127.0.0.1:${server.address().port}`;
    await page.goto(base+'/parent');
    const frame=page.frames().find(f=>f.url()===base+'/');
    await frame.waitForSelector('.machine');
    const names=()=>frame.locator('.machine h3').allTextContents();
    assert.deepEqual(await names(),['alpha','haas 0i-2','Haas 0i-10']);
    assert.equal(await frame.locator('#appearance').inputValue(),'automatic');
    assert.equal(await frame.locator('html').getAttribute('data-theme'),'light'); // HA wins over dark OS.
    await page.evaluate(()=>document.querySelector('home-assistant').hass.themes.darkMode=true);
    await frame.waitForFunction(()=>document.documentElement.dataset.theme==='dark');
    await frame.selectOption('#appearance','light');
    assert.equal(await frame.locator('html').getAttribute('data-theme'),'light');
    if (process.env.UI_SCREENSHOT_DIR) {fs.mkdirSync(process.env.UI_SCREENSHOT_DIR,{recursive:true});await frame.locator('body').screenshot({path:path.join(process.env.UI_SCREENSHOT_DIR,'light.png')});}
    await frame.selectOption('#machine-sort','name-desc');
    assert.deepEqual(await names(),['Haas 0i-10','haas 0i-2','alpha']);
    await frame.selectOption('#machine-sort','added-asc');
    assert.deepEqual(await names(),['Haas 0i-10','alpha','haas 0i-2']);
    await frame.selectOption('#machine-sort','added-desc');
    assert.deepEqual(await names(),['haas 0i-2','alpha','Haas 0i-10']);
    await page.reload();const reopened=page.frames().find(f=>f.url()===base+'/');
    await reopened.waitForSelector('.machine');
    assert.equal(await reopened.locator('#appearance').inputValue(),'light');
    assert.equal(await reopened.locator('#machine-sort').inputValue(),'added-desc');
    await reopened.selectOption('#appearance','dark');
    const colors=await reopened.locator('.machine').first().evaluate(el=>({background:getComputedStyle(el).backgroundColor,color:getComputedStyle(el).color}));
    assert.equal(colors.background,'rgb(25, 43, 56)');assert.equal(colors.color,'rgb(229, 237, 244)');
    assert.equal(await reopened.locator('#appearance').evaluate(el=>getComputedStyle(el).backgroundColor),'rgb(34, 57, 70)');
    assert.equal(await reopened.locator('aside #appearance').count(),1);
    await reopened.locator('#title-edit').click();
    await reopened.locator('#page-title').fill('Production floor');
    await reopened.locator('#title-save').click();
    await reopened.waitForFunction(()=>document.getElementById('floor-title').textContent==='Production floor');
    await reopened.locator('#add').click();assert.equal(await reopened.locator('#editor').evaluate(el=>getComputedStyle(el).backgroundColor),'rgb(25, 43, 56)');
    await reopened.selectOption('#protocol','mdc');
    assert.equal(await reopened.locator('#port').inputValue(),'5051');
    assert.equal(await reopened.locator('#device').isDisabled(),true);
    await reopened.selectOption('#protocol','mtconnect');
    assert.equal(await reopened.locator('#port').inputValue(),'8082');
    assert.equal(await reopened.locator('#device').isEnabled(),true);
    await reopened.locator('#device').fill('VF-2');
    await reopened.locator('#close-editor').click();
    if (process.env.UI_SCREENSHOT_DIR) await reopened.locator('body').screenshot({path:path.join(process.env.UI_SCREENSHOT_DIR,'dark.png')});
    await page.setViewportSize({width:390,height:844});
    assert.equal(await reopened.locator('#machine-sort').isVisible(),true);
    assert.equal(await reopened.evaluate(()=>document.documentElement.scrollWidth<=window.innerWidth),true);
    if (process.env.UI_SCREENSHOT_DIR) await reopened.locator('body').screenshot({path:path.join(process.env.UI_SCREENSHOT_DIR,'mobile.png')});
    await reopened.selectOption('#machine-sort','name-asc');
    status.machines.push(machine('Beta'));
    await reopened.waitForFunction(()=>[...document.querySelectorAll('.machine h3')].map(e=>e.textContent).join('|')==='alpha|Beta|haas 0i-2|Haas 0i-10');
    await reopened.selectOption('#appearance','automatic');
    // Standalone view has no HA parent; now it follows device appearance.
    await page.goto(base+'/');await page.waitForSelector('.machine');
    assert.equal(await page.locator('html').getAttribute('data-theme'),'dark');
    if (process.env.UI_SCREENSHOT_DIR) await page.screenshot({path:path.join(process.env.UI_SCREENSHOT_DIR,'standalone-mobile.png'),fullPage:true});
    assert.equal(await page.locator('#floor-title').textContent(),'Production floor');
    assert.equal(apiWrites,1);assert.deepEqual(errors,[]);
    assert.deepEqual(status.machines.map(m=>m.name),['Haas 0i-10','alpha','haas 0i-2','Beta']);
    console.log('Chromium UI checks passed: HA appearance, overrides, persistence, four sorts, dialogs, mobile and shared title persistence (only the intended title API write).');
    await context.close();
  } finally {await browser.close();}
})().catch(error=>{console.error(error);process.exitCode=1;}).finally(()=>server.close());
