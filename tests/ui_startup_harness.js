// No network or third-party DOM dependency. Exercise real scripts with controlled startup events.
const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const scenario=process.argv[2],listeners=new Map(),tasks=[],timers=[],requests=[],nodes=new Map();
const emit=type=>{for(const fn of listeners.get(type)||[])fn({type});};
const listen=(type,fn)=>listeners.set(type,[...(listeners.get(type)||[]),fn]);
const source=name=>fs.readFileSync(`adaptive_ai/src/static/${name}.js`,'utf8');
const flush=async()=>{while(tasks.length)tasks.shift()();await new Promise(r=>setImmediate(r));};
class Actions{
  constructor(){this.dataset={};this.buttons=new Map();}
  set innerHTML(raw){this.raw=raw;this.buttons.clear();
    for(const m of raw.matchAll(/<button\b([^>]*)data-wf="([^"]+)"([^>]*)>(.*?)<\/button>/g)){
      this.buttons.set(m[2],{label:m[4],disabled:/\bdisabled\b/.test(m[1]+m[3])});
    }
  }
  querySelector(s){return this.buttons.get(s.match(/data-wf=['"]?([^'"\]]+)/)?.[1])||null;}
}
const card={dataset:{agentId:'room'},actions:new Actions(),querySelector(s){return s==='.actions'?this.actions:null;}};
const trained={id:'room',mode:'shadow',training_state:'paused',benchmark_score:.85,
  training_cursor_ts:123,target_property:'power',runtime:{}};
const c={console,AbortController,URLSearchParams,CustomEvent:class{constructor(type,init){this.type=type;this.detail=init.detail;}},
  document:{readyState:['loading','interactive','complete'].includes(scenario)?scenario:'loading',
    getElementById(){return null;},hidden:scenario==='hidden',addEventListener:listen,body:{append(){}},
    createElement(){return {addEventListener(){},querySelector(){return null;},querySelectorAll(){return [];}};},
    querySelector(s){if(!nodes.has(s))nodes.set(s,{value:'',classList:{add(){},remove(){}}});return nodes.get(s);},
    querySelectorAll(s){return s==='#agents > .agent:not(.candidate-agent)'&&c.__adaptiveAiAgents?.length?[card]:[];}},
  addEventListener:listen,dispatchEvent:e=>emit(e.type),queueMicrotask:fn=>tasks.push(fn),
  localStorage:{getItem(){return null;},setItem(){}},performance:require('node:perf_hooks').performance,
  setInterval(fn,ms){timers.push({fn,ms,interval:true});},setTimeout(fn,ms){timers.push({fn,ms});return timers.length;},clearTimeout(){},
  fetch(path){requests.push(path);const reply=body=>Promise.resolve({ok:true,status:200,json:async()=>body});
    if(path==='api/status')return scenario==='fast-status'?reply({startup:{ready:true}}):new Promise(()=>{});
    if(path.startsWith('api/live'))return reply({configs:[trained],agents:[{id:'room',current_value:1,last_prediction:1,last_confidence:.85}]});
    if(path.startsWith('api/agents'))return reply([trained]);
    if(path.startsWith('api/events'))return reply([]);
    if(path==='api/candidates')return reply({candidates:[]});
    throw Error(`unexpected request: ${path}`);
  }};
c.window=c;vm.createContext(c);
const run=name=>vm.runInContext(source(name),c,{filename:name+'.js'});
(async()=>{
  run('ui_bootstrap');
  if(['loading','interactive','complete','fallback'].includes(scenario)){
    const seen=[];c.whenAdaptiveUiReady(()=>seen.push('first'));c.whenAdaptiveUiReady(()=>seen.push('second'));
    assert.deepEqual(seen,[]);
    if(scenario==='complete')assert.equal(c.__adaptiveAiUiReady,true);
    else{await flush();assert.deepEqual(seen,[]);assert.equal(c.__adaptiveAiUiReady,false);
      emit(scenario==='fallback'?'load':'DOMContentLoaded');}
    await flush();assert.deepEqual(seen,['first','second']);
    emit('DOMContentLoaded');emit('load');await flush();assert.deepEqual(seen,['first','second']);
    c.whenAdaptiveUiReady(()=>seen.push('late'));assert.equal(seen.length,2);
    await flush();assert.deepEqual(seen,['first','second','late']);return;
  }
  run('app');c.renderHistory=()=>{};c.renderHome=()=>{};c.renderEvents=()=>{};
  const paints=[];
  // Represent the old P0 actions. A browser paint is observed after the render task finishes.
  c.renderAgents=()=>{c.applyLiveValues?.();if(!card.actions.raw)card.actions.innerHTML='<button data-wf="old">Teach</button>';};
  run('manual_feedback');run('candidate_ui');
  emit('visibilitychange');await c.load();await c.refreshCandidates();await flush();
  assert.deepEqual(requests,[]);assert.equal(timers.length,0);
  assert.equal(c.__adaptiveAiAgents,undefined);
  // The later workflow script can take arbitrarily long to download; no data reads have started.
  run('agent_workflow_ui');const finalRender=c.renderAgents;
  c.renderAgents=()=>{finalRender();paints.push([...card.actions.buttons.values()].map(b=>b.label));};
  emit('DOMContentLoaded');await flush();
  assert.equal(c.__adaptiveAiUiReady,true);
  assert.equal(requests.filter(p=>p==='api/status').length,1);
  assert.equal(timers.filter(t=>t.interval&&t.ms===4000).length,1);
  assert.equal(requests.filter(p=>p==='api/candidates').length,1);
  assert.ok(timers.some(t=>!t.interval&&t.ms===4000));
  if(scenario==='hidden'){assert.equal(requests.some(p=>p.startsWith('api/live')),false);}
  else{
    assert.ok(paints.length>0);
    for(const labels of paints){assert.ok(labels.includes('Pause Shadow'));assert.ok(labels.includes('Correct'));
      assert.ok(labels.includes('Resume training'));assert.ok(labels.includes('Autonomous'));assert.equal(labels.includes('Teach'),false);}
    assert.equal(vm.runInContext('lastAgents[0].runtime.current_value',c),1);
    assert.equal(vm.runInContext('lastAgents[0].runtime.last_prediction',c),1);
    assert.ok(timers.some(t=>!t.interval&&t.ms<=500&&t.ms>=100));
    if(scenario==='slow-status')assert.equal(c.__adaptiveAiRuntimeReady,null);
  }
  const before=requests.length,installed=timers.length;
  emit('load');await flush();assert.equal(requests.length,before);assert.equal(timers.length,installed);
})().catch(e=>{console.error(e);process.exitCode=1;});
