import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { request } from 'node:http';
import { test } from 'node:test';
import { markdownToHwpx } from 'kordoc';
import { createServer } from '../src/server.mjs';

test('HTTP service parses an HWPX document through its worker process', async () => {
  const server = createServer();
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const base = `http://127.0.0.1:${server.address().port}`;
  try {
    const health = await fetch(`${base}/health`).then(response => response.json());
    assert.equal(health.ready, true);
    const source = Buffer.from(await markdownToHwpx('합성 본문'));
    const response = await fetch(`${base}/v1/parse`, {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({
        source_base64: source.toString('base64'),
        source_hash: createHash('sha256').update(source).digest('hex'),
        source_format: 'hwpx',
      }),
    });
    assert.equal(response.status, 200);
    const result = await response.json();
    assert.equal(result.parser_name, 'kordoc');
    assert.ok(result.blocks.length > 0);
  } finally {
    await new Promise(resolve => server.close(resolve));
  }
});

test('HTTP service rejects bad input and does not echo it', async () => {
  const server = createServer();
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  try {
    const response = await fetch(`http://127.0.0.1:${server.address().port}/v1/parse`, {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: '{private text}',
    });
    assert.equal(response.status, 400);
    assert.ok(!(await response.text()).includes('private text'));
  } finally {
    await new Promise(resolve => server.close(resolve));
  }
});

test('HTTP service allows only one in-flight upload', async () => {
  const server = createServer();
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const base = `http://127.0.0.1:${server.address().port}`;
  const pending = request(`${base}/v1/parse`, {
    method: 'POST',
    headers: { 'content-type': 'application/json', 'content-length': '100' },
  });
  pending.on('error', () => {});
  try {
    pending.write('{');
    await new Promise(resolve => setTimeout(resolve, 30));
    const response = await fetch(`${base}/v1/parse`, {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: '{}',
    });
    assert.equal(response.status, 503);
  } finally {
    pending.destroy();
    await new Promise(resolve => server.close(resolve));
  }
});
