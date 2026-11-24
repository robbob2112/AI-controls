"""Tools for HAL's one managed Android emulator. Never selects a physical phone."""
import base64, json, os, pathlib, shlex, subprocess, tempfile

ADB='/usr/local/libexec/hal-android-adb'
SERIAL='emulator-5556'
UNIT='hal-android-phone.service'

def adb(args, timeout=30, binary=False):
    with tempfile.TemporaryDirectory(prefix='hal-adb-client-') as home:
        env=dict(os.environ,HOME=home,ANDROID_USER_HOME=home+'/.android')
        result=subprocess.run([ADB,'-H','127.0.0.1','-P','5038','-s',SERIAL,*args],capture_output=True,timeout=timeout,env=env)
    if result.returncode:raise ValueError(result.stderr.decode(errors='replace')[-3000:])
    if len(result.stdout)>12*1024*1024:raise ValueError('Android response too large')
    return result.stdout if binary else result.stdout.decode(errors='replace')[-64000:]

def status():
    active=subprocess.run(['systemctl','is-active',UNIT],capture_output=True,text=True).stdout.strip()
    state={'serial':SERIAL,'service':active,'booted':False,'width':720,'height':1280,'api':36,'graphics':'SwiftShader (software)','acceleration':'KVM','screen':'/llama/android/screen.png'}
    if active=='active':
        try:state['booted']=adb(['shell','getprop','sys.boot_completed'],5).strip()=='1'
        except (ValueError,subprocess.TimeoutExpired):pass
    return state

def lifecycle(action):
    if action not in ('start','stop','restart'):raise ValueError('Unknown Android power action')
    p=subprocess.run(['systemctl','--no-block',action,UNIT],capture_output=True,text=True,timeout=10)
    if p.returncode:raise ValueError(p.stderr[-2000:])
    return {'action':action,'accepted':True}

def shell(command):
    if not isinstance(command,str) or not 0<len(command)<=16000:raise ValueError('Android shell command required (maximum 16000 characters)')
    return {'output':adb(['shell',command],120),'device':SERIAL}

def input_event(kind, value):
    if kind=='text':
        if not isinstance(value,str) or len(value)>2000:raise ValueError('Text too long')
        command='input text '+shlex.quote(value.replace('%','%%').replace(' ','%s'))
    elif kind=='key':
        keys={'home':'KEYCODE_HOME','back':'KEYCODE_BACK','enter':'KEYCODE_ENTER','recent':'KEYCODE_APP_SWITCH','wake':'KEYCODE_WAKEUP'}
        if value not in keys:raise ValueError('Unknown navigation key')
        command='input keyevent '+keys[value]
    elif kind in ('tap','swipe'):
        count=2 if kind=='tap' else 4
        if not isinstance(value,list) or len(value)!=count or any(type(n) is not int for n in value):raise ValueError('Invalid coordinates')
        if any(not 0<=n<(720 if i%2==0 else 1280) for i,n in enumerate(value)):raise ValueError('Coordinate outside screen')
        command='input '+kind+' '+' '.join(map(str,value))+(' 350' if kind=='swipe' else '')
    else:raise ValueError('Unknown Android input')
    return shell(command)

def install(path):
    p=pathlib.Path(path)
    if not p.is_file() or not 0<p.stat().st_size<=256*1024*1024:raise ValueError('APK must be 1 byte to 256 MiB')
    return {'output':adb(['install','-r',str(p)],120),'device':SERIAL}

def install_lab(path):
    # SSH is the existing fixed OL10.2 lab transport, never caller-selected host/root.
    import hal_lab_tools
    if not isinstance(path,str) or not path.startswith('/home/lab/work/') or '..' in pathlib.PurePosixPath(path).parts or not path.endswith('.apk'):raise ValueError('APK must be beneath /home/lab/work in the OL10.2 lab')
    command='python3 -c '+shlex.quote("import pathlib,base64;p=pathlib.Path("+repr(path)+").resolve();assert str(p).startswith('/home/lab/work/');assert p.stat().st_size<=48*1024*1024;print(base64.b64encode(p.read_bytes()).decode())")
    # Separate bounded transfer: lab command tool intentionally truncates ordinary output.
    result=subprocess.run(hal_lab_tools.SSH+['sudo -n '+command],capture_output=True,timeout=120)
    if result.returncode:raise ValueError(result.stderr.decode(errors='replace')[-2000:])
    if len(result.stdout)>68*1024*1024:raise ValueError('Lab APK too large; use browser upload')
    with tempfile.NamedTemporaryFile(suffix='.apk') as f:
        f.write(base64.b64decode(result.stdout,validate=False));f.flush();return install(f.name)

def tool(name,args):
    if name=='android_status':return status()
    if name=='android_power':return lifecycle(args['action'])
    if name=='android_shell':return shell(args['command'])
    if name=='android_input':return input_event(args['kind'],args['value'])
    if name=='android_install_lab_apk':return install_lab(args['path'])
    if name=='android_ui':
        adb(['shell','uiautomator dump /sdcard/hal-window.xml'],30)
        return {'xml':adb(['shell','cat /sdcard/hal-window.xml']),'screen':'/llama/android/screen.png'}
    if name=='android_screenshot':return {'png_base64':base64.b64encode(adb(['exec-out','screencap','-p'],binary=True)).decode(),'url':'/llama/android/screen.png'}
    raise ValueError('Unknown Android tool')

SCHEMAS=[
 ('android_status','Read HAL Android virtual phone boot/status; it uses software graphics.',{}),
 ('android_power','Start, stop or restart only the Android emulator; start before testing.',{'action':'start, stop or restart'}),
 ('android_shell','Run an Android shell command inside the managed emulator only (not HAL or OL10.2). Use for am/pm/logcat and app testing.',{'command':'Android shell command'}),
 ('android_input','Send tap/swipe/key/text to Android. value is a JSON string: coordinates array for tap/swipe, string for text/key.',{'kind':'tap, swipe, key or text','value':'JSON value, e.g. [300,500] or "home"'}),
 ('android_ui','Read current Android UI hierarchy and screenshot URL.',{}),
 ('android_screenshot','Capture the emulator screenshot as PNG base64 plus browser URL.',{}),
 ('android_install_lab_apk','Install an APK built in the OL10.2 lab, maximum 48 MiB; browser upload supports 256 MiB.',{'path':'Absolute .apk path beneath /home/lab/work/'})]

def dispatch(name,args):
    if name=='android_input' and isinstance(args.get('value'),str):
        args=dict(args);args['value']=json.loads(args['value'])
    return tool(name,args)
