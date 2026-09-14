"""Execute the production P0 reconciliation with a small DOM test double."""
import shutil
import subprocess
import unittest
from support import ROOT


class AgentRenderingTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "Node required")
    def test_refresh_recovers_legacy_nodes_and_keeps_one_card_per_id(self):
        script = r'''
const fs=require('node:fs'), vm=require('node:vm'), assert=require('node:assert/strict');
class Element {
  constructor(){this.children=[];this.parts=new Map();this.dataset={};this.className='';this.open=false;
    this.classList={contains:x=>this.className.split(' ').includes(x),toggle(){}};}
  appendChild(n){n.remove();this.children.push(n);n.parentNode=this;return n;}
  remove(){if(this.parentNode){const p=this.parentNode;p.children=p.children.filter(n=>n!==this);this.parentNode=null;}}
  querySelector(s){if(s==='.p0-empty')return this.children.find(n=>n.classList.contains('p0-empty'))||null;
    if(!this.parts.has(s))this.parts.set(s,new Element());return this.parts.get(s);}
  querySelectorAll(){return [];}
  addEventListener(){}
  setAttribute(){}
}
const roots=new Map(), root=new Element();roots.set('#agents',root);
let agents=[{id:7,name:'Test',mode:'shadow',target_entity:'switch.test',target_property:'power',runtime:{},training_state:'qualified'}];
let filtered=agents;
const c={document:{createElement:()=>new Element()},
  $:s=>{if(!roots.has(s))roots.set(s,new Element());return roots.get(s);},
  renderHome(){},renderAgents(){},updateBounds(){},duration(){},
  openAgentDetails:new Set(),persistOpenAgentDetails(){},lastAgents:agents,
  sortedFilteredAgents:()=>filtered,esc:String,pct:String,num:String,currentValue:()=>0,prediction:()=>1,
};
c.window=c;
vm.runInNewContext(fs.readFileSync('adaptive_ai/src/static/p0.js','utf8'),c);
c.renderAgents();const card=root.children[0];
assert.equal(root.children.length,1);assert.equal(card.hidden,false);
// Simulate a legacy render replacing the DOM, while P0 still owns the old nodes.
card.remove();const legacy=new Element();legacy.className='agent card';root.appendChild(legacy);
c.renderAgents();assert.equal(root.children.length,1);assert.equal(root.children[0],card);
// Filtering and subsequent refresh must not create a second card or lose its state.
card.querySelector('details').open=true;
filtered=[];c.renderAgents();assert.equal(card.hidden,true);
filtered=agents;c.renderAgents();assert.equal(root.children.length,1);
assert.equal(card.querySelector('details').open,true);
// JSON ID representation and repeated response rows must not duplicate a card.
card.querySelector('details').open=false;
c.lastAgents=[{...agents[0],id:'7'},agents[0]];filtered=c.lastAgents;
c.renderAgents();assert.equal(root.children.length,1);assert.equal(root.children[0],card);
c.lastAgents=[];filtered=[];c.renderAgents();assert.equal(card.parentNode,null);
'''
        result = subprocess.run(["node", "-e", script], cwd=ROOT, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
