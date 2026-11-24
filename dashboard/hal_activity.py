"""Bounded, metadata-only activity for the authenticated HAL chat service."""

from collections import deque
import json
from pathlib import Path
from threading import Lock
from time import time


class Activity:
    def __init__(self):
        self.lock = Lock()
        self.events = deque(maxlen=100)
        self.active = {}
        self.worker_state = None

    def record(self, request_id, phase, **details):
        event = dict(request=request_id, time=time(), phase=phase, **details)
        with self.lock:
            self.events.append(event)
            if phase in {'Complete', 'Failed'}:
                self.active.pop(request_id, None)
            else:
                self.active[request_id] = event

    def snapshot(self):
        worker = read_status('/var/lib/hal-worker/status.json').get('state', 'unavailable')
        with self.lock:
            if worker != self.worker_state:
                self.events.append(dict(request='worker', time=time(), source='HAL Worker', phase=worker))
                self.worker_state = worker
            active = list(self.active.values())
            events = list(self.events)
        engine = read_status('/run/hal-ollama-activity/status.json')
        if engine:
            active.extend(engine['active'])
            events.extend(engine['events'])
        return dict(active=active, events=sorted(events, key=lambda e: e['time'])[-100:],
                    engine_available=bool(engine), worker=worker)


def read_status(path):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return {}


activity = Activity()


def usage(result):
    counts = {}
    for source, target in (('prompt_eval_count', 'input'), ('eval_count', 'output')):
        value = result.get(source)
        if type(value) is int and value >= 0:
            counts[target] = value
    duration = result.get('eval_duration')
    if 'output' in counts and type(duration) is int and duration > 0:
        counts['rate'] = round(counts['output'] * 1_000_000_000 / duration, 1)
    return counts
