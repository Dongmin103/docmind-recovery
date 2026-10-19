"""Authenticated GTE endpoint with an explicit CPU or CUDA deployment device."""
import argparse
import gc
import hmac
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

MODEL='gte-multilingual-base'
class RequestError(Exception):
    def __init__(self,message,status=400):super().__init__(message);self.status=status

class EmbeddingService:
    def __init__(self,engine,key):
        if not key:raise ValueError('A nonempty API key is required')
        self.engine=engine;self.key=key;self.lock=threading.Lock()
    def embed(self,payload,authorization):
        if not hmac.compare_digest(authorization or '', 'Bearer '+self.key):raise RequestError('Unauthorized',401)
        if not isinstance(payload,dict):raise RequestError('Expected JSON object')
        if payload.get('model',MODEL)!=MODEL:raise RequestError('Unknown model')
        if payload.get('encoding_format','float')!='float':raise RequestError('Only float encoding is supported')
        if payload.get('dimensions',768)!=768:raise RequestError('Expected 768 dimensions')
        texts=payload.get('input')
        if isinstance(texts,str):texts=[texts]
        if not isinstance(texts,list) or not 1<=len(texts)<=64 or any(not isinstance(t,str) or not t.strip() for t in texts):raise RequestError('Expected 1 to 64 nonempty strings')
        if not self.lock.acquire(timeout=180):raise RequestError('Embedding queue busy',503)
        try:vectors,tokens=self.engine.encode(texts)
        finally:self.lock.release()
        return {'object':'list','model':MODEL,'data':[{'object':'embedding','index':i,'embedding':v} for i,v in enumerate(vectors)],'usage':{'prompt_tokens':tokens,'total_tokens':tokens}}

class GTEEngine:
    short_batch = 16
    min_token_fill = 85

    def __init__(self,config):
        import torch
        from transformers import AutoModel,AutoTokenizer
        self.device=config.get('device','cuda')
        if self.device not in {'cpu','cuda'}:raise ValueError('device must be cpu or cuda')
        if self.device=='cuda' and not torch.cuda.is_available():raise RuntimeError('CUDA required; CPU fallback is disabled')
        torch.set_num_threads(int(config.get('threads',6)))
        if self.device=='cpu':self.short_batch=1
        self.torch=torch
        self.tokenizer=AutoTokenizer.from_pretrained(config['model_path'],local_files_only=True)
        self.model=AutoModel.from_pretrained(config['model_path'],local_files_only=True,trust_remote_code=True,code_revision=config['code_revision'],dtype=torch.float32,attn_implementation='eager').to(self.device).eval()
        if next(self.model.parameters()).dtype!=torch.float32:raise RuntimeError('FP32 required')
    def _batch(self,texts):
        t=self.torch
        x=self.tokenizer(texts,padding=True,truncation=False,return_tensors='pt').to(self.device)
        with t.inference_mode():
            vectors=t.nn.functional.normalize(self.model(**x).last_hidden_state[:,0],p=2,dim=1).cpu()
        if vectors.shape!=(len(texts),768) or not t.isfinite(vectors).all():raise RuntimeError('Invalid embedding output')
        return vectors.tolist()
    def _retry(self,texts):
        try:return self._batch(texts)
        except self.torch.cuda.OutOfMemoryError:pass
        gc.collect()
        if self.device=='cuda':self.torch.cuda.empty_cache()
        if len(texts)==1:raise RequestError('Input cannot fit available GPU memory',503)
        mid=len(texts)//2
        return self._retry(texts[:mid])+self._retry(texts[mid:])
    def encode(self, texts):
        lengths = [len(ids) for ids in self.tokenizer(texts, truncation=False)['input_ids']]
        if max(lengths) > 8192:
            raise RequestError('Input exceeds 8192 GTE tokens; nothing was truncated')
        order = sorted(range(len(texts)), key=lambda i: lengths[i], reverse=True)
        out = [None] * len(texts)
        offset = 0
        while offset < len(order):
            n = lengths[order[offset]]
            size = 1 if self.device=='cpu' or n > 2048 else 2 if n > 1024 else self.short_batch
            ids = order[offset:offset + size]
            if n <= 1024 and self.min_token_fill:
                # Sorted lengths make the average fill decrease as this group grows.
                selected = [ids[0]]
                real_tokens = n
                for index in ids[1:]:
                    next_tokens = real_tokens + lengths[index]
                    if next_tokens * 100 < self.min_token_fill * n * (len(selected) + 1):
                        break
                    selected.append(index)
                    real_tokens = next_tokens
                ids = selected
            for i, vector in zip(ids, self._retry([texts[i] for i in ids])):
                out[i] = vector
            offset += len(ids)
        return out, sum(lengths)


class Handler(BaseHTTPRequestHandler):
    def log_message(self,*args):pass
    def setup(self):super().setup();self.connection.settimeout(240)
    def reply(self,status,payload):
        data=json.dumps(payload,separators=(',',':'),allow_nan=False).encode()
        self.send_response(status);self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(data)));self.end_headers();self.wfile.write(data)
    def do_GET(self):
        if self.path=='/health':self.reply(200,{'status':'ok','model':MODEL,'dimensions':768,'device':getattr(self.server.service.engine,'device','cuda')})
        else:self.reply(404,{'error':'Not found'})
    def do_POST(self):
        if self.path!='/v1/embeddings':self.reply(404,{'error':'Not found'});return
        try:
            if not hmac.compare_digest(self.headers.get('Authorization',''),'Bearer '+self.server.service.key):raise RequestError('Unauthorized',401)
            size=int(self.headers.get('Content-Length','0'))
            if not 0<size<=8*1024*1024:raise RequestError('Invalid request size',413)
            data=json.loads(self.rfile.read(size))
            self.reply(200,self.server.service.embed(data,self.headers.get('Authorization')))
        except RequestError as e:self.reply(e.status,{'error':str(e)})
        except (ValueError,UnicodeError):self.reply(400,{'error':'Invalid request'})
        except (BrokenPipeError,ConnectionResetError):pass
        except Exception:self.reply(500,{'error':'Embedding failed'})

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--config',required=True);args=parser.parse_args()
    config=json.loads(Path(args.config).read_text(encoding='utf-8-sig'))
    engine=GTEEngine(config);engine.encode(['서비스 준비 확인'])
    server=ThreadingHTTPServer((config.get('bind','127.0.0.1'),config.get('port',18882)),Handler)
    key=Path(config['api_key_file']).read_text(encoding='utf-8').strip() if config.get('api_key_file') else config['api_key']
    server.service=EmbeddingService(engine,key)
    print('GTE service ready',flush=True);server.serve_forever()
if __name__=='__main__':main()
