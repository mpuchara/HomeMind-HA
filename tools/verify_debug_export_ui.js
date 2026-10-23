// Execute the shipped helper with transient HA Ingress failures and a retained job.
const fs=require('fs'),vm=require('vm'),assert=require('assert/strict');
const source=fs.readFileSync('adaptive_ai/src/static/debug_export_ui.js','utf8');
async function scenario({expire=false,fatal=false}={}){
  let posts=0,polls=0,downloads=0,now=0,alerts=[];
  const response=(status,body)=>({ok:status>=200&&status<300,status,
    text:async()=>JSON.stringify(body)});
  const context={window:{},console:{error(){}},Date:class extends Date{static now(){return now;}},
    setTimeout(fn){fn();},alert:message=>alerts.push(message),
    document:{body:{appendChild(){}},createElement(){return {style:{},click(){downloads++;},remove(){}};}},
    fetch:async(url,init)=>{
      if(init.method==='POST'){
        posts++;return response(202,{job_id:'abc',ref:'root',state:'running'});
      }
      polls++;
      if(polls===1){
        if(expire)now+=16*60*1000;
        const error=new Error('Adaptive AI read timeout after 5000 ms');error.name='TimeoutError';throw error;
      }
      if(fatal&&polls===2)return response(404,{error:'job expired'});
      if(polls===2)return response(503,{error:'Ingress unavailable'});
      if(polls===3)return response(200,{state:'running',progress:.5});
      return response(200,{job_id:'abc',ref:'root',state:'done',progress:1});
    }};
  vm.runInNewContext(source,context);
  const button={textContent:'Export debug',dataset:{},isConnected:true};
  const result=await context.window.exportCorrectLearningDebug('root',button);
  assert.equal(posts,1);
  assert.equal(button.disabled,false);
  assert.equal(button.dataset.debugExportBusy,undefined);
  if(expire){
    assert.equal(result,null);assert.equal(downloads,0);
    await context.window.exportCorrectLearningDebug('root',button);
    assert.equal(posts,1,'retry must resume the same job after losing status');
    assert.equal(downloads,1);
  }else if(fatal){
    assert.equal(result,null);assert.equal(downloads,0);assert.equal(alerts.length,1);
    await context.window.exportCorrectLearningDebug('root',button);
    assert.equal(posts,2,'expired job can be explicitly started again');
  }else{
    assert.equal(downloads,1);assert.equal(alerts.length,0);assert.ok(result);
    assert.equal(polls,4);
  }
}
(async()=>{await scenario();await scenario({expire:true});await scenario({fatal:true});
  console.log('Debug export UI: timeout/503 retry, resume and expiry OK');
})().catch(error=>{console.error(error);process.exitCode=1;});
