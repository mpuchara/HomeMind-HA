const fs=require('fs'),vm=require('vm'),assert=require('assert/strict');
const source=fs.readFileSync('adaptive_ai/src/static/app.js','utf8');
const helper=source.slice(source.indexOf('let trainingActionDialog=null;'),source.indexOf('async function trainAgent(id){'));
const actions=source.slice(source.indexOf('async function resumeLearning(id){'),source.indexOf('async function removeAgent(id){'));
class Element{constructor(){this.textContent='';this.disabled=false;this.hidden=false;this.attrs={};}setAttribute(k,v){this.attrs[k]=v;}}
class Dialog extends Element{
 constructor(){super();this.open=false;this.elements=new Map(['h2','[data-description]','[data-result]','[data-submit]','[data-cancel]','[data-check]','form'].map(k=>[k,new Element()]));}
 querySelector(k){return this.elements.get(k);}showModal(){this.open=true;}close(){this.open=false;this.onclose?.();}remove(){this.removed=true;}
}
const event=()=>({preventDefault(){}}),settle=async()=>{for(let i=0;i<12;i++)await Promise.resolve();};
const ticket=(path,state='accepted',extra={})=>({request_id:path.split('/').pop(),state,boot_id:'boot-a',response:{message:'Training queued at position 1'},...extra});
const error=(status,message)=>Object.assign(new Error(message),{httpStatus:status});
function harness(){
 const dialogs=[],requests=[],refreshErrors=[],timers=new Map();let timerId=0,requestId=0;
 let impl=async(path,options)=>options?.method==='POST'
  ?{request_id:JSON.parse(options.body).request_id,state:'pending',boot_id:'boot-a',message:'Request received'}:ticket(path);
 let refresh=async()=>{};
 const context={lastAgents:[{id:'a',name:'Paused light <name>'}],document:{createElement:()=>new Dialog(),body:{append:d=>dialogs.push(d)}},
  api:async(...args)=>{requests.push(args);return impl(...args);},load:()=>refresh(),
  confirm:()=>{throw Error('native confirm unavailable');},alert:()=>{throw Error('native alert unavailable');},
  console:{error:(...args)=>refreshErrors.push(args)},encodeURIComponent,AbortController,crypto:{randomUUID:()=>`request-${++requestId}-fixture`},
  setTimeout:(fn,ms)=>{timers.set(++timerId,{fn,ms});return timerId;},clearTimeout:id=>timers.delete(id)};
 vm.createContext(context);vm.runInContext(helper+'\n'+actions,context);
 return {context,dialogs,requests,refreshErrors,timers,api:f=>impl=f,load:f=>refresh=f,
  mutations:()=>requests.filter(r=>r[1]?.method==='POST'),tick:async ms=>{const entry=[...timers].find(([,v])=>v.ms===ms);assert.ok(entry,`missing ${ms} timer`);timers.delete(entry[0]);entry[1].fn();await settle();}};
}
async function main(){
 for(const rebuild of [false,true]){
  const h=harness();await h.context[rebuild?'resetLearning':'resumeLearning']('a');const d=h.dialogs[0];
  assert.equal(h.requests.length,0);assert.ok(d.querySelector('[data-description]').textContent.includes(rebuild?'clears this agent model':'preserved'));
  await d.querySelector('form').onsubmit(event());assert.equal(h.mutations().length,1);
  assert.equal(h.mutations()[0][0],'api/agents/a/training-requests');assert.equal(JSON.parse(h.mutations()[0][1].body).action,rebuild?'rebuild':'resume');
  assert.equal(d.querySelector('h2').textContent,'Training request accepted');assert.equal(d.querySelector('[data-submit]').hidden,true);
  await d.querySelector('form').onsubmit(event());assert.equal(h.mutations().length,1);assert.equal(h.timers.size,0);
 }
 const cancelled=harness();await cancelled.context.resetLearning('a');cancelled.dialogs[0].querySelector('[data-cancel]').onclick();assert.equal(cancelled.requests.length,0);
 const busy=harness();let finish;
 busy.api(async(path,options)=>options?.method==='POST'?{request_id:JSON.parse(options.body).request_id,state:'pending',message:'Request received'}:new Promise(resolve=>finish=()=>resolve(ticket(path))));
 await busy.context.resumeLearning('a');const d=busy.dialogs[0],pending=d.querySelector('form').onsubmit(event());await settle();
 assert.ok(finish);assert.ok(d.querySelector('[data-result]').textContent.includes('Request received'));
 assert.equal(d.querySelector('[data-cancel]').disabled,false);assert.equal(d.oncancel,undefined);
 d.querySelector('[data-cancel]').onclick();await busy.context.resetLearning('a');const reopened=busy.dialogs[1];
 await reopened.querySelector('form').onsubmit(event());assert.equal(busy.mutations().length,1);
 finish();await pending;assert.equal(reopened.querySelector('h2').textContent,'Training request accepted');
 const lost=harness();lost.api(async(path,options)=>{if(options?.method==='POST')throw error(502,'acknowledgement lost');return ticket(path);});
 await lost.context.resumeLearning('a');await lost.dialogs[0].querySelector('form').onsubmit(event());assert.equal(lost.mutations().length,1);assert.equal(lost.dialogs[0].querySelector('h2').textContent,'Training request accepted');
 const timeout=harness();timeout.api((path,options)=>options?.method==='POST'?new Promise(()=>{}):Promise.resolve(ticket(path)));
 await timeout.context.resumeLearning('a');const waiting=timeout.dialogs[0].querySelector('form').onsubmit(event());await settle();await timeout.tick(30000);await waiting;
 assert.equal(timeout.mutations().length,1);assert.equal(timeout.mutations()[0][1].signal.aborted,true);assert.equal(timeout.dialogs[0].querySelector('h2').textContent,'Training request accepted');
 const restart=harness();restart.api(async(path,options)=>{if(options?.method==='POST')return {request_id:JSON.parse(options.body).request_id,state:'pending'};throw new Error('offline');});
 await restart.context.resumeLearning('a');const r=restart.dialogs[0];await r.querySelector('form').onsubmit(event());
 assert.equal(r.querySelector('[data-check]').hidden,false);assert.equal(r.querySelector('[data-submit]').disabled,true);
 restart.api(async()=>{throw error(404,'runtime restarted');});await r.querySelector('[data-check]').onclick();
 assert.equal(r.querySelector('[data-submit]').disabled,false);assert.equal(restart.mutations().length,1);
 const previousId=JSON.parse(restart.mutations()[0][1].body).request_id;r.close();await restart.context.resumeLearning('a');
 restart.api(async(path,options)=>options?.method==='POST'?{request_id:JSON.parse(options.body).request_id,state:'pending'}:ticket(path));
 await restart.dialogs[1].querySelector('form').onsubmit(event());assert.equal(restart.mutations().length,2);assert.equal(JSON.parse(restart.mutations()[1][1].body).request_id,previousId);
 const rejected=harness();rejected.api(async(path,options)=>options?.method==='POST'?{request_id:JSON.parse(options.body).request_id,state:'pending'}:ticket(path,'failed',{response_status:409,response:{error:'Candidate blocks rebuild'}}));
 await rejected.context.resetLearning('a');const f=rejected.dialogs[0];await f.querySelector('form').onsubmit(event());assert.ok(f.querySelector('[data-result]').textContent.includes('Candidate blocks rebuild'));assert.equal(f.querySelector('[data-submit]').disabled,false);
 const refresh=harness();refresh.load(async()=>{throw Error('refresh failed');});await refresh.context.resumeLearning('a');const view=refresh.dialogs[0];await view.querySelector('form').onsubmit(event());assert.equal(view.querySelector('h2').textContent,'Training request accepted');assert.equal(view.querySelector('[data-result]').attrs.role,'status');assert.ok(view.querySelector('[data-result]').textContent.includes('request was accepted'));assert.equal(refresh.refreshErrors.length,1);
 const encoded=harness();await encoded.context.resumeLearning('id/with space');await encoded.dialogs[0].querySelector('form').onsubmit(event());assert.equal(encoded.mutations()[0][0],'api/agents/id%2Fwith%20space/training-requests');
 console.log(JSON.stringify({destructive_confirmation:true,async_receipt:true,accepted_only_after_queue:true,pending_close:true,no_mutation_retry_on_lost_ack:true,status_check:true,restart_requires_confirmation:true,retry_reuses_request_id:true,candidate_rejection:true,refresh_failure_preserves_acceptance:true,encoded_id:true}));
}
main().catch(error=>{console.error(error);process.exitCode=1;});
