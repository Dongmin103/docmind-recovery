"""Offline Linux/amd64 installer. Creates a new Compose project and empty volumes."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess

ROOT = Path(__file__).resolve().parent


def run(*args, **kw):
    return subprocess.run(list(args), check=True, **kw)


def verify():
    m = json.loads((ROOT / 'manifest.json').read_text())
    for row in m['files']:
        p = (ROOT / row['path']).resolve()
        if not p.is_relative_to(ROOT) or not p.is_file():
            raise ValueError('Missing or unsafe package file')
        h = hashlib.sha256()
        with p.open('rb') as f:
            while b := f.read(4 * 1024 * 1024):
                h.update(b)
        if p.stat().st_size != row['bytes'] or h.hexdigest() != row['sha256']:
            raise ValueError('Package hash mismatch: ' + row['path'])
    return m


def compose(*args):
    return run('docker', 'compose', '-f', str(ROOT / '.local/compose.json'), *args)


def initialize(args):
    manifest = verify()
    if not re.fullmatch(r'docmind-ov-[a-z0-9-]{1,30}', args.project):
        raise ValueError('Project must begin docmind-ov- (separate from existing installations)')
    if (ROOT / '.local').exists():
        raise ValueError('Existing installation preserved; use start/check instead')
    if args.api_port == args.web_port or any(not 1024 <= p <= 65535 for p in (args.api_port,args.web_port)):
        raise ValueError('Choose two distinct unprivileged ports')
    # Docker executes on the local Linux host; CPU numbering must belong to it.
    if os.name != 'posix':
        raise ValueError('Run inside the target Ubuntu server, not Windows CMD')
    info = json.loads(subprocess.check_output(['docker','info','--format','{{json .}}']))
    cpus = sorted(os.sched_getaffinity(0))
    if info['OSType'] != 'linux' or info['Architecture'] not in ('x86_64','amd64'):
        raise ValueError('Linux amd64 Docker is required')
    if info['NCPU'] != os.cpu_count():
        raise ValueError('Run installer on the Docker host; remote Docker is unsupported')
    required_ram = 60 if args.profile == 'avdc' else 7
    if info['MemTotal'] < required_ram * 1024**3:
        raise ValueError(f'{args.profile} requires Docker memory >= {required_ram} GiB')
    if len(cpus) < (96 if args.profile == 'avdc' else 6):
        raise ValueError('Insufficient logical processors for selected profile')
    for kind in ('container','volume','network'):
        if subprocess.check_output(['docker',kind,'ls','-q','--filter','label=com.docker.compose.project='+args.project]).strip():
            raise ValueError('Project already has resources; existing data preserved')
    if shutil.disk_usage(ROOT).free < 30 * 1024**3:
        raise ValueError('At least 30 GiB free required for image loading and empty data')
    for archive in manifest['image_archives']:
        run('docker','load','-i',str(ROOT/archive))
    for image in manifest['images']:
        actual = subprocess.check_output(['docker','image','inspect',image['tag'],'--format','{{.Id}}'],text=True).strip()
        if actual != image['id']:
            raise ValueError('Loaded image ID mismatch')
    old_umask = os.umask(0o077)
    try:
        local = ROOT/'.local'; local.mkdir()
        keydir = local/'secrets'; keydir.mkdir()
        for key in ('session-key','worker-key','embedding-key'):
            (keydir/key).write_text(secrets.token_hex(32))
        (local/'installation').mkdir()
        (local/'installation/sources.json').write_text('[]')
        c = json.loads((ROOT/'compose.template.json').read_text())
        c['name'] = args.project
        s = c['services']; env = s['docmind']['environment']
        for name in ('MYSQL_PASSWORD','ELASTIC_PASSWORD','REDIS_PASSWORD','MINIO_PASSWORD'):
            env[name] = secrets.token_hex(32)
        s['mysql']['environment']['MYSQL_ROOT_PASSWORD'] = env['MYSQL_PASSWORD']
        s['es']['environment']['ELASTIC_PASSWORD'] = env['ELASTIC_PASSWORD']
        s['redis']['command'][2] = env['REDIS_PASSWORD']
        s['redis']['healthcheck']['test'] = ['CMD','valkey-cli','-a',env['REDIS_PASSWORD'],'ping']
        s['minio']['environment']['MINIO_ROOT_PASSWORD'] = env['MINIO_PASSWORD']
        s['docmind']['ports'] = [f'127.0.0.1:{args.web_port}:80',f'127.0.0.1:{args.api_port}:9380']
        s['docmind']['volumes'] += [str(keydir)+':/run/secrets:ro',str(local/'installation')+':/run/installation:ro']
        groups = [cpus[:4]]
        other = cpus[4:]
        workers, slots, requests, parser_workers, parser_cpu = 1, 1, 2, 1, 2
        if args.profile == 'avdc':
            groups = json.loads((ROOT/'embedding.avdc.json').read_text())['cpu_groups']
            if not set(range(96)) <= set(cpus):
                raise ValueError('AVDC affinity needs CPU IDs 0..95')
            other = list(range(48,56))
            workers, slots, requests, parser_workers, parser_cpu = 22,12,24,2,4
        embedding = json.loads((ROOT/'embedding.avdc.json').read_text())
        embedding['cpu_groups'] = groups
        (local/'embedding.json').write_text(json.dumps(embedding,indent=2))
        for name, service in s.items():
            service['cpuset'] = ','.join(map(str, [n for g in groups for n in g] if name=='embedding' else other))
        s['embedding']['cpus'] = workers*4
        s['embedding']['mem_limit'] = '52g' if workers==22 else '3g'
        s['embedding']['volumes'] = [str(local/'embedding.json')+':/opt/docmind/config.json:ro',str(keydir/'embedding-key')+':/run/secrets/embedding-key:ro']
        s['parser']['cpus'] = parser_cpu
        s['parser']['environment']['KORDOC_WORKERS'] = str(parser_workers)
        env.update(DOCMIND_INGESTION_DOCUMENT_CONCURRENCY=str(slots),KORDOC_CLIENT_CONCURRENCY=str(parser_workers),MAX_CONCURRENT_EMBEDDING_REQUESTS=str(requests))
        (local/'compose.json').write_text(json.dumps(c,indent=2))
        (local/'installation.json').write_text(json.dumps({'profile':args.profile,'project':args.project,'source_commit':manifest['source_commit'],'web_port':args.web_port,'api_port':args.api_port},indent=2))
    finally:
        os.umask(old_umask)
    print('Fresh configuration generated. Secrets remain in .local/.')


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('action',choices=['verify','install','start','status','check','stop'])
    p.add_argument('--project',default='docmind-ov-main')
    p.add_argument('--profile',choices=['compact','avdc'],default='compact')
    p.add_argument('--web-port',type=int,default=18081)
    p.add_argument('--api-port',type=int,default=19391)
    a=p.parse_args()
    if a.action=='verify':
        verify(); print('All package file hashes match.'); return
    if a.action=='install': initialize(a)
    if a.action in ('install','start'):
        compose('up','-d','--wait','--wait-timeout','900')
        compose('exec','-T','docmind','/ragflow/.venv/bin/python','/opt/docmind/bootstrap.py')
        print('Installation ready. Run: python3 install.py check')
    elif a.action=='check':
        compose('exec','-T','docmind','/ragflow/.venv/bin/python','/opt/docmind/smoke.py')
    elif a.action=='status': compose('ps')
    elif a.action=='stop': compose('stop')


if __name__=='__main__': main()
