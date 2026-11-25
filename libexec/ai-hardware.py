#!/usr/bin/python3
import os, sys, json, time, glob, pathlib, subprocess, datetime, signal, re, socket
from ai_nvme import temperature_celsius
from ai_usage_metrics import collect as collect_usage_metrics
from ai_smart_temperatures import collect as collect_smart_temperatures

P = pathlib.Path
BASE = P("/var/log/hardware-validation")


def read(p):
    return P(p).read_text().strip()


def command(a):
    return subprocess.check_output(a, text=True, timeout=10)


def sensor_identity(h, name):
    # Retain original history identifiers, but resolve them from hardware identity.
    fixed = {"acpitz": 0, "asus": 5, "coretemp": 6, "nct6799": 8}
    if name == "coolermaster_psu":
        return "/sys/class/hwmon/usb-2516_01a5"
    if name in ("xe", "amdgpu"):
        device = str(P(h + "/device").resolve())
        return "/sys/class/hwmon/pci-" + P(device).name.replace(":", "_").replace(".", "_")
    if name == "nvme":
        device = str(P(h + "/device").resolve())
        for pci, number in {
            "0000:01:00.0": 1,
            "0000:83:00.0": 2,
            "0000:82:00.0": 3,
            "0000:05:00.0": 4,
        }.items():
            if pci in device:
                return "/sys/class/hwmon/hwmon" + str(number)
    if name in fixed:
        return "/sys/class/hwmon/hwmon" + str(fixed[name])
    return h


def sample():
    s = {
        "time": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "uptime": read("/proc/uptime"),
        "memory": read("/proc/meminfo"),
        "cpu_stat": read("/proc/stat").splitlines()[0],
        "sensors": {},
        "nvme": {},
        "throttle": {},
        "frequencies_khz": {},
    }
    gpu_rpm = {}
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as rpc:
            rpc.settimeout(5)
            rpc.connect("/run/hal-gpu-fan/control.sock")
            rpc.sendall(b'{"action":"status"}\n')
            buf = b""
            while b"\n" not in buf:
                chunk = rpc.recv(65536)
                if not chunk:
                    break
                buf += chunk
            response = json.loads(buf)
            if response.get("ok"):
                gpu_rpm = {
                    g["pci"]: g.get("rpm")
                    for g in response["gpus"]
                    if g.get("rpm_age_seconds", 999) <= 5
                }
    except (OSError, ValueError, KeyError):
        pass
    for h in glob.glob("/sys/class/hwmon/hwmon*"):
        name = read(h + "/name")
        for f in glob.glob(h + "/*_input"):
            b = P(f).stem.removesuffix("_input")
            label = P(h + "/" + b + "_label")
            key = (
                sensor_identity(h, name) + "/" + name + "/" + (read(label) if label.exists() else b)
            )
            try:
                if name == "xe" and b.startswith("fan"):
                    value = gpu_rpm.get(P(h + "/device").resolve().name)
                    if value is None:
                        raise OSError("GPU RPM sampler unavailable")
                    s["sensors"][key] = value
                else:
                    s["sensors"][key] = int(read(f))
            except OSError as error:
                s.setdefault("sensor_errors", {})[key] = str(error)
                continue
            s.setdefault("sensor_metadata", {})[key] = {
                "kind": (
                    "power"
                    if name == "coolermaster_psu" and b == "power1"
                    else (
                        "temperature"
                        if b.startswith("temp")
                        else "fan" if b.startswith("fan") else "other"
                    )
                ),
                "input": b,
                "driver": name,
            }
    for n in glob.glob("/sys/class/nvme/nvme*"):
        dev = "/dev/" + P(n).name
        s["nvme"][dev] = {
            "model": read(n + "/model"),
            "serial": read(n + "/serial"),
            "smart": json.loads(command(["nvme", "smart-log", dev, "-o", "json"])),
        }
        smart = s["nvme"][dev]["smart"]
        identity = sensor_identity(n, "nvme")
        for field, label in [
            ("temperature", "Composite"),
            ("temperature_sensor_1", "Sensor 1"),
            ("temperature_sensor_2", "Sensor 2"),
        ]:
            if field not in smart or not smart[field]:
                continue
            key = identity + "/nvme/" + label
            if key in s["sensors"]:
                continue
            s["sensors"][key] = round(temperature_celsius(smart[field]) * 1000)
            s.setdefault("sensor_metadata", {})[key] = {
                "kind": "temperature",
                "driver": "nvme",
                "source": "SMART",
                "serial": s["nvme"][dev]["serial"],
            }

    collect_smart_temperatures(s)
    collect_usage_metrics(s)

    # BMC header identities are independent of hwmon enumeration and physical fan placement.
    bmc = subprocess.run(["ipmitool", "sensor"], capture_output=True, text=True, timeout=12)
    if bmc.returncode:
        s.setdefault("sensor_errors", {})["BMC"] = bmc.stderr.strip()
    for line in bmc.stdout.splitlines():
        fields = [v.strip() for v in line.split("|")]
        if len(fields) < 4 or fields[2] not in ("RPM", "degrees C"):
            continue
        try:
            value = float(fields[1])
        except ValueError:
            continue
        key = "/sys/class/hwmon/bmc/ipmi/" + fields[0]
        kind = "fan" if fields[2] == "RPM" else "temperature"
        s["sensors"][key] = round(value if kind == "fan" else value * 1000)
        s.setdefault("sensor_metadata", {})[key] = {
            "kind": kind,
            "driver": "ipmi",
            "source": "BMC",
            "header": fields[0],
        }
    for f in glob.glob("/sys/devices/system/cpu/cpu*/thermal_throttle/*_throttle_count"):
        s["throttle"][f] = int(read(f))
    for f in glob.glob("/sys/devices/system/cpu/cpufreq/policy*/scaling_cur_freq"):
        s["frequencies_khz"][f] = int(read(f))
    for f in glob.glob("/sys/class/drm/card*/device/gpu_busy_percent"):
        try:
            s[f] = read(f)
        except OSError as error:
            s.setdefault("sensor_errors", {})[f] = str(error)
    return s


def check(s, base):
    cpu = [v / 1000 for k, v in s["sensors"].items() if "/coretemp/" in k]
    if not cpu or len(s["nvme"]) != 4:
        raise RuntimeError("Required CPU or NVMe monitoring missing")
    gpu_labels = {k.rsplit("/", 1)[-1] for k in s["sensors"] if "/amdgpu/" in k}
    if not {"edge", "junction", "mem"} <= gpu_labels:
        raise RuntimeError("Required GPU temperature unavailable")
    if max(cpu) >= 95:
        raise RuntimeError("CPU reached conservative 95C stop threshold; Intel limit 105C")
    for k, v in s["sensors"].items():
        if "/amdgpu/" in k and ("edge" in k or "junction" in k or "/mem" in k) and v >= 100000:
            raise RuntimeError("GPU temperature at conservative 100C stop threshold")
    for dev, d in s["nvme"].items():
        a = d["smart"]
        t = float(a["temperature"])
        t = t - 273.15 if t > 200 else t
        limit = 85 if "SN850X" in d["model"] else 70
        if t >= limit:
            raise RuntimeError(dev + " composite temperature at operating limit " + str(limit))
        if int(a["critical_warning"]) or int(a["media_errors"]) > int(
            base["nvme"][dev]["smart"]["media_errors"]
        ):
            raise RuntimeError(dev + " NVMe health warning")
    if any(v > base["throttle"].get(k, v) for k, v in s["throttle"].items()):
        raise RuntimeError("CPU thermal throttle counter increased")


if __name__ == "__main__":
    if len(sys.argv) == 1:
        print(json.dumps(sample()))
        sys.exit(0)
    name = sys.argv[1]
    argv = sys.argv[2:]
    start = time.time()
    p = None
    status = {}
    try:
        base = sample()
        check(base, base)
        with (
            open(BASE / (name + "-samples.jsonl"), "x", buffering=1) as out,
            open(BASE / (name + "-workload.log"), "x") as log,
        ):
            out.write(json.dumps(base) + chr(10))
            out.flush()
            os.fsync(out.fileno())
            p = subprocess.Popen(argv, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
            while True:
                t = time.monotonic()
                s = sample()
                check(s, base)
                out.write(json.dumps(s) + chr(10))
                out.flush()
                os.fsync(out.fileno())
                errors = command(
                    [
                        "journalctl",
                        "-k",
                        "--since",
                        "@" + str(int(start)),
                        "--no-pager",
                        "-o",
                        "cat",
                    ]
                )
                if re.search(
                    r"Hardware Error|Machine check|uncorrected|AER:.*(fatal|error)|nvme.*(reset|timeout|I/O error)|EXT4-fs error|amdgpu.*(timeout|failed)",
                    errors,
                    re.I,
                ):
                    raise RuntimeError("New kernel hardware error; review journal")
                rc = p.poll()
                if rc is not None:
                    status = {"result": "PASS" if rc == 0 else "FAIL", "exit_code": rc}
                    break
                time.sleep(max(0, 5 - (time.monotonic() - t)))
    except BaseException as e:
        status = {"result": "STOPPED", "reason": str(e)}
    finally:
        if p is not None and p.poll() is None:
            os.killpg(p.pid, signal.SIGTERM)
            try:
                p.wait(timeout=10)
            except subprocess.TimeoutExpired:
                os.killpg(p.pid, signal.SIGKILL)
                p.wait()
        status.update(start_epoch=start, end_epoch=time.time(), command=argv)
        (BASE / (name + "-result.json")).write_text(json.dumps(status, indent=2))
        print(json.dumps(status))
    sys.exit(0 if status.get("result") == "PASS" else 1)
