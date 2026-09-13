"""Execute the real entry script: syntax checking misses undefined globals."""
import shutil
import subprocess
import unittest
from support import ROOT


class FrontendStartupTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which('node'), 'Node is required for browser-script execution')
    def test_entry_script_reaches_first_request_and_installs_refresh(self):
        script = r'''
const vm = require('node:vm');
const fs = require('node:fs');
const assert = require('node:assert/strict');
const nodes = new Map();
const requests = [], timers = [];
const context = {
  document: {querySelector(selector) {
    if (!nodes.has(selector)) nodes.set(selector, {value:'', classList:{add(){},remove(){}}});
    return nodes.get(selector);
  }},
  localStorage: {getItem(){return null},setItem(){}},
  fetch(path) { requests.push(path); return new Promise(()=>{}); },
  setInterval(fn, ms) {timers.push([fn,ms]);},
  setTimeout(){}, console
};
context.window = context;
vm.runInNewContext(fs.readFileSync('adaptive_ai/src/static/app.js','utf8'), context);
assert.deepEqual(requests, ['api/status']);
assert.equal(timers.length, 1);
assert.equal(timers[0][1], 4000);
assert.equal(typeof nodes.get('#newAgentBtn').onclick, 'function');
assert.equal(typeof nodes.get('#rescanBtn').onclick, 'function');
assert.equal(typeof nodes.get('#agentForm').onsubmit, 'function');
'''
        result = subprocess.run(['node', '-e', script], cwd=ROOT, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
