import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';
import { compare, diffBlocks, markdownToHwpx } from 'kordoc';
import { parseDocument } from '../src/parse.mjs';

function payloadFor(source, sourceFormat) {
  return {
    source_base64: source.toString('base64'),
    source_hash: createHash('sha256').update(source).digest('hex'),
    source_format: sourceFormat,
  };
}

// Two original pages: a cropped, rotated text page and a blank page.
function croppedRotatedPdf() {
  const objects = [
    '<< /Type /Catalog /Pages 2 0 R >>',
    '<< /Type /Pages /Kids [3 0 R 4 0 R] /Count 2 >>',
    '<< /Type /Page /Parent 2 0 R /MediaBox [0 0 500 400] /CropBox [50 100 450 300] /Rotate 90 /Resources << /Font << /F1 5 0 R >> >> /Contents 6 0 R >>',
    '<< /Type /Page /Parent 2 0 R /MediaBox [0 0 300 500] /Contents 7 0 R >>',
    '<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>',
    null,
    null,
  ];
  const streams = {
    6: 'BT /F1 12 Tf 70 150 Td (Alpha funding record) Tj ET',
    7: '',
  };
  for (const [number, stream] of Object.entries(streams)) {
    objects[Number(number) - 1] = `<< /Length ${Buffer.byteLength(stream)} >>\nstream\n${stream}\nendstream`;
  }
  let body = '%PDF-1.4\n';
  const offsets = [0];
  for (let i = 0; i < objects.length; i++) {
    offsets.push(Buffer.byteLength(body));
    body += `${i + 1} 0 obj\n${objects[i]}\nendobj\n`;
  }
  const xref = Buffer.byteLength(body);
  body += `xref\n0 ${offsets.length}\n0000000000 65535 f \n`;
  for (const offset of offsets.slice(1)) body += `${String(offset).padStart(10, '0')} 00000 n \n`;
  body += `trailer\n<< /Size ${offsets.length} /Root 1 0 R >>\nstartxref\n${xref}\n%%EOF\n`;
  return Buffer.from(body);
}

test('response exposes diagnostic markdown without changing structured blocks', async () => {
  const source = Buffer.from(await markdownToHwpx('# Heading\n\n본문 123'));
  const result = await parseDocument(payloadFor(source, 'hwpx'));
  assert.equal(result.schema_version, 'docmind-kordoc-v2');
  assert.equal(result.patch_revision, 'sha256:25378aebb75d6507296cc22b60a6158935ca3af5a4ac888708b3cee08f21646b');
  assert.ok(result.markdown.includes('본문 123'));
  assert.ok(result.blocks.some(block => JSON.stringify(block).includes('본문 123')));
  assert.equal('pdf_pages' in result, false);
});

test('PDF reports original cropped page geometry and the blank page', async () => {
  const source = croppedRotatedPdf();
  const result = await parseDocument(payloadFor(source, 'pdf'));
  assert.equal(result.schema_version, 'docmind-kordoc-v2');
  assert.equal(result.metadata.pageCount, 2);
  assert.deepEqual(result.pdf_pages.map(({ ocr_applied, ...page }) => page), [
    { page: 1, width: 400, height: 200, has_images: false },
    { page: 2, width: 300, height: 500, has_images: false },
  ]);
  assert.ok(result.pdf_pages.every(page => page.ocr_applied === false));
  const boxes = result.blocks.filter(block => block.pageNumber === 1 && block.bbox).map(block => block.bbox);
  assert.ok(boxes.length > 0);
  assert.ok(boxes.every(box => box.x >= 0 && box.y >= 0 &&
    box.x + box.width <= result.pdf_pages[0].width &&
    box.y + box.height <= result.pdf_pages[0].height), JSON.stringify(boxes));
  assert.ok(result.markdown.includes('Alpha funding record'));
});

test('configured PDF page cap rejects before returning a partial document', async () => {
  const previous = process.env.KORDOC_MAX_PDF_PAGES;
  process.env.KORDOC_MAX_PDF_PAGES = '1';
  try {
    await assert.rejects(parseDocument(payloadFor(croppedRotatedPdf(), 'pdf')), error =>
      error.code === 'PARSER_PAGE_LIMIT_EXCEEDED' && error.page_count === 2);
  } finally {
    if (previous === undefined) delete process.env.KORDOC_MAX_PDF_PAGES;
    else process.env.KORDOC_MAX_PDF_PAGES = previous;
  }
});

test('request PDF page cap is applied when lower than the configured cap', async () => {
  const previous = process.env.KORDOC_MAX_PDF_PAGES;
  process.env.KORDOC_MAX_PDF_PAGES = '5';
  try {
    await assert.rejects(parseDocument({ ...payloadFor(croppedRotatedPdf(), 'pdf'), max_pdf_pages: 1 }), error =>
      error.code === 'PARSER_PAGE_LIMIT_EXCEEDED' && error.page_count === 2);
  } finally {
    if (previous === undefined) delete process.env.KORDOC_MAX_PDF_PAGES;
    else process.env.KORDOC_MAX_PDF_PAGES = previous;
  }
});

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

for (const [format, marker] of [['pdf', 'PAGE'], ['pptx', 'SLIDE']]) {
  test(`${format.toUpperCase()} keeps text from separate pages in separate source blocks`, {
    skip: format === 'pptx' && process.platform !== 'linux',
  }, async () => {
    const source = readFileSync(new URL(`./fixtures/three-${format === 'pdf' ? 'pages.pdf' : 'slides.pptx'}`, import.meta.url));
    const result = await parseDocument(payloadFor(source, format));
    for (const page of [1, 2, 3]) {
      const label = `${marker}_${page}_UNIQUE`;
      const matches = result.blocks.filter(block => block.text?.includes(label));
      assert.equal(matches.length, 1, label);
      assert.equal(matches[0].pageNumber, page, label);
      assert.ok(!matches[0].text.includes(`${marker}_${page === 1 ? 2 : 1}_UNIQUE`), label);
    }
  });
}

test('DOCX embedded image uses local kordoc OCR when models are provisioned', {
  skip: !process.env.KORDOC_MODEL_CACHE,
}, async () => {
  const source = readFileSync(new URL('./fixtures/image-ocr.docx', import.meta.url));
  const result = await parseDocument({
    source_base64: source.toString('base64'),
    source_hash: createHash('sha256').update(source).digest('hex'),
    source_format: 'docx',
  });
  assert.ok(result.blocks.some(block => block.type === 'image'));
  assert.ok(result.image_ocr?.some(item => item.text.includes('ALPHA FUNDING 123')));
  assert.ok(!JSON.stringify(result).includes('imageData'));
});

test('DOCX image OCR failure keeps its searchable body without an OCR warning', async () => {
  const source = readFileSync(new URL('./fixtures/image-ocr.docx', import.meta.url));
  const result = await parseDocument(payloadFor(source, 'docx'), {
    parseImageFn: async () => ({ success: false, code: 'OCR_FAILED' }),
  });
  assert.ok(result.blocks.some(block => block.text?.includes('Visible paragraph')));
  assert.deepEqual(result.image_ocr, []);
  assert.ok(!result.warnings.some(warning => String(warning.code).includes('OCR_FAILED')));
});

test('scanned PDF uses local OCR with page geometry when models are provisioned', {
  skip: !process.env.KORDOC_MODEL_CACHE,
}, async () => {
  const source = readFileSync(new URL('./fixtures/scanned-ocr.pdf', import.meta.url));
  const result = await parseDocument({
    source_base64: source.toString('base64'),
    source_hash: createHash('sha256').update(source).digest('hex'),
    source_format: 'pdf',
  });
  const textBlocks = result.blocks.filter(block => block.type === 'paragraph');
  assert.ok(textBlocks.some(block => block.text.includes('ALPHA FUNDING 123')));
  assert.ok(textBlocks.some(block => block.text.includes('SECOND RECORD 456')));
  assert.ok(textBlocks.every(block => block.pageNumber === 1 && block.bbox));
  assert.ok(result.warnings.some(warning => warning.code === 'OCR_APPLIED'));
  assert.deepEqual(result.pdf_pages, [
    { page: 1, width: 600, height: 800, has_images: true, ocr_applied: true },
  ]);
});
