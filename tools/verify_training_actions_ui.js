// Exercise real training action functions with native browser dialogs unavailable.
const fs=require('fs'),vm=require('vm'),assert=require('assert/strict');
const source=fs.readFileSync('adaptive_ai/src/static/app.js','utf8');
const helper=source.slice(source.indexOf('let trainingActionDialog=null;'),source.indexOf('async function trainAgent(id){'));
const actions=source.slice(source.indexOf('async function resumeLearning(id){'),source.indexOf('async function removeAgent(id){'));
assert.ok(helper.includes('function openTrainingAction'));
class Element{
  constructor(){this.textContent='';this.disabled=false;this.hidden=false;this.attrs={};}
  setAttribute(k,v){this.attrs[k]=v;}
}
class Dialog extends Element{
  constructor(){super();this.open=false;this.elements=new Map(['h2','[data-description]','[data-result]','[data-submit]','[data-cancel]','form'].map(k=>[k,new Element()]));}
  querySelector(k){return this.elements.get(k);}
  showModal(){this.open=true;}
  close(){this.open=false;this.onclose?.();}
  remove(){this.removed=true;}
}
function harness(){
  const dialogs=[],requests=[],refreshErrors=[],timers=new Map();let timerId=0;
  let impl=async()=>({ok:true,state:'queued',message:'Training queued at position 1'}),refresh=async()=>{};
  const context={lastAgents:[{id:'a',name:'Paused light <name>'}],
    document:{createElement:()=>new Dialog(),body:{append:d=>dialogs.push(d)}},
    api:async(...args)=>{requests.push(args);return impl(...args);},load:()=>refresh(),
    confirm:()=>{throw Error('native confirmation unavailable');},alert:()=>{throw Error('native alert unavailable');},
    console:{error:(...args)=>refreshErrors.push(args)},encodeURIComponent,AbortController,
    setTimeout:(fn,ms)=>{assert.equal(ms,30000);timers.set(++timerId,fn);return timerId;},
    clearTimeout:id=>timers.delete(id)};
  vm.createContext(context);vm.runInContext(helper+'\n'+actions,context);
  return {context,dialogs,requests,refreshErrors,timers,api:f=>impl=f,load:f=>refresh=f};
}
const event=()=>({preventDefault(){this.prevented=true;}});
async function main(){
  for(const rebuild of [false,true]){
    const h=harness();await h.context[rebuild?'resetLearning':'resumeLearning']('a');
    const d=h.dialogs[0];assert.equal(d.open,true);assert.equal(h.requests.length,0);
    assert.ok(d.querySelector('[data-description]').textContent.includes(rebuild?'clears this agent model':'preserved'));
    assert.ok(d.querySelector('[data-description]').textContent.startsWith('Paused light <name>'));
    await d.querySelector('form').onsubmit(event());
    assert.equal(h.requests.length,1);assert.equal(h.requests[0][0],`api/agents/a/${rebuild?'learning':'resume'}`);
    assert.equal(h.requests[0][1].method,rebuild?'DELETE':'POST');
    assert.equal(d.querySelector('h2').textContent,'Training request accepted');
    assert.equal(d.querySelector('[data-submit]').hidden,true);
    await d.querySelector('form').onsubmit(event());assert.equal(h.requests.length,1);
  }
  const cancel=harness();await cancel.context.resetLearning('a');
  cancel.dialogs[0].querySelector('[data-cancel]').onclick();
  assert.equal(cancel.requests.length,0);assert.equal(cancel.dialogs[0].removed,true);
  await cancel.context.resumeLearning('a');assert.equal(cancel.dialogs.length,2);
  const busy=harness();let finish;busy.api(()=>new Promise(resolve=>finish=resolve));
  await busy.context.resumeLearning('a');const d=busy.dialogs[0];
  const pending=d.querySelector('form').onsubmit(event());
  await d.querySelector('form').onsubmit(event());await busy.context.resetLearning('other');
  assert.equal(busy.requests.length,1);assert.equal(busy.dialogs.length,1);
  assert.equal(d.querySelector('[data-cancel]').disabled,false);
  assert.equal(d.oncancel,undefined); // Native Escape remains available while pending.
  d.querySelector('[data-cancel]').onclick();assert.equal(d.removed,true);
  await busy.context.resetLearning('a');const reopened=busy.dialogs[1];
  await reopened.querySelector('form').onsubmit(event());assert.equal(busy.requests.length,1);
  finish({ok:true,message:'Training is running now'});await pending;
  assert.equal(reopened.querySelector('h2').textContent,'Training request accepted');
  assert.equal(busy.timers.size,0);
  const timed=harness();timed.api(()=>new Promise(()=>{}));
  await timed.context.resetLearning('a');const t=timed.dialogs[0];
  const waiting=t.querySelector('form').onsubmit(event());
  [...timed.timers.values()][0]();await waiting;
  assert.equal(timed.requests[0][1].signal.aborted,true);
  assert.equal(t.querySelector('[data-result]').attrs.role,'alert');
  assert.ok(t.querySelector('[data-result]').textContent.includes('may still start training'));
  t.close();await timed.context.resetLearning('a');
  await timed.dialogs[1].querySelector('form').onsubmit(event());assert.equal(timed.requests.length,1);
  const failed=harness();failed.api(async()=>{const e=Error('HTTP 409 Candidate blocks rebuild');e.httpStatus=409;throw e;});
  await failed.context.resetLearning('a');const f=failed.dialogs[0];
  await f.querySelector('form').onsubmit(event());
  assert.equal(f.querySelector('[data-result]').attrs.role,'alert');
  assert.ok(f.querySelector('[data-result]').textContent.includes('Candidate blocks rebuild'));
  assert.equal(f.querySelector('[data-submit]').disabled,false);
  assert.equal(failed.requests.length,1);
  failed.api(async()=>({ok:true,message:'Queued'}));await f.querySelector('form').onsubmit(event());
  assert.equal(failed.requests.length,2);assert.equal(f.querySelector('h2').textContent,'Training request accepted');
  const refresh=harness();refresh.load(async()=>{throw Error('render failed');});
  await refresh.context.resumeLearning('a');const r=refresh.dialogs[0];await r.querySelector('form').onsubmit(event());
  assert.equal(r.querySelector('[data-result]').attrs.role,'status');
  assert.ok(r.querySelector('[data-result]').textContent.includes('request was accepted'));
  assert.equal(refresh.requests.length,1);assert.equal(refresh.refreshErrors.length,1);
  const encoded=harness();await encoded.context.resumeLearning('id/with space');
  await encoded.dialogs[0].querySelector('form').onsubmit(event());
  assert.equal(encoded.requests[0][0],'api/agents/id%2Fwith%20space/resume');
  console.log(JSON.stringify({native_dialogs_unused:true,resume:true,rebuild:true,destructive_confirmation:true,
    cancel_no_request:true,pending_duplicate_guard:true,pending_close:true,timeout_unknown_no_retry:true,
    inline_api_error:true,accepted_refresh_failure:true,encoded_id:true}));
}
main().catch(error=>{console.error(error);process.exitCode=1;});
