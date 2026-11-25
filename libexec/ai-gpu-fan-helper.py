#!/usr/bin/python3
"""Capability-based GPU thermal governor, minimum duty and tachometer sampler."""
from ai_cooling_policy import GPU_FULL_C
import json, math, os, pathlib, pwd, signal, socket, struct, sys, time

import ai_gpu_hardware as hardware

DEVICES = hardware.discover()
BDFS = list(DEVICES)
last_discovery = time.monotonic()
SOCK = "/run/hal-gpu-fan/control.sock"
PREFS = pathlib.Path("/var/lib/hal-gpu-fan/preferences.json")
leases, curve_state, last_error, tach = {}, {}, {}, {}
preferences = {b: "curve" for b in BDFS}
running = True


def save_preferences():
    p = PREFS.with_suffix(".tmp")
    p.write_text(json.dumps(preferences))
    p.chmod(0o600)
    p.replace(PREFS)


def refresh_devices(force=False):
    global DEVICES, last_discovery
    if not force and time.monotonic() - last_discovery < 10:
        return
    detected = hardware.discover()
    if detected != DEVICES:
        DEVICES = detected
        BDFS[:] = detected
        for state in (leases, curve_state, last_error, tach):
            for b in list(state):
                if b not in detected:
                    state.pop(b, None)
    last_discovery = time.monotonic()


def hwmon(b):
    if b not in DEVICES:
        raise ValueError("Unknown GPU")
    return DEVICES[b]["hwmon"]


def read(h, n):
    for _ in range(3):
        try:
            return int((h / n).read_bytes().strip())
        except (OSError, ValueError, TypeError, AttributeError):
            time.sleep(0.03)
    return None


def temperature(h, name):
    v = read(h, name)
    return v / 1000 if v is not None and 0 < v < 130000 else None


def measure_rpm(b, h):
    # The B65 pulse counter stops with the idle GT clock. Hold the driver's
    # read-only wake reference only across a bounded tach measurement window.
    if DEVICES[b]["driver"] != "xe":
        return read(h, "fan1_input")
    try:
        with open("/sys/kernel/debug/dri/" + b + "/forcewake_all", "rb"):
            if read(h, "fan1_input") is None:
                return None
            time.sleep(0.30)
            return read(h, "fan1_input")
    except OSError:
        return None


def sample_rpm(b, h):
    now = time.monotonic()
    old = tach.get(b)
    if old and now - old["time"] < 2:
        return old
    v = measure_rpm(b, h)
    t = {
        "time": time.monotonic(),
        "rpm": v,
        "zero_count": (old.get("zero_count", 0) + 1 if v in (0, None) else 0) if old else 0,
    }
    tach[b] = t
    return t


def status(b):
    try:
        h = hwmon(b)
        device = DEVICES[b]
        supported = device["control"] != "firmware"
        core, memory = hardware.temperatures(device)
        t = sample_rpm(b, h)
        return {
            "pci": b,
            "driver": device["driver"],
            "control_interface": device["control"],
            "label": device["label"],
            "supported": supported,
            "rpm": t["rpm"],
            "rpm_age_seconds": round(time.monotonic() - t["time"], 1),
            "rpm_warning": (
                "Zero RPM reported; checking fan feedback" if t["zero_count"] >= 3 else None
            ),
            "pkg_c": core,
            "vram_c": memory,
            "mode": (
                read(h, "pwm1_enable")
                if device["control"] == "pwm"
                else (1 if device["control"] == "rpm" else 2)
            ),
            "pwm": read(h, "pwm1"),
            "manual_seconds_left": max(0, int(leases.get(b, 0) - time.monotonic())),
            "preferred_mode": "curve" if supported else "firmware",
            "minimum_percent": 10 if supported else None,
            "curve_percent": curve_state.get(b, {}).get("percent"),
            "error": last_error.get(b)
            or (
                None
                if supported
                else "Firmware fan control: OS duty/floor cannot be enforced by this driver"
            ),
        }
    except (OSError, StopIteration) as e:
        return {
            "pci": b,
            "supported": False,
            "rpm": None,
            "pkg_c": None,
            "vram_c": None,
            "mode": None,
            "pwm": None,
            "manual_seconds_left": 0,
            "preferred_mode": "curve",
            "minimum_percent": 10,
            "error": "GPU sensor/driver unavailable: " + str(e),
        }


def curve_percent(pkg, vram):
    if pkg >= GPU_FULL_C or vram >= GPU_FULL_C:
        return 100

    def interpolate(t, points):
        if t <= points[0][0]:
            return points[0][1]
        for (a, x), (b, y) in zip(points, points[1:]):
            if t <= b:
                return x + (y - x) * (t - a) / (b - a)
        return points[-1][1]

    return min(
        100,
        max(
            10,
            5
            * math.ceil(
                max(
                    interpolate(pkg, [(40, 10), (55, 50), (65, 65), (75, 85), (80, 100)]),
                    interpolate(vram, [(50, 10), (65, 50), (75, 65), (85, 85), (90, 100)]),
                )
                / 5
            ),
        ),
    )


def full_speed(b):
    hardware.apply(DEVICES[b], 100)
    leases.pop(b, None)


def apply(b, percent):
    hardware.apply(DEVICES[b], percent)


def guard():
    refresh_devices()
    for b in BDFS:
        try:
            s = status(b)
            if not s["supported"]:
                last_error[b] = (
                    "Firmware fan control: OS duty/floor cannot be enforced by this driver"
                )
                leases.pop(b, None)
                continue
            if s["pkg_c"] is None or s["vram_c"] is None:
                raise RuntimeError("Temperature unavailable or invalid")
            now = time.monotonic()
            previous = curve_state.get(b, {})
            count = tach[b]["zero_count"]
            if count >= 6:
                raise RuntimeError("Fan RPM feedback unavailable: full-speed failsafe")
            manual_active = (
                b in leases and now < leases[b] and max(s["pkg_c"], s["vram_c"]) < GPU_FULL_C
            )
            if manual_active:
                percent = max(10, previous.get("manual_percent", 10))
            else:
                leases.pop(b, None)
                percent = curve_percent(s["pkg_c"], s["vram_c"])
            if count >= 3:
                percent = max(30, percent)
                last_error[b] = "Zero RPM: duty reapplied with spin-up boost"
            else:
                last_error.pop(b, None)
            temperature_now = max(s["pkg_c"], s["vram_c"] - 10)
            old = previous.get("percent", 0)
            if (
                not manual_active
                and s["mode"] == 1
                and s["pwm"] is not None
                and s["pwm"] >= 26
                and count < 3
                and percent < old
                and (
                    now - previous.get("changed", 0) < 30
                    or temperature_now > previous.get("temperature", temperature_now) - 3
                )
            ):
                continue
            target = math.ceil(percent * 255 / 100)
            if (
                percent != old
                or s["mode"] != 1
                or s["pwm"] is None
                or abs(s["pwm"] - target) > 3
                or s["pwm"] < 26
                or count >= 3
            ):
                apply(b, percent)
                curve_state[b] = {
                    **previous,
                    "percent": percent,
                    "temperature": temperature_now,
                    "changed": now,
                }
        except Exception as e:
            last_error[b] = str(e)
            try:
                full_speed(b)
            except Exception as failure:
                last_error[b] += "; " + str(failure)


def manual(b, percent):
    if type(percent) is not int or percent not in range(10, 101, 10):
        raise ValueError("Choose 10% to 100% in 10% steps")
    s = status(b)
    if not s["supported"] or s["pkg_c"] is None or s["vram_c"] is None:
        raise RuntimeError("GPU temperature/control unavailable")
    if max(s["pkg_c"], s["vram_c"]) >= GPU_FULL_C:
        raise ValueError("At75 C or above automatic control requires full speed")
    if max(s["pkg_c"], s["vram_c"]) >= 60 and percent < 50:
        raise ValueError("At 60 C or above, choose at least 50% or automatic")
    try:
        apply(b, percent)
        leases[b] = time.monotonic() + 300
        curve_state[b] = {
            "percent": percent,
            "manual_percent": percent,
            "changed": time.monotonic(),
            "temperature": max(s["pkg_c"], s["vram_c"] - 10),
        }
        last_error.pop(b, None)
    except BaseException:
        full_speed(b)
        raise


def automatic(b):
    leases.pop(b, None)
    curve_state.pop(b, None)
    preferences[b] = "curve"
    save_preferences()
    guard()
    if last_error.get(b):
        raise RuntimeError(last_error[b])


def stop(*args):
    global running
    running = False


def main():
    global running
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    # Both legacy automatic choices now enforce the same minimum-duty policy.
    save_preferences()
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        for b in BDFS:
            try:
                full_speed(b)
            except Exception as e:
                last_error[b] = str(e)
        if os.path.lexists(SOCK):
            os.unlink(SOCK)
        server.bind(SOCK)
        os.chmod(SOCK, 0o660)
        server.listen(8)
        server.settimeout(1)
        while running:
            guard()
            try:
                conn, _ = server.accept()
            except socket.timeout:
                continue
            with conn:
                conn.settimeout(2)
                try:
                    _, uid, _ = struct.unpack(
                        "3i", conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
                    )
                    if uid not in (0, pwd.getpwnam("hal-monitor").pw_uid):
                        raise PermissionError("Client not allowed")
                    data = b""
                    while b"\n" not in data and len(data) < 1025:
                        chunk = conn.recv(1025 - len(data))
                        if not chunk:
                            break
                        data += chunk
                    if len(data) > 1024:
                        raise ValueError("Request too large")
                    request = json.loads(data)
                    action = request.get("action")
                    if action == "status":
                        result = {
                            "gpus": [status(b) for b in BDFS],
                            "kernel": os.uname().release,
                            "manual_limit_seconds": 300,
                            "minimum_percent": 10,
                        }
                    elif action in ("auto", "curve", "manual"):
                        b = request.get("pci")
                        if b not in BDFS:
                            raise ValueError("Unknown GPU")
                        if action == "manual":
                            manual(b, request.get("percent"))
                        else:
                            automatic(b)
                        result = {
                            "gpu": status(b),
                            "message": (
                                "Manual duty applied (10% minimum, 5 minute limit)"
                                if action == "manual"
                                else "Temperature automatic enabled (10% minimum; fan stop disabled)"
                            ),
                        }
                    else:
                        raise ValueError("Unknown action")
                    conn.sendall(json.dumps({"ok": True, **result}).encode() + b"\n")
                except Exception as e:
                    try:
                        conn.sendall(json.dumps({"ok": False, "error": str(e)}).encode() + b"\n")
                    except OSError:
                        pass
    finally:
        for b in BDFS:
            try:
                full_speed(b)
            except Exception as e:
                print("Full-speed failsafe failed", b, e, flush=True)
        server.close()
        if os.path.lexists(SOCK):
            os.unlink(SOCK)


if __name__ == "__main__":
    if sys.argv[1:] == ["--failsafe-only"]:
        failures = []
        for b in BDFS:
            try:
                full_speed(b)
            except Exception as e:
                failures.append(b + ": " + str(e))
        if failures:
            raise RuntimeError("; ".join(failures))
    else:
        main()
