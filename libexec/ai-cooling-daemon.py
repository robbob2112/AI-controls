from pathlib import Path

#!/usr/bin/python3
"""HAL's sole OS cooling owner.

One PID owns GPU PWM, the BMC thermal curves, and both compatibility sockets.
The one-second sensor/governor thread never waits for the BMC network thread.
GPU register/RPM handling is reused from the already validated B65 library.
Configuration and pure thermal decisions live in ai_cooling_policy.py.
"""
import grp
import importlib.util
import json
import os
import pathlib
import pwd
import signal
import socket
import struct
import subprocess
import sys
import threading
import time
from ai_bmc_fan_api import BMC
from ai_cooling_transport import CoolingTransport
from ai_cooling_policy import SAMPLE_SECONDS, CHASSIS_ZONES, FAN_NAMES, decide, gpu_duty

spec = importlib.util.spec_from_file_location(
    "gpu_hardware", "/usr/local/libexec/ai-gpu-fan-helper.py"
)
gpu = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gpu)  # Its standalone main is not run.
gpu.curve_percent = gpu_duty

STOP = threading.Event()
CHANGED = threading.Event()
LOCK = threading.RLock()
GPU_LOCK = threading.RLock()
SETTINGS = pathlib.Path("/var/lib/hal-case-fans/settings.json")
STATUS = pathlib.Path("/run/hal-cooling/status.json")
floors = {name: 20 for name in CHASSIS_ZONES}
snapshot = {}
applied = {}
desired = {}
policy = {}
bmc_error = "Waiting for first BMC readback"
last_bmc_ok = 0
last_sample = 0
sample_count = 0
max_interval = 0
settings_generation = 0
applied_generation = -1
sample_failed = False
gpu_hot = False
control_protection = False
bmc_transport = "starting"
bmc_verification = None
bmc_network_error = None


def load_settings():
    if SETTINGS.exists():
        saved = json.loads(SETTINGS.read_text())
        for header, value in saved.get("floors", {}).items():
            if header in floors and type(value) is int and value in range(10, 101, 10):
                floors[header] = value


def save_settings():
    old = json.loads(SETTINGS.read_text()) if SETTINGS.exists() else {}
    old["floors"] = dict(floors)
    temporary = SETTINGS.with_suffix(".tmp")
    temporary.write_text(json.dumps(old, indent=2))
    temporary.chmod(0o600)
    temporary.replace(SETTINGS)


def cpu_temperature():
    for device in pathlib.Path("/sys/class/hwmon").glob("hwmon*"):
        try:
            if (device / "name").read_text().strip() == "coretemp":
                value = int((device / "temp1_input").read_text()) / 1000
                return value if 0 < value < 130 else None
        except (OSError, ValueError):
            continue
    return None


def same_curve(left, right):
    return [(p["Temp"], p["Duty"]) for p in left] == [(p["Temp"], p["Duty"]) for p in right]


def put_zone(client, zone, points):
    """Only the six explicitly owned zones; zero is never sent to this BMC."""
    if zone not in (0, 1, 3, 4, 5, 6) or len(points) != 5:
        raise ValueError("Unowned cooling zone")
    if any(not 0 <= p["Temp"] <= 100 or not 1 <= p["Duty"] <= 100 for p in points):
        raise ValueError("Invalid cooling curve")
    if zone == 6 and any(p["Duty"] != 100 for p in points):
        raise ValueError("Pump must always be 100 percent")
    flat = [value for p in points for value in (p["Temp"], p["Duty"])]
    client.request(
        "/api/fanctrl/PWM",
        "PUT",
        {"PWMIndex": zone, "PWMSrc": 0, "PWMNum": zone, "CurrentPWMdata": flat},
    )


def public_state():
    with LOCK:
        fresh = last_sample and time.monotonic() - last_sample < 3
        result = {
            "ok": bool(applied) and not bmc_error,
            "time": time.time(),
            "transport": "Unified OS cooling daemon via BMC",
            "bmc_transport": bmc_transport,
            "bmc_verification": bmc_verification,
            "bmc_network_error": bmc_network_error,
            "control_protection": control_protection,
            "sample_interval_seconds": SAMPLE_SECONDS,
            "sample_count": sample_count,
            "max_sample_interval_seconds": round(max_interval, 3),
            "sensor_age_seconds": round(time.monotonic() - last_sample, 3) if last_sample else None,
            "sensors_fresh": bool(fresh),
            "temperatures": snapshot,
            "policy": {k: v for k, v in policy.items() if k != "curves"},
            "cpu_automatic": policy.get("mode") != "sensor-failsafe",
            "benchmark_rear_supported": True,
            "pump_percent": 100,
            "error": bmc_error,
            "fans": {},
            "gpu_controls_ok": all(g.get("supported", False) for g in snapshot.get("gpus", [])),
            "gpu_control_warnings": [
                {"pci": g.get("pci"), "error": g.get("error")}
                for g in snapshot.get("gpus", [])
                if not g.get("supported", False) or g.get("error")
            ],
            "last_bmc_readback": last_bmc_ok,
            "pending": settings_generation > applied_generation,
        }
        for header, zone in CHASSIS_ZONES.items():
            points = applied.get(zone, [])
            result["fans"][header] = {
                "base_percent": floors[header],
                "desired_percent": floors[header],
                "curve": points,
                "os_configured": True,
                "applied": bool(points) and zone in desired and same_curve(points, desired[zone]),
                "policy_override": policy.get("gpu_hot", False),
                "command": (
                    str(policy["benchmark_rear_percent"]) + "% benchmark duty"
                    if zone == 1 and "benchmark_rear_percent" in policy
                    else (
                        "50% duty"
                        if zone == 1 and policy.get("mode") == "radeon-boost"
                        else (
                            "Stopped (1% duty)"
                            if zone == 1 and policy.get("mode") == "gpu-cooling"
                            else (
                                "Full speed"
                                if policy.get("mode")
                                in (
                                    "gpu-cooling",
                                    "radeon-boost",
                                    "benchmark-cooldown",
                                    "sensor-failsafe",
                                )
                                else "CPU thermal ramp"
                            )
                        )
                    )
                ),
                "name": FAN_NAMES[header],
            }
        return result


def publish_status():
    temporary = STATUS.with_suffix(".tmp")
    temporary.write_text(json.dumps(public_state(), indent=2))
    temporary.chmod(0o644)
    temporary.replace(STATUS)


def sensor_loop():
    global last_sample, sample_count, max_interval, snapshot, desired, policy, gpu_hot, sample_failed
    deadline = time.monotonic()
    while not STOP.is_set():
        started = time.monotonic()
        try:
            with GPU_LOCK:
                if control_protection:
                    for b in gpu.BDFS:
                        try:
                            gpu.full_speed(b)
                        except Exception as error:
                            gpu.last_error[b] = str(error)
                else:
                    gpu.guard()
                readings = [gpu.status(b) for b in gpu.BDFS]
            cpu = cpu_temperature()
            with LOCK:
                if last_sample:
                    max_interval = max(max_interval, started - last_sample)
                last_sample = started
                sample_count += 1
                snapshot = {
                    "cpu_c": cpu,
                    "gpus": [
                        {
                            k: g.get(k)
                            for k in (
                                "pci",
                                "driver",
                                "control_interface",
                                "label",
                                "supported",
                                "error",
                                "pkg_c",
                                "vram_c",
                                "rpm",
                                "pwm",
                                "mode",
                            )
                        }
                        for g in readings
                    ],
                }
                policy = decide(gpu_hot, cpu, readings, floors)
                gpu_hot = policy["gpu_hot"]
                try:
                    cooldown = float(Path("/run/hal-cooling/benchmark-cooldown").read_text())
                except (OSError, ValueError):
                    cooldown = 0
                if 0 < cooldown - time.monotonic() <= 1805 and policy["mode"] != "sensor-failsafe":
                    policy.update(
                        mode="benchmark-cooldown",
                        reason="Radeon benchmark cooldown: all case fans full",
                        gpu_hot=True,
                    )
                    for zone in CHASSIS_ZONES.values():
                        policy["curves"][zone] = [
                            dict(point, Duty=100) for point in policy["curves"][zone]
                        ]
                if (
                    policy["mode"] not in ("sensor-failsafe", "benchmark-cooldown")
                    and not control_protection
                ):
                    try:
                        rear = json.loads(Path("/run/hal-cooling/benchmark-rear.json").read_text())
                        requested = rear["duty"]
                        if (
                            type(requested) is int
                            and 1 <= requested <= 100
                            and 0 < rear["expires"] - time.monotonic() <= 4100
                        ):
                            policy["curves"][1] = [
                                dict(point, Duty=requested) for point in policy["curves"][1]
                            ]
                            policy["benchmark_rear_percent"] = requested
                    except (OSError, ValueError, TypeError, KeyError):
                        pass
                sample_failed = False
                if desired != policy["curves"]:
                    desired = policy["curves"]
                    CHANGED.set()
            publish_status()
        except Exception as error:
            print("Sensor loop error:", error, flush=True)
            with LOCK:
                sample_failed = True
                policy = decide(True, None, [], floors)
                desired = policy["curves"]
                CHANGED.set()
            with GPU_LOCK:
                for b in gpu.BDFS:
                    try:
                        gpu.full_speed(b)
                    except Exception as failure:
                        print("GPU failsafe:", failure, flush=True)
        deadline += SAMPLE_SECONDS
        if deadline < time.monotonic():
            deadline = time.monotonic()
        STOP.wait(max(0, deadline - time.monotonic()))


def apply_network_curves(targets):
    """Write and independently read back the network BMC curves."""
    with BMC(timeout=2) as client:
        actual = client.curves()
        for zone in (6, 0, 3, 4, 5, 1):
            if not same_curve(actual[zone]["CurrentPWMdata"], targets[zone]):
                put_zone(client, zone, targets[zone])
        if client.request("/api/fanctrl/mode")["FanMode"] != 4:
            put_zone(client, 0, targets[0])
        actual = client.curves()
        for zone, points in targets.items():
            if not same_curve(actual[zone]["CurrentPWMdata"], points):
                raise RuntimeError("Cooling-zone readback mismatch: " + str(zone))


def bmc_loop():
    """Keep one policy owner, with network and internal-IPMI transports."""
    global applied, bmc_error, last_bmc_ok, applied_generation, control_protection
    global bmc_transport, bmc_verification, bmc_network_error
    transport = CoolingTransport(apply_network_curves)
    while not STOP.is_set():
        CHANGED.wait(2)
        CHANGED.clear()
        with LOCK:
            if not desired:
                continue
            stale = not last_sample or time.monotonic() - last_sample > 3 or sample_failed
            targets = decide(True, None, [], floors)["curves"] if stale else dict(desired)
            generation = settings_generation
            need_check = (
                bool(bmc_error)
                or time.time() - last_bmc_ok > 15
                or any(
                    zone not in applied or not same_curve(applied[zone], points)
                    for zone, points in targets.items()
                )
            )
        if not need_check:
            continue
        try:
            result = transport.apply(targets)
            with LOCK:
                previous = bmc_transport
                applied = targets
                bmc_error = None
                last_bmc_ok = time.time()
                applied_generation = generation
                bmc_transport = result["transport"]
                bmc_verification = result["verification"]
                bmc_network_error = result.get("network_error")
                control_protection = False
            if previous != bmc_transport:
                print("Cooling transport:", bmc_transport, "-", bmc_verification, flush=True)
        except Exception as error:
            with LOCK:
                bmc_error = str(error)
                bmc_transport = "protection"
                bmc_verification = None
                bmc_network_error = transport.network_error
                control_protection = True
            print("Both BMC paths failed; protection active:", error, flush=True)
            with GPU_LOCK:
                for b in gpu.BDFS:
                    try:
                        gpu.full_speed(b)
                    except Exception as failure:
                        print("GPU protection:", failure, flush=True)
            # Best effort: an unresponsive controller cannot guarantee fan actuation.
            local_bmc_full_speed()
            STOP.wait(2)


def gpu_request(request):
    with GPU_LOCK:
        action = request.get("action")
        if action == "status":
            return {
                "ok": True,
                "gpus": [gpu.status(b) for b in gpu.BDFS],
                "kernel": os.uname().release,
                "manual_limit_seconds": 300,
                "minimum_percent": 10,
            }
        if action not in ("auto", "curve", "manual") or request.get("pci") not in gpu.BDFS:
            raise ValueError("Invalid GPU command")
        b = request["pci"]
        if action == "manual":
            gpu.manual(b, request.get("percent"))
        else:
            gpu.automatic(b)
        return {
            "ok": True,
            "gpu": gpu.status(b),
            "message": (
                "Manual override (10% minimum, five minutes)"
                if action == "manual"
                else "OS temperature curve active; minimum 10%"
            ),
        }


def case_request(request):
    global settings_generation
    action = request.get("action")
    if action == "get":
        return public_state()
    if action != "set" or set(request) != {"action", "header", "percent"}:
        raise ValueError("Invalid chassis command")
    name = request["header"]
    percent = request["percent"]
    if name not in floors or type(percent) is not int or percent not in range(10, 101, 10):
        raise ValueError("Only connected chassis fans, 10–100% in steps of 10")
    with LOCK:
        floors[name] = percent
        save_settings()
        settings_generation += 1
    CHANGED.set()
    # This saves a base preference, not an override of the temperature policy.
    result = public_state()
    result["ok"] = True
    result["message"] = "Base preference saved; thermal policy has priority"
    return result


def socket_loop(path, handle):
    pathlib.Path(path).unlink(missing_ok=True)
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
        server.bind(path)
        os.chown(path, 0, grp.getgrnam("hal-monitor").gr_gid)
        os.chmod(path, 0o660)
        server.listen(8)
        server.settimeout(1)
        while not STOP.is_set():
            try:
                connection, _ = server.accept()
            except socket.timeout:
                continue
            with connection:
                try:
                    _, uid, _ = struct.unpack(
                        "3i", connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
                    )
                    if uid not in (0, pwd.getpwnam("hal-monitor").pw_uid):
                        raise PermissionError("Local caller not authorized")
                    connection.settimeout(2)
                    raw = b""
                    while b"\n" not in raw and len(raw) <= 1024:
                        part = connection.recv(1024)
                        if not part:
                            break
                        raw += part
                    if len(raw) > 1024:
                        raise ValueError("Request too large")
                    result = handle(json.loads(raw))
                except Exception as error:
                    result = {"ok": False, "error": str(error)}
                try:
                    connection.sendall(json.dumps(result).encode() + b"\n")
                except OSError:
                    pass
    pathlib.Path(path).unlink(missing_ok=True)


def local_bmc_full_speed():
    """Known ASUS global full-speed command if the BMC web API fails."""
    try:
        subprocess.run(
            ["/usr/bin/ipmitool", "raw", "0x30", "0x0e", "0x03"],
            check=True,
            timeout=5,
            capture_output=True,
        )
    except (OSError, subprocess.SubprocessError) as error:
        print("Local IPMI full-speed fallback unavailable:", error, flush=True)


def failsafe():
    """Stop/failure path: GPUs full, BMC fans full; no fan is left stopped."""
    with GPU_LOCK:
        for b in gpu.BDFS:
            try:
                gpu.full_speed(b)
            except Exception as error:
                print("GPU stop failsafe:", error, flush=True)
    try:
        with BMC() as client:
            for zone, points in decide(True, None, [], floors)["curves"].items():
                put_zone(client, zone, points)
    except Exception as error:
        print("BMC stop failsafe:", error, flush=True)
        local_bmc_full_speed()


def main():
    load_settings()
    signal.signal(signal.SIGTERM, lambda *_: STOP.set())
    signal.signal(signal.SIGINT, lambda *_: STOP.set())
    workers = [
        threading.Thread(target=target, args=args, name=name, daemon=True)
        for name, target, args in (
            ("temperature-1s", sensor_loop, ()),
            ("bmc-control", bmc_loop, ()),
            ("gpu-api", socket_loop, ("/run/hal-gpu-fan/control.sock", gpu_request)),
            ("case-api", socket_loop, ("/run/hal-case-fans/control.sock", case_request)),
        )
    ]
    try:
        for worker in workers:
            worker.start()
        while not STOP.wait(1):
            if any(not worker.is_alive() for worker in workers):
                raise RuntimeError("Cooling worker stopped unexpectedly")
    finally:
        STOP.set()
        CHANGED.set()
        for worker in workers:
            worker.join(timeout=15)
        failsafe()


if __name__ == "__main__":
    if sys.argv[1:] == ["--failsafe-only"]:
        failsafe()
    else:
        main()
