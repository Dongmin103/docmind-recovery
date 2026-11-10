"""Synthetic engine acceptance, not a test of the proprietary Windows decryptor."""
import hashlib
import hmac
import json
import os
from pathlib import Path
import secrets
import sys
import tempfile
import time
import urllib.request
import uuid

sys.path.insert(0, '/opt/docmind/handoff')
from engine import register_source, inspect_job
from make_sample import create


def request(method, suffix, data, extra=None):
    path = '/api/v1/cloud-sync/host-worker/' + suffix
    body = data if isinstance(data, bytes) else json.dumps(data,separators=(',',':')).encode()
    key = bytes.fromhex(Path('/run/secrets/worker-key').read_text().strip())
    stamp, nonce, digest = str(int(time.time())), secrets.token_hex(16), hashlib.sha256(body).hexdigest()
    canonical = lambda t,n,d: f'{method}\n{path}\n{t}\n{n}\n{d}'.encode()
    headers = {'Content-Type':'application/octet-stream' if isinstance(data,bytes) else 'application/json',
        'X-DocMind-Key-Id':'docmind-runtime-host','X-DocMind-Timestamp':stamp,
        'X-DocMind-Nonce':nonce,'X-DocMind-Content-SHA256':digest,
        'X-DocMind-Signature':hmac.new(key,canonical(stamp,nonce,digest),hashlib.sha256).hexdigest()}
    headers.update(extra or {})
    with urllib.request.urlopen(urllib.request.Request('http://127.0.0.1:9380'+path,body,headers,method=method),timeout=900) as r:
        out = r.read(); rh = r.headers
    rd = hashlib.sha256(out).hexdigest()
    rs = hmac.new(key,canonical(rh['X-DocMind-Timestamp'],rh['X-DocMind-Nonce'],rd),hashlib.sha256).hexdigest()
    assert rh['X-DocMind-Key-Id']=='docmind-runtime-host'
    assert abs(int(rh['X-DocMind-Timestamp'])-int(time.time()))<=300
    assert hmac.compare_digest(rd,rh['X-DocMind-Content-SHA256'])
    assert hmac.compare_digest(rs,rh['X-DocMind-Signature'])
    return json.loads(out)


def main():
    assert os.environ.get('DOCMIND_PACKAGE_INSTALLATION')=='openvino-v1', 'Package installation required'
    assert os.environ['PARSER_PLATFORM_KORDOC_CHUNKER_REVISION']=='docmind-kordoc-chunker-v3'
    os.environ['RAGFLOW_SECRET_KEY']=Path('/run/secrets/session-key').read_text().strip()
    from common import settings
    settings.init_settings()
    from api.db import db_models as m
    from api.apps.services import docmind_ingestion_service as ingestion
    from api.apps.services.docmind_shared_workspace_service import resolve_user
    from api.apps.services.docmind_bootstrap_service import bootstrap_shared_workspace
    user=resolve_user(); workspace=bootstrap_shared_workspace(user.id)
    project=m.DocmindProject.get_by_id(workspace['project_id'])
    source='package-check-'+uuid.uuid4().hex[:12]
    registered=register_source(m,project.id,source,'Package synthetic acceptance',apply=True)
    results=[]
    with tempfile.TemporaryDirectory(prefix='package-smoke-') as tmp:
        tmp=Path(tmp)
        docx=tmp/'synthetic.docx'; create(docx)
        import fitz
        pdf=fitz.open(); page=pdf.new_page()
        page.insert_text((60,80),'Installation acceptance',fontsize=18)
        page.insert_textbox(fitz.Rect(60,110,500,500), ('DOCMIND-PACKAGE-PDF. This synthetic document verifies heading and paragraph chunking. The engine parses a document, creates chunks, computes OpenVINO vectors and activates the search index. '*7),fontsize=11)
        pdf.save(tmp/'synthetic.pdf'); pdf.close()
        for fixture in (docx,tmp/'synthetic.pdf'):
            started=time.monotonic(); data=fixture.read_bytes(); digest=hashlib.sha256(data).hexdigest()
            document_id=uuid.uuid4().hex
            m.Document.create(id=document_id,kb_id=project.dataset_id,parser_id='naive',source_type='cloud-source',
                type='doc' if fixture.suffix=='.docx' else 'pdf',suffix=fixture.suffix[1:],created_by=user.id,name=fixture.name,location=None)
            ingestion.register_source_document_mapping(user.id,project_id=project.id,source_id=source,
                document_id=document_id,folder_id=registered['folder_id'],relative_path=fixture.name)
            observation={'source_id':source,'document_id':document_id,'relative_path':fixture.name,
                'ciphertext_sha256':digest,'size':len(data),'mtime_ns':fixture.stat().st_mtime_ns}
            # This explicitly synthetic harness substitutes plaintext at the worker boundary.
            # It does not assert that encryption/decryption was tested.
            request('POST','observations',observation)
            observed=request('POST','observations',observation)
            assert observed.get('job_id'), observed
            worker='package-smoke-'+uuid.uuid4().hex[:12]
            lease=request('POST','claim',{'worker_id':worker,'protocol_version':1,'lease_seconds':1800,'claim_source_id':source})['job']
            assert lease and lease['job_id']==observed['job_id']
            identity={'worker_id':worker,'version_id':lease['version_id'],'fencing_token':lease['fencing_token']}
            assert request('POST','jobs/'+lease['job_id']+'/validate',identity)['valid']
            ack=request('PUT','jobs/'+lease['job_id']+'/artifact',data,{
                'X-DocMind-Worker-Id':worker,'X-DocMind-Version-Id':lease['version_id'],
                'X-DocMind-Fencing-Token':str(lease['fencing_token']),
                'X-DocMind-Plaintext-SHA256':digest,'X-DocMind-Plaintext-Size':str(len(data))})
            assert ack.get('accepted') and ack['job_id']==lease['job_id']
            fixture.unlink(); assert not fixture.exists()
            ack=request('POST','jobs/'+lease['job_id']+'/status',dict(identity,status='CLEANED',error_code=None))
            assert ack.get('accepted') and ack['cleanup_required'] is False
            check=inspect_job(m,settings,lease['job_id'],digest)
            assert check['passed'], check
            from api.apps.services.docmind_ingestion_runtime import _index_name
            j=m.DocmindIngestionJob.get_by_id(lease['job_id'])
            # Query only this synthetic job; no external reranker or model call.
            import requests
            es=settings.ES
            response=requests.get(es['hosts'].rstrip('/')+'/'+_index_name(project.tenant_id)+'/_search',
                auth=(es['username'],es['password']),json={'query':{'bool':{'filter':[{'term':{'doc_id':document_id}},{'term':{'chunk_set_id':j.chunk_set_id}}]}},'size':100},timeout=15)
            response.raise_for_status(); hits=response.json()['hits']['hits']
            assert len(hits)==check['indexed_chunks']
            assert all(len(x['_source']['q_768_vec'])==768 for x in hits)
            marker='DOCMIND-HANDOFF-20261110' if fixture.suffix=='.docx' else 'DOCMIND-PACKAGE-PDF'
            assert any(marker in x['_source'].get('content_with_weight','') for x in hits)
            results.append({'format':fixture.suffix[1:],'seconds':round(time.monotonic()-started,3),
                'chunks':check['indexed_chunks'],'checks':check['checks'],'vector_dimensions':768,'marker_preserved':True})
    assert not tmp.exists()
    print('PACKAGE_ACCEPTANCE='+json.dumps({'passed':True,'documents':results,
        'temporary_fixture_cleanup':True,'decryption_tested':False,'reranker_tested':False,
        'chunker_revision':os.environ['PARSER_PLATFORM_KORDOC_CHUNKER_REVISION']},ensure_ascii=False))


if __name__=='__main__': main()
