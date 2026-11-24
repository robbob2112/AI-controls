import hal_model_routing
"""Bounded model working context, durable progress and streamed Ollama replies.

Full messages and tool output stay in SQLite. Context trimming only changes what
is sent to the model, and the model can retrieve omitted history explicitly.
"""
import copy,json,time,urllib.request

from hal_model_catalog import LITTLEBIT_MODEL, NATIVE_PREFIX, installed_models


def chat_models():
    return installed_models()


def stream_native(payload, notify, opener, timeout):
    messages = [
        {'role': msg['role'], 'content': str(msg.get('content') or '')}
        for msg in payload['messages']
        if msg['role'] in ('system', 'user', 'assistant')
    ]
    body = {'model': payload['model'].removeprefix(NATIVE_PREFIX), 'messages': messages, 'stream': True}
    request = urllib.request.Request(
        'http://127.0.0.1:11436/llama/v1/chat/completions',
        json.dumps(body).encode(),
        {'Content-Type': 'application/json'},
    )
    notify('Loading model / reading context', 0)
    parts = []
    count = 0
    last = 0
    with opener(request, timeout=timeout) as response:
        for line in response:
            if not line.startswith(b'data:'):
                continue
            data = line[5:].strip()
            if data == b'[DONE]':
                return {'role': 'assistant', 'content': ''.join(parts)}
            chunk = json.loads(data)
            if chunk.get('error'):
                raise ValueError(str(chunk['error']))
            for choice in chunk.get('choices', []):
                content = choice.get('delta', {}).get('content') or ''
                parts.append(content)
                count += len(content)
            now = time.monotonic()
            if now - last >= 1:
                notify('Writing reply' if count else 'Reasoning', count)
                last = now
    raise ValueError('Model stream ended before the reply was complete')


def setup(c):
    c.execute('CREATE TABLE IF NOT EXISTS turn_progress(turn TEXT PRIMARY KEY,phase TEXT,characters INTEGER,updated REAL,started REAL)')

def progress(c,turn,phase,characters=0):
    if not turn:return
    c.execute('INSERT INTO turn_progress VALUES(?,?,?,?,?) ON CONFLICT(turn) DO UPDATE SET phase=excluded.phase,characters=excluded.characters,updated=excluded.updated',
              (turn,phase,characters,time.time(),time.time()))
    c.commit()

def state(c,chat):
    row=c.execute("SELECT p.* FROM turn_progress p JOIN turns t ON t.id=p.turn WHERE t.chat=? AND t.state IN ('running','stopping') ORDER BY t.created DESC LIMIT 1",(chat,)).fetchone()
    return dict(row) if row else None

def read_history(c,chat,query='',offset='0',length='12000'):
    offset=int(offset);length=int(length)
    if offset<0 or not 1<=length<=12000:raise ValueError('History offset must be >=0; length 1..12000')
    rows=c.execute('SELECT seq,role,body FROM messages WHERE chat=? ORDER BY seq',(chat,)).fetchall()
    text='\n\n'.join('MESSAGE '+str(r['seq'])+' ['+r['role']+']\n'+r['body'] for r in rows if not query or query.casefold() in r['body'].casefold())
    return {'total_characters':len(text),'offset':offset,'text':text[offset:offset+length],'next_offset':offset+length if offset+length<len(text) else None,'notice':'Historical messages may be stale; use repository_status for current Git facts.'}

def compact(messages,max_chars=48000):
    """Keep systems, recent turn groups and complete assistant/tool pairs.

    The byte budget is conservative for the configured 32768-token context.
    Recent user instructions survive; old conversation remains retrievable.
    """
    systems=[];groups=[];clipped=0
    for source in messages:
        m=copy.deepcopy(source)
        # Internal reasoning need not be replayed; completed answers/tool calls do.
        m.pop('thinking',None)
        if m.get('role')=='system':systems.append(m);continue
        content=m.get('content','')
        limit=16000 if m.get('role')=='user' else 10000
        if isinstance(content,str) and len(content)>limit:
            m['content']=content[:limit//2]+'\n[Middle omitted from model context; use read_conversation_history to retrieve the saved full message.]\n'+content[-limit//2:];clipped+=1
        if m.get('role')=='user' or not groups:groups.append([])
        groups[-1].append(m)
    def size(values):return len(json.dumps(values,ensure_ascii=False))
    budget=max(8000,max_chars-size(systems));kept=[];used=0
    for group in reversed(groups):
        cost=size(group)
        if used+cost>budget and kept:break
        if cost>budget:
            # A single tool-heavy turn: keep user input and the newest atomic
            # assistant + its tool-result blocks rather than orphaning results.
            anchor=[m for m in group if m.get('role')=='user'][:1]
            blocks=[]
            for m in group[len(anchor):]:
                if m.get('role')!='tool' or not blocks:blocks.append([])
                blocks[-1].append(m)
            tail=[];room=budget-size(anchor)
            for block in reversed(blocks):
                if size(block)>room:break
                tail.insert(0,block);room-=size(block)
            group=anchor+[m for block in tail for m in block];clipped+=1
        kept.insert(0,group);used+=size(group)
    dropped=len(groups)-len(kept)
    if dropped or clipped:
        # Short user-only breadcrumbs preserve intent without repeating old HAL
        # claims about an approval or infrastructure error as current truth.
        breadcrumbs=[]
        for group in groups[:dropped]:
            for m in group:
                if m.get('role')=='user':breadcrumbs.append(str(m.get('content',''))[:500])
        systems.append({'role':'system','content':'Model working context was compacted; full history is still saved and searchable with read_conversation_history. Earlier user requests (historical, may have been superseded):\n'+'\n'.join(breadcrumbs)[-6000:]})
    return systems+[m for group in kept for m in group],{'omitted_turn_groups':dropped,'shortened_messages':clipped}

def stream_chat(payload,notify,opener=urllib.request.urlopen,idle_timeout=900, *, affinity='auto'):
    """A long reply no longer has a 240-second total wait for one JSON blob.

    The timeout applies to a silent socket. Active token generation may continue
    indefinitely; the existing per-turn process still makes Stop cancellable.
    """
    payload=dict(payload,stream=True)
    # Qwen3-Coder's renderer keeps only the first system message. Coalesce all
    # application instructions and fresh state into that one message so policy,
    # lab access and current repository facts are actually seen by the model.
    messages=payload.get('messages',[])
    systems=[str(m.get('content','')) for m in messages if m.get('role')=='system']
    if systems:payload['messages']=[{'role':'system','content':'\n\n'.join(systems)}]+[m for m in messages if m.get('role')!='system']
    try:
        with urllib.request.urlopen('http://127.0.0.1/api/benchmark-state',timeout=3) as status:
            benchmark=json.load(status)
    except (OSError,ValueError):
        if affinity!='auto':raise ValueError('GPU affinity: HAL could not check benchmark reservations. Retry shortly.')
        benchmark={}
    if benchmark.get('running') and benchmark.get('expires',0)>time.time():
        stage=benchmark.get('stage','').lower()
        reserved=benchmark.get('gpu') or ('b65' if 'b65' in stage else 'r9700' if 'radeon' in stage else None)
        if affinity=='auto' or reserved in (None,'both') or affinity==reserved:
            other={'b65':'R9700','r9700':'B65'}.get(reserved)
            guidance=' Choose '+other+' to continue.' if other else ' Wait for the current benchmark step.'
            raise ValueError('GPU affinity: HAL is reserving inference resources for a benchmark.'+guidance)
    if payload['model'].startswith(NATIVE_PREFIX):
        if affinity != 'auto':
            raise ValueError('GPU affinity: Native llama.cpp models use the B65. Select Auto.')
        return stream_native(payload, notify, opener, idle_timeout)
    littlebit=payload.get('model')==LITTLEBIT_MODEL
    if littlebit and affinity!='auto':raise ValueError('GPU affinity: LittleBit runs on CPU. Select Auto.')
    port=hal_model_routing.GPU_AFFINITIES[affinity]
    if port:
        payload['options']=dict(payload.get('options',{}),num_gpu=999)
        payload['keep_alive']='60s'
    base='http://127.0.0.1:11450' if littlebit else f'http://127.0.0.1:{port or 11434}'
    request=urllib.request.Request(base+'/api/chat',json.dumps(payload).encode(),{'Content-Type':'application/json'})
    message={'role':'assistant','content':'','thinking':'','tool_calls':[]};last=0;done=False
    notify('Loading model / reading context',0)
    try:
        stream=opener(request,timeout=idle_timeout)
    except urllib.error.HTTPError as error:
        if not littlebit:raise
        try:detail=json.loads(error.read(8192)).get('error')
        except (OSError,ValueError):detail=None
        raise ValueError('LittleBit: '+(detail or str(error))) from error
    with stream as response:
        for line in response:
            if not line.strip():continue
            chunk=json.loads(line)
            if chunk.get('error'):raise ValueError('Model server: '+str(chunk['error']))
            delta=chunk.get('message',{})
            for key in ('content','thinking'):message[key]+=delta.get(key) or ''
            message['tool_calls'].extend(delta.get('tool_calls') or [])
            now=time.monotonic()
            if now-last>=1 or chunk.get('done'):
                notify('Preparing tool call' if message['tool_calls'] else 'Writing reply' if message['content'] else 'Reasoning',len(message['content'])+len(message['thinking']));last=now
            if chunk.get('done'):done=True;break
    if not done:raise ValueError('Model stream ended before the reply was complete. Your saved messages and completed tool actions are kept.')
    if port:
        with opener(base+'/api/ps',timeout=5) as response:
            loaded=json.load(response)['models']
        model=payload['model']
        if not any(m['name'] in (model,model+':latest') and m['size_vram']>0 and m['size_vram']>=m['size']*.98 for m in loaded):
            raise ValueError('GPU affinity: HAL could not verify the model on '+hal_model_routing.GPU_LABELS[affinity]+'.')
    return {k:v for k,v in message.items() if v or k in ('role','content')}

def friendly_error(detail):
    if 'LittleBit:' in detail:return detail.split('LittleBit:',1)[1].strip()
    if 'GPU affinity:' in detail:return detail.split('GPU affinity:',1)[1].strip()
    if 'TimeoutError' in detail or 'timed out' in detail:
        return 'HAL stopped receiving data from the model server. Your messages, workspace files and completed commits are saved. Send “continue” to resume; check the repository status above before repeating a commit.'
    if 'Connection refused' in detail or 'URLError' in detail:
        return 'HAL could not reach its local model or tool service. Your work is saved. Try again; if it repeats, the local backend needs checking.'
    if 'stream ended' in detail:return 'The model disconnected before finishing its reply. Your work is saved. Send “continue” to resume.'
    return 'HAL could not finish this response. Your saved messages and workspace files are kept. Send a follow-up to continue, or show Technical details when asking for help.'
