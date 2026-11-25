#!/usr/bin/python3
"""Versioned, fixed-work Vulkan benchmark with independent speed and quality cases."""
import concurrent.futures
import datetime
import hashlib
import importlib.util
import json
import math
import os
from pathlib import Path
import re
import statistics
import subprocess
import sys
import time
import urllib.request

sys.path.insert(0, "/usr/local/libexec")
import ai_bench_tuning

PROFILE = Path(__file__).with_name("radeon-vulkan.py")
GPU = {
    "b65": ("0000:04:00.0", "xe", "Intel Arc Pro B65-0", "intel_icd.x86_64.json"),
    "r9700": ("0000:09:00.0", "amdgpu", "Radeon AI PRO R9700", "radeon_icd.x86_64.json"),
}
UNIT = os.environ.get("HAL_BENCH_UNIT", "hal-queue-radeon-infer.service")
FIXTURES = Path("/scratch/b65-model-testing/expanded-20261113")
SPEED_SCENARIOS = [("short", 2), ("medium", 24), ("long", 72)]
PASSAGE = (
    "An instrumentation station records humidity, fan speed, and inlet temperature. "
    "Each reading has a monotonic sample number and is published only after validation. "
)
SPEED_CAP = 256
REPEATS = 3


def speed_prompt(copies, repeat):
    return (
        f"Case {repeat+1}. Read the following neutral background.\n"
        + PASSAGE * copies
        + "\nWrite a technical guide on designing a reliable instrument data logger. "
        "Include sampling, validation, persistence, recovery, and tests. Continue discussing details."
    )


def streamed_speed(model, prompt, remaining, opener=urllib.request.urlopen):
    options = {
        "num_ctx": 8192,
        "num_gpu": 999,
        "num_thread": 8,
        "seed": 42,
        "num_predict": SPEED_CAP,
        "temperature": 0.6 if model.startswith("deepseek") else 0,
    }
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "stream": True,
        "think": False,
        "keep_alive": -1,
        "options": options,
    }
    req = urllib.request.Request(
        "http://127.0.0.1:" + os.environ.get("HAL_BENCH_PORT", "11445") + "/api/chat",
        json.dumps(ai_bench_tuning.ollama_body(payload)).encode(),
        {"Content-Type": "application/json"},
    )
    started = time.monotonic()
    first = content_first = None
    final = None
    text = []
    expiry = started + min(remaining, 330)
    with opener(req, timeout=max(1, min(remaining, 330))) as response:
        for line in response:
            if time.monotonic() > expiry:
                raise TimeoutError("Speed case exceeded its time limit")
            if not line.strip():
                continue
            chunk = json.loads(line)
            if chunk.get("error"):
                raise RuntimeError(chunk["error"])
            message = chunk.get("message", {})
            content = message.get("content", "")
            if first is None and (content or message.get("thinking")):
                first = time.monotonic() - started
            if content and content_first is None:
                content_first = time.monotonic() - started
            text.append(content)
            if chunk.get("done"):
                final = {key: value for key, value in chunk.items() if key != "message"}
                break
    if final is None:
        raise RuntimeError("Speed response ended without metrics")
    output = final.get("eval_count", 0)
    duration = final.get("eval_duration", 0)
    if not output or not duration:
        raise RuntimeError("Speed response did not report generation work")
    return {
        "output_tokens": output,
        "input_tokens": final.get("prompt_eval_count"),
        "generation_seconds": duration / 1e9,
        "prompt_seconds": final.get("prompt_eval_duration", 0) / 1e9,
        "rate": output * 1e9 / duration,
        "first_token_seconds": first,
        "first_visible_token_seconds": content_first,
        "request_seconds": time.monotonic() - started,
        "reached_output_cap": output >= SPEED_CAP,
        "response_metrics": final,
        "content": "".join(text),
    }


def summarize(values):
    if not values:
        return {}
    return {
        "mean": statistics.mean(values),
        "median": statistics.median(values),
        "min": min(values),
        "max": max(values),
        "count": len(values),
    }


def measure_power(pci, driver, previous):
    hwmon = Path("/sys/bus/pci/devices") / pci / "hwmon"
    try:
        if driver == "amdgpu":
            path = next(hwmon.glob("hwmon*/power1_average"))
            return int(path.read_text()) / 1e6, previous
        paths = hwmon.glob("hwmon*/energy*_label")
        label = next(p for p in paths if p.read_text().strip() == "card")
        energy = int(label.with_name(label.name.replace("_label", "_input")).read_text())
        now = time.monotonic()
        watts = (
            (energy - previous[1]) / 1e6 / (now - previous[0])
            if previous and energy >= previous[1]
            else None
        )
        return watts, (now, energy)
    except (OSError, ValueError, StopIteration):
        return None, previous


def fail(category, detail, **evidence):
    folder = os.environ.get("HAL_QUEUE_EVIDENCE")
    if folder:
        path = Path(folder) / "failure-evidence.json"
        path.write_text(
            json.dumps({"category": category, "detail": detail, "evidence": evidence}, indent=2)
        )
    raise RuntimeError(category + " — " + detail)


def case_error(error):
    if isinstance(error, urllib.error.HTTPError):
        try:
            body = json.loads(error.read(8192))
            detail = body.get("error") if isinstance(body, dict) else None
            if isinstance(detail, dict):
                detail = detail.get("message")
            if isinstance(detail, str) and detail.strip():
                return f"Inference API HTTP {error.code}: " + detail.strip()[:480]
        except (ValueError, OSError):
            pass
    return str(error)


def monitor(cfg, since, previous):
    pci, driver, _, _ = cfg
    state = json.loads(Path("/run/hal-cooling/status.json").read_text())
    age = time.time() - state["time"]
    if state.get("control_protection"):
        fail(
            "Cooling control protection",
            "Cooling daemon reported protection active",
            pci=pci,
            controller_error=state.get("error"),
            policy=state.get("policy"),
        )
    if not state["ok"] or not state["sensors_fresh"] or age > 4:
        fail(
            "Cooling telemetry",
            "Cooling status unhealthy, invalid or stale",
            pci=pci,
            ok=state["ok"],
            sensors_fresh=state["sensors_fresh"],
            sample_age_seconds=round(age, 3),
            controller_error=state.get("error"),
        )
    cpu = state["temperatures"].get("cpu_c")
    if not isinstance(cpu, (int, float)) or not math.isfinite(cpu) or cpu <= 0:
        fail("Sensor reading", "CPU temperature invalid", reported_c=cpu)
    if cpu >= 90:
        fail(
            "Thermal limit",
            f"CPU reached {cpu:g}°C (stop threshold 90°C)",
            reported_c=cpu,
            limit_c=90,
        )
    gpu = next(
        (g for g in state["temperatures"]["gpus"] if g["pci"] == pci and g["driver"] == driver),
        None,
    )
    if gpu is None:
        fail(
            "GPU visibility",
            f"{driver} device {pci} missing from cooling telemetry",
            pci=pci,
            driver=driver,
        )
    evidence = {
        "pci": pci,
        "driver": driver,
        "core_c": gpu.get("pkg_c"),
        "vram_c": gpu.get("vram_c"),
        "fan_rpm": gpu.get("rpm"),
        "cpu_c": cpu,
        "sample_time": state["time"],
        "controller_error": gpu.get("error"),
    }
    log = subprocess.check_output(
        ["journalctl", "-k", "-b", "--since", f"@{since}", "-o", "cat", "--no-pager"],
        timeout=5,
        text=True,
    )
    faults = [
        line
        for line in log.splitlines()
        if pci in line
        and driver in line
        and re.search(r"timeout|page fault|GPU reset|wedged|GPU HANG", line, re.I)
    ]
    if faults:
        fail("Kernel GPU fault", faults[0][-400:], **evidence, kernel_excerpt=faults[:5])
    limit = (85, 95) if driver == "xe" else (130, 130)
    for label, key, threshold in (
        ("core/hotspot", "pkg_c", limit[0]),
        ("VRAM", "vram_c", limit[1]),
    ):
        value = gpu.get(key)
        if not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
            fail("Sensor reading", f"Invalid GPU {label} reading: {value!r}", **evidence)
        if value >= threshold:
            category = "Thermal limit" if driver == "xe" else "Sensor range"
            description = "stop threshold" if driver == "xe" else "telemetry validity ceiling"
            fail(
                category,
                f"GPU {label} reported {value:g}°C ({description} {threshold}°C)",
                **evidence,
                limit_c=threshold,
            )
    rpm = gpu.get("rpm")
    if not isinstance(rpm, (int, float)) or not math.isfinite(rpm) or rpm <= 0:
        fail(
            "Fan telemetry",
            f"GPU fan RPM reported {rpm!r}; physical fan state not established",
            **evidence,
        )
    watts, previous = measure_power(pci, driver, previous)
    if watts is not None and not 0 <= watts < 1500:
        watts = None
    return {
        "sample_time": state["time"],
        "package_c": gpu["pkg_c"],
        "vram_c": gpu["vram_c"],
        "rpm": gpu["rpm"],
        "gpu_watts": watts,
        "cpu_c": state["temperatures"]["cpu_c"],
        "fans": state["fans"],
    }, previous


def inherited_runner(root, pci):
    spec = importlib.util.spec_from_file_location("hal_hour_profile", PROFILE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.ROOT = root
    module.PCI = pci
    return module


def run_cases(parent, model, root, row, deadline):
    native = "tools" in parent.api(0, "/api/show", {"model": model}).get("capabilities", [])
    row["native_tool_support"] = native
    speed = []
    quality = {
        kind: {"attempted": 0, "completed": 0, "correct": 0, "errors": 0, "total": total}
        for kind, total in [("coding", 20), ("reasoning", 50), ("tools", 20)]
    }
    with (root / "cases.jsonl").open("w") as log:

        def archive(item):
            log.write(json.dumps(item, ensure_ascii=False) + "\n")
            log.flush()

        for name, copies in SPEED_SCENARIOS:
            for repeat in range(REPEATS):
                if parent.STOP.is_set() or time.monotonic() >= deadline:
                    raise TimeoutError("Stopped during speed suite")
                key = {"kind": "speed", "scenario": name, "repeat": repeat + 1}
                prompt = speed_prompt(copies, repeat)
                key["prompt_sha256"] = hashlib.sha256(prompt.encode()).hexdigest()
                try:
                    item = streamed_speed(model, prompt, deadline - time.monotonic())
                    speed.append(
                        {
                            **key,
                            **{
                                k: v
                                for k, v in item.items()
                                if k not in ("content", "response_metrics")
                            },
                        }
                    )
                    archive({**key, **item})
                except (OSError, ValueError, RuntimeError) as error:
                    speed.append({**key, "error": case_error(error)})
                    archive({**key, "error": case_error(error)})
        row["speed_cases"] = speed
        row["speed_summary"] = {}
        for name, _ in SPEED_SCENARIOS:
            cases = [x for x in speed if x["scenario"] == name and "rate" in x]
            row["speed_summary"][name] = {
                "generation_tokens_per_second": summarize([x["rate"] for x in cases]),
                "first_token_seconds": summarize(
                    [
                        x["first_token_seconds"]
                        for x in cases
                        if x["first_token_seconds"] is not None
                    ]
                ),
                "outputs_at_cap": sum(x["reached_output_cap"] for x in cases),
                "expected_samples": REPEATS,
            }
        rates = [x["rate"] for x in speed if "rate" in x]
        row["rate"] = statistics.median(rates) if rates else None
        row["quality"] = quality
        start = time.monotonic()
        for kind, index in parent.TASKS:
            if kind == "speed":
                continue
            if parent.STOP.is_set() or time.monotonic() >= deadline:
                raise TimeoutError("Stopped during quality suite")
            stats = quality[kind]
            stats["attempted"] += 1
            key = {"kind": kind, "index": index}
            try:
                result = parent.answer_task(
                    0, model, kind, index, native, deadline - time.monotonic()
                )
                stats["completed"] += 1
                stats["correct"] += bool(result.get("pass"))
                archive(result)
            except (OSError, ValueError, RuntimeError) as error:
                stats["errors"] += 1
                archive({**key, "error": case_error(error)})
        row["quality_seconds"] = time.monotonic() - start
    return (
        all(s["completed"] == s["total"] for s in quality.values())
        and len(rates) == len(SPEED_SCENARIOS) * REPEATS
    )


def main(spec, root):
    pci, driver, label, icd = cfg = GPU[spec["gpu"]]
    model = spec["model"]
    parent = inherited_runner(root, pci)
    row = {
        "id": "fixed-work-v1",
        "model": model,
        "gpu": label,
        "backend": "Ollama / Vulkan",
        "streams": 1,
        "status": "loading",
        "rate": None,
        "settings": "Fixed tests v1 · context 8192 · 8 host threads · seed 42",
    }
    report = {
        "rows": [row],
        "accuracy": [],
        "hardware": {"gpu": label, "pci": pci},
        "models": [model],
        "versions": {"kernel": os.uname().release, "suite": "fixed-work-v1"},
        "notes": [
            "Speed requests have a fixed output cap. Early EOS and actual token counts remain in the record.",
            "First token is the first nonempty streamed content or thinking delta. Load is measured separately.",
            "Temperature and power are from card telemetry when available. Quality failures and incomplete cases are shown separately.",
        ],
    }
    report["fixtures_sha256"] = {
        n: hashlib.sha256((FIXTURES / n).read_bytes()).hexdigest()
        for n in ("mbpp20.json", "gsm8k50.json", "hal-tool20.json")
    }
    parent.save(root / "results.json", report)
    started = int(time.time())
    telemetry = []
    previous = None
    env = {
        "OLLAMA_HOST": "127.0.0.1:" + os.environ.get("HAL_BENCH_PORT", "11445"),
        "OLLAMA_MODELS": "/data/ollama/models",
        "OLLAMA_NO_CLOUD": "1",
        "OLLAMA_NUM_PARALLEL": "1",
        "OLLAMA_CONTEXT_LENGTH": "8192",
        "OLLAMA_MAX_LOADED_MODELS": "1",
        "OLLAMA_VULKAN": "1",
        "ROCR_VISIBLE_DEVICES": "-1",
        "HIP_VISIBLE_DEVICES": "-1",
        "VK_DRIVER_FILES": "/usr/share/vulkan/icd.d/" + icd,
    }
    if driver == "amdgpu":
        env["RADV_DEBUG"] = "nocompute"
    props = [
        "User=ollama",
        "DevicePolicy=closed",
        f"DeviceAllow=/dev/dri/by-path/pci-{pci}-render rw",
        "NoNewPrivileges=yes",
        "CapabilityBoundingSet=",
        "IPAddressDeny=any",
        "IPAddressAllow=127.0.0.1/32",
        "MemoryMax=32G",
        "MemorySwapMax=0",
        f'RuntimeMaxSec={spec["timeout_seconds"]+300}',
        "TimeoutStopSec=10",
        "LimitCORE=0",
    ]

    def progress(stage):
        parent.save(root / "public-progress.json", {"status": stage})

    def watch(future):
        nonlocal previous
        while not future.done() and not parent.STOP.wait(2):
            sample, previous = monitor(cfg, started, previous)
            telemetry.append(sample)
            with (root / "telemetry.jsonl").open("a") as out:
                out.write(json.dumps(sample) + "\n")
        if parent.STOP.is_set():
            raise RuntimeError("Benchmark interrupted")
        return future.result()

    try:
        first, previous = monitor(cfg, started, previous)
        row["starting_temperatures_c"] = {"pkg_c": first["package_c"], "vram_c": first["vram_c"]}
        for fixture in parent.MBPP:
            if not parent.quality.check_code(fixture["code"], fixture)[0]:
                raise RuntimeError("Quality reference failed")
        progress(f"Loading {model} on {label}")
        subprocess.run(
            [
                "systemd-run",
                "--unit=" + UNIT,
                "--collect",
                *[v for p in props for v in ("-p", p)],
                *["--setenv=" + k + "=" + v for k, v in ai_bench_tuning.ollama_env(env).items()],
                "/usr/bin/ollama",
                "serve",
            ],
            check=True,
        )
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            try:
                loaded, load_seconds, discovery = watch(pool.submit(parent.load, UNIT, model))
                if loaded["digest"] != spec["model_digest"]:
                    raise RuntimeError("Model digest changed")
                peak_start = time.monotonic()
                row.update(residency=loaded, load_seconds=load_seconds, status="running")
                report["gpu_discovery"] = discovery
                progress(f"Fixed-work suite · {model} · {label}")
                finished = watch(
                    pool.submit(
                        run_cases, parent, model, root, row, peak_start + spec["timeout_seconds"]
                    )
                )
                row["suite_seconds"] = time.monotonic() - peak_start
                row["status"] = "complete" if finished else "incomplete"
                if not finished:
                    errors = {}
                    with (root / "cases.jsonl").open() as cases:
                        for line in cases:
                            item = json.loads(line)
                            if reason := item.get("error"):
                                errors[reason] = errors.get(reason, 0) + 1
                    details = "; ".join(
                        f"{count}× {reason[:240]}" for reason, count in list(errors.items())[:4]
                    )
                    report["error"] = "Fixed cases incomplete" + (": " + details if details else "")
            finally:
                peak = parent.ctl(
                    "show", UNIT, "-p", "MemoryPeak", "--value", check=False
                ).stdout.strip()
                if peak.isdecimal():
                    row["peak_host_ram_gib"] = int(peak) / 2**30
                parent.ctl("stop", UNIT, check=False)
    except Exception as error:
        row.update(status="failed", error=str(error))
        report["error"] = case_error(error)
    finally:
        for kind, stats in row.get("quality", {}).items():
            report["accuracy"].append(
                {
                    "model": model,
                    "gpu": label,
                    "benchmark": kind,
                    "score": stats["correct"] / stats["completed"] if stats["completed"] else None,
                    "examples": stats["completed"],
                    "settings": f"{stats['correct']} correct, {stats['completed']-stats['correct']} incorrect, {stats['total']-stats['completed']} incomplete of {stats['total']}",
                }
            )
        row["telemetry_summary"] = {
            field: summarize([v[field] for v in telemetry if v[field] is not None])
            for field in ("package_c", "vram_c", "rpm", "gpu_watts")
        }
        evidence = root / "failure-evidence.json"
        if evidence.exists():
            report["failure_diagnostic"] = json.loads(evidence.read_text())
        parent.save(root / "results.json", report)
    return 0 if row["status"] == "complete" else 1


if __name__ == "__main__":
    sys.exit(
        main(json.loads(Path(sys.argv[1]).read_text()), Path(os.environ["HAL_QUEUE_EVIDENCE"]))
    )
