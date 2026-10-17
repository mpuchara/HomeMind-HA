// Execute production tile refresh and P0 explanations with controlled API replies.
const vm=require('node:vm'),fs=require('node:fs'),assert=require('node:assert/strict');
const nodes=new Map(),timers=[],requests=[];
const node=key=>{if(!nodes.has(key))nodes.set(key,{textContent:'',className:'',classList:{toggle(){}},dataset:{agentId:'a'}});return nodes.get(key);};
const card={dataset:{agentId:'a'},classList:{toggle(){}},querySelector:selector=>node(selector)};
const a={id:'a',name:'Lamp',mode:'shadow',target_entity:'light.a',target_property:'power',training_state:'qualified',runtime:{}};
const replies=[
  {revision:'epoch:1',agents:[{id:'a',current_value:1,last_prediction:1,last_confidence:.9,fast_off_confirmation_active:true,fast_off_confirmation_elapsed:2,fast_off_confirmation_required:6}]},
  {revision:'epoch:2',agents:[{id:'a',current_value:0,last_prediction:1,last_confidence:.8,current_newer_than_decision:true,fast_off_confirmation_active:false}]},
  {revision:'epoch:3',agents:[{id:'a',current_value:0,last_prediction:0,last_confidence:.8,current_newer_than_decision:false,fast_off_confirmation_active:false}]},
];
const c={lastAgents:[a],renderHome(){},renderOverview(){},renderAgents(){},updateBounds(){},
  pct:v=>`${(Number(v)*100).toFixed(1)}%`,num:v=>String(v),esc:v=>String(v??''),
  currentValue:a=>a.runtime.current_value>=.5?'ON':'OFF',prediction:a=>a.runtime.last_prediction>=.5?'ON':'OFF',
  document:{hidden:false,body:{append(){}},createElement(){return {addEventListener(){}};},addEventListener(){},
    querySelectorAll:()=>[card]},
  fetch:async path=>{requests.push(path);assert.ok(replies.length);return {ok:true,json:async()=>replies.shift()};},
  AbortController,performance,setTimeout:(fn,ms)=>{timers.push({fn,ms});return timers.length;},clearTimeout(){},
  alert(){},console};
c.window=c;c.$=()=>({addEventListener(){}});vm.createContext(c);
vm.runInContext(fs.readFileSync('adaptive_ai/src/static/p0.js','utf8'),c);
vm.runInContext(fs.readFileSync('adaptive_ai/src/static/manual_feedback.js','utf8'),c);
const flush=()=>new Promise(resolve=>setImmediate(resolve));
(async()=>{
  await flush();await flush();
  assert.equal(node('[data-p0="current"]').textContent,'ON');
  assert.equal(node('[data-p0="state"]').textContent,'Confirming OFF');
  assert.match(node('[data-p0="detail"]').textContent,/2.0 \/ 6.0 s/);
  for(let i=0;i<2;i++){
    const timer=timers.findLast(x=>x.ms===100);assert.ok(timer);await timer.fn();await flush();
    if(i===0){
      assert.equal(node('[data-p0="current"]').textContent,'OFF');
      assert.equal(node('[data-p0="desired"]').textContent,'ON');
      assert.equal(node('[data-p0="state"]').textContent,'Updating decision');
    }
  }
  assert.deepEqual(requests,['api/live','api/live?after=epoch%3A1&wait_ms=1500','api/live?after=epoch%3A2&wait_ms=1500']);
  assert.equal(node('[data-p0="desired"]').textContent,'OFF');
  assert.equal(node('[data-p0="state"]').textContent,'Observing');
  console.log(JSON.stringify({revision_wait:true,latest_desired:true,physical_current:true,off_confirmation:true,pending_explanation:true}));
})().catch(error=>{console.error(error);process.exitCode=1;});
