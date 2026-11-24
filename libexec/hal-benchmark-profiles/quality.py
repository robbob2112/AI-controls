import json,subprocess as sp,tempfile,re
from decimal import Decimal,InvalidOperation
def check_code(code, task):
    body = {'code': code, 'imports': task['test_imports'], 'tests': task['test_list']}
    script = "import json,sys;d=json.load(sys.stdin);exec(compile('\\n'.join(d['imports'])+'\\n'+d['code']+'\\n'+'\\n'.join(d['tests']),'candidate','exec'));print('HAL_TESTS_PASS')"
    cmd = ['bwrap', '--unshare-all', '--die-with-parent', '--new-session', '--ro-bind', '/usr', '/usr', '--symlink', 'usr/lib64', '/lib64', '--symlink', 'usr/lib', '/lib', '--proc', '/proc', '--dev', '/dev', '--tmpfs', '/tmp', '--chdir', '/tmp', '--uid', '65534', '--gid', '65534', '--cap-drop', 'ALL', '/usr/bin/python3', '-I', '-c', script]
    cmd=['/usr/bin/prlimit','--cpu=3','--as=536870912','--fsize=1048576','--nofile=64','--nproc=32','--']+cmd
    with tempfile.TemporaryFile() as out:
        try:
            result = sp.run(cmd, input=json.dumps(body).encode(), stdout=out, stderr=sp.STDOUT, timeout=8, )
            out.seek(0)
            text = out.read(4096).decode(errors='replace')
            return (result.returncode == 0 and 'HAL_TESTS_PASS' in text, text)
        except sp.TimeoutExpired:
            return (False, 'Sandbox timeout')

def normalized_number(s):
    values = re.findall('[-+]?\\d[\\d,]*(?:\\.\\d+)?', s)
    return str(Decimal(values[-1].replace(',', '')).normalize()) if values else None