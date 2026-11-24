"""Discover PCI GPUs and use the fan interfaces their current drivers expose."""
import pathlib,stat,subprocess,time
PCI_ROOT=pathlib.Path('/sys/bus/pci/devices')

def read(path):
 try:return int(path.read_text().strip())
 except (OSError,ValueError):return None

def writable(path):
 try:return bool(path.stat().st_mode & stat.S_IWUSR)
 except OSError:return False

def discover(root=None):
 root=pathlib.Path(root) if root is not None else PCI_ROOT;devices={}
 for p in sorted(root.glob('*')):
  try:
   if not (p/'class').read_text().strip().startswith('0x03'):continue
   hwmons=list((p/'hwmon').glob('hwmon*'))
   h=next((h for h in hwmons if any(h.glob('fan*_input')) or any(h.glob('pwm*'))),None)
   if h is None:continue  # Integrated/BMC display devices without GPU fan sensors.
   driver=(p/'driver').resolve().name
   temperatures={}
   for label in h.glob('temp*_label'):
    temperatures[label.name.replace('_label','_input')]=label.read_text().strip().lower()
   core=[n for n,label in temperatures.items() if label in ('pkg','package','core','edge','junction','hotspot','gpu')]
   memory=[n for n,label in temperatures.items() if label in ('vram','mem','memory')]
   control='firmware'
   if writable(h/'pwm1') and writable(h/'pwm1_enable'):control='pwm'
   elif writable(h/'fan1_target') and writable(h/'fan1_enable') and read(h/'fan1_enable') is not None and (read(h/'fan1_max') or 0)>0:control='rpm'
   devices[p.name]={'pci':p.name,'driver':driver,'hwmon':h,'core_inputs':core,'memory_inputs':memory,'control':control,'rpm_max':read(h/'fan1_max'),'label':driver+' GPU '+p.name,'vendor':(p/'vendor').read_text().strip(),'device':(p/'device').read_text().strip()}
  except (OSError,StopIteration):continue
 return devices

def temperatures(device):
 def maximum(names):
  values=[read(device['hwmon']/name) for name in names]
  return max(v for v in values if v is not None and 0<v<130000)/1000 if any(v is not None and 0<v<130000 for v in values) else None
 return maximum(device['core_inputs']),maximum(device['memory_inputs'])

def apply(device,percent):
 h=device['hwmon'];percent=max(10,min(100,percent))
 if device['control']=='pwm':
  target=(percent*255+99)//100;(h/'pwm1_enable').write_text('1\n');(h/'pwm1').write_text(str(int(target))+'\n')
  actual=read(h/'pwm1')
  if read(h/'pwm1_enable')!=1 or actual is None or actual<26 or abs(actual-target)>3:raise RuntimeError('PWM duty readback failed')
 elif device['control']=='rpm':
  target=int((percent*device['rpm_max']+99)//100);(h/'fan1_enable').write_text('1\n');(h/'fan1_target').write_text(str(target)+'\n')
  if read(h/'fan1_target')!=target:raise RuntimeError('Fan RPM target readback failed')
 else:raise RuntimeError('Driver provides monitoring only; firmware controls this GPU fan')
