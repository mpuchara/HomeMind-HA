"""Regression coverage for generation workflow controls on a fresh discovered agent."""
import shutil
import subprocess
import unittest

from support import ROOT


class AgentWorkflowColdStartUiTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "Node required")
    def test_waiting_agent_keeps_train_and_settings_then_unlocks_generation_actions(self):
        script = r'''
const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');

class Button {
  constructor(key,attrs,label){
    this.key=key;this.label=label;this.disabled=/\bdisabled\b/.test(attrs);this.onclick=null;
    this.currentTarget=this;
  }
}
class Actions {
  constructor(){this.dataset={};this.buttons=new Map();this.raw='';}
  set innerHTML(raw){
    this.raw=String(raw);this.buttons=new Map();
    const re=/<button\b([^>]*)data-wf="([^"]+)"([^>]*)>(.*?)<\/button>/g;
    let m;
    while((m=re.exec(this.raw))!==null){
      this.buttons.set(m[2],new Button(m[2],`${m[1]} ${m[3]}`,m[4]));
    }
  }
  get innerHTML(){return this.raw;}
  querySelector(selector){
    const m=String(selector).match(/\[data-wf=['"]?([^'"\]]+)/);
    return m?this.buttons.get(m[1])||null:null;
  }
}
class Card {
  constructor(id){this.dataset={agentId:String(id)};this.actions=new Actions();}
  querySelector(selector){return selector==='.actions'?this.actions:null;}
}
class Dialog {
  constructor(){this.id='';this.open=false;}
  querySelector(){return null;}
  querySelectorAll(){return [];}
}

const card=new Card('fresh-1');
const trainCalls=[],resumeCalls=[],editCalls=[],modeCalls=[],requests=[];
const agents=[{
  id:'fresh-1',name:'Fresh light',mode:'paused',training_state:'waiting',benchmark_score:null,
  training_cursor_ts:null,runtime:{},target_property:'power'
}];
const context={
  lastAgents:agents,
  document:{
    createElement:()=>new Dialog(),
    body:{append(){}},
    querySelectorAll:selector=>selector==='#agents > .agent:not(.candidate-agent)'?[card]:[]
  },
  renderAgents(){},
  trainAgent:id=>trainCalls.push(String(id)),
  resumeLearning:id=>resumeCalls.push(String(id)),
  setMode:(id,mode)=>modeCalls.push([String(id),String(mode)]),
  editAgent:id=>editCalls.push(String(id)),
  fetch:path=>{requests.push(String(path));return Promise.reject(new Error('unexpected fetch'));},
  alert(){},confirm(){return true;},prompt(){return null;},console
};
context.window=context;
vm.runInNewContext(fs.readFileSync('adaptive_ai/src/static/agent_workflow_ui.js','utf8'),context);

// Fresh discovery has no policy model. Train and Settings must remain reachable without
// touching /api/agent-workflow, whose generation actions require a model.
assert.ok(card.actions.buttons.has('train'));
assert.ok(card.actions.buttons.has('settings'));
for(const key of ['auto','correct','explore','change'])assert.equal(card.actions.buttons.has(key),false);
card.actions.buttons.get('train').onclick();
card.actions.buttons.get('settings').onclick();
assert.deepEqual(trainCalls,['fresh-1']);
assert.deepEqual(editCalls,['fresh-1']);
assert.deepEqual(requests,[]);

// A trained but paused agent must expose Resume and the generation workflow.
agents[0].training_state='paused';
agents[0].benchmark_score=.81;
agents[0].training_cursor_ts=123;
context.bindExploreButtons=()=>{const b=card.actions.buttons.get('explore');if(b)b.disabled=false;};
context.renderAgents();
assert.ok(card.actions.buttons.has('resume'));
assert.ok(card.actions.buttons.has('shadow'));
assert.equal(card.actions.buttons.get('shadow').label,'Start Shadow');
for(const key of ['auto','correct','explore','change','settings'])assert.ok(card.actions.buttons.has(key));
assert.equal(card.actions.buttons.get('explore').disabled,false);
card.actions.buttons.get('resume').onclick();
card.actions.buttons.get('shadow').onclick();
assert.deepEqual(resumeCalls,['fresh-1']);
assert.deepEqual(modeCalls,[['fresh-1','shadow']]);

// A successful Start Shadow changes only mode; training may remain PAUSED. The action
// renderer must still invalidate its cached button set and replace Start with Pause.
agents[0].mode='shadow';
context.renderAgents();
assert.equal(card.actions.buttons.get('shadow').label,'Pause Shadow');

// The workflow renderer must react to lifecycle changes on the same DOM node rather than
// keeping its first button set forever.
agents[0].training_state='qualified';
agents[0].benchmark_score=.92;
agents[0].mode='shadow';
context.renderAgents();
assert.equal(card.actions.buttons.has('train'),false);
assert.equal(card.actions.buttons.has('resume'),false);
assert.ok(card.actions.buttons.has('shadow'));
assert.equal(card.actions.buttons.get('shadow').label,'Pause Shadow');
for(const key of ['auto','correct','explore','change','settings'])assert.ok(card.actions.buttons.has(key));
assert.equal(card.actions.buttons.get('auto').disabled,false);
assert.equal(card.actions.buttons.get('correct').disabled,false);
assert.equal(card.actions.buttons.get('change').disabled,false);
assert.equal(card.actions.buttons.get('explore').disabled,false);
'''
        result = subprocess.run(["node", "-e", script], cwd=ROOT, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
