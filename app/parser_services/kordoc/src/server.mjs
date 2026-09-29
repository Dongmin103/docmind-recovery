import { spawn } from 'node:child_process';
import { randomUUID } from 'node:crypto';
import { existsSync } from 'node:fs';
import { mkdir, mkdtemp, rm } from 'node:fs/promises';
import { createServer as createHttpServer } from 'node:http';
import { homedir, tmpdir } from 'node:os';
import { join, resolve, sep } from 'node:path';
import { fileURLToPath } from 'node:url';
import { VERSION } from 'kordoc';

const workerScript = fileURLToPath(new URL('./worker.mjs', import.meta.url));
const maxSourceBytes = Number(process.env.KORDOC_MAX_SOURCE_BYTES || 64 * 1024 * 1024);
const maxRequestBytes = Math.ceil(maxSourceBytes * 4 / 3) + 4096;
const maxResponseBytes = Math.max(maxRequestBytes * 4, 8 * 1024 * 1024);
const timeoutMs = Number(process.env.KORDOC_TIMEOUT_MS || 900_000);
const ocrModelDir = join(process.env.KORDOC_MODEL_CACHE || join(homedir(), '.cache', 'kordoc', 'models'), 'ppocr');
const defaultJobRoot = join(tmpdir(), 'docmind-kordoc-jobs');

function ocrModelsPresent() {
  return ['det.onnx', 'rec_korean.onnx', 'rec_korean.yml'].every(name => existsSync(join(ocrModelDir, name)));
}

function respond(res, status, payload) {
  if (res.writableEnded || res.destroyed) return;
  res.writeHead(status, { 'content-type': 'application/json; charset=utf-8' });
  res.end(JSON.stringify(payload));
}

function defaultSpawnWorker() {
  const detached = process.platform === 'linux';
  const child = spawn(process.execPath, [workerScript], { stdio: ['pipe', 'pipe', 'ignore'], detached });
  child.kordocDetached = detached;
  return child;
}

export function createServer({ spawnWorker = defaultSpawnWorker, timeoutMs: jobTimeoutMs = timeoutMs,
  jobRoot = defaultJobRoot } = {}) {
  const ownedRoot = resolve(jobRoot);
  let active = null;
  let worker = null;
  let cleanupFailed = false;

  async function removeWorkDir(directory) {
    if (!directory || !resolve(directory).startsWith(`${ownedRoot}${sep}`)) {
      throw new Error('work directory is outside the owned root');
    }
    await rm(directory, { recursive: true, force: true });
  }

  async function terminateWorker(info) {
    if (!info) return;
    if (worker === info) worker = null;
    const { child } = info;
    try {
      if (process.platform === 'linux' && child.kordocDetached && child.pid) {
        process.kill(-child.pid, 'SIGKILL');
      } else {
        child.kill('SIGKILL');
      }
    } catch (error) {
      if (error.code !== 'ESRCH') throw error;
    }
    await info.closed;
  }

  async function finishJob(job, status, body, { discard = false } = {}) {
    if (active !== job || job.state === 'cleaning') return;
    job.state = 'cleaning';
    clearTimeout(job.timer);
    try {
      if (discard) await terminateWorker(job.worker);
      if (job.workDirPromise) await removeWorkDir(await job.workDirPromise);
      active = null;
      respond(job.res, status, body);
    } catch {
      cleanupFailed = true;
      respond(job.res, 502, { code: 'PARSER_CLEANUP_FAILED' });
    }
  }

  function getWorker() {
    if (worker) return worker;
    const child = spawnWorker();
    const info = { child, closed: new Promise(resolveClose => child.once('close', resolveClose)) };
    worker = info;
    child.stdout.on('data', part => {
      if (worker !== info) return;
      const job = active;
      if (!job || job.state !== 'parsing' || job.worker !== info) {
        void terminateWorker(info);
        return;
      }
      job.outputSize += part.length;
      if (job.outputSize > maxResponseBytes) {
        void finishJob(job, 504, { code: 'PARSER_LIMIT_EXCEEDED' }, { discard: true });
        return;
      }
      const end = part.indexOf(10);
      if (end < 0) {
        job.output.push(part);
        return;
      }
      if (end !== part.length - 1) {
        void finishJob(job, 502, { code: 'PARSER_INVALID_OUTPUT' }, { discard: true });
        return;
      }
      job.output.push(part.subarray(0, end));
      try {
        const message = JSON.parse(Buffer.concat(job.output).toString('utf8'));
        if (message.job_id !== job.id) {
          void finishJob(job, 502, { code: 'PARSER_INVALID_OUTPUT' }, { discard: true });
        } else if (message.status === 200 && message.body?.schema_version === 'docmind-kordoc-v2') {
          void finishJob(job, 200, message.body);
        } else if (message.status === 400 && message.body?.code === 'PARSER_PAGE_LIMIT_EXCEEDED' &&
          Number.isSafeInteger(message.body.page_count) && message.body.page_count > 0) {
          void finishJob(job, 400, { code: message.body.code, page_count: message.body.page_count }, { discard: true });
        } else if (message.status === 400 && message.body?.code === 'PARSER_INVALID_INPUT') {
          void finishJob(job, 400, { code: 'PARSER_INVALID_INPUT' }, { discard: true });
        } else {
          void finishJob(job, 502, { code: 'PARSER_INVALID_OUTPUT' }, { discard: true });
        }
      } catch {
        void finishJob(job, 502, { code: 'PARSER_INVALID_OUTPUT' }, { discard: true });
      }
    });
    child.on('error', () => {
      if (active?.worker === info) void finishJob(active, 502, { code: 'PARSER_UNAVAILABLE' }, { discard: true });
      else void terminateWorker(info);
    });
    child.on('close', () => {
      if (worker === info) worker = null;
      if (active?.worker === info) void finishJob(active, 502, { code: 'PARSER_UNAVAILABLE' }, { discard: true });
    });
    child.stdin.on('error', () => {});
    return info;
  }

  async function startJob(job, chunks) {
    job.state = 'starting';
    let payload;
    try {
      payload = JSON.parse(Buffer.concat(chunks).toString('utf8'));
    } catch {
      void finishJob(job, 400, { code: 'PARSER_INVALID_INPUT' });
      return;
    }
    job.workDirPromise = (async () => {
      await mkdir(ownedRoot, { recursive: true, mode: 0o700 });
      return mkdtemp(join(ownedRoot, 'job-'));
    })();
    try {
      const workDir = await job.workDirPromise;
      if (active !== job || job.state !== 'starting') return;
      const info = getWorker();
      job.worker = info;
      job.state = 'parsing';
      job.output = [];
      job.outputSize = 0;
      job.timer = setTimeout(() => {
        if (active === job) void finishJob(job, 504, { code: 'PARSER_LIMIT_EXCEEDED' }, { discard: true });
      }, jobTimeoutMs);
      info.child.stdin.write(JSON.stringify({
        job_id: job.id, work_dir: workDir, payload,
      }) + '\n');
    } catch {
      void finishJob(job, 502, { code: 'PARSER_UNAVAILABLE' }, { discard: true });
    }
  }

  const server = createHttpServer((req, res) => {
    if (req.method === 'GET' && req.url === '/health') {
      const ready = process.env.KORDOC_REQUIRE_OCR !== '1' || ocrModelsPresent();
      respond(res, ready ? 200 : 503, { ready, version: VERSION, busy: Boolean(active || cleanupFailed),
        ocr_models_ready: ocrModelsPresent() });
      return;
    }
    if (req.method !== 'POST' || req.url !== '/v1/parse') {
      respond(res, 404, { code: 'NOT_FOUND' });
      return;
    }
    if (active || cleanupFailed) {
      respond(res, 503, { code: 'PARSER_BUSY' });
      return;
    }
    if (!req.headers['content-type']?.startsWith('application/json')) {
      respond(res, 415, { code: 'INVALID_CONTENT_TYPE' });
      return;
    }
    const job = { id: randomUUID(), req, res, state: 'upload', timer: null, worker: null,
      workDirPromise: null };
    active = job;
    const chunks = [];
    let size = 0;
    req.on('data', chunk => {
      if (active !== job || job.state !== 'upload') return;
      size += chunk.length;
      if (size > maxRequestBytes) {
        active = null;
        job.state = 'rejected';
        chunks.length = 0;
        respond(res, 413, { code: 'SOURCE_TOO_LARGE' });
      } else {
        chunks.push(chunk);
      }
    });
    req.on('aborted', () => {
      if (active !== job) return;
      if (job.state === 'upload') active = null;
      else void finishJob(job, 502, { code: 'PARSER_UNAVAILABLE' }, { discard: true });
    });
    req.on('end', () => {
      if (active !== job || job.state !== 'upload') return;
      if (res.writableEnded || res.destroyed) {
        active = null;
        return;
      }
      void startJob(job, chunks);
    });
    res.on('close', () => {
      if (res.writableEnded || active !== job) return;
      if (job.state === 'upload') active = null;
      else void finishJob(job, 502, { code: 'PARSER_UNAVAILABLE' }, { discard: true });
    });
  });
  server.on('close', () => { void terminateWorker(worker); });
  return server;
}

if (process.argv[1] && fileURLToPath(import.meta.url) === process.argv[1]) {
  createServer().listen(Number(process.env.KORDOC_PORT || 8095), process.env.KORDOC_HOST || '0.0.0.0');
}
