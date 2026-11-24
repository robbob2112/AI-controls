"""Read NVMe SMART temperatures on kernels with or without NVMe hwmon."""
import json
import math
import pathlib
import subprocess


EXPECTED_SERIALS = {
    'S7YCNJ0L504674Z', 'S818NT0L701880R',
    'S818NT0L701891X', '23162P800478',
}


def temperature_celsius(value):
    value = float(value)
    if value > 200:
        value -= 273.15
    if not math.isfinite(value) or not -10 <= value <= 125:
        raise ValueError('Invalid NVMe temperature')
    return value


def read_temperatures():
    readings = {}
    for device in pathlib.Path('/sys/class/nvme').glob('nvme*'):
        serial = (device / 'serial').read_text().strip()
        if serial not in EXPECTED_SERIALS:
            continue
        result = subprocess.check_output(
            ['nvme', 'smart-log', '/dev/' + device.name, '-o', 'json'],
            text=True, timeout=2,
        )
        readings[serial] = temperature_celsius(json.loads(result)['temperature'])
    if readings.keys() != EXPECTED_SERIALS:
        raise ValueError('Required NVMe temperature missing')
    return readings
