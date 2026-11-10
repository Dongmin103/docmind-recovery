"""Package already built, verified CPU images; never export containers or volumes."""
import argparse
import gzip
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import zipfile

ROOT=Path(__file__).resolve().parents[1]
IMAGES=['docmind-openvino-api:2026.11.10-r1','docmind-openvino-embedding:2026.11.10-r1',
        'docmind-kordoc-parser:2026.10.19','docmind-preview:2026.10.19',
        'mysql:8.0.40','elasticsearch:8.11.3','valkey/valkey:8','pgsty/silo:RELEASE.2026-08-06T00-00-00Z']


def sha(p):
    h=hashlib.sha256()
    with p.open('rb') as f:
        while b:=f.read(4*1024*1024): h.update(b)
    return h.hexdigest()


def build(out):
    # Source snapshot must be clean in all included paths. Unrelated GPU work is excluded.
    paths=['app','deploy/openvino','deploy/windows/scripts','tools/windows','tools/avdc_embedding','tools/handoff',
           'tools/build_openvino_package.py']
    if subprocess.check_output(['git','status','--porcelain','--',*paths],cwd=ROOT).strip():
        raise ValueError('Commit included source paths before packaging')
    commit=subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip()
    out.mkdir(parents=True,exist_ok=False)
    bundle=out/'docmind-openvino'; bundle.mkdir()
    for name in ['install.py','compose.template.json','embedding.avdc.json','README.md','configure-worker.ps1']:
        shutil.copyfile(ROOT/'deploy/openvino'/name,bundle/name)
    for tree in ['tools/windows','tools/handoff','docs/engine-handoff']:
        files=subprocess.check_output(['git','ls-files',tree],cwd=ROOT,text=True).splitlines()
        for name in files:
            p=ROOT/name
            if p.suffix not in ('.py','.ps1','.cs','.json','.md') or p.name.startswith(('Test-','Measure-','test_')): continue
            target=bundle/name; target.parent.mkdir(parents=True,exist_ok=True); shutil.copyfile(p,target)
    legal=bundle/'licenses'; legal.mkdir()
    shutil.copyfile(ROOT/'app/LICENSE',legal/'DOCMIND-LICENSE')
    shutil.copyfile(ROOT/'app/THIRD_PARTY_NOTICES.md',legal/'THIRD_PARTY_NOTICES.md')
    images=[]
    for tag in IMAGES:
        r=json.loads(subprocess.check_output(['docker','image','inspect',tag]))[0]
        if r['Architecture']!='amd64' or r['Os']!='linux': raise ValueError('Linux amd64 required')
        if tag.startswith('docmind-openvino-') and r['Config']['Labels'].get('org.opencontainers.image.revision')!=commit:
            raise ValueError('Release image source revision mismatch')
        images.append({'tag':tag,'id':r['Id']})
    print('Exporting eight immutable images, without volumes',flush=True)
    p=subprocess.Popen(['docker','image','save',*IMAGES],stdout=subprocess.PIPE)
    with gzip.open(bundle/'images.tar.gz','wb',compresslevel=1) as f: shutil.copyfileobj(p.stdout,f,4*1024*1024)
    p.stdout.close()
    if p.wait(): raise RuntimeError('Image export failed')
    files=[{'path':p.relative_to(bundle).as_posix(),'bytes':p.stat().st_size,'sha256':sha(p)} for p in sorted(bundle.rglob('*')) if p.is_file()]
    (bundle/'manifest.json').write_text(json.dumps({'schema':'docmind-openvino-package-v1','source_commit':commit,
        'images':images,'image_archives':['images.tar.gz'],'files':files},indent=2)+'\n')
    archive=out/'docmind-openvino-2026.11.10-r1.zip'
    with zipfile.ZipFile(archive,'x',compression=zipfile.ZIP_STORED,allowZip64=True) as z:
        for p in sorted(bundle.rglob('*')):
            if p.is_file(): z.write(p,p.relative_to(out).as_posix())
    parts=[]
    with archive.open('rb') as f:
        index=1
        while b:=f.read(256*1024*1024):
            part=out/(archive.name+f'.part{index:03}')
            part.write_bytes(b)
            parts.append({'name':part.name,'bytes':len(b),'sha256':hashlib.sha256(b).hexdigest()}); index+=1
    download={'zip':archive.name,'bytes':archive.stat().st_size,'sha256':sha(archive),'parts':parts}
    (out/'download-manifest.json').write_text(json.dumps(download,indent=2)+'\n')
    shutil.copyfile(ROOT/'deploy/openvino/download.py',out/'download.py')
    print(json.dumps({'archive':str(archive),'bytes':download['bytes'],'parts':len(parts),'sha256':download['sha256']}),flush=True)


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--output',type=Path,required=True);build(p.parse_args().output.resolve())
