"""Read-only hardware discovery and a persistent monitoring selection."""

import argparse
import json
from pathlib import Path
import re
import shlex
import subprocess
import tempfile

CONFIG = Path("/var/lib/hal-monitor/discovered-hardware.json")
CHANNEL = re.compile(r"(temp|fan|power|in|curr|humidity)(\d+)_input\Z")
BDFS = re.compile(r"\b[0-9a-f]{4}:[0-9a-f]{2}:[0-9a-f]{2}\.[0-7]\b")
UNITS = {
    "temp": (1000, "°C"),
    "fan": (1, "RPM"),
    "power": (1000000, "W"),
    "in": (1000, "V"),
    "curr": (1000, "A"),
    "humidity": (1000, "%"),
}


def read(path):
    try:
        return path.read_text().strip()
    except OSError:
        return ""


def pci_names():
    try:
        result = subprocess.run(
            ["lspci", "-Dmm"], capture_output=True, text=True, check=True, timeout=3
        )
    except (OSError, subprocess.SubprocessError):
        return {}
    names = {}
    for line in result.stdout.splitlines():
        try:
            fields = shlex.split(line)
        except ValueError:
            continue
        if len(fields) >= 4:
            names[fields[0]] = " ".join(fields[2:4])
    return names


def discover(root=Path("/sys"), names=None):
    gpus = {}
    for device in sorted((root / "bus/pci/devices").glob("*")):
        if not read(device / "class").startswith("0x03"):
            continue
        vendor, product = read(device / "vendor"), read(device / "device")
        driver = device / "driver"
        gpus[device.name] = {
            "id": device.name,
            "model": (names or {}).get(device.name) or f"PCI {vendor}:{product}",
            "driver": driver.resolve().name if driver.is_symlink() else "",
            "vram_bytes": int(read(device / "mem_info_vram_total") or 0),
        }

    sensors = {}
    for node in sorted((root / "class/hwmon").glob("hwmon*")):
        driver = read(node / "name")
        if not driver:
            continue
        location = (
            str((node / "device").resolve())
            if (node / "device").exists()
            else str(node.resolve().parent.parent)
        )
        try:
            location = str(Path(location).relative_to(root.resolve()))
        except ValueError:
            pass
        pci = next(
            (address for address in reversed(BDFS.findall(location)) if address in gpus), None
        )
        for entry in sorted(node.glob("*_input")):
            match = CHANNEL.fullmatch(entry.name)
            if not match:
                continue
            stem = entry.name.removesuffix("_input")
            scale, unit = UNITS[match[1]]
            value = read(entry)
            key = f"{location}/{driver}/{stem}"
            sensors[key] = {
                "id": key,
                "label": read(node / (stem + "_label")) or f"{driver} {stem}",
                "driver": driver,
                "kind": match[1],
                "unit": unit,
                "gpu": pci,
                "value": int(value) / scale if value.lstrip("-").isdecimal() else None,
            }
    return {"gpus": gpus, "sensors": sensors}


def setup(path, inventory):
    selection = {
        key: {
            identifier: {k: v for k, v in item.items() if k != "value"}
            for identifier, item in inventory[key].items()
        }
        for key in ("gpus", "sensors")
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=path.parent, delete=False
    ) as stream:
        json.dump(selection, stream, indent=2)
        temp = Path(stream.name)
    try:
        temp.chmod(0o644)
        temp.replace(path)
    finally:
        temp.unlink(missing_ok=True)
    return selection


def status(path=CONFIG, root=Path("/sys")):
    try:
        saved = json.loads(path.read_text())
    except FileNotFoundError:
        return {"configured": False, "gpus": [], "sensors": []}
    current = discover(root)
    return {
        "configured": True,
        **{
            key: [
                dict(
                    item,
                    present=identifier in current[key],
                    **(
                        {"value": current[key].get(identifier, {}).get("value")}
                        if key == "sensors"
                        else {}
                    ),
                )
                for identifier, item in saved[key].items()
            ]
            for key in ("gpus", "sensors")
        },
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--setup", action="store_true", help="Scan this host and save the monitoring inventory"
    )
    parser.add_argument("--config", type=Path, default=CONFIG)
    args = parser.parse_args()
    if args.setup:
        result = setup(args.config, discover(names=pci_names()))
        print(
            f"Saved {len(result['gpus'])} GPUs and {len(result['sensors'])} sensor channels to {args.config}"
        )
    else:
        print(json.dumps(status(args.config), indent=2))
