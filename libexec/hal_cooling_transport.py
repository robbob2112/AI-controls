"""Motherboard cooling transport: HTTPS with an internal KCS fallback.

Local success means a fresh CPU sensor response and acknowledged curve writes.
It is deliberately not described as independent curve readback.
"""
import subprocess
import time

OWNED_ZONES = (6, 0, 3, 4, 5, 1)


def validate_targets(targets):
    if set(targets) != set(OWNED_ZONES):
        raise ValueError('Expected exactly the six owned cooling zones')
    for zone, points in targets.items():
        if len(points) != 5:
            raise ValueError('Expected five temperature/duty pairs')
        for point in points:
            temp, duty = point['Temp'], point['Duty']
            if type(temp) is not int or type(duty) is not int:
                raise ValueError('Cooling curve values must be integers')
            if not 0 <= temp <= 100 or not 1 <= duty <= 100:
                raise ValueError('Cooling curve outside valid range')
            if zone == 6 and duty != 100:
                raise ValueError('Pump must remain at full speed')


class LocalIPMI:
    def __init__(self, run=subprocess.run):
        self.run = run

    def raw(self, *values):
        result = self.run(
            ['/usr/bin/ipmitool', '-I', 'open', 'raw'] + [hex(v) for v in values],
            check=True, capture_output=True, text=True, timeout=4)
        return result.stdout.strip()

    def apply(self, targets):
        validate_targets(targets)
        # CPU Package Temp is sensor 0x01 on this verified HAL board.
        # Read Sensor Reading: bit 5 of byte 1 marks an unavailable reading.
        reading = [int(value, 16) for value in self.raw(0x04, 0x2d, 0x01).split()]
        if len(reading) < 3 or reading[1] & 0x20 or not 0 < reading[0] < 130:
            raise RuntimeError('Local BMC CPU sensor is unavailable or invalid')
        for zone in OWNED_ZONES:
            pairs = [v for p in targets[zone] for v in (p['Temp'], p['Duty'])]
            self.raw(0x30, 0x0e, 0x04, zone, *pairs)
        return {'transport': 'local-ipmi',
                'verification': 'Fresh BMC CPU sensor and acknowledged curve writes',
                'bmc_cpu_c': reading[0]}


class CoolingTransport:
    def __init__(self, network_apply, local=None, clock=time.monotonic):
        self.network_apply = network_apply
        self.local = local or LocalIPMI()
        self.clock = clock
        self.retry_network_at = 0
        self.network_error = None

    def apply(self, targets):
        validate_targets(targets)
        if self.clock() >= self.retry_network_at:
            try:
                self.network_apply(targets)
                self.network_error = None
                self.retry_network_at = 0
                return {'transport': 'https', 'verification': 'BMC curve readback',
                        'network_error': None}
            except Exception as error:
                self.network_error = str(error)
                self.retry_network_at = self.clock() + 30
        try:
            result = self.local.apply(targets)
            result['network_error'] = self.network_error
            return result
        except Exception as error:
            raise RuntimeError('BMC network unavailable: %s; local IPMI failed: %s' %
                               (self.network_error, error)) from error
