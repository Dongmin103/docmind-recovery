"""Download authenticated GitLab package parts, verify and reconstruct the ZIP."""
import argparse
import getpass
import hashlib
import json
from pathlib import Path
import urllib.request


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--base-url',required=True,help='Generic package version URL from release notes')
    p.add_argument('--output',type=Path,default=Path('docmind-download'))
    a=p.parse_args()
    # Token is entered without echo and is never placed in URL, command line or file.
    token=getpass.getpass('GitLab read_api token: ')
    a.output.mkdir(exist_ok=True)
    def get(name):
        return urllib.request.urlopen(urllib.request.Request(a.base_url.rstrip('/')+'/'+name,headers={'PRIVATE-TOKEN':token}),timeout=120)
    with get('download-manifest.json') as f: manifest=json.load(f)
    target=a.output/manifest['zip']
    if target.name!=manifest['zip'] or target.exists(): raise ValueError('Unsafe or existing output')
    partial=target.with_suffix(target.suffix+'.partial')
    h=hashlib.sha256();total=0
    with partial.open('xb') as dest:
        for part in manifest['parts']:
            if Path(part['name']).name!=part['name']: raise ValueError('Unsafe part name')
            ph=hashlib.sha256();n=0
            with get(part['name']) as src:
                while b:=src.read(4*1024*1024):
                    dest.write(b);h.update(b);ph.update(b);n+=len(b);total+=len(b)
            if n!=part['bytes'] or ph.hexdigest()!=part['sha256']: raise ValueError('Part integrity failure')
            print(part['name']+' verified',flush=True)
    if total!=manifest['bytes'] or h.hexdigest()!=manifest['sha256']: raise ValueError('ZIP integrity failure')
    partial.rename(target)
    print('Verified ZIP: '+str(target))


if __name__=='__main__': main()
