import unittest
from concurrent.futures import ThreadPoolExecutor
import time
from service import EmbeddingService, RequestError, GTEEngine
from types import SimpleNamespace
class Engine:
    def __init__(self): self.active=0; self.peak=0
    def encode(self,texts):
        self.active+=1;self.peak=max(self.peak,self.active);time.sleep(.01)
        out=[[float(len(t))]*768 for t in texts];self.active-=1
        return out,sum(map(len,texts))
class Tests(unittest.TestCase):
    def setUp(self): self.engine=Engine();self.service=EmbeddingService(self.engine,'test-key')
    def test_order_and_usage(self):
        r=self.service.embed({'model':'gte-multilingual-base','input':['a','longer']},'Bearer test-key')
        self.assertEqual([x['index'] for x in r['data']],[0,1]);self.assertEqual(r['data'][1]['embedding'][0],6);self.assertEqual(r['usage']['total_tokens'],7)
    def test_auth(self):
        with self.assertRaises(RequestError):self.service.embed({'input':['a']},'Bearer wrong')
    def test_validation(self):
        for p in [{'input':[]},{'input':[1]},{'input':['x']*65},{'input':['x'],'model':'other'},{'input':['x'],'encoding_format':'base64'}]:
            with self.assertRaises(RequestError):self.service.embed(p,'Bearer test-key')
    def test_serialized(self):
        with ThreadPoolExecutor(max_workers=4) as pool:list(pool.map(lambda _:self.service.embed({'input':['x']},'Bearer test-key'),range(8)))
        self.assertEqual(self.engine.peak,1)
    def test_lock_released_after_failure(self):
        def fail(texts):raise RuntimeError('engine failure')
        self.engine.encode=fail
        with self.assertRaises(RuntimeError):self.service.embed({'input':['x']},'Bearer test-key')
        self.assertTrue(self.service.lock.acquire(blocking=False));self.service.lock.release()

class EngineTests(unittest.TestCase):
    def setUp(self):
        self.engine=GTEEngine.__new__(GTEEngine)
        self.engine.device='cuda'
        self.engine.tokenizer=lambda texts,**kwargs:{'input_ids':[range(int(t)) for t in texts]}
        self.calls=[]
        def batch(texts):self.calls.append(texts);return [[int(t)] for t in texts]
        self.engine._batch=batch
        class OOM(Exception):pass
        self.oom=OOM
        self.engine.torch=SimpleNamespace(cuda=SimpleNamespace(OutOfMemoryError=OOM,empty_cache=lambda:None))
    def test_production_bucket_separates_padding_and_restores_order(self):
        texts=['95','1000','20','900','100','19']
        vectors,tokens=self.engine.encode(texts)
        self.assertEqual(vectors,[[int(t)] for t in texts])
        self.assertEqual(tokens,sum(map(int,texts)))
        self.assertEqual(self.calls,[['1000','900'],['100','95'],['20','19']])
    def test_production_short_batch_cap_is_16(self):
        self.engine.encode(['1024']*17)
        self.assertEqual([len(c) for c in self.calls],[16,1])
    def test_token_boundary(self):
        self.assertEqual(self.engine.encode(['8192']),([[8192]],8192))
        self.calls.clear()
        with self.assertRaises(RequestError):self.engine.encode(['8193'])
        self.assertEqual(self.calls,[])
    def test_explicit_cpu_keeps_order_and_uses_single_item_batches(self):
        self.engine.device='cpu'
        vectors,tokens=self.engine.encode(['10','1000','20'])
        self.assertEqual(vectors,[[10],[1000],[20]])
        self.assertEqual(tokens,1030)
        self.assertTrue(all(len(batch)==1 for batch in self.calls))
    def test_sort_and_restore(self):
        texts=['10','3000','1200','11','2500']
        vectors,tokens=self.engine.encode(texts)
        self.assertEqual(vectors,[[int(t)] for t in texts])
        self.assertEqual(self.calls[0],['3000']);self.assertEqual(tokens,6721)
    def test_oom_splits_and_preserves_order(self):
        def batch(texts):
            if len(texts)>1:raise self.oom()
            return texts
        self.engine._batch=batch
        self.assertEqual(self.engine._retry(['a','b','c']),['a','b','c'])
    def test_single_oom_is_terminal(self):
        def batch(texts):raise self.oom()
        self.engine._batch=batch
        with self.assertRaises(RequestError) as caught:self.engine._retry(['a'])
        self.assertEqual(caught.exception.status,503)
if __name__=='__main__':unittest.main()
