"""Live read-only RAID completeness checks; never repair or modify arrays."""

import json
import pathlib
import re
import time

EXPECTED_DRIVES = 9
EXPECTED_ARRAYS = {
    "de816bbc-f0b1-5f69-b81a-e5694d7486f9": 4,
    "ddaa0922-e459-5d69-ab6c-8d224392bd23": 5,
}


def sample(block_root=pathlib.Path("/sys/class/block")):
    alerts = []
    arrays = []
    observed = set()
    for path in sorted(block_root.glob("md*/md")):
        name = path.parent.name
        try:
            uuid = (path / "uuid").read_text().strip()
            missing = int((path / "degraded").read_text())
            expected = int((path / "raid_disks").read_text())
            state = (path / "array_state").read_text().strip()
            sync = (path / "sync_action").read_text().strip()
            observed.add(uuid)
            record = {
                "name": name,
                "uuid": uuid,
                "missing": missing,
                "expected": expected,
                "present": expected - missing,
                "state": state,
                "sync_action": sync,
            }
            arrays.append(record)
            if missing:
                alerts.append(
                    name
                    + ": DEGRADED — "
                    + str(expected - missing)
                    + " of "
                    + str(expected)
                    + " members present"
                )
            if state not in (
                "clean",
                "active",
                "active-idle",
                "read-auto",
                "readonly",
                "write-pending",
            ):
                alerts.append(name + ": array state " + state)
            if uuid in EXPECTED_ARRAYS and expected != EXPECTED_ARRAYS[uuid]:
                alerts.append(name + ": configured member count changed")
        except (OSError, ValueError) as error:
            alerts.append(name + ": RAID status unavailable (" + str(error) + ")")
    for uuid, count in EXPECTED_ARRAYS.items():
        if uuid not in observed:
            alerts.append("Expected " + str(count) + "-member RAID array is missing: " + uuid)
    disks = []
    for path in block_root.glob("sd*"):
        if not re.fullmatch(r"sd[a-z]+", path.name):
            continue
        if "/usb" in str(path.resolve()):
            continue
        try:
            if (path / "device/type").read_text().strip() == "0":
                disks.append(path.name)
        except OSError:
            continue
    if len(disks) < EXPECTED_DRIVES:
        alerts.append(
            "Drive inventory: "
            + str(len(disks))
            + " of "
            + str(EXPECTED_DRIVES)
            + " expected SATA/SAS drives detected"
        )
    return {
        "ok": not alerts,
        "time": time.time(),
        "alerts": alerts,
        "arrays": arrays,
        "detected_drives": len(disks),
        "expected_drives": EXPECTED_DRIVES,
    }


def get(handler):
    try:
        handler.send_json(200, sample())
    except Exception:
        handler.send_json(
            503,
            {"ok": False, "alerts": ["Storage health unavailable; RAID completeness is unknown"]},
        )
