import { parseDocument } from './parse.mjs';

const maxInputBytes = Math.ceil(Number(process.env.KORDOC_MAX_SOURCE_BYTES || 64 * 1024 * 1024) * 4 / 3) + 4096;

async function main() {
  const chunks = [];
  let length = 0;
  for await (const chunk of process.stdin) {
    length += chunk.length;
    if (length > maxInputBytes) throw new Error('input too large');
    chunks.push(chunk);
  }
  const payload = JSON.parse(Buffer.concat(chunks).toString('utf8'));
  const result = await parseDocument(payload);
  process.stdout.write(JSON.stringify(result));
}

main().catch(error => {
  if (error.code === 'PARSER_PAGE_LIMIT_EXCEEDED' && Number.isSafeInteger(error.page_count)) {
    process.stdout.write(JSON.stringify({ code: error.code, page_count: error.page_count }));
    return;
  }
  process.stderr.write('Kordoc parse failed\n');
  process.exitCode = 1;
});
