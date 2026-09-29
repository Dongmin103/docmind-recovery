import { createInterface } from 'node:readline';
import { parseDocument } from './parse.mjs';

const maxInputBytes = Math.ceil(Number(process.env.KORDOC_MAX_SOURCE_BYTES || 64 * 1024 * 1024) * 4 / 3) + 4096;

async function parseLine(line) {
  let jobId;
  try {
    if (Buffer.byteLength(line) > maxInputBytes) throw new Error('input too large');
    const envelope = JSON.parse(line);
    jobId = envelope.job_id;
    if (typeof jobId !== 'string' || typeof envelope.work_dir !== 'string') {
      throw new Error('invalid worker envelope');
    }
    const body = await parseDocument(envelope.payload, { workDir: envelope.work_dir });
    return { job_id: jobId, status: 200, body };
  } catch (error) {
    if (error.code === 'PARSER_PAGE_LIMIT_EXCEEDED' && Number.isSafeInteger(error.page_count) && error.page_count > 0) {
      return { job_id: jobId, status: 400, body: { code: error.code, page_count: error.page_count } };
    }
    return { job_id: jobId, status: 400, body: { code: 'PARSER_INVALID_INPUT' } };
  }
}

for await (const line of createInterface({ input: process.stdin, crlfDelay: Infinity })) {
  const result = await parseLine(line);
  process.stdout.write(`${JSON.stringify(result)}\n`);
}
