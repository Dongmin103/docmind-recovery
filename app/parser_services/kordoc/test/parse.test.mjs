import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';
import { compare, diffBlocks, markdownToHwpx } from 'kordoc';
import { parseDocument } from '../src/parse.mjs';

test('HWPX parses to structured blocks with actual engine identity', async () => {
  const source = Buffer.from(await markdownToHwpx('# 시험 계획\n\n본문 123'));
  const result = await parseDocument({
    source_base64: source.toString('base64'),
    source_hash: createHash('sha256').update(source).digest('hex'),
    source_format: 'hwpx',
  });
  assert.equal(result.parser_name, 'kordoc');
  assert.equal(result.parser_version, '4.15.7');
  assert.equal(result.source_format, 'hwpx');
  assert.ok(result.blocks.some(block => JSON.stringify(block).includes('본문 123')));
  assert.ok(!JSON.stringify(result).includes('imageData'));
});

test('wrong hash and wrong format fail closed', async () => {
  const source = Buffer.from(await markdownToHwpx('본문'));
  const payload = { source_base64: source.toString('base64'), source_hash: '0'.repeat(64), source_format: 'hwpx' };
  await assert.rejects(parseDocument(payload), /hash/i);
  payload.source_hash = createHash('sha256').update(source).digest('hex');
  payload.source_format = 'hwp';
  await assert.rejects(parseDocument(payload), /format/i);
});

test('kordoc table comparison identifies changed cells', () => {
  const table = text => ({ type: 'table', table: { rows: 1, cols: 2, cells: [[
    { text: '금액', rowSpan: 1, colSpan: 1 },
    { text, rowSpan: 1, colSpan: 1 },
  ]] } });
  const result = diffBlocks([table('100')], [table('200')]);
  assert.equal(result.stats.modified, 1);
  assert.equal(result.diffs[0].cellDiffs[0][1].type, 'modified');
});

test('compare API finds a changed cell in two synthetic HWPX versions', async () => {
  const oldFile = await markdownToHwpx('| 항목 | 금액 |\n| --- | --- |\n| 가 | 100 |');
  const newFile = await markdownToHwpx('| 항목 | 금액 |\n| --- | --- |\n| 가 | 200 |');
  const result = await compare(oldFile, newFile, { images: false, ocr: false });
  assert.ok(result.diffs.some(diff => diff.cellDiffs?.flat().some(cell =>
    cell.before === '100' && cell.after === '200' && cell.type === 'modified')));
});

for (const format of ['docx', 'pdf', 'xlsx']) {
  test(`${format.toUpperCase()} pilot returns source-identified blocks`, async () => {
    const source = readFileSync(new URL(`./fixtures/office-sample.${format}`, import.meta.url));
    const hash = createHash('sha256').update(source).digest('hex');
    const result = await parseDocument({
      source_base64: source.toString('base64'), source_hash: hash, source_format: format,
    });
    assert.equal(result.source_format, format);
    assert.equal(result.source_hash, hash);
    assert.ok(result.blocks.length > 0);
    assert.ok(JSON.stringify(result.blocks).includes('Alpha'));
    if (format === 'pdf') {
      assert.ok(result.blocks.some(block => block.bbox && block.pageNumber === 1));
    }
  });
}

test('PDF pilot keeps the first body line on every repeated page', async () => {
  const source = readFileSync(new URL('./fixtures/repeated-header.pdf', import.meta.url));
  const result = await parseDocument({
    source_base64: source.toString('base64'),
    source_hash: createHash('sha256').update(source).digest('hex'),
    source_format: 'pdf',
  });
  const content = result.blocks.map(block => block.text || '').join('\n');
  const records = new Set([...content.matchAll(/Record (\d{4})/g)].map(match => Number(match[1])));
  assert.equal(records.size, 400);
  assert.ok(content.includes('Synthetic page 1'));
  assert.ok(content.includes('Record 0000'));
  assert.ok(content.includes('Record 0380'));
});
