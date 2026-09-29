import { spawn } from 'node:child_process';
import { mkdtemp, mkdir, readFile, rm, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

const MAX_BYTES = Number(process.env.KORDOC_MAX_SOURCE_BYTES || 64 * 1024 * 1024);
const TIMEOUT_MS = Number(process.env.KORDOC_CONVERSION_TIMEOUT_MS || 180_000);

export async function convertOffice(source, sourceFormat, { workDir } = {}) {
  const targetFormat = sourceFormat === 'doc' ? 'docx' : sourceFormat === 'pptx' ? 'pdf' : null;
  if (!targetFormat) throw new Error('unsupported conversion format');
  const directory = workDir || await mkdtemp(join(tmpdir(), 'kordoc-convert-'));
  try {
    const input = join(directory, `source.${sourceFormat}`);
    const output = join(directory, 'output');
    const profile = join(directory, 'profile');
    await mkdir(output, { mode: 0o700 });
    await mkdir(profile, { mode: 0o700 });
    await writeFile(input, source, { mode: 0o600 });
    const args = [
      '--headless', '--nologo', '--nodefault', '--nofirststartwizard', '--nolockcheck', '--norestore',
      `-env:UserInstallation=file://${profile}`, '--convert-to',
      sourceFormat === 'doc' ? 'docx:Office Open XML Text' : 'pdf:impress_pdf_Export',
      '--outdir', output, input,
    ];
    await new Promise((resolve, reject) => {
      const child = spawn('/usr/bin/soffice', args, {
        stdio: 'ignore', env: { HOME: directory, LANG: 'C.UTF-8', LC_ALL: 'C.UTF-8', PATH: '/usr/bin:/bin', TMPDIR: directory },
      });
      const timer = setTimeout(() => child.kill('SIGKILL'), TIMEOUT_MS);
      child.once('error', error => { clearTimeout(timer); reject(error); });
      child.once('close', code => {
        clearTimeout(timer);
        if (code === 0) resolve();
        else reject(new Error('office conversion failed'));
      });
    });
    const converted = await readFile(join(output, `source.${targetFormat}`));
    if (!converted.length || converted.length > MAX_BYTES) throw new Error('converted source exceeds limit');
    if (targetFormat === 'docx' && !converted.subarray(0, 2).equals(Buffer.from('PK'))) {
      throw new Error('invalid DOCX conversion output');
    }
    if (targetFormat === 'pdf' && !converted.subarray(0, 5).equals(Buffer.from('%PDF-'))) {
      throw new Error('invalid PDF conversion output');
    }
    return converted;
  } finally {
    if (!workDir) await rm(directory, { recursive: true, force: true });
  }
}
