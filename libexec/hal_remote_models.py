"""Search public model catalogs without downloading their weights."""
import html
from html.parser import HTMLParser
from functools import lru_cache
import time
import json
import re
import urllib.parse
import urllib.request

SEGMENT=r'[A-Za-z0-9][A-Za-z0-9_.-]{0,95}'
HF_REPO=re.compile(SEGMENT+'/'+SEGMENT)
HF_MODEL=re.compile('hf[.]co/'+SEGMENT+'/'+SEGMENT+'(?:[:][A-Za-z0-9][A-Za-z0-9_.-]{0,159})?')
OLLAMA_NAME=re.compile(r'[a-z0-9][a-z0-9_.-]{0,79}')


def fetch(host,path):
    req=urllib.request.Request('https://'+host+path,headers={'User-Agent':'HAL Model Control'})
    with urllib.request.urlopen(req,timeout=6) as response:
        return response.read(4_000_000).decode()


class OllamaReadme(HTMLParser):
    blocks={'p','li','h1','h2','h3','h4','br','tr'}
    excluded={'script','style','pre','table'}

    def __init__(self):
        super().__init__()
        self.depth=0
        self.skip=None
        self.parts=[]
        self.done=False

    def handle_starttag(self,tag,attrs):
        if self.done:return
        if not self.depth:
            if dict(attrs).get('id')=='display':
                self.depth=1
            elif tag=='meta' and dict(attrs).get('name')=='description':
                self.summary=dict(attrs).get('content','')
            return
        if tag=='div':self.depth+=1
        if tag in self.excluded and not self.skip:self.skip=tag
        if tag in self.blocks and not self.skip:self.parts.append('\n\n')

    def handle_endtag(self,tag):
        if not self.depth:return
        if self.skip==tag:self.skip=None
        if tag in self.blocks and not self.skip:self.parts.append('\n\n')
        if tag=='div':
            self.depth-=1
            self.done=not self.depth

    def handle_data(self,data):
        if self.depth and not self.skip:self.parts.append(data.strip() if not data.strip() else data)


@lru_cache(maxsize=64)
def card_text(source,family,period):
    if source=='ollama' and OLLAMA_NAME.fullmatch(family) and '..' not in family:
        parser=OllamaReadme()
        parser.feed(fetch('ollama.com','/library/'+family))
        text=''.join(parser.parts) or getattr(parser,'summary','')
    elif source=='hf' and HF_REPO.fullmatch(family) and '..' not in family:
        text=fetch('huggingface.co','/'+family+'/raw/main/README.md')
        original=re.search(
            r'(?:original model|base model)\s*:\s*(?:\[[^\]]+\]\()?https://huggingface\.co/('+SEGMENT+'/'+SEGMENT+r')(?=[\s)\]]|$)',
            text,re.I)
        if original and original[1].casefold()!=family.casefold() and '..' not in original[1]:
            try:
                base=fetch('huggingface.co','/'+original[1]+'/raw/main/README.md')
                base=re.sub(r'\A---\s*\n.*?\n---\s*\n','',base,count=1,flags=re.S)
                text=text+'\n\n## Original model: '+original[1]+'\n\n'+base
            except (OSError,ValueError):pass
        text=re.sub(r'\A---\s*\n.*?\n---\s*\n','',text,count=1,flags=re.S)
        text=re.sub(r'```.*?```','',text,flags=re.S)
        text=re.sub(r'!?\[([^\]]*)\]\([^)]+\)',r'\1',text)
        text=re.sub(r'<[^>]+>','',text)
        text='\n\n'.join(line.strip('# ').strip() for line in text.splitlines()
                           if line.strip() and not line.lstrip().startswith('|'))
    else:raise ValueError('Invalid model card')
    paragraphs=[' '.join(part.split()) for part in text.split('\n\n') if part.strip()]
    return '\n\n'.join(dict.fromkeys(paragraphs))[:18000]


def model_card(data):
    family=data.get('family')
    if not isinstance(family,str):raise ValueError('Choose a model to view its card')
    source=data.get('source')
    if source not in ('ollama','hf'):raise ValueError('Choose Ollama or Hugging Face')
    return card_text(source,family,int(time.time())//900)


def variant_description(name):
    tag=name.split(':',1)[-1].lower().removesuffix('.gguf')
    parts=[]
    if tag=='latest':return 'The publisher’s default download for this model family.'
    for pattern,explanation in (
        (r'(^|[-_.])(?:coder|code|coding)(?:[-_.]|$)','Named for coding tasks.'),
        (r'(^|[-_.])(?:vision|vl|image|multimodal)(?:[-_.]|$)','Tagged for image or multimodal input.'),
        (r'(^|[-_.])(?:instruct|it|chat)(?:[-_.]|$)','Instruction-tuned for prompts and conversations.'),
        (r'(^|[-_.])(?:base|pretrain)(?:[-_.]|$)','Base checkpoint for fine-tuning or completion.'),
        (r'(^|[-_.])(?:thinking|reasoning)(?:[-_.]|$)','Tagged for reasoning and thinking tasks.'),
        (r'(^|[-_.])qat(?:[-_.]|$)','Uses quantization-aware training.'),
    ):
        if re.search(pattern,tag):parts.append(explanation)
    quant=re.search(r'(?:^|[-_.])((?:ud[-_])?(?:iq|tq|q)[1-8](?:_[a-z0-9]+)*|bf16|f16|f32|mxfp4)(?:[-_.]|$)',tag)
    if quant:
        label=quant[1].upper()
        if label in ('F16','BF16','F32'):parts.append(f'{label} precision keeps more weight detail and needs more memory.')
        else:parts.append(f'{label} weight compression trades some precision for a smaller download and lower memory use.')
    scale=re.search(r'(?:^|[-_.])(e?[0-9]+(?:\.[0-9]+)?b)(?:[-_.]|$)',tag)
    if scale:parts.append(f'{scale[1].upper()} model size variant.')
    version=re.search(r'(?:^|[-_.])(v\d+(?:\.\d+)+)(?:[-_.]|$)',tag)
    if version:parts.append(f'Publisher release {version[1]}.')
    return ' '.join(parts) or 'The publisher uses a separate tag for this build and provides no clear specialization in its name.'


def model_purpose(name, hints='', fallback=''):
    label=name.lower()
    if re.search(r'code(?:llama|stral|gemma|r)|coding|devstral|starcoder|granite-code',label):
        return 'Coding and code completion'
    if re.search(r'image generation|text-to-image|stable.diffusion',hints+' '+label,re.I):
        return 'Image generation from prompts'
    if re.search(r'speech.recognition|speech.to.text|whisper|parakeet',hints+' '+label,re.I):
        return 'Speech and audio transcription'
    if re.search(r'embed|sentence.similarity|feature.extraction',hints+' '+label,re.I):
        return 'Text embeddings and retrieval'
    if re.search(r'vision|multimodal|image.to.text|(?:^|[-_])vl(?:[-_:]|$)',hints+' '+label,re.I):
        return 'Image understanding and chat'
    if re.search(r'reasoning|thinking|deepseek.r1|cogito',hints+' '+label,re.I):
        return 'Reasoning and problem solving'
    if re.search(r'coding|programming|software engineering',hints,re.I):
        return 'Coding and software engineering'
    if fallback:return fallback
    if re.search(r'text.generation|chat|instruct|llama|gemma|qwen|mistral|gpt',hints+' '+label,re.I):
        return 'General writing and conversation'
    return 'See documented use cases'


def lookup(data):
    source=data.get('source')
    query=data.get('query','')
    family=data.get('family','')
    if not isinstance(data.get('purpose',''),str) or len(data.get('purpose',''))>64:raise ValueError('Invalid model purpose')
    if not isinstance(query,str) or len(query)>100 or not isinstance(family,str):
        raise ValueError('Invalid model search')
    if source=='ollama':
        if not family:
            page=fetch('ollama.com','/search?'+urllib.parse.urlencode({'q':query}))
            models={}
            for name,block in re.findall(r'href="/library/([a-z0-9][a-z0-9_.-]{0,79})"[^>]*>(.*?)</a>',page,re.S):
                if '<h2' not in block:continue
                prose=re.search(r'<p\b[^>]*>(.*?)</p>',block,re.S)
                hints=html.unescape(re.sub('<[^>]+>',' ',prose[1] if prose else ''))
                models[name]={'name':name,'url':'https://ollama.com/library/'+name,'family':True,
                              'purpose':model_purpose(name,hints)}
            return list(models.values())[:30]
        if not OLLAMA_NAME.fullmatch(family) or '..' in family:raise ValueError('Invalid Ollama model family')
        page=fetch('ollama.com','/library/'+family+'/tags')
        models={}
        pattern=r'href="/library/('+re.escape(family)+r':[A-Za-z0-9_.-]+)"[^>]*class="md:hidden[^"]*"[^>]*>(.*?)</a>'
        for name,block in re.findall(pattern,page,re.S):
            info=' '.join(html.unescape(re.sub('<[^>]+>',' ',block)).split())
            if 'cloud' in name.lower() or not re.search(r'\d\s*(?:GB|MB|TB)\b',info,re.I):continue
            models[name]={'name':name,'url':'https://ollama.com/library/'+name,'specs':info.split('•',1)[-1].strip(' •')}
        return [dict(model,description=variant_description(model['name']),purpose=model_purpose(model['name'],fallback=data.get('purpose') or '')) for model in list(models.values())[:100]]
    if source=='hf':
        if not family:
            path='/api/models?'+urllib.parse.urlencode({'search':query,'filter':'gguf','limit':25,'sort':'downloads','expand[]':['gguf','gated','pipeline_tag']},doseq=True)
            models=[]
            for item in json.loads(fetch('huggingface.co',path)):
                name=item.get('id','')
                if not HF_REPO.fullmatch(name) or item.get('gated'):continue
                gguf=item.get('gguf') or {}
                specs=' · '.join(str(x) for x in (gguf.get('architecture'),item.get('pipeline_tag'),f"{gguf['context_length']:,} context tokens" if gguf.get('context_length') else None) if x)
                models.append({'name':name,'url':'https://huggingface.co/'+name,'family':True,'specs':specs,'purpose':model_purpose(name,item.get('pipeline_tag') or '')})
            return models
        if not HF_REPO.fullmatch(family) or '..' in family:raise ValueError('Invalid Hugging Face repository')
        files=json.loads(fetch('huggingface.co','/api/models/'+family+'/tree/main'))
        models=[]
        for item in files:
            filename=item.get('path','')
            name='hf.co/'+family+':'+filename
            if (not HF_MODEL.fullmatch(name) or not filename.lower().endswith('.gguf') or
                'mmproj' in filename.lower() or re.search(r'-\d{5}-of-\d{5}',filename)):continue
            models.append({'name':name,'label':filename,'url':'https://huggingface.co/'+family+'/blob/main/'+filename,'specs':f"{item['size']/2**30:.2f} GiB GGUF"})
        return [dict(model,description=variant_description(model['name']),purpose=model_purpose(model['name'],fallback=data.get('purpose') or '')) for model in models[:100]]
    raise ValueError('Choose Ollama or Hugging Face')
