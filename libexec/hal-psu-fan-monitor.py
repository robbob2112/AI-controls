"""Passive, provisional report-6 monitor; never sends PSU commands."""
import datetime
import json
import os
import pathlib
import select
import time

destination = pathlib.Path('/run/hal-psu-fan/current.json')

def save(data):
    temporary = destination.with_suffix('.tmp')
    temporary.write_text(json.dumps(data))
    temporary.chmod(0o644)
    temporary.replace(destination)

while True:
    try:
        candidates = [p for p in pathlib.Path('/sys/class/hidraw').glob('hidraw*')
                      if 'HID_ID=0003:00002516:000001A5' in (p/'device/uevent').read_text().upper()]
        if len(candidates) != 1:
            raise RuntimeError('Expected exactly one X Silent MAX 1300 HID device')
        fd = os.open('/dev/'+candidates[0].name, os.O_RDONLY | os.O_NONBLOCK)
        try:
            last = time.monotonic()
            while True:
                if select.select([fd], [], [], 2)[0]:
                    packet = os.read(fd, 4096)
                    if not packet:
                        raise RuntimeError('PSU HID disconnected')
                    if len(packet) == 3 and packet[0] == 6:
                        save({'ok': True, 'time': time.time(), 'provisional_rpm': int.from_bytes(packet[1:], 'little'),
                              'raw_hex': packet.hex(' '), 'verified': False,
                              'interpretation': 'USB report 06 interpreted as unsigned little-endian RPM; unverified'})
                        last = time.monotonic()
                if time.monotonic()-last > 10:
                    raise RuntimeError('No report 06 received for 10 seconds')
        finally:
            os.close(fd)
    except (OSError, RuntimeError) as error:
        save({'ok': False, 'time': time.time(), 'verified': False, 'error': str(error)})
        time.sleep(3)
