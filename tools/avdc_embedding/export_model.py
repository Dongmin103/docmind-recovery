import os,json,time,sys
from pathlib import Path
import torch,numpy as np,openvino as ov
from transformers import AutoTokenizer,AutoModel
torch.set_num_threads(4);torch.set_num_interop_threads(1)
b=Path('/trial');c=json.loads((b/'config.json').read_text())
tok=AutoTokenizer.from_pretrained(c['model_path'],local_files_only=True)
m=AutoModel.from_pretrained(c['model_path'],local_files_only=True,trust_remote_code=True,code_revision=c['code_revision'],dtype=torch.float32,attn_implementation='sdpa').eval()
class Wrapper(torch.nn.Module):
 def __init__(self,model):super().__init__();self.model=model
 def forward(self,input_ids,attention_mask):
  h=self.model(input_ids=input_ids,attention_mask=attention_mask).last_hidden_state
  return torch.nn.functional.normalize(h[:,0],p=2,dim=1)
w=Wrapper(m).eval()
x=tok(['Document search validation.','한국어 검색 검증입니다.'],padding=True,return_tensors='pt')
print('CONVERT_BEGIN',flush=True);start=time.perf_counter()
with torch.inference_mode():
 ir=ov.convert_model(w,example_input=(x['input_ids'],x['attention_mask']),input=[('input_ids',ov.PartialShape([-1,-1]),ov.Type.i64),('attention_mask',ov.PartialShape([-1,-1]),ov.Type.i64)])
ov.save_model(ir,b/'model.xml',compress_to_fp16=False)
print('CONVERT_DONE',time.perf_counter()-start,flush=True)
compiled=ov.Core().compile_model(ir,'CPU',{'INFERENCE_PRECISION_HINT':'f32','INFERENCE_NUM_THREADS':4,'NUM_STREAMS':1,'ENABLE_CPU_PINNING':False,'PERFORMANCE_HINT':'LATENCY'})
results=[]
for repeats,batch in [(1,1),(20,4),(80,4),(160,2),(600,1),(800,1)]:
 texts=[('검증 test 문장 '*repeats)+str(i) for i in range(batch)]
 xx=tok(texts,padding=True,truncation=False,return_tensors='pt')
 with torch.inference_mode():a=w(**xx).numpy()
 z=compiled({k:v.numpy() for k,v in xx.items()})[0]
 cos=(a*z).sum(1)/(np.linalg.norm(a,axis=1)*np.linalg.norm(z,axis=1));error=float(np.max(np.abs(a-z)))
 row={'length':xx['input_ids'].shape[1],'batch':batch,'max_abs_error':error,'min_cosine':float(cos.min())};results.append(row);print(json.dumps(row),flush=True)
 assert np.isfinite(z).all() and error<1e-4 and cos.min()>.99999
out={'compatible':True,'precision':'FP32','dimensions':768,'openvino':ov.__version__,'torch':torch.__version__,'threads':4,'cpuset':sorted(os.sched_getaffinity(0)),'tests':results,'compile_properties':{k:str(compiled.get_property(k)) for k in ['INFERENCE_NUM_THREADS','NUM_STREAMS','INFERENCE_PRECISION_HINT']}}
(b/'compatibility.json').write_text(json.dumps(out,indent=2));print('COMPATIBILITY_PASS',flush=True)


