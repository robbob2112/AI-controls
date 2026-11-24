'use strict';
const physicalFanNames={CPU_FAN:'Radiator fans',CPU_OPT:'CPU cooler fan',CHA_FAN1:'Rear case fan',CHA_FAN3:'Front lower',CHA_FAN4:'Front top',CHA_FAN5:'Front middle','W_PUMP+':'Water pump'};
const CHARTS=['psu','motherboard','nvme','drives','gpuCombined','b650','b651','r9700','fan','system','cpu'];
const $=id=>document.getElementById(id);
const palette=['#65dcc0','#f6be62','#79aefb','#f08aa2','#c899fa','#8bd76b','#f09958','#7cd8ed'];
const hidden=new Set();
let data=null,selection=null,live=true,originalView=null,drag=null,hover=null,requestNumber=0,controller=null;
let loadingHistory=false,lastHistoryFetch=0,lastHistoryError="";

const gpuColors={b650:'#65dcc0',b651:'#79aefb',r9700:'#f08aa2'};
const gpuLabels={b650:'B65-0',b651:'B65-1 (historical)',r9700:'R9700'};
const gpuCharts=new Set([...Object.keys(gpuColors),'gpuCombined']);
function color(index,series){return series?.color||(series?.id==='/hal-metrics/power/r9700'?'#f08aa2':palette[index%palette.length])}
function fontScale(){return window.HalStatusFont?.scale()||1}
function bounds(canvas){const scale=fontScale(),left=Math.min(60*scale,canvas.clientWidth*.2),reserve=Math.min((gpuCharts.has(canvas.id)&&canvas.id!=='gpuCombined'?145:20)*scale,canvas.clientWidth*.55);return {left,right:Math.max(left+48,canvas.clientWidth-reserve),top:35*scale,bottom:canvas.clientHeight-48*scale}}
function chartSeries(chart){
 if(chart!=='gpuCombined')return data?.series.filter(s=>s.chart===chart)||[];
 return (data?.series||[]).filter(s=>s.chart in gpuColors && s.kind==='temperature' && ['Package','Hotspot','VRAM'].includes(s.name))
  .map(s=>({...s,id:'combined:'+s.id,name:gpuLabels[s.chart]+' '+s.name,color:gpuColors[s.chart],dashed:s.name==='VRAM'}));
}
function valueText(series,value){return value.toLocaleString(undefined,{maximumFractionDigits:series.kind==='fan'?0:1})+' '+series.unit}

// Stack only real, time-aligned samples. Missing device readings stay gaps.
function powerStack(series){
 const components=series.filter(s=>s.power_role==='component'&&!hidden.has(s.id));
 const maps=components.map(s=>new Map(s.points));
 const times=[...new Set(components.flatMap(s=>s.points.map(p=>p[0])))].sort((a,b)=>a-b);
 const bands=components.map(series=>({series,points:[]})),tops=new Map();
 const totals=[];
 for(const time of times){
  const slotOne=components.map((s,index)=>[s.id,index]).filter(([id])=>['/hal-metrics/power/b65-1','/hal-metrics/power/r9700'].includes(id)).map(([,index])=>index);
  if(maps.some((map,index)=>!slotOne.includes(index)&&!map.has(time)))continue;
  if(slotOne.length===2 && slotOne.every(index=>!maps[index].has(time)))continue;
  let bottom=0;
  components.forEach((series,index)=>{
   if(!maps[index].has(time))return;
   const value=maps[index].get(time),top=bottom+value;
   bands[index].points.push({time,bottom,top,value});tops.set(series.id+'@'+time,top);bottom=top;
  });
  totals.push([time,bottom]);
 }
 return {bands,tops,totals};
}

function liveEdgeZoom(wasLive,rightFraction,plotWidth,bucket,span){
 // Snap the final two pixels (or one sample interval) to the live edge.
 const tolerance=Math.max(2/Math.max(1,plotWidth),Math.min(.01,bucket/Math.max(1,span)));
 return Boolean(wasLive&&rightFraction>=1-tolerance);
}
function historyQuery(){
 const query=new URLSearchParams({hours:selection?(selection.end-selection.start)/3600:$('range').value});
 if(selection&&!live)query.set('end',selection.end);
 else if(!selection&&!live&&$('end').value)query.set('end',Math.floor(new Date($('end').value).getTime()/1000));
 return query;
}
function followHistoryWindow(window){
 if(selection&&live){selection={...selection,start:window.start,end:window.end,followLive:true};updateZoomLabel()}
}
function updateZoomLabel(){
 if(!selection)return;
 const option=$('range').querySelector('[value="custom"]');
 option.disabled=false;option.textContent=(live?'Live last ':'Selected ')+Math.round((selection.end-selection.start)/60)+' minutes';
 $('range').value='custom';$('resetZoom').hidden=false;
}

function historyModeStatus(){
 const el=$('historyMode');if(!el)return;
 const following=live;
 const overdue=following&&lastHistoryFetch&&Date.now()-lastHistoryFetch>90000;
 el.className='history-mode '+(!following?'paused':lastHistoryError||overdue?'warning':'following');
 $('live').textContent=following?(selection?'Live · zoomed':'Live · following latest'):'Resume Live';
 $('live').setAttribute('aria-pressed',String(following));
 if(!following){el.textContent='HISTORICAL · Fixed graph window. Graphs do not follow new readings. Choose Resume Live to follow the server.'+(lastHistoryError?' Refresh failed: '+lastHistoryError:'');return}
 const checked=lastHistoryFetch?'Last graph refresh '+new Date(lastHistoryFetch).toLocaleTimeString()+'.':'Waiting for the first graph refresh.';
 el.textContent=(selection?'LIVE ZOOM · Last '+Math.round((selection.end-selection.start)/60)+' minutes; window follows the newest readings. ':'LIVE · ')+ 'Charts refresh automatically every minute; history is sampled once a minute. '+(lastHistoryError?'REFRESH FAILED: '+lastHistoryError+'. ':overdue?'REFRESH OVERDUE. ':'')+checked+(loadingHistory?' Refreshing…':'');
}

async function load(){
 const number=++requestNumber;loadingHistory=true;historyModeStatus();
 controller?.abort();controller=new AbortController();
 const current=controller,timer=setTimeout(()=>current.abort(),15000);
 try{
  const query=historyQuery();
  const response=await fetch('/api/history?'+query,{cache:'no-store',signal:current.signal});
  if(!response.ok)throw Error('History request failed');
  const next=await response.json();if(number!==requestNumber)return;data=next;followHistoryWindow(next);
  $('error').textContent='';
  const age=data.latest?Date.now()/1000-data.latest[0]:Infinity;
  $('health').textContent=age>180?'Sensor data stale':data.latest[1]==='OK'?'● Sensors healthy':'● Sensor warning';
  $('summary').textContent=data.retained[2].toLocaleString()+' retained minute samples · '+Math.round(data.bucket/60)+'-minute readings';
  $('windowDescription').textContent=new Date(data.start*1000).toLocaleString()+' – '+new Date(data.end*1000).toLocaleString();
  $('updated').textContent=data.latest?'Latest sample: '+new Date(data.latest[0]*1000).toLocaleString()+(live?(selection?' · Live zoom updates every minute':' · Live history updates every minute'):' · Historical view'):'No samples';
  for(const chart of CHARTS){
   const legend=$(chart+'Legend');legend.replaceChildren();
   chartSeries(chart).forEach((series,index)=>{
    const button=document.createElement('button');button.textContent=series.name;
    button.title=series.full_name+(series.points.length?' · '+valueText(series,series.points.at(-1)[1]):'');
    button.style.color=color(index,series);button.classList.toggle('off',hidden.has(series.id));
    button.setAttribute('aria-pressed',String(!hidden.has(series.id)));
    button.onclick=()=>{hidden.has(series.id)?hidden.delete(series.id):hidden.add(series.id);button.classList.toggle('off',hidden.has(series.id));button.setAttribute('aria-pressed',String(!hidden.has(series.id)));draw()};
    legend.append(button);
   });
  }
  const inventory=data.storage_inventory;
  if(inventory){
   for(const [chart,category] of [['nvme','nvme'],['drives','ata']]){
    const drives=inventory.drives.filter(d=>d.category===category);
    const unavailable=drives.filter(d=>d.temperature_c===null);
    $(chart+'Status').textContent=drives.length+' detected · SMART readings every minute'+(unavailable.length?' · Unavailable: '+unavailable.map(d=>d.label||d.device).join(', '):'')+'. New SMART history starts when collection was enabled; sleeping drives are not woken.';
   }
  }
  draw();lastHistoryFetch=Date.now();lastHistoryError='';
 }catch(error){if(number===requestNumber){lastHistoryError=error.name==='AbortError'?'Request timed out':'History unavailable';$('error').textContent=lastHistoryError+'. Retrying automatically.'}}
 finally{clearTimeout(timer);if(number===requestNumber)loadingHistory=false;historyModeStatus()}
}

function draw(){
 if(!data)return;
 for(const chart of CHARTS){
  const canvas=$(chart),context=canvas.getContext('2d'),width=canvas.clientWidth,height=canvas.clientHeight;
  if(!width)continue;
  const ratio=devicePixelRatio||1;canvas.width=width*ratio;canvas.height=height*ratio;context.scale(ratio,ratio);
  const area=bounds(canvas),all=chartSeries(chart),active=all.filter(s=>!hidden.has(s.id));
  const dual=gpuCharts.has(chart)&&chart!=='gpuCombined',fansOnly=chart==='fan';
  const leftSeries=active.filter(s=>!dual||s.kind==='temperature');
  const stack=chart==='psu'?powerStack(all):null;
  const rightSeries=active.filter(s=>dual&&s.kind==='fan');
  const leftMax=chart==='system'?100:fansOnly?Math.max(1500,...leftSeries.flatMap(s=>s.points.map(p=>Math.ceil(p[1]/500)*500))):Math.max(60,...leftSeries.flatMap(s=>s.points.map(p=>Math.ceil(p[1]/10)*10)),...(stack?.totals||[]).map(p=>Math.ceil(p[1]/10)*10))*1.1;
  const rightMax=Math.max(1500,...rightSeries.flatMap(s=>s.points.map(p=>Math.ceil(p[1]/500)*500)));
  const x=time=>area.left+(time-data.start)/(data.end-data.start)*(area.right-area.left);
  const y=(series,value)=>area.bottom-value/(series.kind==='percent'?100:dual&&series.kind==='fan'?rightMax:leftMax)*(area.bottom-area.top);
  const pointY=(series,point)=>y(series,stack&&series.power_role==='component'?(stack.tops.get(series.id+'@'+point[0])??point[1]):point[1]);
  const scale=fontScale(),rpmX=area.right+8*scale,percentX=Math.min(area.right+85*scale,width-52*scale);
  context.font=(14*scale)+'px system-ui';context.fillStyle='#aab9c8';context.strokeStyle='#2b3a48';
  context.fillText(chart==='psu'?'W':chart==='system'?'%':fansOnly?'RPM':'°C',6,18*scale);
  if(dual&&chart!=='gpuCombined'){context.fillText('RPM',rpmX,18*scale);context.fillText('%',percentX,18*scale)}
  for(let tick=0;tick<=4;tick++){
   const yy=area.bottom-tick/4*(area.bottom-area.top);
   context.beginPath();context.moveTo(area.left,yy);context.lineTo(area.right,yy);context.stroke();
   context.fillText(Math.round(leftMax*tick/4).toLocaleString(),5,yy+4*scale);
   if(dual&&chart!=='gpuCombined'){context.fillText(Math.round(rightMax*tick/4).toLocaleString(),rpmX,yy+4*scale,Math.max(20,percentX-rpmX-6));context.fillText(String(tick*25)+'%',percentX,yy+4*scale);}
  }
  const ticks=Math.max(1,Math.min(4,Math.floor((area.right-area.left)/(190*scale))));
  for(let tick=0;tick<=ticks;tick++){
   const time=data.start+(data.end-data.start)*tick/ticks;
   context.textAlign=tick===ticks?'right':tick===0?'left':'center';
   context.fillText(new Date(time*1000).toLocaleString([],{month:'short',day:'numeric',hour:'2-digit',minute:'2-digit'}),x(time),height-12*scale);
  }
  context.textAlign='left';context.save();context.beginPath();context.rect(area.left,area.top,area.right-area.left,area.bottom-area.top);context.clip();
  if(stack){
   for(const band of stack.bands){
    context.fillStyle=color(all.indexOf(band.series),band.series);context.globalAlpha=.6;
    let segment=[];
    function fill(){
     if(!segment.length)return;
     if(segment.length===1){const p=segment[0];context.fillRect(x(p.time)-3,y(band.series,p.top),6,y(band.series,p.bottom)-y(band.series,p.top));return;}
     context.beginPath();context.moveTo(x(segment[0].time),y(band.series,segment[0].top));
     for(const p of segment)context.lineTo(x(p.time),y(band.series,p.top));
     for(const p of [...segment].reverse())context.lineTo(x(p.time),y(band.series,p.bottom));
     context.closePath();context.fill();
    }
    for(const point of band.points){if(segment.length&&point.time-segment.at(-1).time>data.bucket*2.5){fill();segment=[]}segment.push(point)}
    fill();context.globalAlpha=1;
   }
   const totals=new Map(all.find(s=>s.power_role==='total')?.points||[]);
   const over=stack.totals.some(([time,sum])=>totals.has(time)&&sum>totals.get(time)+5);
   $('psuPowerStatus').textContent='Stacked areas: measured GPU card and CPU package power. '+(all.some(s=>s.id.endsWith('/power/memory'))?'DRAM power is measured separately. ':'RAM power is not exposed and remains in the uncolored gap. ')+(over?'Some component samples exceed the total because readings differ in timing or coverage; values are not rescaled.':'The uncolored gap includes other hardware, PSU losses and measurement differences.');
  }
  all.forEach((series,index)=>{
   if(hidden.has(series.id)||series.power_role==='component')return;
   context.strokeStyle=color(index,series);context.lineWidth=series.kind==='fan'&&dual?2:1.5;
   context.setLineDash(series.kind==='fan'&&dual?[6,4]:series.kind==='percent'&&dual?[2,3]:[]);context.beginPath();let previous=null;
   for(const point of series.points){
    if(previous===null||point[0]-previous>data.bucket*2.5)context.moveTo(x(point[0]),pointY(series,point));
    else context.lineTo(x(point[0]),pointY(series,point));previous=point[0];
   }
   context.stroke();
   if(series.points.length===1){const point=series.points[0];context.fillStyle=color(index,series);context.fillRect(x(point[0])-3,pointY(series,point)-3,6,6)}
  });
  context.setLineDash([]);context.restore();
  if(drag){context.fillStyle='#65dcc038';context.fillRect(area.left+Math.min(drag.start,drag.end)*(area.right-area.left),area.top,Math.abs(drag.end-drag.start)*(area.right-area.left),area.bottom-area.top)}
  const tooltip=$(chart+'Tooltip');tooltip.textContent='Hover a line for its reading.';
  if(hover&&!drag){
   const time=data.start+hover.fraction*(data.end-data.start);
   context.strokeStyle='#dde9f3';context.setLineDash([4,4]);context.beginPath();context.moveTo(x(time),area.top);context.lineTo(x(time),area.bottom);context.stroke();context.setLineDash([]);
   if(hover.chart===chart){
    const nearest=active.map(series=>({series,point:series.points.reduce((a,b)=>!a||Math.abs(b[0]-time)<Math.abs(a[0]-time)?b:a,null)})).filter(v=>v.point&&Math.abs(v.point[0]-time)<data.bucket*2);
    nearest.sort((a,b)=>Math.abs(pointY(a.series,a.point)-hover.y)-Math.abs(pointY(b.series,b.point)-hover.y));
    if(nearest[0]){const {series,point}=nearest[0];tooltip.textContent=series.name+': '+valueText(series,point[1])+' · '+new Date(point[0]*1000).toLocaleString()}
    else tooltip.textContent='No sample near this time.';
   }
  }
  if(!active.some(s=>s.points.length)){context.fillStyle='#aab9c8';context.fillText('No readings in this window',area.left+12,area.top+35)}
 }
}

function fraction(event){const canvas=event.currentTarget,box=canvas.getBoundingClientRect(),area=bounds(canvas);return Math.max(0,Math.min(1,(event.clientX-box.left-area.left)/(area.right-area.left)))}
function clearZoom(){selection=null;originalView=null;$('resetZoom').hidden=true;$('range').querySelector('[value="custom"]').disabled=true}
for(const chart of CHARTS){
 const canvas=$(chart);
 canvas.onpointerdown=event=>{
  if(event.button!==0||!data)return;
  const box=canvas.getBoundingClientRect(),area=bounds(canvas),xx=event.clientX-box.left,yy=event.clientY-box.top;
  if(xx<area.left||xx>area.right||yy<area.top||yy>area.bottom)return;
  event.preventDefault();drag={chart,start:fraction(event),end:fraction(event)};hover=null;canvas.setPointerCapture(event.pointerId);draw();
 };
 canvas.onpointermove=event=>{if(drag&&drag.chart===chart)drag.end=fraction(event);else if(!drag)hover={chart,fraction:fraction(event),y:event.clientY-canvas.getBoundingClientRect().top};draw()};
 canvas.onpointerup=event=>{
  if(!drag||drag.chart!==chart)return;
  const selected=drag;selected.end=fraction(event);drag=null;canvas.releasePointerCapture(event.pointerId);
  const span=data.end-data.start,start=Math.floor(data.start+Math.min(selected.start,selected.end)*span),end=Math.floor(data.start+Math.max(selected.start,selected.end)*span);
  if(end-start<60||Math.abs(selected.end-selected.start)<.005){draw();return}
  if(!originalView)originalView={range:$('range').value,end:$('end').value,live};
  const plot=bounds(canvas),right=Math.max(selected.start,selected.end);
  const following=liveEdgeZoom(live,right,plot.right-plot.left,data.bucket||60,span);
  selection={start,end,followLive:following};live=following;hover=null;
  updateZoomLabel();load();
 };
 canvas.onpointercancel=()=>{drag=null;hover=null;draw()};
 canvas.onlostpointercapture=()=>{if(drag){drag=null;draw()}};
 canvas.onpointerleave=()=>{if(!drag){hover=null;draw()}};
}
$('resetZoom').onclick=()=>{if(!originalView)return;const view=originalView;clearZoom();$('range').value=view.range;$('end').value=view.end;live=view.live;load()};
$('range').onchange=()=>{if($('range').value!=='custom'){clearZoom();if(!$('end').value)live=true;load()}};
$('end').onchange=()=>{if(selection)$('range').value=originalView?.range||'24';clearZoom();live=false;load()};
$('live').onclick=()=>{live=true;$('end').value='';if(selection){selection.followLive=true;updateZoomLabel()}load()};
$('refresh').onclick=load;window.addEventListener('resize',draw);window.addEventListener('hal-status-font-change',draw);
$('captionToggle').onclick=()=>{const collapsed=$('captionToggle').getAttribute('aria-expanded')==='true';for(const chart of CHARTS)$(chart+'Legend').hidden=collapsed;$('captionToggle').setAttribute('aria-expanded',String(!collapsed));$('captionToggle').textContent=collapsed?'Show legends':'Hide legends'};
historyModeStatus();setInterval(historyModeStatus,5000);
HalHistoryRefresh.start({refresh:load,isLoading:()=>loadingHistory,isInteracting:()=>Boolean(drag),resetInteraction:()=>{drag=null;hover=null}});

async function fanResponse(){
 try{
  const [readings,control]=await Promise.all([fetch('/api/fan-current',{cache:'no-store'}),fetch('/api/case-fans',{cache:'no-store'})]);
  const current=await readings.json(),state=await control.json();
  if(!readings.ok||!control.ok||!state.ok)throw Error(state.error||'Fan readings unavailable');
  const rows=$('fanResponse');
  for(const [header,label] of Object.entries(physicalFanNames)){
   let row=rows.querySelector('[data-header="'+header+'"]');
   if(!row){row=document.createElement('tr');row.dataset.header=header;for(let i=0;i<3;i++)row.append(document.createElement('td'));row.cells[0].textContent=label;rows.append(row)}
   row.cells[1].textContent=header==='W_PUMP+'?'Full speed':header.startsWith('CPU_')?'CPU automatic':state.fans[header].command;
   const rpm=current.fans?.[header];row.cells[2].textContent=current.stale||rpm==null?'Unavailable':Math.round(rpm).toLocaleString()+' RPM';
  }
  $('airflowStatus').textContent='Unified OS cooling · 1-second checks · '+state.policy.reason;
  $('airflowFans').textContent=Object.entries(physicalFanNames).map(([key,label])=>label+': '+(current.fans?.[key]==null?'Unavailable':Math.round(current.fans[key]).toLocaleString()+' RPM')).join(' · ');
  $('airflowLimits').textContent='GPU package 60°C or VRAM 70°C: fronts full, rear stopped until both GPUs are below 50°C package and 60°C VRAM. CPU/radiator follow CPU heat; GPU minimum 10%; pump 100%.';
 }catch(error){$('fanIdleStatus').textContent=error.message}
}
fanResponse();setInterval(fanResponse,5000);
async function workerStatus(){try{const response=await fetch('/api/local-ai'),state=await response.json();$('localAIStatus').textContent='Local worker: '+state.worker_state;$('localAIModel').textContent=state.scope||''}catch{$('localAIStatus').textContent='Worker status unavailable'}}
workerStatus();setInterval(workerStatus,5000);

async function storageHealth(){
 const banner=document.getElementById('storageAlert');
 try{
  const response=await fetch('/api/storage-health',{cache:'no-store'}),state=await response.json();
  if(!response.ok)throw Error('Storage health unavailable');
  const critical=!state.ok;
  banner.classList.toggle('critical',critical);banner.classList.toggle('healthy',!critical);
  banner.querySelector('h2').textContent=critical?'Storage alert':'Storage complete';
  banner.querySelector('p').textContent=critical?state.alerts.join(' · '):state.detected_drives+' of '+state.expected_drives+' drives detected · All expected RAID members present';
 }catch(error){
  banner.classList.add('critical');banner.classList.remove('healthy');
  banner.querySelector('h2').textContent='Storage status unknown';
  banner.querySelector('p').textContent='Unable to verify RAID completeness. Retrying automatically.';
 }
}
storageHealth();setInterval(storageHealth,5000);
