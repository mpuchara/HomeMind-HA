// Actual Correct dialog code with a hung full-status request and controlled chart replies.
const fs=require('fs'),vm=require('vm'),assert=require('assert/strict');
class Node{
 constructor(){this.textContent='';this.innerHTML='';this.value='';this.disabled=false;this.dataset={};this.attrs={};this.children=new Map();}
 querySelector(key){if(!this.children.has(key))this.children.set(key,new Node());return this.children.get(key);}
 querySelectorAll(){return [];}setAttribute(k,v){this.attrs[k]=v;}hasAttribute(k){return k in this.attrs;}
 getBoundingClientRect(){return {left:0,width:1030};}setPointerCapture(){}releasePointerCapture(){}
}
class Dialog extends Node{showModal(){this.open=true;}close(){this.open=false;this.onclose?.();}}
const dialogs=[],requests=[],timers=new Map();let counter=0,reply;
const payload=path=>{const u=new URL(path,'http://fixture/'),id=u.pathname.split('/')[3],start=Number(u.searchParams.get('start')),end=Number(u.searchParams.get('end'));
 return {generation_id:'root:'+id,generation_number:3,generation_type:'live',agent:{id,name:'Observed '+id,target_property:'power',min_value:0,max_value:1},chart_mode:'live',start,end,series:{current:{label:'Current',pairs:[[start,0],[end,1]]},live_desired:{label:'Live Desired',pairs:[[start,0],[end,0]]}},labels:[],gaps:[],policy_replay_used:false};};
reply=async path=>({ok:true,json:async()=>payload(path)});
const c={console,URL,Date,Map,Set,Promise,lastAgents:[{id:'a',name:'Lamp',min_value:0,max_value:1,target_property:'power'}],
 document:{hidden:false,createElement:()=>new Dialog(),body:{append:d=>dialogs.push(d)},querySelectorAll:()=>[]},
 setTimeout:(fn,ms)=>{timers.set(++counter,{fn,ms});return counter;},clearTimeout:id=>timers.delete(id),
 sessionStorage:{getItem:()=>null,setItem(){},removeItem(){}},alert(){throw Error('Unexpected alert');},
 fetch:async(path,options={})=>{requests.push({path,method:options.method||'GET'});if(path.endsWith('/status'))return new Promise(()=>{});return reply(path);}};
c.window=c;vm.createContext(c);vm.runInContext(fs.readFileSync('adaptive_ai/src/static/agent_workflow_ui.js','utf8'),c);
const settle=async()=>{for(let i=0;i<15;i++)await Promise.resolve();};
(async()=>{
 const opened=c.openWorkflowCorrect('a');await settle();
 assert.equal(requests.length,1);assert.ok(requests[0].path.includes('/correct-history?'),'Chart must not wait for workflow status');
 await opened;const d=dialogs[0];assert.match(d.querySelector('[data-chart]').innerHTML,/data-series="current"/);assert.match(d.querySelector('[data-title]').textContent,/Gen 3/);
 let release;reply=path=>new Promise(resolve=>{release=()=>resolve({ok:true,json:async()=>payload(path)});});
 const count=requests.length,first=d.querySelector('[data-retry]').onclick(),second=d.querySelector('[data-retry]').onclick();await settle();
 assert.equal(requests.length,count+1,'Repeated retry must share the pending read');release();await Promise.all([first,second]);
 reply=async()=>{throw Error('controlled chart timeout');};await d.querySelector('[data-retry]').onclick();
 assert.equal(d.open,true);assert.match(d.querySelector('[data-chart]').innerHTML,/data-series="current"/);assert.match(d.querySelector('[data-error]').textContent,/controlled chart timeout/);
 reply=async path=>({ok:true,json:async()=>payload(path)});await d.querySelector('[data-retry]').onclick();assert.equal(d.querySelector('[data-error]').textContent,'');
 reply=path=>new Promise(resolve=>{release=()=>resolve({ok:true,json:async()=>payload(path)});});
 const old=d.querySelector('[data-retry]').onclick();await settle();d.close();
 reply=async path=>({ok:true,json:async()=>payload(path)});await c.openWorkflowCorrect('b');release();await old;
 assert.match(d.querySelector('[data-title]').textContent,/Observed b/);assert.equal(requests.filter(r=>r.path.endsWith('/status')).length,0);assert.ok(requests.every(r=>r.method==='GET'));
 console.log(JSON.stringify({chart_without_status:true,rendered_svg:true,coalesced_retry:true,last_chart_preserved_on_error:true,reconnect:true,stale_view_ignored:true,no_mutations:true}));
})().catch(error=>{console.error(error);process.exitCode=1;});
