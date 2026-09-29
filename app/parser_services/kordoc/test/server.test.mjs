import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { spawn } from 'node:child_process';
import { readFileSync } from 'node:fs';
import { request } from 'node:http';
import { test } from 'node:test';
import { fileURLToPath } from 'node:url';
import { markdownToHwpx } from 'kordoc';
import { createServer } from '../src/server.mjs';

const workerScript = fileURLToPath(new URL('../src/worker.mjs', import.meta.url));

function parsePayload(source, sourceFormat = 'hwpx') {
  return {
    source_base64: source.toString('base64'),
    source_hash: createHash('sha256').update(source).digest('hex'),
    source_format: sourceFormat,
  };
}

async function postDocument(server, payload) {
  return fetch(`http://127.0.0.1:${server.address().port}/v1/parse`, {
    method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify(payload),
  });
}

test('HTTP service reuses one isolated worker for consecutive documents', async () => {
  let spawns = 0;
  const server = createServer({ spawnWorker: () => {
    spawns++;
    return spawn(process.execPath, [workerScript], { stdio: ['pipe', 'pipe', 'ignore'] });
  } });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  try {
    const first = Buffer.from(await markdownToHwpx('First document'));
    const second = Buffer.from(await markdownToHwpx('Second document'));
    const firstResponse = await postDocument(server, parsePayload(first));
    const secondResponse = await postDocument(server, parsePayload(second));
    assert.equal(firstResponse.status, 200);
    assert.equal(secondResponse.status, 200);
    assert.ok((await firstResponse.json()).markdown.includes('First document'));
    assert.ok((await secondResponse.json()).markdown.includes('Second document'));
    assert.equal(spawns, 1);
  } finally {
    await new Promise(resolve => server.close(resolve));
  }
});

test('timed-out document kills its worker and the next document uses a fresh worker', async () => {
  let spawns = 0;
  const server = createServer({ timeoutMs: 400, spawnWorker: () => {
    spawns++;
    return spawns === 1
      ? spawn(process.execPath, ['-e', 'process.stdin.resume()'], { stdio: ['pipe', 'pipe', 'ignore'] })
      : spawn(process.execPath, [workerScript], { stdio: ['pipe', 'pipe', 'ignore'] });
  } });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  try {
    const source = Buffer.from(await markdownToHwpx('Recovery document'));
    const payload = parsePayload(source);
    const timedOut = await postDocument(server, payload);
    assert.equal(timedOut.status, 504);
    assert.deepEqual(await timedOut.json(), { code: 'PARSER_LIMIT_EXCEEDED' });
    const recovered = await postDocument(server, payload);
    assert.equal(recovered.status, 200);
    assert.ok((await recovered.json()).markdown.includes('Recovery document'));
    assert.equal(spawns, 2);
  } finally {
    await new Promise(resolve => server.close(resolve));
  }
});

test('crashed worker is replaced for the next document', async () => {
  let spawns = 0;
  const server = createServer({ spawnWorker: () => {
    spawns++;
    return spawns === 1
      ? spawn(process.execPath, ['-e', 'process.exit(7)'], { stdio: ['pipe', 'pipe', 'ignore'] })
      : spawn(process.execPath, [workerScript], { stdio: ['pipe', 'pipe', 'ignore'] });
  } });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  try {
    const source = Buffer.from(await markdownToHwpx('After crash'));
    const payload = parsePayload(source);
    const failed = await postDocument(server, payload);
    assert.equal(failed.status, 502);
    assert.deepEqual(await failed.json(), { code: 'PARSER_UNAVAILABLE' });
    const recovered = await postDocument(server, payload);
    assert.equal(recovered.status, 200);
    assert.ok((await recovered.json()).markdown.includes('After crash'));
    assert.equal(spawns, 2);
  } finally {
    await new Promise(resolve => server.close(resolve));
  }
});

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

test('HTTP service OCRs an embedded DOCX image offline', {
  skip: !process.env.KORDOC_MODEL_CACHE,
}, async () => {
  const server = createServer();
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  try {
    const source = readFileSync(new URL('./fixtures/image-ocr.docx', import.meta.url));
    const response = await fetch(`http://127.0.0.1:${server.address().port}/v1/parse`, {
      method: 'POST', headers: { 'content-type': 'application/json' },
      body: JSON.stringify({
        source_base64: source.toString('base64'),
        source_hash: createHash('sha256').update(source).digest('hex'),
        source_format: 'docx',
      }),
    });
    assert.equal(response.status, 200);
    const result = await response.json();
    assert.ok(result.image_ocr.some(item => item.text.includes('ALPHA FUNDING 123')));
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

test('HTTP service reports the original page count when the request cap is exceeded', async () => {
  const server = createServer();
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  try {
    const source = readFileSync(new URL('./fixtures/repeated-header.pdf', import.meta.url));
    const response = await fetch(`http://127.0.0.1:${server.address().port}/v1/parse`, {
      method: 'POST', headers: { 'content-type': 'application/json' },
      body: JSON.stringify({
        source_base64: source.toString('base64'),
        source_hash: createHash('sha256').update(source).digest('hex'),
        source_format: 'pdf', max_pdf_pages: 1,
      }),
    });
    assert.equal(response.status, 400);
    assert.deepEqual(await response.json(), { code: 'PARSER_PAGE_LIMIT_EXCEEDED', page_count: 20 });
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
