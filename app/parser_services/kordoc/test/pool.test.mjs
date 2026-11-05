import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { mkdtemp, readdir, rm } from 'node:fs/promises';
import { request } from 'node:http';
import { connect } from 'node:net';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { test } from 'node:test';
import { createServer } from '../src/server.mjs';

// Exercise real HTTP, subprocess pipes, deadlines and workspace cleanup without
// making scheduler assertions depend on a particular Office document's speed.
const workerCode = `require('readline').createInterface({input:process.stdin}).on('line',line=>{
 const j=JSON.parse(line); if(j.payload.crash)process.exit(7);
 setTimeout(()=>process.stdout.write(JSON.stringify({job_id:j.job_id,status:200,
 body:{schema_version:'docmind-kordoc-v2',marker:j.payload.marker,pid:process.pid}})+'\\n'),j.payload.delay||0);
});`;
const post = (base, payload, signal) => fetch(base+'/v1/parse', {
  method:'POST', headers:{'content-type':'application/json'}, body:JSON.stringify(payload), signal,
});
async function fixture(t, options={}) {
  const root=await mkdtemp(join(tmpdir(),'kordoc-pool-test-'));
  const server=createServer({ workerCount:2, timeoutMs:2000, jobRoot:root,
    spawnWorker:()=>spawn(process.execPath,['-e',workerCode],{stdio:['pipe','pipe','ignore']}), ...options });
  await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
  t.after(async()=>{server.closeAllConnections(); await new Promise(resolve=>server.close(resolve)); await rm(root,{recursive:true,force:true});});
  return {base:`http://127.0.0.1:${server.address().port}`,root};
}
async function active(base, count) {
  for(let i=0;i<100;i++) {
    const health=await fetch(base+'/health').then(r=>r.json());
    if(health.active_jobs===count)return health;
    await new Promise(r=>setTimeout(r,10));
  }
  assert.fail(`never observed ${count} admitted jobs`);
}
for (const workerCount of [2,4]) {
  test(`${workerCount} workers own independent jobs; overflow is rejected before upload buffering`,async t=>{
    const {base,root}=await fixture(t,{workerCount});
    const markers=Array.from({length:workerCount},(_,i)=>String(i));
    const requests=markers.map(marker=>post(base,{marker,delay:500}));
    const health=await active(base,workerCount); assert.equal(health.capacity,workerCount);
    assert.equal((await post(base,{marker:'overflow'})).status,503);
    const responses=await Promise.all(requests); assert.deepEqual(responses.map(r=>r.status),Array(workerCount).fill(200));
    const values=await Promise.all(responses.map(r=>r.json()));
    assert.deepEqual(values.map(v=>v.marker),markers); assert.equal(new Set(values.map(v=>v.pid)).size,workerCount);
    assert.deepEqual(await readdir(root),[]);
    const reused=await post(base,{marker:'reuse'}).then(r=>r.json());
    assert.ok(values.some(v=>v.pid===reused.pid));
  });
}
test('crash in one worker leaves the sibling result and slot intact',async t=>{
  const {base,root}=await fixture(t);
  const sibling=post(base,{marker:'sibling',delay:400}); await active(base,1);
  assert.equal((await post(base,{crash:true})).status,502);
  const recovered=await post(base,{marker:'recovered'}); assert.equal(recovered.status,200);
  assert.equal((await (await sibling).json()).marker,'sibling');
  assert.deepEqual(await readdir(root),[]);
});
test('disconnect discards only its worker and frees capacity after cleanup',async t=>{
  const {base,root}=await fixture(t);
  const abort=new AbortController();
  const dropped=post(base,{delay:1500},abort.signal).catch(()=>null);
  await active(base,1);
  const sibling=post(base,{marker:'keep',delay:300}); await active(base,2);
  abort.abort(); await dropped;
  assert.equal((await (await sibling).json()).marker,'keep'); await active(base,0);
  assert.equal((await post(base,{marker:'next'})).status,200);
  assert.deepEqual(await readdir(root),[]);
});
test('one timed out parse does not terminate the other worker',async t=>{
  const {base,root}=await fixture(t,{timeoutMs:350});
  const slow=post(base,{delay:2000}); await active(base,1);
  const fast=await post(base,{marker:'fast',delay:30});
  const fastBody=await fast.json(); assert.equal(fastBody.marker,'fast');
  assert.equal((await slow).status,504);
  const next=await post(base,{marker:'next'}); assert.equal(next.status,200);
  assert.deepEqual(await readdir(root),[]);
});
test('incomplete uploads expire and cannot occupy all slots indefinitely',async t=>{
  const {base}=await fixture(t,{timeoutMs:150});
  const uploads=[0,1].map(()=>{
    const req=request(base+'/v1/parse',{method:'POST',headers:{'content-type':'application/json','content-length':'100'}});
    req.on('error',()=>{});
    const status=new Promise(resolve=>req.on('response',r=>{r.resume();r.on('end',()=>resolve(r.statusCode));}));
    req.write('{'); t.after(()=>req.destroy()); return status;
  });
  assert.deepEqual(await Promise.all(uploads),[504,504]);
  assert.equal((await fetch(base+'/v1/parse',{method:'POST',headers:{'content-type':'application/json'},body:'{'})).status,400);
});
test('expired incomplete upload closes its socket instead of retaining its body',async t=>{
  const {base}=await fixture(t,{timeoutMs:80});
  const socket=connect(Number(new URL(base).port),'127.0.0.1');
  t.after(()=>socket.destroy());
  socket.on('error',()=>{}); socket.resume();
  const closed=new Promise(resolve=>socket.once('close',()=>resolve(true)));
  socket.write('POST /v1/parse HTTP/1.1\r\nHost: localhost\r\nContent-Type: application/json\r\nContent-Length: 1048576\r\nConnection: keep-alive\r\n\r\n');
  socket.write(Buffer.alloc(256*1024,120));
  assert.equal(await Promise.race([closed,new Promise(resolve=>setTimeout(()=>resolve(false),500))]),true);
  assert.equal((await fetch(base+'/v1/parse',{method:'POST',headers:{'content-type':'application/json'},body:'{'})).status,400);
});
