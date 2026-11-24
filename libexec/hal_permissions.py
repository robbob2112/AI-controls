"""Conversation permissions, enforced by both gateway and credential broker."""
KEYS=('gitea','edit','worker','local_web','test_db','deploy','dependencies','auto_commit','auto_lab_deploy','lab','android','internet','internet_restricted')
LEGACY={key:True for key in KEYS}
LEGACY["auto_commit"]=False
LEGACY["auto_lab_deploy"]=False
LEGACY["lab"]=False
LEGACY["android"]=False
LEGACY["internet"]=False
LEGACY["internet_restricted"]=False
LEGACY["internet_sites"]=[]
DEFAULT={key:key in ('gitea','edit','worker','local_web') for key in KEYS}
DEFAULT['internet_sites']=[]
REQUIRED={
    "render_public_page":("internet",), "search_web":("internet",), "fetch_public_page":("internet",),
    'deploy_lab_workspace':('lab','gitea','worker'),'approve_lab_deployment':('lab','gitea','worker'),'lab_deployments':('lab',),
    'repository_status':('gitea','worker'),
'android_status':('android',),
'android_power':('android',),
'android_shell':('android',),
'android_input':('android',),
'android_ui':('android',),
'android_screenshot':('android',),
'android_install_lab_apk':('android',),

    'lab_status':('lab',), 'run_lab_command':('lab',), 'sync_lab_workspace':('lab','worker'),
    'list_repositories':('gitea',),'checkout_repository':('gitea',),
    'create_project':('gitea','edit','worker'),
    'fetch_repository':('gitea',),'run_command':('worker',),
    'git_status':('worker',),'git_diff':('worker',),
    'request_commit':('gitea','edit'), 'approve_commit':('gitea','edit'),
    'approvals':('gitea',), 'fetch_local_page':('local_web',),
    'postgres_query':('test_db',),
    'deploy_test_workspace':('deploy','worker','edit'),
    'get_wheel':('dependencies','worker','edit'),
    'get_nuget_package':('dependencies','worker','edit'),
}

def normalize(value,default=None):
    if value is None:return dict(LEGACY if default is None else default)
    if not isinstance(value,dict) or set(value)-(set(KEYS)|{'internet_sites'}):raise ValueError('Unknown access permission')
    if any(type(item) is not bool for key,item in value.items() if key!='internet_sites'):raise ValueError('Access permissions must be checkboxes')
    sites=value.get('internet_sites',[])
    if not isinstance(sites,list) or len(sites)>100 or any(not isinstance(v,str) or not v.strip() or len(v)>253 for v in sites):raise ValueError('Enter up to100 website patterns')
    import re
    sites=list(dict.fromkeys(v.strip().lower() for v in sites))
    if any(not re.fullmatch(r'[a-z0-9*.-]+',v) or '..' in v for v in sites):raise ValueError('List hostnames only, e.g. example.com or *.example.com; * allows all public websites')
    return dict({key:value.get(key,False) for key in KEYS},internet_sites=sites)

def require(tool,permissions):
    permissions=normalize(permissions)
    missing=[key for key in REQUIRED.get(tool,()) if not permissions[key]]
    if missing:raise ValueError('Access disabled for this conversation: '+', '.join(missing))

def allowed(tool,permissions):
    try:require(tool,permissions);return True
    except ValueError:return False

def setup(connection):
    connection.execute('CREATE TABLE IF NOT EXISTS chat_permissions(chat TEXT PRIMARY KEY,permissions TEXT NOT NULL)')

def get(connection,chat):
    import json
    row=connection.execute('SELECT permissions FROM chat_permissions WHERE chat=?',(chat,)).fetchone()
    return normalize(json.loads(row[0])) if row else dict(LEGACY)

def save(connection,chat,value):
    import json
    permissions=normalize(value)
    connection.execute('INSERT OR REPLACE INTO chat_permissions VALUES(?,?)',(chat,json.dumps(permissions)))
    return permissions
