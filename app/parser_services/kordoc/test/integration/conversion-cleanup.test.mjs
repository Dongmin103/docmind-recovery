import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { spawn } from 'node:child_process';
import { mkdtemp, readFile, readdir, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { test } from 'node:test';
import { fileURLToPath } from 'node:url';
import { markdownToHwpx } from 'kordoc';
import { createServer } from '../../src/server.mjs';

const workerScript = fileURLToPath(new URL('../../src/worker.mjs', import.meta.url));
const source = await readFile(new URL('../fixtures/three-slides.pptx', import.meta.url));
const payload = JSON.stringify({
  source_format: 'pptx', source_base64: source.toString('base64'),
  source_hash: createHash('sha256').update(source).digest('hex'),
});
const recoverySource = Buffer.from(await markdownToHwpx('Recovery document'));
const recoveryPayload = JSON.stringify({
  source_format: 'hwpx', source_base64: recoverySource.toString('base64'),
  source_hash: createHash('sha256').update(recoverySource).digest('hex'),
});

async function waitUntil(predicate, timeoutMs = 10000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if (await predicate()) return;
    await new Promise(resolve => setTimeout(resolve, 25));
  }
  throw new Error('timed out waiting for conversion state');
}

async function groupMembers(pgid) {
  const members = [];
  for (const entry of await readdir('/proc')) {
    if (!/^\d+$/.test(entry)) continue;
    try {
      const stat = await readFile(`/proc/${entry}/stat`, 'utf8');
      const fields = stat.slice(stat.lastIndexOf(')') + 2).split(' ');
      if (Number(fields[2]) !== pgid || fields[0] === 'Z') continue;
      const command = await readFile(`/proc/${entry}/cmdline`, 'utf8');
      members.push({ pid: Number(entry), command });
    } catch { /* process exited during scan */ }
  }
  return members;
}

test('killed conversion worker leaves no office process or plaintext and next request succeeds', {
  skip: process.platform !== 'linux',
}, async () => {
  const root = await mkdtemp(join(tmpdir(), 'kordoc-cleanup-test-'));
  const children = [];
  const server = createServer({ jobRoot: root, spawnWorker: () => {
    const child = spawn(process.execPath, [workerScript], { detached: true, stdio: ['pipe', 'pipe', 'ignore'] });
    child.kordocDetached = true;
    children.push(child);
    return child;
  } });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  try {
    const url = `http://127.0.0.1:${server.address().port}/v1/parse`;
    const first = fetch(url, { method: 'POST', headers: { 'content-type': 'application/json' }, body: payload });
    await waitUntil(async () => {
      if (!children[0]) return false;
      const dirs = await readdir(root);
      if (!dirs.length) return false;
      return (await groupMembers(children[0].pid)).some(member => member.command.includes('soffice'));
    });
    const firstGroup = children[0].pid;
    process.kill(firstGroup, 'SIGKILL');
    const failed = await first;
    assert.equal(failed.status, 502);
    assert.deepEqual(await readdir(root), []);
    assert.deepEqual(await groupMembers(firstGroup), []);

    const recovered = await fetch(url, { method: 'POST', headers: { 'content-type': 'application/json' }, body: payload });
    assert.equal(recovered.status, 200);
    assert.deepEqual(await readdir(root), []);
  } finally {
    for (const child of children) {
      try { process.kill(-child.pid, 'SIGKILL'); } catch { /* already gone */ }
    }
    await new Promise(resolve => server.close(resolve));
    await rm(root, { recursive: true, force: true });
  }
});

test('cancelled conversion clears its process group and work directory', {
  skip: process.platform !== 'linux',
}, async () => {
  const root = await mkdtemp(join(tmpdir(), 'kordoc-cancel-test-'));
  const children = [];
  const server = createServer({ jobRoot: root, spawnWorker: () => {
    const child = spawn(process.execPath, [workerScript], { detached: true, stdio: ['pipe', 'pipe', 'ignore'] });
    child.kordocDetached = true;
    children.push(child);
    return child;
  } });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const controller = new AbortController();
  try {
    const url = `http://127.0.0.1:${server.address().port}/v1/parse`;
    const first = fetch(url, { method: 'POST', headers: { 'content-type': 'application/json' },
      body: payload, signal: controller.signal }).catch(() => null);
    await waitUntil(async () => children[0] &&
      (await groupMembers(children[0].pid)).some(member => member.command.includes('soffice')));
    const firstGroup = children[0].pid;
    controller.abort();
    await first;
    await waitUntil(async () => (await readdir(root)).length === 0 &&
      (await groupMembers(firstGroup)).length === 0);
    const recovered = await fetch(url, { method: 'POST', headers: { 'content-type': 'application/json' }, body: payload });
    assert.equal(recovered.status, 200);
    assert.deepEqual(await readdir(root), []);
  } finally {
    controller.abort();
    for (const child of children) {
      try { process.kill(-child.pid, 'SIGKILL'); } catch { /* already gone */ }
    }
    await new Promise(resolve => server.close(resolve));
    await rm(root, { recursive: true, force: true });
  }
});

test('conversion timeout clears its process group and permits the next request', {
  skip: process.platform !== 'linux',
}, async () => {
  const root = await mkdtemp(join(tmpdir(), 'kordoc-timeout-test-'));
  const children = [];
  const server = createServer({ jobRoot: root, timeoutMs: 800, spawnWorker: () => {
    const child = spawn(process.execPath, [workerScript], { detached: true, stdio: ['pipe', 'pipe', 'ignore'] });
    child.kordocDetached = true;
    children.push(child);
    return child;
  } });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  try {
    const url = `http://127.0.0.1:${server.address().port}/v1/parse`;
    const timedOut = await fetch(url, { method: 'POST', headers: { 'content-type': 'application/json' }, body: payload });
    assert.equal(timedOut.status, 504);
    assert.deepEqual(await readdir(root), []);
    assert.deepEqual(await groupMembers(children[0].pid), []);
    const recovered = await fetch(url, { method: 'POST', headers: { 'content-type': 'application/json' }, body: recoveryPayload });
    assert.equal(recovered.status, 200);
  } finally {
    for (const child of children) {
      try { process.kill(-child.pid, 'SIGKILL'); } catch { /* already gone */ }
    }
    await new Promise(resolve => server.close(resolve));
    await rm(root, { recursive: true, force: true });
  }
});
