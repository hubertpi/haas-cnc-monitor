const {test} = require('node:test');
const assert = require('node:assert/strict');
const ui = require('../../haas_monitor/web/preferences.js');
function storage() {
  const values = new Map();
  return {getItem:k=>values.get(k)??null, setItem:(k,v)=>values.set(k,v)};
}
function windowFixture({dark=false, ha=null, store=storage()}={}) {
  const elements = Object.fromEntries(['appearance','machine-sort','appearance-hint','preference-warning'].map(id=>[id,{hidden:true,addEventListener(name,fn){this[name]=fn;}}]));
  const callbacks = {};
  const win = {document:{documentElement:{dataset:{}},getElementById:id=>elements[id]},localStorage:store,
    parent:ha?{document:{querySelector:()=>({hass:{themes:ha}})},getComputedStyle:()=>({colorScheme:'normal'})}:null,
    matchMedia:()=>({matches:dark,addEventListener:(name,fn)=>callbacks.media=fn}),
    addEventListener:(name,fn)=>callbacks[name]=fn,setInterval:fn=>{callbacks.tick=fn;return 1;},clearInterval:()=>{}};
  if (!ha) win.parent=win; else win.parent.parent=win.parent;
  return {win,elements,callbacks};
}
test('defaults, malformed preferences and unavailable storage fail safely',()=>{
  assert.deepEqual(ui.load(null),{appearance:'automatic',sort:'name-asc'});
  assert.deepEqual(ui.load({getItem:()=>'{bad json'}),ui.defaults);
  assert.deepEqual(ui.normalize({appearance:'bad',sort:'bad'}),ui.defaults);
  assert.equal(ui.save(null,ui.defaults),false);
});
test('preferences survive creating a new page instance',()=>{
  const store=storage();ui.save(store,{appearance:'dark',sort:'added-desc'});
  assert.deepEqual(ui.load(store),{appearance:'dark',sort:'added-desc'});
});
test('all sorts use natural names or unchanged backend insertion order',()=>{
  const machines=[{name:'Haas 0i-10'},{name:'alpha'},{name:'haas 0i-2'}];
  const names=list=>list.map(m=>m.name);
  assert.deepEqual(names(ui.sortedMachines(machines,'name-asc')),['alpha','haas 0i-2','Haas 0i-10']);
  assert.deepEqual(names(ui.sortedMachines(machines,'name-desc')),['Haas 0i-10','haas 0i-2','alpha']);
  assert.deepEqual(ui.sortedMachines(machines,'added-asc'),machines);
  assert.deepEqual(ui.sortedMachines(machines,'added-desc'),[...machines].reverse());
  assert.equal(machines[0].name,'Haas 0i-10');
});
test('HA selected appearance takes precedence over device appearance',()=>{
  const fixture=windowFixture({dark:true,ha:{darkMode:false}});
  assert.deepEqual(ui.resolvedAppearance('automatic',fixture.win),{theme:'light',source:'home-assistant'});
  fixture.win.parent.document.querySelector=()=>({hass:{themes:{darkMode:true}}});
  assert.equal(ui.resolvedAppearance('automatic',fixture.win).theme,'dark');
});
test('explicit overrides do not need parent access; inaccessible parent falls back',()=>{
  const fixture=windowFixture({dark:true});
  Object.defineProperty(fixture.win,'parent',{get(){throw new Error('Cross-origin access denied');}});
  assert.equal(ui.resolvedAppearance('light',fixture.win).theme,'light');
  assert.equal(ui.resolvedAppearance('dark',fixture.win).theme,'dark');
  assert.deepEqual(ui.resolvedAppearance('automatic',fixture.win),{theme:'dark',source:'device'});
});
test('explicit parent color scheme is used when hass is not exposed',()=>{
  const fixture=windowFixture();fixture.win.parent={document:{querySelector:()=>null,documentElement:{}},getComputedStyle:()=>({colorScheme:'dark'})};fixture.win.parent.parent=fixture.win.parent;
  assert.equal(ui.resolvedAppearance('automatic',fixture.win).theme,'dark');
});
test('live HA changes and overrides apply without a reload; sort persists immediately',()=>{
  const ha={darkMode:false},fixture=windowFixture({ha});let renders=0;
  ui.start(fixture.win,()=>renders++);
  assert.equal(fixture.elements.appearance.value,'automatic');
  ha.darkMode=true;fixture.callbacks.tick();assert.equal(fixture.win.document.documentElement.dataset.theme,'dark');
  fixture.elements.appearance.value='light';fixture.elements.appearance.change();fixture.callbacks.tick();
  assert.equal(fixture.win.document.documentElement.dataset.theme,'light');
  fixture.elements['machine-sort'].value='added-desc';fixture.elements['machine-sort'].change();assert.equal(renders,1);
  const reopened=windowFixture({store:fixture.win.localStorage});ui.start(reopened.win,()=>{});
  assert.equal(reopened.elements.appearance.value,'light');assert.equal(reopened.elements['machine-sort'].value,'added-desc');
});
test('cross-tab preferences update and storage denial are handled',()=>{
  const fixture=windowFixture();ui.start(fixture.win,()=>{});
  ui.save(fixture.win.localStorage,{appearance:'dark',sort:'name-desc'});fixture.callbacks.storage({key:'haas-monitor.ui.v1'});
  assert.equal(fixture.win.document.documentElement.dataset.theme,'dark');
  fixture.win.localStorage.setItem=()=>{throw new Error('Storage disabled');};
  fixture.elements.appearance.value='light';fixture.elements.appearance.change();
  assert.equal(fixture.elements['preference-warning'].hidden,false);
  assert.equal(fixture.win.document.documentElement.dataset.theme,'light');
});

test('audited readings format RPM, valid zero and elapsed seconds clearly',()=>{
  assert.equal(ui.formatReading({value:4299.8199462890625,unit:'rpm',precision:0}),'4300 rpm');
  assert.equal(ui.formatReading({value:4,unit:'s'}),'00:04 (4 s)');
  assert.equal(ui.formatReading({value:107,unit:'s'}),'01:47 (107 s)');
  assert.equal(ui.formatReading({value:3604,unit:'s'}),'1:00:04 (3604 s)');
  assert.equal(ui.formatReading({value:0,unit:'rpm',precision:0}),'0 rpm');
  assert.equal(ui.formatReading({value:0,unit:'%',precision:1}),'0 %');
  assert.equal(ui.formatReading({value:0,unit:'s',available:false}),'Unavailable');
});

test('connection summary uses controller metadata without inventing physical axes',()=>{
  const summary=ui.controllerSummary({model:'VF-4SS',serial:'1234567',version:'REL-100.19.000.1123',manufacturer:'Haas',agent_version:'1.2.0.1.2',reported_axes:[]});
  assert.match(summary,/Software: REL-100.19.000.1123/);
  assert.match(summary,/Serial: 1234567/);
  assert.match(summary,/Axis components: not published/);
  assert.doesNotMatch(summary,/unknown|3 axes|5 axes/);
  assert.match(ui.controllerSummary({model:'Fixture',reported_axes:['X','Y','Z','A','B']}),/Reported axes: X, Y, Z, A, B/);
});
