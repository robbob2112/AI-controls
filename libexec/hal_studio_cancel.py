"""Cancellation tokens and process groups for isolated workspace commands."""

import os
import signal
import subprocess
import threading
import time

CONTEXT = threading.local()
LOCK = threading.Lock()
PROCESSES = {}
CANCELLED = {}


def check():
    turn = getattr(CONTEXT, "turn_id", "")
    with LOCK:
        if turn and turn in CANCELLED:
            raise ValueError("Conversation stopped by user")


def register(process):
    turn = getattr(CONTEXT, "turn_id", "")
    with LOCK:
        if turn:
            if turn in CANCELLED:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
                raise ValueError("Conversation stopped by user")
            PROCESSES.setdefault(turn, set()).add(process)


def unregister(process):
    turn = getattr(CONTEXT, "turn_id", "")
    with LOCK:
        if turn in PROCESSES:
            PROCESSES[turn].discard(process)
            if not PROCESSES[turn]:
                PROCESSES.pop(turn)


def cancel(turn):
    if not isinstance(turn, str) or not turn or len(turn) > 200:
        raise ValueError("Invalid turn ID")
    with LOCK:
        now = time.monotonic()
        for old, when in list(CANCELLED.items()):
            if now - when > 3600:
                CANCELLED.pop(old)
        CANCELLED[turn] = now
        processes = list(PROCESSES.get(turn, ()))
    for process in processes:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    return {"cancelled": True, "process_groups": len(processes)}


def run(args, cwd=None, env=None, timeout=120):
    check()
    process = subprocess.Popen(
        args,
        cwd=cwd,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    register(process)
    try:
        try:
            stdout, stderr = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait()
            raise ValueError("Command timed out")
        check()
        return subprocess.CompletedProcess(args, process.returncode, stdout, stderr)
    finally:
        unregister(process)
