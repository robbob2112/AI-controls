from pathlib import Path
import json,urllib.request
root=Path('/data/ollama/models/manifests/registry.ollama.ai/library');models=[]
for family in sorted(root.iterdir()):
 for tag in sorted(family.iterdir()):
  try:
   d=json.loads(tag.read_text());layer=next(x for x in d['layers'] if x['mediaType']=='application/vnd.ollama.image.model');models.append((family.name+':'+tag.name,'/data/ollama/models/blobs/'+layer['digest'].replace(':','-')))
  except (OSError,ValueError,StopIteration):pass
friendly=Path('/data/models');friendly.mkdir(exist_ok=True)
for name,path in models:
 if name in ('cogito:3b','cogito:32b'):
  link=friendly/(name.replace(':','-')+'-Q4_K_M.gguf')
  if not link.exists() and not link.is_symlink():link.symlink_to(path)
models.append(('qwen3.8:27b','/scratch/b65-model-testing/Qwen3.8-27B-Q4_K_M.gguf'))
Path('/etc/hal-llama/models.ini').write_text('version = 1\n\n'+'\n'.join('['+name+']\nmodel = '+path+'\n' for name,path in models))
with urllib.request.urlopen('http://127.0.0.1:11436/llama/models?reload=1',timeout=15) as r:d=json.load(r)
print('Native model menu refreshed without restarting active chat:',[m['id'] for m in d['data']])
