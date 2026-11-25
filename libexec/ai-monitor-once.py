#!/usr/bin/python3
import sys, json, subprocess, importlib.util, pathlib

spec = importlib.util.spec_from_file_location("hal", "/usr/local/libexec/ai-hardware.py")
hal = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hal)
s = hal.sample()
s["kernel_warnings_last_minute"] = subprocess.check_output(
    [
        "journalctl",
        "-k",
        "--since",
        "65 seconds ago",
        "-p",
        "warning",
        "--no-pager",
        "-o",
        "short-iso",
    ],
    text=True,
    timeout=10,
)
try:
    hal.check(s, s)
    if any(int(d["smart"]["media_errors"]) for d in s["nvme"].values()):
        raise RuntimeError("NVMe media error count is nonzero")
    s["health"] = "OK"
except Exception as e:
    s["health"] = "WARNING"
    s["warning"] = str(e)
with open("/var/log/hardware-monitor/samples.jsonl", "a") as f:
    f.write(json.dumps(s) + chr(10))
print(s["time"], s["health"])
