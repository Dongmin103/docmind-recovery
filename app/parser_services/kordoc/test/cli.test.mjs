import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { spawnSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { test } from 'node:test';
import { markdownToHwpx } from 'kordoc';

const cli = fileURLToPath(new URL('../src/cli.mjs', import.meta.url));

test('CLI accepts one HWPX payload and prints one JSON result', async () => {
  const source = Buffer.from(await markdownToHwpx('합성 시험 문서'));
  const payload = JSON.stringify({
    source_base64: source.toString('base64'),
    source_hash: createHash('sha256').update(source).digest('hex'),
    source_format: 'hwpx',
  });
  const done = spawnSync(process.execPath, [cli], { input: payload, encoding: 'utf8' });
  assert.equal(done.status, 0, done.stderr);
  const result = JSON.parse(done.stdout);
  assert.equal(result.parser_name, 'kordoc');
  assert.equal(result.source_format, 'hwpx');
});

test('CLI rejects malformed payload without printing document content', () => {
  const done = spawnSync(process.execPath, [cli], { input: 'invalid', encoding: 'utf8' });
  assert.notEqual(done.status, 0);
  assert.equal(done.stdout, '');
  assert.ok(!done.stderr.includes('invalid'));
});
