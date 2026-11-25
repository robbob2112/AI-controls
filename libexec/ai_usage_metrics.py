"""Read-only CPU/GPU utilization and measured power for the history collector."""

import concurrent.futures
import json
import os
import pathlib
import subprocess
import time

BDFS = {"b65-0": "0000:04:00.0", "b65-1": "0000:09:00.0"}


def counter_delta(before, after, maximum=None):
    if after >= before:
        return after - before
    return maximum - before + after if maximum else None


def cpu_ticks():
    values = [int(v) for v in pathlib.Path("/proc/stat").read_text().splitlines()[0].split()[1:9]]
    return sum(values), values[3] + values[4]


def clients():
    result = {}
    for process in pathlib.Path("/proc").glob("[0-9]*"):
        try:
            files = list((process / "fd").iterdir())
        except OSError:
            continue
        for file in files:
            try:
                if not os.readlink(file).startswith("/dev/dri/"):
                    continue
                lines = (process / "fdinfo" / file.name).read_text().splitlines()
                fields = dict(
                    line.split(":", 1) for line in lines if line.startswith("drm-") and ":" in line
                )
                bdf = fields.get("drm-pdev", "").strip()
                if bdf not in BDFS.values():
                    continue
                key = (bdf, fields["drm-client-id"].strip())
                result[key] = (
                    int(fields.get("drm-cycles-ccs", "0")),
                    int(fields.get("drm-total-cycles-ccs", "0")),
                    int(fields.get("drm-engine-capacity-ccs", "1")),
                )
            except (OSError, KeyError, ValueError):
                continue
    return result


def compute_busy(before, after, bdf):
    ratios = []
    for key, second in after.items():
        if key[0] != bdf or key not in before:
            continue
        first = before[key]
        busy = second[0] - first[0]
        total = second[1] - first[1]
        if total > 0 and busy >= 0:
            ratios.append(busy / total / max(1, second[2]))
    # No open GPU clients is an idle device, not a fabricated utilization sample.
    if not any(k[0] == bdf for k in before) and not any(k[0] == bdf for k in after):
        return 0.0
    return min(100.0, max(0.0, sum(ratios) * 100)) if ratios else None


def energy_sources():
    result = {}
    for name, bdf in BDFS.items():
        for h in pathlib.Path("/sys/bus/pci/devices", bdf, "hwmon").glob("hwmon*"):
            for label in h.glob("energy*_label"):
                if label.read_text().strip() == "card":
                    result[name] = (label.with_name(label.name.replace("_label", "_input")), None)
    # Package already includes cores/uncore: never add those subdomains again.
    for path in pathlib.Path("/sys/class/powercap").glob("intel-rapl:*"):
        try:
            if (path / "name").read_text().strip().startswith("package-"):
                result["cpu"] = (
                    path / "energy_uj",
                    int((path / "max_energy_range_uj").read_text()),
                )
            elif (path / "name").read_text().strip() == "dram":
                result["memory"] = (
                    path / "energy_uj",
                    int((path / "max_energy_range_uj").read_text()),
                )
        except (OSError, ValueError):
            continue
    return result


def memory_percent(bdf):
    result = subprocess.run(
        ["/usr/bin/xpu-smi", "stats", "-d", bdf, "-j"],
        capture_output=True,
        text=True,
        timeout=4,
        check=True,
    )
    data = json.loads(result.stdout)
    if data.get("pci_bdf") != bdf:
        raise ValueError("GPU identity mismatch")
    values = [v["current"] for v in data["memory"]["util_percent"].values()]
    if len(values) != 1:
        raise ValueError("Unexpected GPU memory tile count")
    value = values[0]
    return value if 0 <= value <= 100 else None


def radeon_metrics():
    device = pathlib.Path("/sys/bus/pci/devices/0000:09:00.0")
    if (device / "driver").resolve().name != "amdgpu" or (
        device / "device"
    ).read_text().strip() != "0x7551":
        return
    for key, path in [
        ("r9700/gpu_busy", device / "gpu_busy_percent"),
        ("power/r9700", next(iter(device.glob("hwmon/hwmon*/power1_average")), None)),
    ]:
        try:
            if path is not None:
                value = int(path.read_text())
                if key.startswith("power/"):
                    if 0 <= value < 1500000000:
                        yield key, value / 1000000, "power"
                elif 0 <= value <= 100:
                    yield key, value, "percent"
        except (OSError, ValueError):
            continue
    try:
        used, total = (
            int((device / name).read_text())
            for name in ("mem_info_vram_used", "mem_info_vram_total")
        )
        if 0 <= used <= total and total > 0:
            yield "r9700/vram_used", 100 * used / total, "percent"
    except (OSError, ValueError):
        pass


def collect(sample):
    def add(name, value, kind):
        if value is None:
            return
        key = "/hal-metrics/" + name
        sample["sensors"][key] = round(value * 1000000) if kind == "power" else round(value, 3)
        sample.setdefault("sensor_metadata", {})[key] = {
            "kind": kind,
            "source": "read-only usage metrics",
        }

    try:
        for name, value, kind in radeon_metrics():
            add(name, value, kind)
        intel = {
            name: bdf
            for name, bdf in BDFS.items()
            if pathlib.Path("/sys/bus/pci/devices", bdf, "driver").resolve().name == "xe"
        }
        sources = energy_sources()
        first_energy = {name: int(path.read_text()) for name, (path, _) in sources.items()}
        first_time = time.monotonic()
        first_cpu = cpu_ticks()
        first_clients = clients()
        time.sleep(0.5)
        second_clients = clients()
        second_cpu = cpu_ticks()
        second_time = time.monotonic()
        second_energy = {name: int(path.read_text()) for name, (path, _) in sources.items()}
        total = second_cpu[0] - first_cpu[0]
        idle = second_cpu[1] - first_cpu[1]
        add("system/cpu_used", 100 * (1 - idle / total) if total > 0 else None, "percent")
        memory = {
            line.split(":")[0]: int(line.split()[1])
            for line in pathlib.Path("/proc/meminfo").read_text().splitlines()
        }
        add("system/ram_used", 100 * (1 - memory["MemAvailable"] / memory["MemTotal"]), "percent")
        for name, (_, maximum) in sources.items():
            delta = counter_delta(first_energy[name], second_energy[name], maximum)
            watts = delta / 1000000 / (second_time - first_time) if delta is not None else None
            add(
                "power/" + name, watts if watts is not None and 0 <= watts < 1500 else None, "power"
            )
        for name, bdf in intel.items():
            add(name + "/gpu_busy", compute_busy(first_clients, second_clients, bdf), "percent")
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as workers:
            futures = {name: workers.submit(memory_percent, bdf) for name, bdf in intel.items()}
            for name, future in futures.items():
                try:
                    add(name + "/vram_used", future.result(), "percent")
                except Exception as error:
                    sample.setdefault("sensor_errors", {})[name + " VRAM usage"] = str(error)
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
        sample.setdefault("sensor_errors", {})["usage metrics"] = str(error)
