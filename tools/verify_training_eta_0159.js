const fs=require('fs'),vm=require('vm'),assert=require('assert');
const src=fs.readFileSync('adaptive_ai/src/static/p0.js','utf8');
const body=src.match(/duration = v => \{([\s\S]*?)\n  \};/)[1];
const duration=vm.runInNewContext('(v=>{'+body+'})');
assert.strictEqual(duration(86399),'≈ 24 h');
assert.strictEqual(duration(3599),'≈ 60 min');
assert.strictEqual(duration(599.9),'≈ 10 min');
assert.strictEqual(duration(null),null);
console.log('ETA rollover checks PASS');
