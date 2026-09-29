import { spawn } from 'node:child_process';
import { createServer as createHttpServer } from 'node:http';
import { fileURLToPath } from 'node:url';
import { VERSION } from 'kordoc';

const cli = fileURLToPath(new URL('./cli.mjs', import.meta.url));
const maxSourceBytes = Number(process.env.KORDOC_MAX_SOURCE_BYTES || 64 * 1024 * 1024);
const maxRequestBytes = Math.ceil(maxSourceBytes * 4 / 3) + 4096;
const maxResponseBytes = Math.max(maxRequestBytes * 4, 8 * 1024 * 1024);
const timeoutMs = Number(process.env.KORDOC_TIMEOUT_MS || 900_000);

function respond(res, status, payload) {
  if (res.writableEnded || res.destroyed) return;
  res.writeHead(status, { 'content-type': 'application/json; charset=utf-8' });
  res.end(JSON.stringify(payload));
}

export function createServer() {
  let busy = false;
  return createHttpServer((req, res) => {
    if (req.method === 'GET' && req.url === '/health') {
      respond(res, 200, { ready: true, version: VERSION, busy });
      return;
    }
    if (req.method !== 'POST' || req.url !== '/v1/parse') {
      respond(res, 404, { code: 'NOT_FOUND' });
      return;
    }
    if (busy) {
      respond(res, 503, { code: 'PARSER_BUSY' });
      return;
    }
    if (!req.headers['content-type']?.startsWith('application/json')) {
      respond(res, 415, { code: 'INVALID_CONTENT_TYPE' });
      return;
    }
    busy = true;
    const chunks = [];
    let size = 0;
    let rejected = false;
    let workerStarted = false;
    req.on('data', chunk => {
      if (rejected) return;
      size += chunk.length;
      if (size > maxRequestBytes) {
        rejected = true;
        busy = false;
        chunks.length = 0;
        respond(res, 413, { code: 'SOURCE_TOO_LARGE' });
      } else {
        chunks.push(chunk);
      }
    });
    req.on('aborted', () => {
      if (!workerStarted) busy = false;
    });
    req.on('end', () => {
      if (rejected || res.writableEnded || res.destroyed) {
        busy = false;
        return;
      }
      workerStarted = true;
      const worker = spawn(process.execPath, [cli], { stdio: ['pipe', 'pipe', 'ignore'] });
      const output = [];
      let outputSize = 0;
      let killedByLimit = false;
      const timer = setTimeout(() => {
        killedByLimit = true;
        worker.kill();
      }, timeoutMs);
      res.on('close', () => {
        if (!res.writableEnded) worker.kill();
      });
      worker.stdout.on('data', part => {
        outputSize += part.length;
        if (outputSize > maxResponseBytes) {
          killedByLimit = true;
          worker.kill();
        } else {
          output.push(part);
        }
      });
      worker.on('error', () => {
        clearTimeout(timer);
        busy = false;
        respond(res, 502, { code: 'PARSER_UNAVAILABLE' });
      });
      worker.on('close', code => {
        clearTimeout(timer);
        busy = false;
        if (killedByLimit) {
          respond(res, 504, { code: 'PARSER_LIMIT_EXCEEDED' });
          return;
        }
        if (code !== 0) {
          respond(res, 400, { code: 'PARSER_INVALID_INPUT' });
          return;
        }
        try {
          const body = JSON.parse(Buffer.concat(output).toString('utf8'));
          respond(res, 200, body);
        } catch {
          respond(res, 502, { code: 'PARSER_INVALID_OUTPUT' });
        }
      });
      worker.stdin.on('error', () => {});
      worker.stdin.end(Buffer.concat(chunks));
    });
  });
}

if (process.argv[1] && fileURLToPath(import.meta.url) === process.argv[1]) {
  createServer().listen(Number(process.env.KORDOC_PORT || 8095), process.env.KORDOC_HOST || '0.0.0.0');
}
