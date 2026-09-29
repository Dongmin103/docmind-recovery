import { createHash } from 'node:crypto';
import { VERSION, parse } from 'kordoc';

const MAX_SOURCE_BYTES = Number(process.env.KORDOC_MAX_SOURCE_BYTES || 64 * 1024 * 1024);
const PILOT_FORMATS = new Set(['hwp', 'hwpx', 'docx', 'pdf', 'xlsx']);

export async function parseDocument(payload) {
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
  const result = await parse(source, { images: false, ocr: false });
  if (!result.success) {
    throw new Error(`parse failed: ${result.code || 'unknown'}`);
  }
  if (result.fileType !== payload.source_format) {
    throw new Error('source format mismatch');
  }
  if (!result.blocks.length) {
    throw new Error('empty parse result');
  }
  return {
    schema_version: 'docmind-kordoc-pilot-v1',
    parser_name: 'kordoc',
    parser_version: VERSION,
    source_format: result.fileType,
    source_hash: hash,
    blocks: result.blocks,
    metadata: result.metadata || {},
    warnings: result.warnings || [],
  };
}
