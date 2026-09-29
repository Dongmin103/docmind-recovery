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

main().catch(() => {
  process.stderr.write('Kordoc pilot parse failed\n');
  process.exitCode = 1;
});
