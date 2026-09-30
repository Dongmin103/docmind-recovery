import { createHash } from 'node:crypto';
import { VERSION, parse, parseImage } from 'kordoc';
import { convertOffice } from './convert.mjs';

const MAX_SOURCE_BYTES = Number(process.env.KORDOC_MAX_SOURCE_BYTES || 64 * 1024 * 1024);
const MAX_KORDOC_PDF_PAGES = 5000;
const PATCH_REVISION = 'sha256:25378aebb75d6507296cc22b60a6158935ca3af5a4ac888708b3cee08f21646b';
const PILOT_FORMATS = new Set(['hwp', 'hwpx', 'doc', 'docx', 'pdf', 'xls', 'xlsx', 'pptx']);

function configuredPdfPageLimit() {
  const maxPages = Number(process.env.KORDOC_MAX_PDF_PAGES || MAX_KORDOC_PDF_PAGES);
  if (!Number.isSafeInteger(maxPages) || maxPages < 1 || maxPages > MAX_KORDOC_PDF_PAGES) {
    throw new Error('invalid PDF page limit');
  }
  return maxPages;
}

function requestedPdfPageLimit(payload) {
  const configured = configuredPdfPageLimit();
  if (payload.max_pdf_pages === undefined) return configured;
  if (!Number.isSafeInteger(payload.max_pdf_pages) || payload.max_pdf_pages < 1) {
    throw new Error('invalid requested PDF page limit');
  }
  return Math.min(configured, payload.max_pdf_pages);
}

function completePdfPages(result) {
  const pageCount = result.metadata?.pageCount;
  const pages = result.pdfPages;
  if (result.warnings?.some(warning => warning.code === 'PARTIAL_PARSE')) {
    throw new Error('partial PDF parse');
  }
  if (!Number.isSafeInteger(pageCount) || pageCount < 1 || !Array.isArray(pages) || pages.length !== pageCount) {
    throw new Error('incomplete PDF page metadata');
  }
  for (let index = 0; index < pages.length; index++) {
    const { page, width, height, has_images, ocr_applied } = pages[index];
    if (page !== index + 1 || !Number.isFinite(width) || width <= 0 || !Number.isFinite(height) || height <= 0 ||
      typeof has_images !== 'boolean' || typeof ocr_applied !== 'boolean') {
      throw new Error('invalid PDF page metadata');
    }
  }
  return pages;
}

export async function parseDocument(payload, {
  workDir, parseImageFn = parseImage, parseFn = parse, convertOfficeFn = convertOffice,
} = {}) {
  if (!payload || !PILOT_FORMATS.has(payload.source_format)) {
    throw new Error('unsupported source format');
  }
  if (typeof payload.source_base64 !== 'string' || !/^[A-Za-z0-9+/]+={0,2}$/.test(payload.source_base64)) {
    throw new Error('invalid source base64');
  }
  const source = Buffer.from(payload.source_base64, 'base64');
  if (!source.length || source.length > MAX_SOURCE_BYTES || source.toString('base64') !== payload.source_base64) {
    throw new Error('invalid source size or encoding');
  }
  const hash = createHash('sha256').update(source).digest('hex');
  if (hash !== payload.source_hash) {
    throw new Error('source hash mismatch');
  }
  const ocrEnabled = process.env.KORDOC_OCR_ENABLED !== '0';
  const parseFormat = payload.source_format === 'doc' ? 'docx' : payload.source_format === 'pptx' ? 'pdf' : payload.source_format;
  const parseSource = parseFormat === payload.source_format ? source : await convertOfficeFn(source, payload.source_format, { workDir });
  const result = await parseFn(parseSource, {
    images: parseFormat === 'docx', ocr: ocrEnabled && parseFormat === 'pdf',
    ...(parseFormat === 'pdf' ? { removeHeaderFooter: false, maxPages: requestedPdfPageLimit(payload) } : {}),
  });
  if (!result.success) {
    if (result.code === 'PAGE_LIMIT_EXCEEDED' && Number.isSafeInteger(result.pageCount) && result.pageCount > 0) {
      const error = new Error('PDF page limit exceeded');
      error.code = 'PARSER_PAGE_LIMIT_EXCEEDED';
      error.page_count = result.pageCount;
      throw error;
    }
    throw new Error(`parse failed: ${result.code || 'unknown'}`);
  }
  if (result.fileType !== parseFormat) {
    throw new Error('source format mismatch');
  }
  if (!result.blocks.length) {
    throw new Error('empty parse result');
  }
  const pdfPages = parseFormat === 'pdf' ? completePdfPages(result) : undefined;
  const imageOcr = [];
  if (parseFormat === 'docx' && ocrEnabled) {
    const images = new Map((result.images || []).map(item => [item.filename, item]));
    for (const block of result.blocks) {
      if (block.type !== 'image') continue;
      const image = images.get(block.text);
      if (!image?.data || !['image/png', 'image/jpeg', 'image/gif', 'image/webp'].includes(image.mimeType)) continue;
      const bytes = image.data;
      let ocr;
      try {
        ocr = await parseImageFn(bytes.buffer.slice(bytes.byteOffset, bytes.byteOffset + bytes.byteLength), {
          images: false, ocr: true,
        });
      } catch {
        continue;
      }
      if (!ocr.success || !(ocr.markdown || '').trim()) continue;
      imageOcr.push({
        filename: block.text, source: image.source,
        source_hash: createHash('sha256').update(bytes).digest('hex'),
        text: ocr.markdown.trim(),
      });
    }
  }
  return {
    schema_version: 'docmind-kordoc-v2',
    patch_revision: PATCH_REVISION,
    parser_name: 'kordoc',
    parser_version: VERSION,
    source_format: payload.source_format,
    source_hash: hash,
    blocks: JSON.parse(JSON.stringify(result.blocks, (key, value) => key === 'imageData' ? undefined : value)),
    markdown: result.markdown || '',
    ...(payload.source_format === 'pdf' ? { pdf_pages: pdfPages } : {}),
    ...(parseFormat === 'docx' ? { image_ocr: imageOcr } : {}),
    metadata: result.metadata || {},
    warnings: result.warnings || [],
  };
}
