import shutil
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace

from support import ROOT
from storage import Store
from correct_learning_debug import _recent_observations, MAX_RECENT_ROWS
from policy import DiagonalLinUCB
from agent_correct_generation_history import _current_rows
from training_quality import sensor_snapshot
from radar_context import retain_observed_on_dwell


class Training145Tests(unittest.TestCase):
    def test_unmapped_exact_baseline_preserves_dwell_uncertainty_without_asserting_presence(self):
        eid = "sensor.espen4_stationary_energy"
        agent = {"target_entity": "switch.room"}
        registry = {"switch.room": {"area_id": "bathroom"}}
        for value in ("8", "0", "unavailable"):
            states = {eid: {"state": value}}
            self.assertFalse(retain_observed_on_dwell(sensor_snapshot(agent, [eid], states, registry)))
            snap = sensor_snapshot(agent, [eid], states, registry, local_radars=[eid])
            self.assertTrue(retain_observed_on_dwell(snap))
            self.assertEqual(snap["active"], [])
            self.assertEqual(snap["absent"], [])
            self.assertEqual(snap["reliable"], [])
        foreign = {**registry, eid: {"area_id": "kitchen"}}
        self.assertFalse(retain_observed_on_dwell(sensor_snapshot(
            agent, [eid], {eid: {"state": "8"}}, foreign, local_radars=[eid])))

    def test_current_chart_includes_unflushed_physical_events_without_writing(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "current.db")
            t = 1791404000.
            store.archive_batch([("switch.room", t - 20, "off", {}, None, "live")])
            engine = SimpleNamespace(lock=threading.RLock(), pending_archive=[
                ("switch.room", t, "on", {}, None, "live", t + .1),
                ("switch.room", t + 2, "off", {}, None, "live", t + 2.1),
                ("switch.other", t + 1, "on", {}, None, "live", t + 1.1)])
            manager = SimpleNamespace(store=store, engine=engine)
            agent = {"target_entity": "switch.room", "target_property": "power"}
            points = _current_rows(manager, agent, t - 5, t + 5)
            self.assertEqual([(p["ts"], p["current"]) for p in points],
                             [(t - 5, 0.), (t, 1.), (t + 2, 0.), (t + 5, 0.)])
            self.assertEqual(len(engine.pending_archive), 3)
            self.assertEqual(store.archive_count(), 1)
            # Once flushed, duplicates cannot add another edge or change its time.
            store.archive_batch(engine.pending_archive)
            self.assertEqual(points, _current_rows(manager, agent, t - 5, t + 5))

    def test_correct_false_pass_learns_distance_while_retaining_real_entry(self):
        head = DiagonalLinUCB(12, [0, 1], desired_state_learning=True)
        for _ in range(80):
            head.update(0, {0: 1, 5: .08, 6: 0}, .8, evidence_weight=.25)
            head.update(1, {0: 1, 5: .55, 6: .18}, .8, evidence_weight=.25)
            head.update(1, {0: 1, 5: .24, 6: .18}, .8, evidence_weight=.25)
        far = {0: 1, 5: .55, 6: .56}
        for _ in range(4):
            head.update(0, far, 1)
        head.state_classifier.fit()
        restored = DiagonalLinUCB(12, [0, 1], model=head.export())
        for model in (head, restored):
            self.assertEqual(model.choose(far)[0]["index"], 0)
            self.assertEqual(model.choose({0: 1, 5: .55, 6: .18})[0]["index"], 1)
            self.assertEqual(model.choose({0: 1, 5: .24, 6: .18})[0]["index"], 1)

    def test_debug_without_correct_includes_bounded_actual_signals_and_pending_decisions(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "debug.db")
            store.archive_batch([("sensor.radar", i, "24", {}, None, "test", i + .05)
                                 for i in range(MAX_RECENT_ROWS + 5)])
            with store.conn() as conn:
                conn.execute("CREATE TABLE decision_history(agent_id TEXT,ts REAL,current REAL,desired REAL)")
                conn.execute("INSERT INTO decision_history VALUES('a',2100,1,0)")
            engine = SimpleNamespace(teaching=SimpleNamespace(
                lock=threading.RLock(), buffer=[("a", 2200, 1, 1), ("b", 2201, 0, 0)]))
            # Narrow window yields actual recent rows, no reconstruction and no other agents.
            result = _recent_observations(store, engine, "a", ["sensor.radar"], 2300)
            self.assertEqual(result["observed_decisions"], [
                {"ts": 2100., "current": 1., "desired": 0.}, {"ts": 2200., "current": 1, "desired": 1}])
            self.assertTrue(all(r["ts"] >= result["start"] for r in result["signals"]))
            self.assertFalse(result["signals_truncated"])
            # A chatty sensor cannot make an explicit export unbounded.
            store.archive_batch([("sensor.radar", 2250 + i / 10000, "25", {}, None, "test", None)
                                 for i in range(MAX_RECENT_ROWS + 5)])
            result = _recent_observations(store, engine, "a", ["sensor.radar"], 2300)
            self.assertEqual(len(result["signals"]), MAX_RECENT_ROWS)
            self.assertTrue(result["signals_truncated"])


@unittest.skipUnless(shutil.which("node"), "Node required")
class Realtime145Tests(unittest.TestCase):
    def run_node(self, script):
        out = subprocess.run(["node", "-e", script], cwd=ROOT, text=True, capture_output=True, timeout=15)
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)

    def test_shared_broker_coalesces_live_inflight_but_never_reuses_finished_snapshot(self):
        self.run_node(r'''
const vm=require('node:vm'),fs=require('node:fs'),assert=require('node:assert/strict');
let calls=[],resolve;const c={URL,Response,performance:require('node:perf_hooks').performance,location:{href:'http://localhost/'},
 fetch:(url,init)=>{calls.push([url,init]);return new Promise(r=>resolve=r);}};c.window=c;
vm.runInNewContext(fs.readFileSync('adaptive_ai/src/static/polling_guard.js','utf8'),c);
(async()=>{
 const p=c.fetch('api/live'),q=c.fetch('api/live');assert.equal(calls.length,1);
 assert.equal(calls[0][1].cache,'no-store');assert.equal(calls[0][1].adaptiveAiTimeoutMs,2500);
 resolve(new Response('1'));assert.equal(await (await p).text(),'1');assert.equal(await (await q).text(),'1');
 const r=c.fetch('api/live');assert.equal(calls.length,2);resolve(new Response('2'));assert.equal(await (await r).text(),'2');
 const a=c.fetch('api/agents');resolve(new Response('configs'));await a;await c.fetch('api/agents');assert.equal(calls.length,3);
 const m=c.fetch('api/live',{method:'POST',body:'mutation'});assert.equal(calls.length,4);resolve(new Response('ok'));await m;
})().catch(e=>{console.error(e);process.exitCode=1;});
''')

    def test_correct_live_chart_advances_pauses_for_inspection_and_stops_on_close(self):
        self.run_node(r'''
const vm=require('node:vm'),fs=require('node:fs'),assert=require('node:assert/strict');
const timers=new Map(),requests=[];let clock=2000000,id=0,dialog,defer=null;
class Node{constructor(){this.nodes=new Map();this.value='';this.dataset={};this.innerHTML='';}
 querySelector(k){if(!this.nodes.has(k))this.nodes.set(k,new Node());return this.nodes.get(k);}
 querySelectorAll(){return [];}setAttribute(){}hasAttribute(){return false;}getBoundingClientRect(){return {left:0,width:1030};}
 showModal(){this.open=true;}close(){this.open=false;this.onclose?.();}}
const c={lastAgents:[],Date:class extends Date{static now(){return clock;}},document:{hidden:false,body:{append(){}},
 createElement(){return dialog=new Node();},querySelectorAll(){return [];}},renderAgents(){},
 sessionStorage:{getItem(){return null;}},setTimeout(fn,ms){timers.set(++id,fn);return id;},clearTimeout(id){timers.delete(id);},
 fetch:async path=>{requests.push(path);if(path.endsWith('/status'))return {ok:true,json:async()=>({name:'Room',generation_number:1,min_value:0,max_value:1,target_property:'power'})};
 const u=new URL(path,'http://localhost/');if(defer){await new Promise(r=>defer.resolve=r);defer=null;}
 return {ok:true,json:async()=>({start:Number(u.searchParams.get('start')),end:Number(u.searchParams.get('end')),series:{},labels:[]})};},
 alert(){},console};c.window=c;
vm.runInNewContext(fs.readFileSync('adaptive_ai/src/static/agent_workflow_ui.js','utf8'),c);
const tick=async()=>{const [id,fn]=timers.entries().next().value;timers.delete(id);return fn();};
(async()=>{
 await c.openWorkflowCorrect('a');assert.equal(requests.length,2);assert.equal(timers.size,1);
 clock+=5000;await tick();assert.equal(requests.length,3);assert.ok(requests[2].includes('end=2005'));
 c.document.hidden=true;await tick();assert.equal(requests.length,3);c.document.hidden=false;
 dialog.querySelector('[data-time]').oninput();assert.equal(timers.size,0);
 clock+=1000;dialog.querySelector('[data-live]').onclick();await new Promise(r=>setImmediate(r));assert.equal(timers.size,1);
 defer={};clock+=1000;const pending=tick();await new Promise(r=>setImmediate(r));assert.equal(timers.size,0);
 const before=dialog.querySelector('[data-chart]').innerHTML;dialog.querySelector('[data-close]').onclick();
 defer.resolve();await pending;assert.equal(timers.size,0);assert.equal(dialog.querySelector('[data-chart]').innerHTML,before);
})().catch(e=>{console.error(e);process.exitCode=1;});
''')
