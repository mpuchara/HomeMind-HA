import shutil
import subprocess
import unittest
from support import ROOT


class LiveFrontendTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which('node'), 'Node required')
    def test_live_bootstrap_does_not_wait_for_slow_status(self):
        script = r'''
const vm=require('node:vm'),fs=require('node:fs'),assert=require('node:assert/strict');
const requests=[],nodes=new Map();let renders=0;
const c={document:{hidden:false,body:{append(){}},createElement(){return {addEventListener(){}}},querySelectorAll(){return []},querySelector(s){if(!nodes.has(s))nodes.set(s,{value:'',classList:{add(){},remove(){}}});return nodes.get(s);}},
 localStorage:{getItem(){return null},setItem(){}},AbortController,
 setTimeout(){return 1},clearTimeout(){},setInterval(){},
 fetch(path){requests.push(path);if(path==='api/status')return new Promise(()=>{});
   assert.equal(path,'api/live?bootstrap=1');
   return Promise.resolve({ok:true,json:async()=>({configs:[{id:'a',mode:'shadow'}],agents:[{id:'a',current_value:1,last_prediction:0}]})});}
};c.window=c;vm.createContext(c);
vm.runInContext(fs.readFileSync('adaptive_ai/src/static/app.js','utf8'),c);
c.renderAgents=()=>{renders++;c.applyLiveValues();};
vm.runInContext(fs.readFileSync('adaptive_ai/src/static/manual_feedback.js','utf8'),c);
setImmediate(()=>{
 assert.deepEqual(requests,['api/status','api/live?bootstrap=1']);
 assert.equal(renders,1);
 assert.equal(vm.runInContext('lastAgents[0].runtime.current_value',c),1);
 assert.equal(vm.runInContext('lastAgents[0].runtime.last_prediction',c),0);
});
'''
        result = subprocess.run(['node', '-e', script], cwd=ROOT, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
