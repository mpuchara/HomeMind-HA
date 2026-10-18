// Exercise the real Candidate polling/broker layers with a controlled clock.
const vm=require('node:vm'),fs=require('node:fs'),assert=require('node:assert/strict');
const timers=[],listeners=new Map(),requests=[];
let now=0,cards=false,release=null,hold=false;
const c={console,Response,Headers,URL,AbortController,queueMicrotask,
  location:{href:'http://fixture/'},performance:{now:()=>now},
  document:{hidden:false,head:{appendChild(){}},
    createElement:()=>({}),getElementById:()=>null,
    querySelector:selector=>selector==='.candidate-agent'&&cards?{}:null,
    querySelectorAll:()=>[],addEventListener:(name,fn)=>{
      if(!listeners.has(name))listeners.set(name,[]);listeners.get(name).push(fn);
    }},
  addEventListener(){},
  setTimeout:(fn,ms)=>{timers.push({fn,ms});return timers.length;},clearTimeout(){},
  fetch:async path=>{
    requests.push(path);
    if(hold)await new Promise(resolve=>{release=resolve;});
    return new Response('{"candidates":[]}',{status:200,headers:{'Content-Type':'application/json'}});
  }};
c.window=c;vm.createContext(c);
for(const name of ['polling_guard.js','candidate_preference_ui.js','runtime_activity_ui.js']){
  vm.runInContext(fs.readFileSync('adaptive_ai/src/static/'+name,'utf8'),c);
}
const flush=()=>new Promise(resolve=>setImmediate(resolve));
(async()=>{
  await flush();await flush();
  assert.equal(requests.length,0,'No Candidate cards must mean no backend requests');
  assert.equal(timers.at(-1).ms,100);
  cards=true;
  // The first actual card request must not hit a fabricated empty startup cache.
  await timers.findLast(t=>t.ms===100).fn();await flush();
  assert.equal(requests.length,1);assert.equal(timers.at(-1).ms,100);
  now=99;await c.fetch('api/candidate-live');assert.equal(requests.length,1);
  now=100;hold=true;
  const first=c.fetch('api/candidate-live'),second=c.fetch('api/candidate-live');
  await flush();assert.equal(requests.length,2,'Concurrent readers share one upstream request');
  release();await Promise.all([first,second]);hold=false;
  now=200;
  await timers.findLast(t=>t.ms===100).fn();await flush();
  assert.equal(requests.length,3,'A new display interval must not reuse the old one-second cache');
  c.document.hidden=true;
  await timers.findLast(t=>t.ms===100).fn();await flush();
  assert.equal(requests.length,3);assert.equal(timers.at(-1).ms,1000);
  c.document.hidden=false;cards=false;now=2000;
  await timers.findLast(t=>t.ms===1000).fn();await flush();
  assert.equal(requests.length,3);
  console.log(JSON.stringify({visible_cadence_ms:100,cache_expiry_ms:100,
    shared_inflight:true,first_card_fresh:true,no_cards_no_requests:true,hidden_no_requests:true}));
})().catch(error=>{console.error(error);process.exitCode=1;});
