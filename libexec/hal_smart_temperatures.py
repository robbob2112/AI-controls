"""Read-only SMART temperatures. Never start tests or wake standby ATA disks."""

import concurrent.futures
import json
import pathlib
import re
import subprocess
import time

INVENTORY = pathlib.Path("/var/lib/hal-monitor/storage-inventory.json")


def temperature(document):
    value = document.get("temperature", {}).get("current")
    # smartctl's decoded field is authoritative. ATA raw.value may pack min/max
    # temperatures into a huge integer, so it must never be used as Celsius.
    return value if type(value) in (int, float) and -20 <= value <= 150 else None


def read_drive(drive):
    name = drive["name"]
    category = "nvme" if drive.get("tran") == "nvme" or name.startswith("nvme") else "ata"
    serial = drive.get("serial") or drive.get("wwn")
    record = {
        "device": "/dev/" + name,
        "model": drive.get("model", "").strip(),
        "serial": serial,
        "category": category,
        "temperature_c": None,
        "time": time.time(),
    }
    if not serial:
        return dict(record, error="Stable drive identity unavailable")
    identity = re.sub(r"[^a-zA-Z0-9_.-]", "_", serial.strip())
    record["id"] = "/smart/" + category + "/" + identity + "/temperature"
    record["label"] = name + " · " + serial[-4:]
    try:
        result = subprocess.run(
            ["/usr/sbin/smartctl", "-j", "-i", "-A", "-n", "standby", record["device"]],
            capture_output=True,
            text=True,
            timeout=5,
        )
        document = json.loads(result.stdout)
        value = temperature(document)
        if value is not None:
            record["temperature_c"] = value
            record["status"] = "available"
        else:
            messages = [
                m.get("string", "") for m in document.get("smartctl", {}).get("messages", [])
            ]
            detail = "; ".join(messages) or "SMART temperature unavailable"
            record["status"] = "standby" if "standby" in detail.lower() else "unavailable"
            record["error"] = detail[:240]
    except (OSError, ValueError, subprocess.TimeoutExpired) as error:
        record["status"] = "unavailable"
        record["error"] = str(error)[:240]
    return record


def collect(sample):
    try:
        result = subprocess.run(
            ["/usr/bin/lsblk", "-J", "-d", "-o", "NAME,TYPE,TRAN,MODEL,SERIAL,WWN"],
            capture_output=True,
            text=True,
            timeout=3,
            check=True,
        )
        drives = [
            d
            for d in json.loads(result.stdout)["blockdevices"]
            if d["type"] == "disk"
            and (d.get("tran") in ("sata", "sas", "nvme") or d["name"].startswith("nvme"))
        ]
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as workers:
            inventory = list(workers.map(read_drive, drives))
        for record in inventory:
            value = record["temperature_c"]
            if value is None or "id" not in record:
                continue
            key = record["id"]
            sample["sensors"][key] = round(value * 1000)
            sample.setdefault("sensor_metadata", {})[key] = {
                "kind": "temperature",
                "source": "SMART",
                "serial": record["serial"],
                "driver": record["category"],
                "model": record["model"],
            }
        sample["storage_inventory"] = inventory
        temporary = INVENTORY.with_suffix(".tmp")
        temporary.write_text(json.dumps({"time": time.time(), "drives": inventory}, indent=2))
        temporary.chmod(0o644)
        temporary.replace(INVENTORY)
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
        sample.setdefault("sensor_errors", {})["SMART collection"] = str(error)
