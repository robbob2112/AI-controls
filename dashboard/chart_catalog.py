"""Display categories are explicit; raw channels never imply physical fans."""
import json
import pathlib

FANS={'CPU_FAN':'Radiator','CPU_OPT':'CPU cooler','CHA_FAN1':'Rear',
      'CHA_FAN3':'Front lower','CHA_FAN4':'Front top','CHA_FAN5':'Front middle','W_PUMP+':'Pump'}

def inventory():
    try:return json.loads(pathlib.Path('/var/lib/hal-monitor/storage-inventory.json').read_text())
    except (OSError,ValueError):return {'time':None,'drives':[]}

def series(identifier,kind,points,sensor_name,storage):
    if kind=='percent':points=[p for p in points if 0<=p[1]<=100]
    if kind=='temperature':
        points=[p for p in points if (-20<=p[1]<=150 if identifier.startswith('/smart/') else 0<p[1]<130)]
    if not points:return None
    name=sensor_name(identifier)
    chart=None
    if identifier.startswith('/hal-metrics/'):
        metric=identifier.removeprefix('/hal-metrics/')
        names={'system/cpu_used':('system','CPU'), 'system/ram_used':('system','RAM'),
               'b65-0/gpu_busy':('b650','GPU busy'), 'b65-1/gpu_busy':('b651','GPU busy'),
               'b65-0/vram_used':('b650','VRAM used'), 'b65-1/vram_used':('b651','VRAM used'),
               'power/b65-0':('psu','B65-0'), 'power/b65-1':('psu','B65-1'),
               'r9700/gpu_busy':('r9700','GPU busy'), 'r9700/vram_used':('r9700','VRAM used'),
               'power/r9700':('psu','R9700'), 'power/cpu':('psu','CPU package'), 'power/memory':('psu','DRAM')}
        if metric not in names:return None
        chart,short=names[metric];name=short+(' · compute-engine busy, not execution-unit occupancy' if metric.startswith('b65-') and metric.endswith('/gpu_busy') else '')
    elif kind=='power' and identifier=='/sys/class/hwmon/usb-2516_01a5/coolermaster_psu/input':
        chart='psu';short='AC input';name='PSU total AC input power'
    elif identifier.startswith('/smart/'):
        device=next((d for d in storage['drives'] if d.get('id')==identifier),None)
        if device is None:return None
        chart='nvme' if device['category']=='nvme' else 'drives'
        short=device['label']
        name=device['device']+' · '+device['model']+' · '+device['serial']
    elif identifier.startswith('/sys/class/hwmon/pci-0000_09_00_0/amdgpu/'):
        chart='r9700'
        channel=identifier.rsplit('/',1)[-1]
        short={'edge':'Edge','junction':'Hotspot','mem':'VRAM','fan1':'Fan'}.get(channel)
        if short is None or kind!=('fan' if channel=='fan1' else 'temperature'):return None
    elif '/xe/' in identifier:
        # PCI identities are stable. Legacy mappings are explicitly known from
        # the prior collector, never inferred from today's hwmon numbering.
        if 'pci-0000_04_00_0' in identifier or 'PCI 0000:04:00.0' in name:
            chart='b650'
        elif 'pci-0000_09_00_0' in identifier or 'PCI 0000:09:00.0' in name:
            chart='b651'
        else:return None
        channel=identifier.rsplit('/',1)[-1]
        short={'pkg':'Package','vram':'VRAM','fan1':'Fan'}.get(channel)
        if short is None:return None
        if (short=='Fan')!=(kind=='fan'):return None
    elif '/coretemp/' in identifier and kind=='temperature':
        chart='cpu'
        short=identifier.rsplit('/',1)[-1].replace('Package id 0','Package').replace('Core ','C')
    elif '/bmc/ipmi/' in identifier:
        channel=identifier.rsplit('/',1)[-1]
        if kind=='fan':
            if channel not in FANS:return None
            chart='fan';short=FANS[channel]
        elif channel!='CPU Package Temp' and not channel.startswith('PSU'):
            chart='motherboard'
            short=channel.replace('_Temp','').replace(' Temperature','').replace(' Temp','')
        else:return None
    elif '/nct6799/' in identifier and kind=='temperature':
        channel=identifier.rsplit('/',1)[-1]
        short={'SYSTIN':'System','CPUTIN':'CPU socket','PCH_CHIP_TEMP':'Chipset'}.get(channel)
        if short is None:return None
        chart='motherboard'
    else:
        # Hide unmapped Super I/O fans, stale Radeon channels, PSU duplicates,
        # unverified motherboard inputs and enumeration-dependent NVMe aliases.
        return None
    return {'id':identifier,'name':short,'full_name':name,'kind':kind,
            'power_role':'component' if identifier.startswith('/hal-metrics/power/') else 'total' if chart=='psu' else None,
            'chart':chart,'unit':'RPM' if kind=='fan' else 'W' if kind=='power' else '%' if kind=='percent' else '°C','points':points}
