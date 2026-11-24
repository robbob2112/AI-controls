"""Fixed-work benchmark for the experimental CPU LittleBit runtime."""
import concurrent.futures
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import urllib.request

spec=importlib.util.spec_from_file_location('fixed',Path(__file__).with_name('fixed-work-v1.py'))
fixed=importlib.util.module_from_spec(spec);spec.loader.exec_module(fixed)
NAME='littlebit-qwen3:8b-0.55bpw'
STOP=None
tuning=fixed.hal_bench_tuning
UNIT=tuning.LITTLEBIT_UNIT
PORT=tuning.LITTLEBIT_PORT
DIGEST='5b087e4c79c23697355014fdd00b083c91d0c731ddd80a16d6affbe156f17179'


def start_inference(threads, quota, cpus, timeout):
    props=['DynamicUser=yes','NoNewPrivileges=yes','ProtectSystem=strict','ProtectHome=yes',
           'PrivateTmp=yes','DevicePolicy=closed','IPAddressDeny=any','IPAddressAllow=127.0.0.1/32',
           'RestrictAddressFamilies=AF_UNIX AF_INET','MemoryMax=20G','MemorySwapMax=0','LimitCORE=0',
           f'CPUQuota={quota*100}%','TimeoutStopSec=5',f'RuntimeMaxSec={timeout+60}']
    if cpus:props.append('AllowedCPUs='+','.join(map(str,cpus)))
    subprocess.run(['systemd-run','--unit='+UNIT,'--collect',*[v for p in props for v in ('-p',p)],
                    '--setenv=PYTHONDONTWRITEBYTECODE=1',f'--setenv=HAL_LITTLEBIT_PORT={PORT}',
                    f'--setenv=HAL_LITTLEBIT_THREADS={threads}',
                    '/opt/hal-littlebit-venv/bin/python','/usr/local/libexec/hal-littlebit-server.py'],
                   check=True,timeout=15)
    deadline=time.monotonic()+30
    while time.monotonic()<deadline and not STOP.wait(.5):
        try:
            with urllib.request.urlopen(f'http://127.0.0.1:{PORT}/health',timeout=2) as response:
                if json.load(response).get('model')==NAME:return
        except (OSError,ValueError):pass
    raise RuntimeError('LittleBit benchmark service did not start')


def chat(body,timeout):
    body=dict(body,stream=True)
    body.pop('tools',None)
    req=urllib.request.Request(f'http://127.0.0.1:{PORT}/api/chat',json.dumps(body).encode(),
                               {'Content-Type':'application/json'})
    start=time.monotonic();first=None;tokens=[];done=None
    with urllib.request.urlopen(req,timeout=timeout) as response:
        for line in response:
            if STOP and STOP.is_set():raise RuntimeError('LittleBit benchmark interrupted')
            if time.monotonic()-start>timeout:raise TimeoutError('LittleBit case timed out')
            chunk=json.loads(line)
            if chunk.get('error'):raise RuntimeError(chunk['error'])
            content=chunk.get('message',{}).get('content','')
            if content and first is None:first=time.monotonic()-start
            tokens.append(content)
            if chunk.get('done'):done=chunk;break
    if done is None:raise RuntimeError('LittleBit stream ended without metrics')
    return dict(done,message={'role':'assistant','content':''.join(tokens)},request_seconds=time.monotonic()-start,
                first_token_seconds=first)


def speed(model,prompt,remaining):
    raw=chat({'model':model,'messages':[{'role':'user','content':prompt}],
              'options':{'num_predict':fixed.SPEED_CAP}},min(remaining,330))
    output=raw['eval_count'];seconds=raw['eval_duration']/1e9
    if output<=0 or seconds<=0:raise RuntimeError('LittleBit returned no speed measurement')
    return {'input_tokens':raw['prompt_eval_count'],'output_tokens':output,'rate':output/seconds,
            'first_token_seconds':raw['first_token_seconds'],'first_visible_token_seconds':raw['first_token_seconds'],
            'reached_output_cap':output>=fixed.SPEED_CAP,'request_seconds':raw['request_seconds'],
            'content':raw['message']['content'],'response_metrics':{'prefill_in_rate':True}}


def cpu_temperature():
    state=json.loads(Path('/run/hal-cooling/status.json').read_text())
    if not state['ok'] or not state['sensors_fresh'] or time.time()-state['time']>4:
        raise RuntimeError('CPU benchmark stopped: cooling telemetry stale')
    cpu=state['temperatures']['cpu_c']
    if not isinstance(cpu,(int,float)) or not 0<cpu<90:
        raise RuntimeError(f'CPU benchmark stopped: temperature {cpu!r}°C, limit 90°C')
    return cpu


def main(job,root):
    global STOP
    parent=fixed.inherited_runner(root,'cpu')
    STOP=parent.STOP
    parent.api=lambda _,path,body=None,timeout=30: {'capabilities':[]} if path=='/api/show' else chat(body,timeout)
    fixed.streamed_speed=speed
    settings=job.get('tuning',{})
    threads=settings.get('cpu_threads',8)
    quota=settings.get('cpu_quota',8)
    cpus=settings.get('cpu_affinity')
    affinity=','.join(map(str,cpus)) if cpus else 'all available'
    row={'id':'littlebit-cpu','model':NAME,'gpu':'HAL CPU','backend':'LittleBit / PyTorch CPU','streams':1,'status':'running',
         'rate':None,'settings':f'Fixed-work v1 · {threads} CPU threads · quota {quota} cores · logical CPUs {affinity} · 4,096 input-token limit · 512 output-token limit · schema tool fallback',
         'cpu_threads':threads,'cpu_quota':quota,'cpu_affinity':cpus}
    report={'models':[NAME],'rows':[row],'accuracy':[],'hardware':{'device':'CPU'},'versions':{'suite':'fixed-work-v1','weight_sha256':DIGEST},
            'notes':['CPU output rate includes prompt processing. First-token timing measures first visible text.',
                     'Quality uses the original prompts, with LittleBit’s 512-token output ceiling and text-schema fallback. Do not compare its scores as matched GPU runs.']}
    report['fixtures_sha256']={n:hashlib.sha256((fixed.FIXTURES/n).read_bytes()).hexdigest()
                              for n in ('mbpp20.json','gsm8k50.json','hal-tool20.json')}
    samples=[];start=time.monotonic()
    parent.save(root/'results.json',report)
    parent.save(root/'public-progress.json',{'status':'Fixed-work suite · LittleBit · CPU'})
    try:
        if job['model_digest']!=DIGEST:raise RuntimeError('Unexpected LittleBit checkpoint digest')
        samples.append(cpu_temperature())
        for fixture in parent.MBPP:
            if not parent.quality.check_code(fixture['code'],fixture)[0]:raise RuntimeError('Quality reference failed')
        start_inference(threads,quota,cpus,job['timeout_seconds'])
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            future=pool.submit(fixed.run_cases,parent,NAME,root,row,start+job['timeout_seconds'])
            try:
                while not future.done() and not parent.STOP.wait(2):
                    samples.append(cpu_temperature())
                if parent.STOP.is_set():raise RuntimeError('LittleBit benchmark interrupted')
                done=future.result()
            finally:parent.STOP.set()
        row['status']='complete' if done else 'incomplete'
        if not done:report['error']='One or more LittleBit cases did not complete. See archived cases.jsonl.'
    except Exception as error:
        row['status']='failed';report['error']=fixed.case_error(error)
    finally:
        subprocess.run(['systemctl','stop',UNIT],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=12)
        row['suite_seconds']=time.monotonic()-start
        row['peak_cpu_c']=max(samples,default=None)
        for kind,value in row.get('quality',{}).items():
            report['accuracy'].append({'model':NAME,'gpu':'HAL CPU','benchmark':kind,'examples':value['completed'],
                'score':value['correct']/value['completed'] if value['completed'] else None,
                'settings':f"{value['correct']} correct, {value['completed']} completed out of {value['total']}"})
        parent.save(root/'results.json',report)
    return 0 if row['status']=='complete' else 1


if __name__=='__main__':
    sys.exit(main(json.loads(Path(sys.argv[1]).read_text()),Path(os.environ['HAL_QUEUE_EVIDENCE'])))
