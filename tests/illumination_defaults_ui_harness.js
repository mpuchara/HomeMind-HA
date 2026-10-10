const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');

class Field {
  constructor(value=''){this.value=String(value);this.disabled=false;this.required=false;}
  set innerHTML(value){this.html=value;this.value=(value.match(/<option value="([^"]*)"/)||[])[1]||'';}
}
const names={free:['focus','intensity','max_step','interval','daily_budget','observation_seconds'],targeted_sensor:['sensor_entity'],
  additional_signal:['signal_entity','signal_purpose','signal_threshold','signal_hysteresis','signal_age','signal_reference']};
class Dialog {
  constructor(){this.nodes={};this.open=false;}
  setAttribute(){}
  set innerHTML(html){
    const elements={};
    for(const match of html.matchAll(/<input\b([^>]*\bname="([^"]+)"[^>]*)>/g))elements[match[2]]=new Field((match[1].match(/\bvalue="([^"]*)"/)||[])[1]);
    for(const match of html.matchAll(/<select\b[^>]*\bname="([^"]+)"[^>]*>([\s\S]*?)<\/select>/g)){
      const options=[...match[2].matchAll(/<option\b([^>]*)>/g)];
      const option=options.find(m=>/\bselected\b/.test(m[1]))||options[0];
      elements[match[1]]=new Field((option?.[1].match(/value="([^"]*)"/)||[])[1]||'');
    }
    this.form={elements,addEventListener(){}};
    this.panes=Object.entries(names).map(([pane,fields])=>({dataset:{pane},hidden:pane!=='free',querySelectorAll:()=>fields.map(f=>elements[f])}));
    this.buttons=Object.keys(names).map(mode=>({dataset:{mode},className:''}));
    this.nodes={};
  }
  querySelector(selector){if(selector==='form')return this.form;return this.nodes[selector]??=new Field();}
  querySelectorAll(selector){if(selector==='[data-pane]')return this.panes;if(selector==='[data-mode]')return this.buttons;return [];}
  close(){this.open=false;}
  showModal(){this.open=true;}
}
const LIGHT='sensor.kitchen_light',OTHER='sensor.other_light';
const config=(threshold,hysteresis=0,age=900)=>({version:1,entity_id:LIGHT,purpose:'avoid_bright_on',unit:'raw',threshold,hysteresis,max_age_seconds:age});
const alternatives=[{config:config(40),source:{kind:'automation',id:'automation.kitchen'}},
  {config:config(50,3,60),source:{kind:'agent',id:'kitchen_agent'}}];
let explore={illumination_defaults:{[LIGHT]:{...alternatives[0],alternatives,conflicting:true}}};
let submitted;
const dialog=new Dialog();
const sandbox={console,Map,Number,String,Object,Array,
  document:{createElement:()=>dialog,body:{appendChild(){}}},
  window:{},alert:error=>{throw Error(error);},
  fetch:async(path,opts)=>{
    if(opts.method==='POST'){submitted=JSON.parse(opts.body);return {ok:true,json:async()=>({ok:true})};}
    const value=path==='api/entities'?[{entity_id:LIGHT},{entity_id:OTHER}]:path.endsWith('/explore')?explore:
      {name:'Stairs',target_property:'power',generation_number:0};
    return {ok:true,json:async()=>value};
  }};
vm.runInNewContext(fs.readFileSync('adaptive_ai/src/static/experiments.js','utf8'),sandbox);
(async()=>{
  await sandbox.window.openExplore('stairs');
  let f=dialog.form.elements;
  assert.equal(f.signal_threshold.value,''); // No arbitrary 40 before sensor selection.
  dialog.buttons.find(b=>b.dataset.mode==='additional_signal').onclick();
  f.signal_entity.value=LIGHT;f.signal_entity.onchange();
  f.signal_purpose.value='avoid_bright_on';f.signal_purpose.onchange();
  assert.equal(Number(f.signal_threshold.value),40);
  assert.equal(Number(f.signal_hysteresis.value),0);
  assert.equal(f.signal_threshold.required,true);
  assert.match(dialog.querySelector('[data-signal-source]').textContent,/automation.kitchen/);
  f.signal_threshold.value='31';f.signal_threshold.oninput();
  f.signal_hysteresis.value='2';f.signal_hysteresis.oninput();
  f.signal_age.value='120';f.signal_age.oninput();
  f.signal_entity.value=OTHER;f.signal_entity.onchange();
  assert.equal(f.signal_threshold.value,'');
  f.signal_entity.value=LIGHT;f.signal_entity.onchange();
  assert.equal(f.signal_threshold.value,'31');assert.equal(f.signal_hysteresis.value,'2');assert.equal(f.signal_age.value,'120');
  await dialog.form.onsubmit({preventDefault(){}});
  assert.equal(submitted.additional_signal.threshold,31);assert.equal(submitted.additional_signal.hysteresis,2);
  f.signal_reference.value='1';f.signal_reference.onchange();
  assert.equal(Number(f.signal_threshold.value),50);assert.equal(Number(f.signal_hysteresis.value),3);assert.equal(Number(f.signal_age.value),60);

  const saved=config(67,5,100);
  const own={config:saved,source:{kind:'selected_agent',id:'stairs'}};
  explore={additional_signal:saved,illumination_defaults:{[LIGHT]:{...own,alternatives:[own,...alternatives],conflicting:true}}};
  await sandbox.window.openExplore('stairs');f=dialog.form.elements;
  dialog.buttons.find(b=>b.dataset.mode==='additional_signal').onclick();
  assert.equal(Number(f.signal_threshold.value),67);assert.equal(Number(f.signal_hysteresis.value),5);
  f.signal_threshold.value='72';f.signal_threshold.oninput();
  await dialog.form.onsubmit({preventDefault(){}});
  assert.equal(submitted.additional_signal.threshold,72);
  console.log('Editable same-sensor defaults, alternatives, draft preservation and submitted override: OK');
})().catch(error=>{console.error(error);process.exitCode=1;});
