"""Block unchanged failed operations, without limiting successful agent work."""
import hashlib,json,re,time

def setup(c):
    c.execute('CREATE TABLE IF NOT EXISTS tool_failures(chat TEXT,key TEXT,state TEXT,count INTEGER,detail TEXT,updated REAL,PRIMARY KEY(chat,key))')

def key(function):
    args=function.get('arguments',{})
    if isinstance(args,dict):args={k:' '.join(v.split()) if isinstance(v,str) else v for k,v in args.items()}
    return hashlib.sha256(json.dumps([function.get('name'),args],sort_keys=True).encode()).hexdigest()

def failed(result):
    if result.get('skipped'):return False
    if result.get('status',0)!=0 or result.get('error'):return True
    inner=result.get('result')
    return isinstance(inner,dict) and (inner.get('returncode',0)!=0 or inner.get('exit_code',0)!=0 or inner.get('status',0) not in (0,'ok','success',None) and isinstance(inner.get('status'),int) or bool(inner.get('error')))

def state(facts):
    if not isinstance(facts,dict):return 'no-verified-repository'
    keep={k:facts.get(k) for k in ('head','remote_head','dirty','changed_files','pending_approvals')}
    return hashlib.sha256(json.dumps(keep,sort_keys=True).encode()).hexdigest()

def blocked(c,chat,function,current):
    row=c.execute('SELECT * FROM tool_failures WHERE chat=? AND key=?',(chat,key(function))).fetchone()
    if row and row['state']==current and row['count']>=2:return row['detail']
    return None

def remember(c,chat,function,current,result):
    fingerprint=key(function)
    if not failed(result):
        c.execute('DELETE FROM tool_failures WHERE chat=? AND key=?',(chat,fingerprint));return
    row=c.execute('SELECT state,count FROM tool_failures WHERE chat=? AND key=?',(chat,fingerprint)).fetchone()
    count=row['count']+1 if row and row['state']==current else 1
    detail=str(result.get('error') or result.get('result'))[:2000]
    c.execute('INSERT INTO tool_failures VALUES(?,?,?,?,?,?) ON CONFLICT(chat,key) DO UPDATE SET state=excluded.state,count=excluded.count,detail=excluded.detail,updated=excluded.updated',(chat,fingerprint,current,count,detail,time.time()))

def uncertain_commit(function,result):
    text=json.dumps(result).lower()
    return function['name']=='request_commit' and failed(result) and any(s in text for s in ('timeout','timed out','connection','disconnected'))
