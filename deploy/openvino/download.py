"""Download authenticated GitLab package parts, verify and reconstruct the ZIP."""
import argparse
import getpass
import hashlib
import json
from pathlib import Path
import shutil
import urllib.parse
import urllib.request


def digest(path):
    h=hashlib.sha256()
    with path.open('rb') as f:
        while b:=f.read(4*1024*1024): h.update(b)
    return h.hexdigest()


class SameOriginRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        old,new=urllib.parse.urlsplit(req.full_url),urllib.parse.urlsplit(newurl)
        if (old.scheme,old.netloc)!=(new.scheme,new.netloc):
            raise ValueError('Cross-origin redirect blocked to protect the GitLab token')
        return super().redirect_request(req,fp,code,msg,headers,newurl)


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--base-url',required=True,help='Generic package version URL from release notes')
    p.add_argument('--output',type=Path,default=Path('docmind-download'))
    a=p.parse_args()
    # Token is entered without echo and is never placed in URL, command line or file.
    token=getpass.getpass('GitLab read_api token: ')
    a.output.mkdir(exist_ok=True)
    opener=urllib.request.build_opener(SameOriginRedirect())
    def get(name):
        return opener.open(urllib.request.Request(a.base_url.rstrip('/')+'/'+name,headers={'PRIVATE-TOKEN':token}),timeout=120)
    with get('download-manifest.json') as f: manifest=json.load(f)
    target=a.output/manifest['zip']
    if target.name!=manifest['zip']: raise ValueError('Unsafe output')
    if target.exists():
        if target.stat().st_size==manifest['bytes'] and digest(target)==manifest['sha256']:
            print('Existing ZIP verified: '+str(target));return
        raise ValueError('Existing ZIP differs; preserved without overwrite')
    cache=a.output/(manifest['zip']+'.parts');cache.mkdir(exist_ok=True)
    for part in manifest['parts']:
        if Path(part['name']).name!=part['name']: raise ValueError('Unsafe part name')
        ready=cache/part['name']
        if ready.exists():
            if ready.stat().st_size==part['bytes'] and digest(ready)==part['sha256']:
                print(part['name']+' cached and verified',flush=True);continue
            raise ValueError('Existing part differs; preserved without overwrite')
        incomplete=ready.with_suffix(ready.suffix+'.partial')
        ph=hashlib.sha256();n=0
        with get(part['name']) as src, incomplete.open('wb') as dest:
            while b:=src.read(4*1024*1024):
                dest.write(b);ph.update(b);n+=len(b)
        if n!=part['bytes'] or ph.hexdigest()!=part['sha256']: raise ValueError('Part integrity failure')
        incomplete.rename(ready)
        print(part['name']+' downloaded and verified',flush=True)
    partial=target.with_suffix(target.suffix+'.partial')
    with partial.open('wb') as dest:
        for part in manifest['parts']:
            with (cache/part['name']).open('rb') as src: shutil.copyfileobj(src,dest,4*1024*1024)
    if partial.stat().st_size!=manifest['bytes'] or digest(partial)!=manifest['sha256']: raise ValueError('ZIP integrity failure')
    partial.rename(target)
    print('Verified ZIP: '+str(target))


if __name__=='__main__': main()
