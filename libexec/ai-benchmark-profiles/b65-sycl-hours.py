#!/usr/bin/python3
"""Run the hourly HAL workload using llama.cpp's Intel SYCL backend."""
import concurrent.futures
import datetime
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import urllib.request

LIB = Path(__file__).parent
sys.path.insert(0, str(LIB.parent))
from ai_model_catalog import NATIVE_PREFIX, native_models, native_weight

spec = importlib.util.spec_from_file_location("fixed", LIB / "fixed-work-v1.py")
fixed = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixed)
PCI = "0000:04:00.0"
UNIT = fixed.UNIT
TUNING = fixed.ai_bench_tuning.settings()
ENGINE = "/scratch/b65-model-testing/llama.cpp/build-sycl/bin/llama-server"


def endpoint(path, body=None, timeout=6):
    req = urllib.request.Request(
        "http://127.0.0.1:" + os.environ.get("HAL_BENCH_PORT", "11445") + path,
        None if body is None else json.dumps(body).encode(),
        {"Content-Type": "application/json"},
    )
    return json.load(urllib.request.urlopen(req, timeout=timeout))


def adapt_chat(body, timeout):
    options = body["options"]
    request = {
        "model": body["model"],
        "messages": body["messages"],
        "stream": False,
        "max_tokens": options["num_predict"],
        "temperature": TUNING.get("temperature", options["temperature"]),
        "seed": TUNING.get("seed", options["seed"]),
        "chat_template_kwargs": {"enable_thinking": False},
    }
    if "tools" in body:
        request["tools"] = body["tools"]
    payload = endpoint("/v1/chat/completions", request, timeout)
    first = payload["choices"][0]
    message = first["message"]
    message["content"] = message.get("content") or ""
    times = payload.get("timings", {})
    return {
        "message": message,
        "done_reason": first.get("finish_reason"),
        "eval_count": times.get(
            "predicted_n", payload.get("usage", {}).get("completion_tokens", 0)
        ),
        "eval_duration": round(times.get("predicted_ms", 0) * 1e6),
        "raw_timings": times,
    }


def wait_ready(parent, model, launched):
    begin = time.monotonic()
    until = begin + 240
    while time.monotonic() < until and not parent.STOP.is_set():
        try:
            if endpoint("/health", timeout=2).get("status") == "ok":
                break
        except OSError:
            pass
        if parent.ctl("is-active", UNIT, check=False).returncode != 0:
            logs = subprocess.check_output(
                ["journalctl", "-u", UNIT, "--since", f"@{launched}", "--no-pager", "-o", "cat"],
                text=True,
            )
            (parent.ROOT / "engine-startup.log").write_text(logs)
            reason = next(
                (
                    line.split("error loading model:", 1)[1].strip()
                    for line in logs.splitlines()
                    if "error loading model:" in line
                ),
                None,
            )
            if reason:
                category = (
                    "Model / runner compatibility"
                    if "unknown model architecture" in reason
                    else "Model loading error"
                )
                fixed.fail(category, reason, engine="llama.cpp SYCL", model=model)
            raise RuntimeError(
                "SYCL model service exited during load. See archived engine-startup.log."
            )
        time.sleep(1)
    else:
        raise RuntimeError("SYCL model did not become ready")
    adapt_chat(
        {
            "model": model,
            "messages": [{"role": "user", "content": "Ready."}],
            "options": {"num_predict": 1, "temperature": 0, "seed": 42},
        },
        30,
    )
    invocation = parent.ctl("show", UNIT, "-p", "InvocationID", "--value").stdout.strip()
    logs = subprocess.check_output(
        ["journalctl", "_SYSTEMD_INVOCATION_ID=" + invocation, "--no-pager", "-o", "cat"], text=True
    )
    if "offloaded" not in logs:
        logs = subprocess.check_output(
            ["journalctl", "-u", UNIT, "--since", f"@{launched}", "--no-pager", "-o", "cat"],
            text=True,
        )
    full = re.findall(r"offloaded (\d+)/(\d+) layers to GPU", logs)
    (parent.ROOT / "engine-startup.log").write_text(logs)
    if not full or any(a != b or a == "0" for a, b in full):
        raise RuntimeError("Could not verify full SYCL offload")
    if "SYCL" not in logs:
        raise RuntimeError("Could not verify the SYCL backend")
    return (
        time.monotonic() - begin,
        [l for l in logs.splitlines() if "SYCL" in l or "offloaded" in l][:30],
    )


def main(settings, root):
    model = settings["model"]
    parent = fixed.inherited_runner(root, PCI)
    native = model.startswith(NATIVE_PREFIX)
    report = {
        "rows": [],
        "accuracy": [],
        "versions": {
            "backend": "llama.cpp / Intel oneAPI SYCL",
            "kernel": os.uname().release,
            "llama_cpp_git": settings["llama_cpp_git"],
            "sycl_tuning": {
                "flash_attention": "on",
                "batch_tokens": 2048,
                "microbatch_tokens": 512,
                "batch_threads": 8,
                "xmx_esimd": True,
                "onednn": True,
            },
        },
        "hardware": {"gpu": "Intel Arc Pro B65-0", "pci": PCI},
        "models": [model],
        "notes": [
            "Same GGUF weights, fixed quality fixtures and measured duration as the Vulkan run.",
            "SYCL uses llama.cpp directly, with the embedded GGUF chat template. Ollama template, tool handling, sampler defaults, and bundled speculative decoding can differ. Treat this as an end-to-end stack comparison.",
            "Tool cases use the saved text-schema fallback. Compare only tests using the same mode.",
        ],
    }
    row = {
        "id": "b65-sycl-hour",
        "model": model,
        "gpu": "Intel Arc Pro B65-0",
        "backend": "llama.cpp / SYCL",
        "streams": 1,
        "rate": None,
        "tokens": 0,
        "trials": [],
        "status": "loading",
        "completed_requests": 0,
        "coverage": {},
        "errors": [],
        "settings": "Single SYCL GPU; context 8192; 8 CPU threads; seed 42; 1 hour; text-schema tool grading.",
        **(
            {"registered_gguf_identity": settings["model_digest"]}
            if native
            else {"weight_digest": settings["weight_digest"]}
        ),
    }
    if native:
        report["notes"][
            0
        ] = "Native llama.cpp preset with a pinned local GGUF file identity and fixed quality fixtures."
    report["rows"].append(row)
    report["versions"]["sycl_tuning"].update(TUNING)
    parent.REPORT = report
    parent.PAIRS = [[model]]
    parent.DURATION = settings["duration_seconds"]
    parent.publish = lambda stage=None: parent.save(
        root / "public-progress.json",
        {
            "status": stage
            or (
                "Running " + model + " on B65 SYCL for one hour"
                if row["status"] == "running"
                else row["status"]
            ),
            "rows": [row],
        },
    )

    def api(gpu, path, body=None, timeout=30):
        if path == "/api/show":
            # Only use the portable schema fallback across templates.
            return {"capabilities": []}
        if path == "/api/chat":
            return adapt_chat(body, timeout)
        raise ValueError("Unexpected benchmark API " + path)

    parent.api = api
    previous = None
    started = int(time.time())

    def watch(future):
        nonlocal previous
        while not future.done() and not parent.STOP.wait(2):
            sample, previous = fixed.monitor(fixed.GPU["b65"], started, previous)
            with (root / "telemetry.jsonl").open("a") as f:
                f.write(json.dumps(sample) + "\n")
        if parent.STOP.is_set():
            raise RuntimeError("Interrupted or workload error")
        return future.result()

    try:
        if native:
            alias = model.removeprefix(NATIVE_PREFIX)
            preset = next((item for item in native_models() if item.get("id") == alias), None)
            selected = native_weight(preset) if preset else None
            if not selected or selected[1] != settings["model_digest"]:
                raise RuntimeError(
                    "The registered llama.cpp GGUF changed or is unavailable. Review the preset and queue the benchmark again."
                )
            weights = selected[0]
        else:
            weights = Path("/data/ollama/models/blobs") / ("sha256-" + settings["weight_digest"])
        if not weights.is_file():
            raise RuntimeError("Pinned GGUF weights are missing")
        with weights.open("rb") as f:
            if f.read(4) != b"GGUF":
                raise RuntimeError("The pinned blob is not GGUF")
        report["fixtures_sha256"] = {
            n: hashlib.sha256((fixed.FIXTURES / n).read_bytes()).hexdigest()
            for n in ("mbpp20.json", "gsm8k50.json", "hal-tool20.json")
        }
        for fixture in parent.MBPP:
            if not parent.quality.check_code(fixture["code"], fixture)[0]:
                raise RuntimeError("Quality reference failed")
        sample, previous = fixed.monitor(fixed.GPU["b65"], started, previous)
        row["starting_temperatures_c"] = {"pkg_c": sample["package_c"], "vram_c": sample["vram_c"]}
        parent.publish("Loading " + model + " on B65 SYCL")
        props = [
            "User=ollama",
            "DevicePolicy=closed",
            f"DeviceAllow=/dev/dri/by-path/pci-{PCI}-render rw",
            "NoNewPrivileges=yes",
            "CapabilityBoundingSet=",
            "IPAddressDeny=any",
            "IPAddressAllow=127.0.0.1/32",
            "MemoryMax=32G",
            "MemorySwapMax=0",
            "RuntimeMaxSec=4050",
            "TimeoutStopSec=10",
            "LimitCORE=0",
        ]
        shell = 'source /opt/intel/oneapi/setvars.sh >/dev/null 2>&1; export ONEAPI_DEVICE_SELECTOR=level_zero:0 UR_L0_ENABLE_RELAXED_ALLOCATION_LIMITS=1 GGML_SYCL_ENABLE_OPT=1 GGML_SYCL_ENABLE_DNN=1 GGML_SYCL_ENABLE_FLASH_ATTN=1 GGML_SYCL_ENABLE_ESIMD=1 GGML_SYCL_ENABLE_XMX_ESIMD=1; exec "$@"'
        if TUNING.get("sycl_graph") == "on":
            shell = shell.replace("exec", "export GGML_SYCL_ENABLE_GRAPH=1; exec", 1)
        if TUNING.get("sycl_xmx") == "off":
            shell = shell.replace("exec", "export GGML_SYCL_ENABLE_XMX_ESIMD=0; exec", 1)
        args = [
            ENGINE,
            "--model",
            str(weights),
            "--alias",
            model,
            "--host",
            "127.0.0.1",
            "--port",
            os.environ.get("HAL_BENCH_PORT", "11445"),
            "-c",
            str(TUNING.get("context", 8192)),
            "-np",
            "1",
            "-t",
            str(TUNING.get("threads", 8)),
            "-tb",
            str(TUNING.get("threads", 8)),
            "-b",
            str(TUNING.get("batch", 2048)),
            "-ub",
            str(min(TUNING.get("microbatch", 512), TUNING.get("batch", 2048))),
            "-fa",
            TUNING.get("flash", "on"),
            "-ngl",
            "99",
            "--device",
            "SYCL0",
            "--split-mode",
            "none",
            "-lv",
            "4",
            "--fit",
            "off",
            "--reasoning",
            "off",
            "--jinja",
            "--no-agent",
            "--no-ui-mcp-proxy",
        ]
        if "kv" in TUNING:
            args.extend(["-ctk", TUNING["kv"], "-ctv", TUNING["kv"]])
        launched = int(time.time())
        subprocess.run(
            [
                "systemd-run",
                "--unit=" + UNIT,
                "--collect",
                *[v for p in props for v in ("-p", p)],
                "/bin/bash",
                "-c",
                shell,
                "hal-sycl",
                *args,
            ],
            check=True,
        )
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            try:
                seconds, discovery = watch(pool.submit(wait_ready, parent, model, launched))
                row.update(
                    load_seconds=seconds,
                    status="running",
                    measurement_started_at=datetime.datetime.now(datetime.timezone.utc).isoformat(),
                )
                report["gpu_discovery"] = discovery
                parent.publish("Running " + model + " on B65 SYCL for one hour")
                deadline = time.monotonic() + settings["duration_seconds"]
                slot = 1 if model.startswith(("qwen", "glm")) else 0
                watch(pool.submit(parent.guarded_worker, 0, slot, model, deadline, row))
                if time.monotonic() < deadline or row["errors"]:
                    raise RuntimeError("Hourly workload ended early")
            finally:
                peak = parent.ctl(
                    "show", UNIT, "-p", "MemoryPeak", "--value", check=False
                ).stdout.strip()
                if peak.isdecimal():
                    row["peak_host_ram_gib"] = int(peak) / 2**30
                parent.ctl("stop", UNIT, check=False)
    except Exception as error:
        row["status"] = "failed"
        report["error"] = str(error)
    finally:
        evidence = root / "failure-evidence.json"
        if evidence.exists():
            report["failure_diagnostic"] = json.loads(evidence.read_text())
        parent.save(root / "results.json", report)
    return 0 if row["status"] == "complete" else 1


if __name__ == "__main__":
    sys.exit(
        main(json.loads(Path(sys.argv[1]).read_text()), Path(os.environ["HAL_QUEUE_EVIDENCE"]))
    )
