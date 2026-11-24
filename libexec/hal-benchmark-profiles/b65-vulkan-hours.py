"""Two local one-hour concurrent pairs; fixed fixtures, GPU residency and cooling evidence."""

import concurrent.futures, datetime, json, os, pathlib, re, signal, sqlite3, statistics, subprocess as sp, sys, threading, time, urllib.request

sys.path.insert(0, "/usr/local/libexec")
import hal_bench_tuning
from hal_cooling_policy import gpu_duty

ROOT = pathlib.Path(os.environ["HAL_QUEUE_EVIDENCE"])
D = pathlib.Path("/scratch/b65-model-testing/expanded-20261113")
R = pathlib.Path("/scratch/b65-model-testing/results-20261113")
STOP = threading.Event()
LOCK = threading.RLock()
ACTIVE = {}
CURRENT = []
PAIRS = [["deepseek-r1:32b", "qwen3.5:35b-a3b"], ["gemma4:26b", "glm-4.7-flash:q4_K_M"]]
MBPP = json.loads((D / "mbpp20.json").read_text())
GSM = json.loads((D / "gsm8k50.json").read_text())
TOOLS = json.loads((D / "hal-tool20.json").read_text())
REPORT = {
    "id": "b65-matched-hours-20261122",
    "recorded_at": datetime.datetime.now().astimezone().isoformat(),
    "campaign": "Two independent-GPU model pairs, one hour each",
    "rows": [],
    "accuracy": [],
    "models": PAIRS,
    "hardware": {"GPU0": "B65 PCI04:00.0,32GB", "GPU1": "B65 PCI09:00.0,32GB"},
    "versions": {
        "kernel": os.uname().release,
        "backend": "Ollama0.40.13 / bundled llama.cpp Vulkan",
        "context": 8192,
        "threads_per_model": 8,
    },
    "notes": [
        "Each pair runs3600seconds after both models are loaded; separate visible physical GPU per service; model weights remain fully in VRAM. CPU tokenization/runtime work is normal and is not model-layer offload.",
        "Original MBPP20/GSM8K50/HALtool20 fixtures rotate with speed samples. Deadline-limited coverage is explicit; incomplete cases are not silently graded as model failures. First-pass unique cases determine accuracy. Repeats continue for thermal load.",
        "Native tool capability is checked. If unavailable, the same cases receive a labelled text-JSON schema test; native-tool support remains unsupported, not passed. Tool requests are compared as data, never executed.",
        "Coding candidates run only in the established no-network bubblewrap sandbox. No host tools or hardware commands are granted to any model.",
        "Test output budgets match the earlier suite, not conversation limits. DeepSeek uses recommended temperature0.6; others use previous temperature0. Seed42; fast profile/thinkingfalse. Backend/template differences make old SYCL scores screening references.",
        "Local timed job and telemetry use no Codex or paid API. Agent setup/review uses included usage only. VM findings are read-only, recorded for tomorrow; guest code is not repaired.",
    ],
}


def save(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2))
    tmp.replace(path)


def publish(stage=None):
    with LOCK:
        if stage:
            REPORT["status"] = stage
        REPORT["updated_at"] = datetime.datetime.now().astimezone().isoformat()
        save(ROOT / "results.json", REPORT)
        save(
            ROOT / "public-progress.json",
            {
                "status": REPORT.get("status"),
                "rows": REPORT["rows"],
                "fan_summary": REPORT.get("fan_summary"),
                "phase": REPORT.get("phase"),
                "time": time.time(),
            },
        )
        for row in REPORT["rows"]:
            save(ROOT / (row["id"] + ".json"), row)


def api(gpu, path, body=None, timeout=30):
    req = urllib.request.Request(
        f"http://127.0.0.1:{os.environ.get('HAL_BENCH_PORT','11445')}" + path,
        None if body is None else json.dumps(hal_bench_tuning.ollama_body(body)).encode(),
        {"Content-Type": "application/json"},
    )
    return json.load(urllib.request.urlopen(req, timeout=timeout))


def ctl(*args, check=True):
    return sp.run(["systemctl", *args], check=check, capture_output=True, text=True, timeout=45)


def tasks():
    out = []
    for i in range(50):
        if i % 10 == 0:
            out.append(("speed", i))
        if i < 20:
            out.append(("coding", i))
        out.append(("reasoning", i))
        if i < 20:
            out.append(("tools", i))
    return out


TASKS = tasks()


def answer_task(gpu, model, kind, index, native, remaining):
    options = {
        "num_ctx": 8192,
        "num_gpu": 999,
        "num_thread": 8,
        "seed": 42,
        "temperature": 0.6 if model.startswith("deepseek") else 0,
    }
    body = {"model": model, "stream": False, "think": False, "keep_alive": -1, "options": options}
    expected = None
    if kind == "coding":
        t = MBPP[index]
        prompt = (
            "Write a Python function solving this task. Return only Python source code, without markdown or explanations. Include needed standard-library imports.\n"
            + t["prompt"]
            + "\nExample test (other tests are held out):\n"
            + t["test_list"][0]
        )
        options["num_predict"] = 1536
    elif kind == "reasoning":
        t = GSM[index]
        prompt = (
            t["question"]
            + "\nSolve step by step, then write your final numeric answer as #### <answer>."
        )
        options["num_predict"] = 2048
    elif kind == "tools":
        t = TOOLS["cases"][index]
        prompt = t["prompt"]
        options["num_predict"] = 512
        if native:
            body["tools"] = TOOLS["tools"]
        else:
            prompt += (
                '\nReturn ONLY JSON {"tool_calls":[{"name":"function","arguments":{}}]} or {"tool_calls":[]} when none is appropriate. This is a non-executing test. Available schemas:\n'
                + json.dumps(TOOLS["tools"])
            )
    else:
        prompt = (
            "Write a detailed Python implementation and explanation of a thread-safe bounded work queue, including cancellation, retries, backpressure, and unit tests. Continue until complete.\nBenchmark variant "
            + str(index)
            + " on GPU "
            + str(gpu)
        )
        options["num_predict"] = 512
    body["messages"] = [{"role": "user", "content": prompt}]
    if kind == "tools" and native:
        body["messages"].insert(
            0,
            {
                "role": "system",
                "content": "Use the provided functions when requested. Return exact function names and required arguments. Do not invent results. Do not call functions for greetings or explanations requiring none.",
            },
        )
    started = time.monotonic()
    raw = api(gpu, "/api/chat", body, timeout=max(1, min(remaining, 330)))
    content = raw.get("message", {}).get("content", "")
    row = {
        "kind": kind,
        "index": index,
        "seconds": time.monotonic() - started,
        "response": raw,
        "tokens": raw.get("eval_count", 0),
        "truncated": raw.get("done_reason") == "length",
    }
    if kind == "coding":
        blocks = re.findall(r"```(?:python|py)?\s*\n(.*?)```", content, re.S)
        code = blocks[0] if blocks else content
        row["pass"], row["sandbox_output"] = quality.check_code(code, MBPP[index])
    elif kind == "reasoning":
        actual = quality.normalized_number(content.rsplit("####", 1)[-1])
        expected = quality.normalized_number(GSM[index]["answer"].rsplit("####", 1)[-1])
        row.update(actual=actual, expected=expected)
        row["pass"] = actual == expected
    elif kind == "tools":
        actual = []
        if native:
            for call in raw.get("message", {}).get("tool_calls", []) or []:
                f = call["function"]
                args = f.get("arguments", {})
                actual.append(
                    {
                        "name": f["name"],
                        "arguments": json.loads(args) if isinstance(args, str) else args,
                    }
                )
        else:
            try:
                text = re.sub(r"^```(?:json)?\s*|\s*```$", "", content.strip())
                match = re.search(r"\{.*\}", text, re.S)
                actual = json.loads(match.group(0) if match else text).get("tool_calls", [])
            except Exception:
                actual = []
        row.update(
            actual=actual,
            expected=TOOLS["cases"][index]["expected"],
            tool_mode="native" if native else "text-schema fallback",
        )
        row["pass"] = actual == row["expected"]
    return row


def model_worker(phase, gpu, model, deadline, row):
    native = "tools" in api(gpu, "/api/show", {"model": model}).get("capabilities", [])
    row["native_tool_support"] = native
    index = gpu * 47
    speeds = []
    first = {}
    completed = 0
    while not STOP.is_set() and time.monotonic() < deadline:
        with LOCK:
            for _ in range(len(TASKS)):
                kind, case = TASKS[index % len(TASKS)]
                index += 1
                if (kind, case) not in ACTIVE.values():
                    break
            ACTIVE[gpu] = (kind, case)
        try:
            result = answer_task(gpu, model, kind, case, native, deadline - time.monotonic())
            with LOCK:
                with (ROOT / f"phase{phase}-gpu{gpu}-answers.jsonl").open("a") as f:
                    f.write(json.dumps(result) + "\n")
                completed += 1
                row["completed_requests"] = completed
                row["tokens"] += result["tokens"]
                if kind == "speed" and result["response"].get("eval_duration", 0) > 0:
                    speeds.append(result["tokens"] * 1e9 / result["response"]["eval_duration"])
                    row.update(
                        rate=statistics.median(speeds),
                        mean_rate=statistics.mean(speeds),
                        peak_rate=max(speeds),
                        trials=speeds,
                    )
                if kind != "speed":
                    first.setdefault((kind, case), result)
                row["coverage"] = {
                    k: {
                        "completed": sum(key[0] == k for key in first),
                        "total": n,
                        "correct": sum(
                            key[0] == k and value.get("pass", False) for key, value in first.items()
                        ),
                    }
                    for k, n in [("coding", 20), ("reasoning", 50), ("tools", 20)]
                }
                row["elapsed_seconds"] = DURATION - max(0, deadline - time.monotonic())
                publish()
        except Exception as e:
            if STOP.is_set() or time.monotonic() >= deadline:
                row["deadline_interruption"] = True
                break
            row["errors"].append(str(e))
            row["status"] = "failed"
            STOP.set()
            break
        finally:
            with LOCK:
                ACTIVE.pop(gpu, None)
    with LOCK:
        row["status"] = "failed" if row["errors"] else "stopped" if STOP.is_set() else "complete"
        row["settings"] += "; native tool support " + str(native)
        for kind, n in [("coding", 20), ("reasoning", 50), ("tools", 20)]:
            samples = [v for (k, i), v in first.items() if k == kind]
            if samples:
                REPORT["accuracy"].append(
                    {
                        "model": model,
                        "gpu": "Intel Arc Pro B65-0",
                        "benchmark": {
                            "coding": "MBPP20",
                            "reasoning": "GSM8K50",
                            "tools": "HAL tool20"
                            + (
                                " native" if native else " text-schema fallback; native unsupported"
                            ),
                        }[kind],
                        "examples": len(samples),
                        "score": sum(v.get("pass", False) for v in samples) / len(samples),
                        "settings": f"{len(samples)}/{n} unique cases completed within1hour; partial coverage is not full-suite accuracy",
                    }
                )
        publish()


def guarded_worker(*args):
    try:
        return model_worker(*args)
    except Exception as e:
        row = args[-1]
        row["errors"].append(str(e))
        row["status"] = "failed"
        STOP.set()
        publish()


def safe_monitor(*args):
    try:
        return monitor(*args)
    except Exception:
        STOP.set()
        for unit in list(CURRENT):
            ctl("stop", unit, check=False)
        raise


import quality

PCI = "0000:04:00.0"
RADEON = "0000:09:00.0"
SPLIT = False
TARGET = "Intel Arc Pro B65-0"
RUNS = [(1, 0, PAIRS[1][0]), (0, 1, PAIRS[0][1]), (1, 1, PAIRS[1][1]), (0, 0, PAIRS[0][0])]
REPORT.update(
    campaign="R9700 single-GPU matched one-hour runs",
    models=[r[2] for r in RUNS],
    hardware={"GPU": f"Intel Arc Pro B65-0 PCI {PCI}"},
    notes=[
        "B65-only Vulkan ICD and RADV_DEBUG=nocompute. Default Vulkan hit a compute timeout and recovered via GPU reset; a 29/29-offloaded isolated preflight passed with the workaround.",
        "One main request stream per model for 3600 seconds after verified load. Fixture order/offset, seed, sampling, context and scoring are from the previous paired campaign. Other GPU does not assist. Earlier B65 tests ran two simultaneous models; shared host load and driver workaround differ.",
        "Rate, mean_rate and peak_rate come from completed 512-token speed samples. Load time includes an initial one-token readiness request. Peak host RAM is the service cgroup high-water mark, including charged file cache. VRAM residency is separate. TTFT was not measured.",
        "B65 GPU fan stays firmware controlled. B65 firmware manages its temperature limit. Above 75°C on core/hotspot or VRAM, the chassis policy requests 100% front and 50% rear. Fan RPM, cooling health and kernel errors are monitored through warmup and workload.",
    ],
)


def monitor(since, row):
    state = json.loads(pathlib.Path("/run/hal-cooling/status.json").read_text())
    if (
        not state["ok"]
        or not state["sensors_fresh"]
        or time.time() - state["time"] > 4
        or state.get("control_protection")
    ):
        raise RuntimeError("Cooling readings or control unhealthy")
    if not 0 < state["temperatures"]["cpu_c"] < 90:
        raise RuntimeError("CPU thermal stop")
    gpu = next(g for g in state["temperatures"]["gpus"] if g["pci"] == PCI and g["driver"] == "xe")
    if not 0 < gpu["pkg_c"] < 85 or not 0 < gpu["vram_c"] < 95 or not gpu["rpm"]:
        raise RuntimeError("B65 sensor or firmware fan watchdog stop: " + str(gpu))
    if SPLIT:
        radeon = next(
            (
                g
                for g in state["temperatures"]["gpus"]
                if g["pci"] == RADEON and g["driver"] == "amdgpu"
            ),
            {},
        )
        if (
            not 0 < (radeon.get("pkg_c") or 0) < 130
            or not 0 < (radeon.get("vram_c") or 0) < 130
            or not radeon.get("rpm")
        ):
            raise RuntimeError("Radeon sensor or firmware fan watchdog stop: " + str(radeon))
    expected = (
        {n: 100 for n in ("CHA_FAN3", "CHA_FAN4", "CHA_FAN5")} if state["policy"]["gpu_hot"] else {}
    )
    if state["policy"]["mode"] == "radeon-boost":
        expected["CHA_FAN1"] = 50
    if state["policy"]["mode"] == "benchmark-cooldown":
        expected["CHA_FAN1"] = 100
    if not all(
        state["fans"][n]["applied"] and all(p["Duty"] == duty for p in state["fans"][n]["curve"])
        for n, duty in expected.items()
    ):
        # Cooling daemon reapplies chassis curves asynchronously. Allow a brief transition.
        row["unapplied_since"] = row.get("unapplied_since", time.monotonic())
        if time.monotonic() - row["unapplied_since"] > 15:
            raise RuntimeError("Chassis cooling did not apply")
    else:
        row.pop("unapplied_since", None)
    log = sp.check_output(
        ["journalctl", "-k", "-b", "--since", "@" + str(since), "-o", "cat", "--no-pager"],
        text=True,
        timeout=5,
    )
    if any(
        re.search(r"(timeout|page fault|GPU reset|wedged)", l, re.I)
        for l in log.splitlines()
        if ("xe" in l and PCI in l) or (SPLIT and "amdgpu" in l and RADEON in l)
    ):
        (ROOT / "kernel-stop.log").write_text(log)
        raise RuntimeError("GPU driver fault, timeout or reset; see kernel-stop.log")
    with (ROOT / "physical-cooling.jsonl").open("a") as f:
        f.write(json.dumps(state) + "\n")
    return gpu


def load(unit, model):
    for _ in range(25):
        try:
            version = api(0, "/api/version", timeout=1)["version"]
            if version != "0.40.13":
                raise RuntimeError("Unmatched Ollama version " + version)
            break
        except OSError:
            time.sleep(1)
    start = time.monotonic()
    api(
        0,
        "/api/generate",
        {
            "model": model,
            "prompt": "Ready.",
            "stream": False,
            "think": False,
            "keep_alive": -1,
            "options": {"num_gpu": 999, "num_ctx": 8192, "num_thread": 8, "num_predict": 1},
        },
        timeout=240,
    )
    seconds = time.monotonic() - start
    loaded = next(x for x in api(0, "/api/ps")["models"] if x["name"] == model)
    invocation = ctl("show", unit, "-p", "InvocationID", "--value").stdout.strip()
    log = sp.check_output(
        ["journalctl", "_SYSTEMD_INVOCATION_ID=" + invocation, "--no-pager", "-o", "cat"], text=True
    )
    if SPLIT:
        (ROOT / "ollama-load.log").write_text(log)
    lines = [l for l in log.splitlines() if "inference compute" in l]
    expected = {PCI, RADEON} if SPLIT else {PCI}
    observed = set(re.findall(r"pci_id=([0-9a-f:.]+)", "\n".join(lines)))
    if observed != expected:
        raise RuntimeError(
            f"Unexpected GPU visibility: expected {sorted(expected)}, found {sorted(observed)}"
        )
    if SPLIT:
        weights = re.findall(
            r"load_tensors:\s+(Vulkan[0-9]+) model buffer size\s*=\s*([0-9.]+) MiB", log
        )
        devices = {device for device, size in weights if float(size) > 0}
        if len(devices) != 2:
            raise RuntimeError(
                "Could not verify that both GPUs hold model weights; see Ollama load log"
            )
    if loaded["size_vram"] < loaded["size"] * 0.98:
        raise RuntimeError("GPU residency check failed")
    offloads = re.findall(r"offloaded (\d+)/(\d+) layers to GPU", log)
    if not offloads or any(a != b or a == "0" for a, b in offloads):
        raise RuntimeError("Model layers did not fully offload")
    return loaded, seconds, lines


def halt(*_):
    STOP.set()


signal.signal(signal.SIGTERM, halt)
signal.signal(signal.SIGINT, halt)


def run():
    for fixture in MBPP:
        ok, note = quality.check_code(fixture["code"], fixture)
        if not ok:
            raise RuntimeError("Reference sandbox fixture failed: " + note)
    for index, (phase, slot, model) in enumerate(RUNS):
        if STOP.is_set():
            break
        row = dict(
            id=f"b65-matched-hours-20261122-{index+1}",
            model=model,
            gpu=TARGET,
            backend="Ollama 0.40.13 / Vulkan",
            campaign="b65-matched-hours-20261122",
            streams=1,
            rate=None,
            tokens=0,
            trials=[],
            status="loading",
            completed_requests=0,
            coverage={},
            errors=[],
            settings="One hour; single B65; context 8192; 8 host threads; seed 42; original fixture order, caps and grading.",
        )
        REPORT["rows"].append(row)
        REPORT["phase"] = index + 1
        unit = os.environ.get("HAL_BENCH_UNIT", "hal-queue-radeon-infer.service")
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
            "VK_DRIVER_FILES": "/usr/share/vulkan/icd.d/intel_icd.x86_64.json",
        }
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
        if SPLIT:
            env["VK_DRIVER_FILES"] += ":/usr/share/vulkan/icd.d/radeon_icd.x86_64.json"
            env["OLLAMA_SCHED_SPREAD"] = "true"
            env["OLLAMA_DEBUG"] = "1"
            if hal_bench_tuning.settings().get("radeon_compute", "safe") == "safe":
                env["RADV_DEBUG"] = "nocompute"
            props.append(f"DeviceAllow=/dev/dri/by-path/pci-{RADEON}-render rw")
            row["settings"] = "Timed single stream with the model split across both Vulkan GPUs."
        since = int(time.time())
        baseline = monitor(since, row)
        row["starting_temperatures_c"] = {k: baseline[k] for k in ("pkg_c", "vram_c")}
        publish("Loading " + model + " on " + TARGET)
        sp.run(
            [
                "systemd-run",
                "--unit=" + unit,
                "--collect",
                *[v for p in props for v in ("-p", p)],
                *["--setenv=" + k + "=" + v for k, v in hal_bench_tuning.ollama_env(env).items()],
                "/usr/bin/ollama",
                "serve",
            ],
            check=True,
        )
        try:
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                try:
                    future = pool.submit(load, unit, model)
                    while not future.done() and not STOP.wait(1):
                        monitor(since, row)
                    if STOP.is_set():
                        raise RuntimeError("Stopped during loading")
                    loaded, seconds, lines = future.result()
                    if (
                        specification.get("model_digest")
                        and loaded["digest"] != specification["model_digest"]
                    ):
                        raise RuntimeError("Model digest changed")
                    save(
                        ROOT / f"residency-{index+1}.json",
                        {"model": loaded, "discovery": lines, "pci": PCI},
                    )
                    row.update(residency=loaded, load_seconds=seconds, status="running")
                    row["measurement_started_at"] = datetime.datetime.now().astimezone().isoformat()
                    deadline = time.monotonic() + DURATION
                    publish(
                        "Running " + model + " on " + TARGET + " for " + str(DURATION) + " seconds"
                    )
                    future = pool.submit(guarded_worker, index, slot, model, deadline, row)
                    while not future.done() and not STOP.wait(1):
                        monitor(since, row)
                        if int(time.monotonic()) % 5 == 0:
                            publish()
                    if STOP.is_set():
                        raise RuntimeError("Worker stopped early: " + str(row["errors"]))
                    future.result()
                    if time.monotonic() < deadline:
                        raise RuntimeError("Worker finished before measurement deadline")
                finally:
                    peak = ctl(
                        "show", unit, "-p", "MemoryPeak", "--value", check=False
                    ).stdout.strip()
                    if peak.isdecimal():
                        row["peak_host_ram_gib"] = int(peak) / 2**30
                    ctl("stop", unit, check=False)
        except Exception as e:
            row["status"] = "failed"
            row["errors"].append(str(e))
            STOP.set()
            raise
        finally:
            ctl("stop", unit, check=False)
    REPORT["complete"] = len(REPORT["rows"]) == len(RUNS) and not STOP.is_set()


if __name__ == "__main__":
    specification = json.loads(pathlib.Path(sys.argv[1]).read_text())
    SPLIT = specification["profile"] == "dual-vulkan-hours"
    if SPLIT:
        TARGET = "Radeon R9700 + Intel Arc Pro B65-0"
        REPORT.update(
            campaign="Dual-GPU split · Vulkan",
            hardware={"GPUs": TARGET},
            notes=[
                "Timed single-stream workload splits model weights across both GPUs. Both devices are monitored for fan, sensor and driver faults."
            ],
        )
    DURATION = specification["duration_seconds"]
    model = specification["model"]
    RUNS = [next((item for item in RUNS if item[2] == model), (0, 0, model))]
    REPORT["models"] = [model]
    try:
        run()
    except Exception as error:
        REPORT["error"] = str(error)
    finally:
        REPORT["finished"] = True
        publish(
            "Complete"
            if REPORT.get("complete")
            else "Failed: " + REPORT.get("error", "interrupted")
        )
    sys.exit(0 if REPORT.get("complete") else 1)
