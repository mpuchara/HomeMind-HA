// Production renderer with a minimal DOM: pause, cleanup, recovery and escaping.
const fs=require('node:fs'), vm=require('node:vm'), assert=require('node:assert/strict');
const path=require('node:path');
const node={innerHTML:''};
const context={window:{},document:{getElementById:id=>id==='runtimeDebugPanel'?node:null},
               console,Number,String,Boolean,Array,Object,setTimeout};
vm.createContext(context);
vm.runInContext(fs.readFileSync(path.join(__dirname,'../adaptive_ai/src/static/runtime_debug_ui.js'),'utf8'),context);
const render=context.window.renderRuntimeDebugDiagnostics;
const status={runtime_debug:{entries:740,enabled:true},candidate_worker:{shadow_observations:{
  paused:true,pending:128,max_items:128,storage_reason:'database_budget'}},sqlite:{
  storage_budget:{budget_mode:'auto',auto_use_percent:60,database_used_bytes:10728808448,database_max_bytes:11920883146},
  storage_retention:{recovery:{active:true,deleted:128,reason:'reclaiming_diagnostics'}}}};
render(status);
assert.match(node.innerHTML,/Configured disk allocation: 60%/);
assert.match(node.innerHTML,/Automatic diagnostic cleanup/);
assert.match(node.innerHTML,/Removed 128 rows/);
assert.match(node.innerHTML,/Retained observations: 128\/128/);
assert.doesNotMatch(node.innerHTML,/Check the add-on storage budget configuration/);
status.sqlite.storage_retention.recovery.reason='protected_or_no_disposable_data';render(status);
assert.match(node.innerHTML,/remaining data are protected/);
status.sqlite.storage_retention.recovery.reason='<img src=x onerror=alert(1)>';render(status);
assert.doesNotMatch(node.innerHTML,/<img src=x/);
assert.match(node.innerHTML,/&lt;img/);
status.sqlite.storage_retention.recovery.active=false;
status.candidate_worker.shadow_observations.paused=false;render(status);
assert.match(node.innerHTML,/up to date/);
assert.doesNotMatch(node.innerHTML,/Candidate Shadow observations paused/);
assert.doesNotMatch(node.innerHTML,/<div class="notice"/);
render({runtime_debug:{sqlite:status.sqlite}});
assert.match(node.innerHTML,/Configured disk allocation: 60%/);
render({});
console.log(JSON.stringify({passed:true,pause:true,cleanup:true,recovery:true,allocation60:true,escaping:true,legacyMissingFields:true}));
