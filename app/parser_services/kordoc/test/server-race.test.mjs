import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { request } from 'node:http';
import { test } from 'node:test';

process.env.KORDOC_MAX_SOURCE_BYTES = '1';
const { createServer } = await import('../src/server.mjs');

test('late end of a rejected upload cannot release a later request', async () => {
  const server = createServer({ timeoutMs: 3000, spawnWorker: () =>
    spawn(process.execPath, ['-e', 'process.stdin.resume()'], { stdio: ['pipe', 'pipe', 'ignore'] }) });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const base = `http://127.0.0.1:${server.address().port}`;
  const firstSize = Math.ceil(4 / 3) + 4096;
  let second;
  try {
    const first = request(`${base}/v1/parse`, {
      method: 'POST',
      headers: { 'content-type': 'application/json', 'content-length': String(firstSize + 101) },
    });
    first.on('error', () => {});
    const rejected = new Promise(resolve => first.on('response', res => {
      res.resume();
      res.on('end', () => resolve(res.statusCode));
    }));
    first.write(Buffer.alloc(firstSize + 1, 120));
    assert.equal(await rejected, 413);

    second = request(`${base}/v1/parse`, {
      method: 'POST', headers: { 'content-type': 'application/json', 'content-length': '2' },
    });
    second.on('error', () => {});
    second.end('{}');
    await new Promise(resolve => setTimeout(resolve, 100));
    assert.equal((await fetch(`${base}/health`).then(res => res.json())).busy, true);

    first.end(Buffer.alloc(100, 120));
    await new Promise(resolve => setTimeout(resolve, 100));
    assert.equal((await fetch(`${base}/health`).then(res => res.json())).busy, true);
  } finally {
    second?.destroy();
    await new Promise(resolve => server.close(resolve));
  }
});
