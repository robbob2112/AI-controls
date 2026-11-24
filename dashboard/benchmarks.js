function pairedCampaign(data){
 const root=byId('pairedCampaign');if(!root||!data)return;
 root.replaceChildren(text('h2','Two one-hour model pairs'),text('p',data.status||'Preparing'));
 for(const row of data.rows||[]){
  const coverage=Object.entries(row.coverage||{}).map(([kind,v])=>kind+': '+v.completed+'/'+v.total+' completed, '+v.correct+' correct').join(' · ');
  root.append(text('h3',row.gpu+' · '+row.model+' · '+row.status),text('p',(row.completed_requests||0)+' completed requests · '+(Number.isFinite(row.rate)?row.rate.toFixed(2)+' tokens/sec':'Speed sample pending')),text('p',coverage));
  if(row.errors?.length)root.append(text('p',row.errors.join(' · ')));
 }
 if(data.fan_summary){const f=data.fan_summary;root.append(text('p','Physical cooling: '+(f.gpu_hot?'GPU heat/cooldown policy':'Temperature ramp policy')+' · Front full-speed command: '+f.front_full_commanded));for(const g of f.gpus||[])root.append(text('p',g.pci+' · GPU '+g.pkg_c+'°C · VRAM '+g.vram_c+'°C · '+g.rpm+' RPM · Duty '+(g.mode===0?'100':Math.round(g.pwm*100/255))+'%'));}
 root.append(text('p','GPU0 DeepSeek / GPU1 Qwen3.5 for1hour, then GPU0 Gemma4 / GPU1 GLM for1hour. Progress is local; no AI polling. Full records and downloadable JSON are in Saved runs below.'));
}
function offloadCampaign(data){
 const root=byId('offloadCampaign');if(!root||!data)return;
 root.replaceChildren(text('h2','Cogito · both GPUs + RAM + NVMe'),text('p',data.status||'Preparing local timed campaign.'));
 if(data.total&&data.bytes)root.append(text('p','70B FP16 download: '+(data.bytes/1e9).toFixed(1)+' / '+(data.total/1e9).toFixed(1)+' GB'));
 root.append(text('p','3B/32B baselines, then complete70B FP16. Benchmark token counts are timing samples, not chat limits. Ongoing paging requires NVMe reads after the first generated token. Detailed measurements and JSON download are also in Saved runs below.'));
 const table=document.createElement('table');table.className='fanTable';const head=document.createElement('tr');
 for(const value of ['Model / setup','Tokens/sec','Load sec','First token sec','Peak RAM GiB','Decode NVMe GB','Major faults','Result'])head.append(text('th',value));table.append(head);
 const number=(v,n=2)=>Number.isFinite(v)?v.toFixed(n):'—';
 for(const row of data.rows||[]){const tr=document.createElement('tr');for(const value of [row.model+' · '+row.gpu,number(row.rate),number(row.load_seconds),number(row.ttft_seconds),number(row.memory_peak_bytes/1073741824),number(row.decode_nvme_read_bytes/1e9),row.decode_major_faults??'—',row.error||(row.ongoing_paging_proven?'NVMe paging measured':row.status)])tr.append(text('td',value));table.append(tr)}
 const scroll=document.createElement('div');scroll.className='tableScroll';scroll.append(table);root.append(scroll);
}
const byId=id=>document.getElementById(id),svgNS='http://www.w3.org/2000/svg';
const signedInResults=location.pathname==='/llama/benchmark-results';
let previous='';
function text(tag,value){const el=document.createElement(tag);el.textContent=value;return el}
function bars(container,rows){
 const root=byId(container);root.replaceChildren();rows=rows.filter(r=>Number.isFinite(r.rate)).sort((a,b)=>b.rate-a.rate);const max=Math.max(1,...rows.map(r=>r.rate));
 for(const r of rows){
  const row=document.createElement('div');row.className='benchmarkBar';const label=document.createElement('div');label.append(text('strong',r.model),text('small',r.gpu+' · '+r.backend),text('small',r.settings));
  const svg=document.createElementNS(svgNS,'svg');svg.setAttribute('role','img');svg.setAttribute('aria-label',r.model+', '+r.gpu+', '+r.rate.toFixed(2)+' tokens per second');svg.setAttribute('viewBox','0 0 100 16');svg.setAttribute('preserveAspectRatio','none');
  for(const [width,cls] of [[100,'track'],[r.rate/max*100,r.status==='reference'?'reference':r.backend==='SYCL'?'sycl':'vulkan']]){const rect=document.createElementNS(svgNS,'rect');rect.setAttribute('width',width);rect.setAttribute('height','16');rect.setAttribute('rx','2');rect.setAttribute('class',cls);svg.append(rect)}
  const rate=text('div',r.rate.toFixed(2));rate.className='benchmarkRate';row.append(label,svg,rate);root.append(row);
 }
}
function currentComparison(data){
 const models=new Map();for(const r of data.rows.filter(r=>r.campaign==='expanded-20261113')){if(!models.has(r.model))models.set(r.model,{model:r.model,single:null,pair:null,status:[]});const m=models.get(r.model);if(Number.isFinite(r.rate)){if(r.gpu.startsWith('Both'))m.pair=r.rate;else m.single=Math.max(m.single||0,r.rate)}if(r.error)m.status.push(r.gpu+': '+r.error)}
 for(const q of (data.accuracy||[]).filter(q=>q.campaign==='expanded-20261113')){if(!models.has(q.model))continue;const m=models.get(q.model),key=q.benchmark.startsWith('MBPP')?'coding':q.benchmark.startsWith('GSM8K')?'reasoning':q.benchmark.startsWith('HAL tool')?'tools':'overall';m[key]=q}
 const root=byId('currentRows');root.replaceChildren();for(const m of [...models.values()].sort((a,b)=>(b.overall?.score??-1)-(a.overall?.score??-1)||(b.single||0)-(a.single||0))){const tr=document.createElement('tr'),rate=v=>Number.isFinite(v)?v.toFixed(2):'Pending',score=q=>q&&Number.isFinite(q.score)?(q.score*100).toFixed(1)+'%':'Pending';for(const v of [m.model,rate(m.single),rate(m.pair),score(m.coding),score(m.reasoning),score(m.tools),score(m.overall),m.status.join('; ')||(m.overall?'Accuracy complete; speed tests recorded as available':'Running')])tr.append(text('td',v));root.append(tr)}
}
function render(data){
 pairedCampaign(data.paired_campaign);
 offloadCampaign(data.offload_campaign);
 currentComparison(data);
 bars('singleBars',data.rows.filter(r=>r.streams===1));bars('parallelBars',data.rows.filter(r=>r.streams>1));
 const tbody=byId('trialRows');tbody.replaceChildren();
 for(const r of data.rows){const tr=document.createElement('tr');let value=Number.isFinite(r.rate)?r.rate.toFixed(2):'—';let result=failureReason(r)?'Failure: '+failureReason(r):r.status;if(r.trials?.length)result+=' · repeats: '+r.trials.filter(Number.isFinite).map(x=>x.toFixed(2)).join(', ');for(const v of [r.model,r.gpu,r.backend+' · '+r.settings,String(r.streams),value,result])tr.append(text('td',v));if(r.status==='failed')tr.className='failed';tbody.append(tr)}
 const accuracy=byId('accuracyRows');accuracy.replaceChildren();for(const r of data.accuracy){const tr=document.createElement('tr');for(const v of [r.model,r.gpu,r.benchmark,Number.isFinite(r.score)?(r.score*100).toFixed(1)+'%':r.error||'Pending',String(r.examples??'—'),r.settings||''])tr.append(text('td',v));accuracy.append(tr)}
 const changes=byId('changeRows');changes.replaceChildren();for(const c of data.changes||[]){changes.append(text('h3',c.title+' · '+c.status),text('p',new Date(c.time).toLocaleString()+' · '+c.notes))}
 renderHistory(data.history||[]);
 byId('benchmarkActivity').textContent=data.activity.message||'';
}
let historySignature='';
function failureReason(item){
 const reasons=[item?.error,...(Array.isArray(item?.errors)?item.errors:[])].filter(value=>typeof value==='string'&&value.trim());
 return [...new Set(reasons)].join(' · ');
}
function cooldownDetails(step){
 if(!step?.target_c&&!step?.file?.includes('cooldown'))return '';
 const temps=value=>{
  if(!value)return 'unrecorded';
  const pair=prefix=>['pkg_c','vram_c'].map(k=>Number.isFinite(value[prefix+k])?value[prefix+k]+'°C':'unrecorded').join(' core / ')+' VRAM';
  return Object.hasOwn(value,'b65_pkg_c')?'B65 '+pair('b65_')+' · R9700 '+pair('r9700_'):pair('');
 };
 const seconds=step.cooldown_elapsed_seconds;
 const minutes=Number.isFinite(seconds)?Math.floor(seconds/60):null;
 const elapsed=minutes===null?'unrecorded':minutes+'m '+(seconds%60).toFixed(1)+'s';
 return 'Elapsed '+elapsed+' · Start '+temps(step.starting_temperatures_c)+' · End '+temps(step.ending_temperatures_c)+' · Target '+temps(step.target_c);
}
function renderHistory(runs){
 const signature=JSON.stringify(runs);
 if(signature===historySignature)return;
 const history=byId('historyRows');
 const expanded=new Set(Array.from(history.children).filter(box=>box.open).map(box=>box.dataset.runKey));
 history.replaceChildren();
 for(const run of runs){
 const key=run.id||JSON.stringify([run.recorded_at,run.campaign]);
 const box=document.createElement('details');
 box.dataset.runKey=key;box.open=expanded.has(key);box.append(text('summary',new Date(run.recorded_at).toLocaleString()+' · '+run.campaign+' · '+run.rows.length+' configurations'));
 const overlaps=run.queue_step?.overlapping_benchmarks||[];
 if(overlaps.length)box.append(text('p','Overlapping benchmark '+overlaps.join(', ')+' · shared case cooling'));
 const diagnosis=run.failure_diagnostic;
 const reason=diagnosis?.detail||failureReason(run.queue_step)||failureReason(run);
 if(reason){
  const line=text('p','Failure reason — '+(diagnosis?.category?diagnosis.category+' · ':'')+reason);
  line.className='failed';box.append(line);
  if(diagnosis?.evidence){
   const e=diagnosis.evidence;
   const observed=[e.pci,e.core_c==null?null:e.core_c+'°C core',e.vram_c==null?null:e.vram_c+'°C VRAM',e.fan_rpm==null?null:e.fan_rpm+' RPM',e.controller_error].filter(Boolean);
   if(observed.length)box.append(text('p','Stop reading · '+observed.join(' · ')));
  }
 }
 if(run.queue_step&&['complete','failed','interrupted','terminated'].includes(run.queue_step.status)){
  const verb=run.queue_step.status==='complete'?'Rerun':'Retry';
  const kind=(run.rows||[]).length?'benchmark':'cooldown';
  const replay=text('button',verb+' '+kind);
  replay.type='button';
  replay.onclick=()=>location.assign('https://192.0.2.10:8443/llama/benchmark-control?replay='+encodeURIComponent(run.id));
  box.append(replay);
 }
 const download=text('button','Download this record (JSON)');download.onclick=()=>{const url=URL.createObjectURL(new Blob([JSON.stringify(run,null,2)],{type:'application/json'}));const a=document.createElement('a');a.href=url;a.download=run.id+'.json';a.click();setTimeout(()=>URL.revokeObjectURL(url),1000)};box.append(download);
 for(const note of run.notes||[])box.append(text('p',note));box.append(text('h3','Models and hardware'),text('pre',JSON.stringify({models:run.models,hardware:run.hardware},null,2)),text('h3','Software versions'),text('pre',JSON.stringify(run.versions,null,2)));
 const table=document.createElement('table');table.className='fanTable';const head=document.createElement('tr');for(const value of ['Model','GPU / engine','Streams','Tokens/sec','Notes'])head.append(text('th',value));table.append(head);
 for(const r of run.rows){const tr=document.createElement('tr');for(const value of [r.model,r.gpu+' / '+r.backend,r.streams,Number.isFinite(r.rate)?r.rate.toFixed(2):'—',failureReason(r)?'Failure: '+failureReason(r):r.settings])tr.append(text('td',value));table.append(tr)}const scroll=document.createElement('div');scroll.className='tableScroll';scroll.append(table);box.append(scroll);for(const row of run.rows){
  if(!row.speed_summary)continue;
  const fmt=v=>Number.isFinite(v)?v.toFixed(2):'—';
  box.append(text('h3','Fixed-work measurements'),
   text('p','Suite '+fmt(row.suite_seconds)+' s · load '+fmt(row.load_seconds)+' s · peak host RAM '+fmt(row.peak_host_ram_gib)+' GiB'));
  for(const [scenario,stats] of Object.entries(row.speed_summary)){
   const speed=stats.generation_tokens_per_second||{},first=stats.first_token_seconds||{};
   box.append(text('p',scenario+' · '+fmt(speed.median)+' median tok/s · '+fmt(speed.mean)+' mean · '+fmt(speed.max)+' peak · '+fmt(first.median)+' s median first token · '+(speed.count||0)+'/'+stats.expected_samples+' samples'));
  }
  const t=row.telemetry_summary||{};
  box.append(text('p','Peak core / VRAM '+fmt(t.package_c?.max)+' / '+fmt(t.vram_c?.max)+' °C · mean sampled GPU power '+fmt(t.gpu_watts?.mean)+' W'));
 }

 const cooling=cooldownDetails(run.queue_step);
 if(cooling)box.append(text('h3','Cooldown'),text('p',cooling));
 for(const row of run.rows||[]){
  if(row.cooldown_temperatures_c&&!cooling){
   box.append(text('p',row.model+' · '+cooldownDetails({target_c:row.starting_temperatures_c,ending_temperatures_c:row.cooldown_temperatures_c,starting_temperatures_c:row.cooldown_starting_temperatures_c,cooldown_elapsed_seconds:row.cooldown_elapsed_seconds})));
  }
 }if(run.accuracy?.length){box.append(text('h3','Accuracy benchmarks'));for(const q of run.accuracy)box.append(text('p',q.model+' · '+q.gpu+' · '+q.benchmark+' · '+(Number.isFinite(q.score)?(q.score*100).toFixed(1)+'%':q.error)+' · '+q.settings))}history.append(box);
 }
 historySignature=signature;
}
async function refresh(){try{const r=await fetch(signedInResults?'/llama/benchmark-results/api':'/api/benchmarks',{cache:'no-store'});if(!r.ok)throw Error();const d=await r.json();const signature=JSON.stringify(d.rows)+JSON.stringify(d.accuracy)+JSON.stringify(d.activity)+JSON.stringify(d.history)+JSON.stringify(d.changes)+JSON.stringify(d.offload_campaign)+JSON.stringify(d.paired_campaign);if(signature!==previous){render(d);previous=signature}byId('benchmarkStatus').textContent='Live · '+d.rows.filter(r=>r.status==='measured').length+' measured configurations · last result '+new Date(d.last_result*1000).toLocaleString()+' · checked '+new Date().toLocaleTimeString()}catch{byId('benchmarkStatus').textContent='Unable to load results. Retrying in five seconds.'}}
refresh();setInterval(refresh,5000);
