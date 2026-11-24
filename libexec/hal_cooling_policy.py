"""Readable cooling decisions, independent of devices and network access.

Temperatures are degrees Celsius; fan commands are percent PWM duty.
GPU workload ending is deliberately irrelevant: actual heat decides release.
"""
import math

SAMPLE_SECONDS = 1.0
GPU_FULL_C = 75  # Either GPU package or VRAM requires full fan duty.
GPU_HOT_C = 60
GPU_COOL_C = 50
VRAM_HOT_C = 70
VRAM_COOL_C = 60
REAR_STOP_DUTY = 1  # Verified on HAL: BMC rejects 0%; 1% produces 0 RPM.
CPU_CURVE = [(20,20),(45,40),(65,70),(70,100),(100,100)]
GPU_CURVE = [(40,10),(55,50),(65,65),(75,85),(80,100)]
VRAM_CURVE = [(50,10),(65,50),(75,65),(85,85),(90,100)]
CHASSIS_ZONES = {'CHA_FAN1':1,'CHA_FAN3':3,'CHA_FAN4':4,'CHA_FAN5':5}
FAN_NAMES = {'CHA_FAN1':'Rear case fan','CHA_FAN3':'Front lower',
             'CHA_FAN4':'Front top','CHA_FAN5':'Front middle'}

def interpolate(temperature, curve):
    if temperature <= curve[0][0]:
        return curve[0][1]
    for (low,low_duty),(high,high_duty) in zip(curve,curve[1:]):
        if temperature <= high:
            return low_duty+(high_duty-low_duty)*(temperature-low)/(high-low)
    return curve[-1][1]

def gpu_duty(package, vram):
    if package >= GPU_FULL_C or vram >= GPU_FULL_C:
        return 100
    required=max(interpolate(package,GPU_CURVE),interpolate(vram,VRAM_CURVE))
    return min(100,max(10,5*math.ceil(required/5)))

def curve_points(curve=CPU_CURVE, floor=0, fixed=None):
    return [{'index':index,'Temp':temperature,
             'Duty':fixed if fixed is not None else max(floor,duty)}
            for index,(temperature,duty) in enumerate(curve)]

def decide(previous_gpu_hot, cpu, gpus, floors):
    """GPU heat takes rear-fan priority; invalid sensors demand full cooling."""
    valid_cpu=isinstance(cpu,(float,int)) and 0 < cpu < 130
    valid_gpus=bool(gpus) and all(
        isinstance(g.get(name),(float,int)) and 0 < g[name] < 130
        for g in gpus for name in ('pkg_c','vram_c'))
    if not valid_cpu or not valid_gpus:
        return {'gpu_hot':True,'mode':'sensor-failsafe',
                'reason':'Temperature missing, invalid or stale: full cooling',
                'curves':{zone:curve_points(fixed=100) for zone in (0,1,3,4,5,6)}}

    radeon_hot=any(g.get('driver')=='amdgpu' and g.get('pci')=='0000:09:00.0'
                   and max(g['pkg_c'],g['vram_c'])>75 for g in gpus)
    hot_now=radeon_hot or any(g['pkg_c']>=GPU_HOT_C or g['vram_c']>=VRAM_HOT_C for g in gpus)
    both_cool=all(g['pkg_c']<GPU_COOL_C and g['vram_c']<VRAM_COOL_C for g in gpus)
    gpu_hot=hot_now or (previous_gpu_hot and not both_cool)
    curves={0:curve_points(),6:curve_points(fixed=100)}
    for header,zone in CHASSIS_ZONES.items():
        floor=floors.get(header,20)
        if gpu_hot:
            curves[zone]=curve_points(fixed=(50 if radeon_hot else REAR_STOP_DUTY) if zone==1 else 100)
        else:
            # BMC interpolates this CPU-based curve between OS samples too.
            curves[zone]=curve_points(floor=floor)
    return {'gpu_hot':gpu_hot,'mode':'radeon-boost' if radeon_hot else 'gpu-cooling' if gpu_hot else 'cpu-and-base',
            'reason':'Radeon above 75°C: fronts full, rear 50%' if radeon_hot else
                     'GPU heat/cooldown: fronts full, rear stopped' if gpu_hot else
                     'GPUs cool: chassis follows CPU temperature and selected minimums',
            'curves':curves}
