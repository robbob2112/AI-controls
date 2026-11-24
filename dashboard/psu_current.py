"""Read-only telemetry from the explicitly identified Cooler Master USB PSU."""

import pathlib, threading, time, json

LOCK = threading.Lock()
CACHE = None
FIELDS = {
    "ac_voltage_v": ("in0_input", 1000),
    "ac_current_a": ("curr1_input", 1000),
    "input_w": ("power1_input", 1000000),
    "output_w": ("power2_input", 1000000),
    "ambient_c": ("temp1_input", 1000),
    "hotspot_c": ("temp2_input", 1000),
    "rail12_voltage_v": ("in1_input", 1000),
    "rail12_current_a": ("curr2_input", 1000),
    "rail12_power_w": ("power3_input", 1000000),
    "rail33_voltage_v": ("in2_input", 1000),
    "rail33_current_a": ("curr3_input", 1000),
    "rail33_power_w": ("power4_input", 1000000),
    "rail5_voltage_v": ("in3_input", 1000),
    "rail5_current_a": ("curr4_input", 1000),
    "rail5_power_w": ("power5_input", 1000000),
}


def sample():
    candidates = [
        h
        for h in pathlib.Path("/sys/class/hwmon").glob("hwmon*")
        if (h / "name").read_text().strip() == "coolermaster_psu"
        and "2516:01a5." in str(h.resolve()).lower()
    ]
    if len(candidates) != 1:
        raise RuntimeError("X Silent MAX 1300 USB telemetry unavailable")
    h = candidates[0]
    values = {k: int((h / n).read_text().strip()) / scale for k, (n, scale) in FIELDS.items()}
    # This is an estimate derived from the PSU's own readings, not a meter calibration.
    efficiency = values["output_w"] / values["input_w"] * 100 if values["input_w"] > 0 else None
    fan = {"ok": False, "verified": False}
    try:
        fan = json.loads(pathlib.Path("/run/hal-psu-fan/current.json").read_text())
        fan["age_seconds"] = round(time.time() - fan["time"], 1)
        fan["stale"] = fan["age_seconds"] > 5
    except (OSError, ValueError, KeyError, TypeError):
        fan = {"ok": False, "verified": False, "stale": True}
    values["fan_provisional_rpm"] = (
        fan.get("provisional_rpm") if fan.get("ok") and not fan.get("stale") else None
    )
    values["efficiency_percent"] = (
        round(efficiency, 1) if efficiency is not None and 0 <= efficiency <= 100 else None
    )
    return {
        "ok": True,
        "model": "Cooler Master X Silent MAX Platinum 1300",
        "source": "USB HID · 2516:01a5",
        "time": time.time(),
        "values": values,
        "fan": fan,
    }


def get(handler):
    global CACHE
    with LOCK:
        error = None
        if CACHE is None or time.time() - CACHE["time"] >= 3:
            try:
                CACHE = sample()
            except (OSError, ValueError, RuntimeError) as e:
                error = str(e)
        if CACHE is None:
            handler.send_json(503, {"ok": False, "error": error or "PSU telemetry unavailable"})
            return
        result = dict(CACHE)
        result["age_seconds"] = round(time.time() - CACHE["time"], 1)
        result["stale"] = error is not None or result["age_seconds"] > 10
        if error:
            result["error"] = error
    handler.send_json(200, result)
