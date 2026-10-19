"""Create portable CPU/GPU DOCMIND bundles from validated local Docker images.

No running container, volume, environment dump or source document is exported.
Only named, prebuilt clean images and the explicitly selected runtime files are
copied. Run on the release machine, not on the destination server.
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import shutil
import subprocess
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
COMMON_IMAGES = ['docmind:2026.10.19', 'docmind-kordoc-parser:2026.10.19', 'docmind-preview:2026.10.19', 'mysql:8.0.40', 'elasticsearch:8.11.3', 'valkey/valkey:8', 'pgsty/silo:RELEASE.2026-08-06T00-00-00Z']


def digest(path):
    value = hashlib.sha256()
    with path.open('rb') as stream:
        while chunk := stream.read(4 * 1024 * 1024):
            value.update(chunk)
    return value.hexdigest()


def docker_prefix():
    override = os.environ.get('DOCMIND_RELEASE_DOCKER_COMMAND')
    if override:
        command = json.loads(override)
        if not isinstance(command, list) or not command or not all(isinstance(part, str) and part for part in command):
            raise ValueError('DOCMIND_RELEASE_DOCKER_COMMAND must be a JSON argument array')
        return command
    if shutil.which('docker'):
        return ['docker']
    if os.name == 'nt' and shutil.which('wsl.exe'):
        return ['wsl.exe', '-d', 'Ubuntu', '--exec', 'docker']
    raise RuntimeError('Docker CLI is unavailable')


def release_parts(zip_path, part_bytes=1536 * 1024 * 1024):
    """Describe bounded release assets without duplicating the large ZIP."""
    parts = []
    total = hashlib.sha256()
    with zip_path.open('rb') as stream:
        while True:
            count = 0
            value = hashlib.sha256()
            while count < part_bytes:
                chunk = stream.read(min(4 * 1024 * 1024, part_bytes - count))
                if not chunk:
                    break
                count += len(chunk)
                value.update(chunk)
                total.update(chunk)
            if not count:
                break
            parts.append({'name': f'{zip_path.name}.part{len(parts) + 1:03}', 'bytes': count, 'sha256': value.hexdigest()})
    manifest = {'zip': zip_path.name, 'bytes': zip_path.stat().st_size, 'sha256': total.hexdigest(), 'parts': parts}
    zip_path.with_suffix('.parts.json').write_text(json.dumps(manifest, indent=2) + '\n', encoding='utf-8')
    return manifest


def build(variant, output, model_dir, legal_dir):
    if subprocess.check_output(['git', 'status', '--porcelain'], cwd=ROOT, text=True).strip():
        raise ValueError('Commit source changes before creating a release package')
    command = docker_prefix()
    destination = output / ('docmind-windows-' + variant)
    if destination.exists():
        raise ValueError('Use a fresh output directory; existing packages are preserved')
    destination.mkdir(parents=True)
    images = COMMON_IMAGES + ['docmind-embedding:2026.10.19-' + variant, 'docmind-ocr:2026.10.19-' + variant]
    identities = []
    for name in images:
        record = json.loads(subprocess.check_output(command + ['image', 'inspect', name]))[0]
        if record['Architecture'] != 'amd64' or record['Os'] != 'linux':
            raise ValueError('Expected Linux amd64 image')
        identities.append({'tag': name, 'id': record['Id']})
    runtime = ROOT / 'deploy/windows'
    for source in runtime.iterdir():
        if source.is_file() and source.suffix in {'.ps1', '.yaml', '.json', '.md'}:
            shutil.copyfile(source, destination / source.name)
    shutil.copyfile(ROOT / 'deploy/windows/README.md', destination / 'README.md')
    tracked = subprocess.check_output(['git', 'ls-files', 'tools/windows'], cwd=ROOT, text=True).splitlines()
    for name in tracked:
        source = ROOT / name
        if source.suffix not in {'.ps1', '.cs', '.json'} or source.name.startswith(('Test-', 'Measure-')) or 'Recovery' in source.name or 'Probe' in source.name:
            continue
        target = destination / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
    ocr = json.loads((runtime / 'model-manifest.json').read_text())['ocr']
    for name, expected in [('surya-2.gguf', ocr['weights_sha256']), ('surya-2-mmproj.gguf', ocr['mmproj_sha256'])]:
        if digest(model_dir / name) != expected:
            raise ValueError('Source model differs from the validated image model')
    legal = destination / 'licenses'
    legal.mkdir()
    shutil.copyfile(ROOT / 'app/LICENSE', legal / 'DOCMIND-Apache-2.0.txt')
    shutil.copyfile(ROOT / 'app/THIRD_PARTY_NOTICES.md', legal / 'THIRD_PARTY_NOTICES.md')
    for source in legal_dir.iterdir():
        if source.is_file():
            shutil.copyfile(source, legal / source.name)
    archive = destination / 'images.tar.gz'
    print(json.dumps({'variant': variant, 'phase': 'exporting_images', 'images': len(images)}), flush=True)
    export = subprocess.Popen(command + ['image', 'save', *images], stdout=subprocess.PIPE)
    try:
        with gzip.open(archive, 'wb', compresslevel=1) as target:
            shutil.copyfileobj(export.stdout, target, length=4 * 1024 * 1024)
        if export.wait() != 0:
            raise RuntimeError('Docker image export failed; no release ZIP was created')
    finally:
        if export.poll() is None:
            export.terminate()
            export.wait()
        export.stdout.close()
    files = [{'path': p.relative_to(destination).as_posix(), 'bytes': p.stat().st_size, 'sha256': digest(p)} for p in sorted(destination.rglob('*')) if p.is_file()]
    manifest = {'schema': 'docmind-runtime-v1', 'variant': variant, 'source_commit': subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip(), 'images': identities, 'image_archives': ['images.tar.gz'], 'files': files}
    (destination / 'runtime-manifest.json').write_text(json.dumps(manifest, indent=2) + '\n', encoding='utf-8')
    zip_path = output / ('docmind-windows-' + variant + '.zip')
    with zipfile.ZipFile(zip_path, 'x', compression=zipfile.ZIP_STORED, allowZip64=True) as result:
        for path in sorted(destination.rglob('*')):
            if path.is_file():
                result.write(path, arcname='docmind-' + variant + '/' + path.relative_to(destination).as_posix())
    parts = release_parts(zip_path)
    shutil.copyfile(runtime / 'Join-DocMindPackage.ps1', output / 'Join-DocMindPackage.ps1')
    receipt = {'variant': variant, 'zip': str(zip_path.resolve()), 'bytes': zip_path.stat().st_size, 'sha256': parts['sha256'], 'images': len(identities), 'files': len(files), 'release_parts': len(parts['parts'])}
    zip_path.with_suffix('.sha256').write_text(receipt['sha256'] + '  ' + zip_path.name + '\n', encoding='ascii')
    print(json.dumps(receipt), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--variant', choices=['cpu', 'gpu', 'both'], default='both')
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--surya-models', type=Path, required=True)
    parser.add_argument('--licenses', type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    for variant in (['cpu', 'gpu'] if args.variant == 'both' else [args.variant]):
        build(variant, args.output.resolve(), args.surya_models.resolve(), args.licenses.resolve())


if __name__ == '__main__':
    main()
