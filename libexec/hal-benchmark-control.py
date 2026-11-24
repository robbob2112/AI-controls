#!/usr/bin/python3
"""Local operator broker for benchmark queue and model downloads."""
import shutil
import datetime, hashlib, importlib.util, json, os, pwd, re, socket, socketserver, struct, subprocess, threading, time, urllib.request, uuid
from pathlib import Path
import hal_bench_tuning as tuning
from hal_remote_models import lookup, model_card, model_purpose
from hal_model_catalog import (
    LITTLEBIT_MODEL as LITTLEBIT,
    NATIVE_PREFIX,
    installed_models,
    native_models,
)

spec = importlib.util.spec_from_file_location("worker", "/usr/local/libexec/hal-benchmark-queue.py")
worker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(worker)
Q = worker.Queue()
SOCKET = Path("/run/hal-benchmark-control/control.sock")
UID = pwd.getpwnam("hal-gateway").pw_uid
GID = pwd.getpwnam("hal-gateway").pw_gid
PULL_UNIT = "hal-model-pull-worker.service"
PULL_LOCK = threading.Lock()
LEGACY = {
    "legacy-r9700": ("hal-r9700-hours.service", "r9700-hours-20261122"),
    "legacy-b65": ("hal-b65-matched-hours.service", "b65-matched-hours-20261122"),
}
PULL = {"status": "idle"}
PROFILE_MAP = {
    ("both", "vulkan", "hourly"): "dual-vulkan-hours",
    ("cpu", "littlebit", "fixed"): "littlebit-cpu",
    ("r9700", "vulkan", "hourly"): "radeon-vulkan",
    ("b65", "vulkan", "hourly"): "b65-vulkan-hours",
    ("b65", "sycl", "hourly"): "b65-sycl-hours",
    ("r9700", "vulkan", "fixed"): "fixed-work-v1",
    ("b65", "vulkan", "fixed"): "fixed-work-v1",
}


def local(port, path, body=None, timeout=4, method=None):
    return urllib.request.urlopen(
        urllib.request.Request(
            f"http://127.0.0.1:{port}" + path,
            None if body is None else json.dumps(body).encode(),
            {"Content-Type": "application/json"},
            method=method,
        ),
        timeout=timeout,
    )


def get(port, path):
    with local(port, path) as response:
        return json.load(response)


def jobs():
    paths = Q.candidates()
    handoff = Q.root / "upgrade-hold"
    if handoff.is_dir():
        paths += list(handoff.glob("*.json"))
    return [
        (path, read(path))
        for path in sorted(paths, key=lambda p: (int(worker.NAME.fullmatch(p.name)[1]), p.name))
    ]


def read(path, default=None):
    try:
        value = worker.read_json(path)
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {} if default is None else default


def model_manifest(model):
    name, _, tag = model.partition(":")
    namespace, name = name.rsplit("/", 1) if "/" in name else ("library", name)
    registry = "registry.ollama.ai"
    if namespace.startswith("hf.co/"):
        registry, namespace = namespace.split("/", 1)
    return Path("/data/ollama/models/manifests") / registry / namespace / name / (tag or "latest")


def model_weight(model, digest):
    raw = model_manifest(model).read_bytes()
    layers = json.loads(raw)["layers"]
    aliases = {
        x["digest"].removeprefix("sha256:")
        for x in layers
        if x["mediaType"] == "application/vnd.docker.distribution.manifest.v2+json"
    }
    if digest not in aliases | {hashlib.sha256(raw).hexdigest()}:
        raise ValueError("Installed model manifest changed")
    weight = next(
        x["digest"].removeprefix("sha256:")
        for x in layers
        if x["mediaType"] == "application/vnd.ollama.image.model"
    )
    if not re.fullmatch("[a-f0-9]{64}", weight):
        raise ValueError("Unsupported model weights")
    with (Path("/data/ollama/models/blobs") / ("sha256-" + weight)).open("rb") as stream:
        if stream.read(4) != b"GGUF":
            raise ValueError("SYCL requires local GGUF weights")
    return weight


def mutable():
    marker = read(Q.root / "control-worker.json")
    pid = marker.get("pid")
    if marker.get("version") != 1 or not isinstance(pid, int):
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    try:
        command = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return False
    return command.split(b"\0")[1:2] == [b"/usr/local/libexec/hal-benchmark-queue.py"]


def groups():
    buckets = {}
    for path, job in jobs():
        key = job.get("pair_id") or path.name
        buckets.setdefault(key, []).append((path, job))
    return list(buckets.values())


def recover_rewrite():
    plan_path = Q.root / "pending-rewrite.json"
    if not plan_path.exists():
        return
    plan = worker.read_json(plan_path)
    if plan["phase"] == "stage":
        for item in plan["files"]:
            tmp = Q.pending / item["tmp"]
            if tmp.exists():
                continue
            if item["source"] is not None:
                (Q.pending / item["source"]).rename(tmp)
            else:
                worker.write_json(tmp, item["data"])
        plan["phase"] = "publish"
        worker.write_json(plan_path, plan)
    for item in plan["files"]:
        tmp = Q.pending / item["tmp"]
        dest = Q.pending / item["destination"]
        if tmp.exists():
            if dest.exists():
                raise ValueError("Queue changed outside the control page during reordering")
            tmp.rename(dest)
        elif not dest.exists():
            raise ValueError("Could not recover an incomplete queue edit")
    plan_path.unlink()


def rewrite(groups):
    recover_rewrite()
    files = []
    for i, (path, data) in enumerate((item for group in groups for item in group), 1):
        suffix = path.name.split("-", 1)[-1] if "-" in path.name else "manual-" + path.name
        files.append(
            {
                "source": path.name if path.exists() else None,
                "tmp": ".move-" + uuid.uuid4().hex,
                "destination": f"{i*10:03d}-" + suffix,
                "data": data if not path.exists() else None,
            }
        )
    worker.write_json(Q.root / "pending-rewrite.json", {"phase": "stage", "files": files})
    recover_rewrite()


def create(data):
    placement = data.get("placement")
    if placement not in ("now", "start", "end"):
        raise ValueError("Choose NOW, START of queue or END of queue")
    gpu = data.get("gpu")
    targets = ("r9700", "b65") if gpu == "compare" else (gpu,)
    additions = [build_entries(data, target, placement) for target in targets]
    with worker.schedule_lock(Q.root):
        recover_rewrite()
        existing = groups()
        rewrite(existing + additions if placement == "end" else additions + existing)
    return {
        "message": (
            "Queued a separate test for each GPU."
            if gpu == "compare"
            else "Added to the benchmark queue."
        )
    }


def build_entries(data, gpu, placement):
    backend, suite = data.get("backend"), data.get("suite")
    profile = PROFILE_MAP.get((gpu, backend, suite))
    if profile is None:
        raise ValueError("This GPU, backend and suite combination is unsupported")
    name = data.get("model")
    if not tuning.valid_model(name):
        raise ValueError("Invalid model name")
    model = next((m for m in installed_models() if m["name"] == name), None)
    if not model:
        raise ValueError("Choose an installed model")
    if not model["benchmark"]:
        raise ValueError(
            "This native llama.cpp preset has no supported local GGUF for benchmarking"
        )
    native = name.startswith(NATIVE_PREFIX)
    if native and profile != "b65-sycl-hours":
        raise ValueError("Native llama.cpp presets require B65 / SYCL / Timed")
    if (name == LITTLEBIT) != (profile == "littlebit-cpu"):
        raise ValueError("LittleBit requires CPU / LittleBit / Fixed work")
    pair = uuid.uuid4().hex
    job = {
        "version": 1,
        "kind": "benchmark",
        "profile": profile,
        "model": name,
        "model_digest": model["digest"],
        "pair_id": pair,
        "tuning": data.get("tuning", {}),
    }
    if profile != "radeon-vulkan":
        job["gpu"] = gpu
    if data.get("parallel", False):
        job["parallel"] = data["parallel"]
    if data.get("rear_percent") is not None:
        if profile != "radeon-vulkan":
            raise ValueError("Rear fan duty is available for timed Radeon Vulkan tests")
        job["rear_percent"] = data["rear_percent"]
    if suite == "fixed":
        job["timeout_seconds"] = data.get("seconds", 14400)
    else:
        job["duration_seconds"] = data.get("seconds", 3600)
    if profile == "b65-sycl-hours":
        if not native:
            job["weight_digest"] = model_weight(name, model["digest"])
        job["llama_cpp_git"] = subprocess.check_output(
            ["git", "-C", "/scratch/b65-model-testing/llama.cpp", "rev-parse", "HEAD"],
            text=True,
            timeout=5,
        ).strip()
    if placement != "now" and data.get("not_before"):
        worker.scheduled_time(data["not_before"])
        job["not_before"] = data["not_before"]
    worker.validate(job)
    new = [(Q.pending / ("new-test-" + pair[:10] + ".json"), job)]
    if gpu != "cpu" and data.get("cooldown", True):
        cool = {
            "version": 1,
            "kind": "cooldown",
            "gpu": gpu,
            "pair_id": pair,
            "target_c": "previous-start",
            "timeout_seconds": data.get("cooldown_seconds", 1800),
        }
        if job.get("not_before"):
            cool["not_before"] = job["not_before"]
        worker.validate(cool)
        new.append((Q.pending / ("new-cooldown-" + pair[:10] + ".json"), cool))
    return new


def archived_job(identifier):
    if not isinstance(identifier, str) or not re.fullmatch(
        "[0-9]{8}T[0-9]{6}Z-[a-f0-9]{10}", identifier
    ):
        raise ValueError("Invalid archive record ID")
    folder = Q.archive / identifier
    if not folder.is_dir() or folder.is_symlink():
        raise ValueError("Archived queue record not found")
    original = next(
        (p for p in folder.iterdir() if worker.NAME.fullmatch(p.name) and not p.is_symlink()), None
    )
    if original is None:
        raise ValueError("Original queue settings are unavailable")
    job = read(original)
    worker.validate(job)
    return job, read(folder / "outcome.json")


def replay(data):
    job, outcome = archived_job(data.get("id"))
    label = "Rerun" if outcome.get("status") == "complete" else "Retry"
    if data["op"] == "replay-preview":
        return {"job": job, "label": label, "status": outcome.get("status"), "editable": mutable()}
    if not mutable():
        raise ValueError("Queue worker is not ready")
    placement = data.get("placement")
    if placement not in ("now", "start", "end"):
        raise ValueError("Invalid queue placement")
    job.pop("pair_id", None)
    job.pop("not_before", None)
    pair = uuid.uuid4().hex
    job["pair_id"] = pair
    if job["kind"] == "cooldown" and job.get("target_c", "previous-start") == "previous-start":
        if not outcome.get("target_c"):
            raise ValueError("This archived cooldown has no saved target temperature")
        job["target_c"] = outcome["target_c"]
    worker.validate(job)
    entries = [(Q.pending / ("new-" + job["kind"] + "-" + pair[:10] + ".json"), job)]
    with worker.schedule_lock(Q.root):
        recover_rewrite()
        existing = groups()
        rewrite(existing + [entries] if placement == "end" else [entries] + existing)
    return {"message": label + " queued. New " + job["kind"] + " file created."}


def reorder(data):
    filename = data.get("file")
    direction = data.get("direction")
    if direction not in ("up", "down"):
        raise ValueError("Choose up or down")
    with worker.schedule_lock(Q.root):
        recover_rewrite()
        pending = groups()
        index = next(
            (i for i, group in enumerate(pending) if any(p.name == filename for p, _ in group)),
            None,
        )
        if index is None:
            raise ValueError("Queue changed. Refresh and retry.")
        other = index + (-1 if direction == "up" else 1)
        if 0 <= other < len(pending):
            pending[index], pending[other] = pending[other], pending[index]
            rewrite(pending)
    return {"message": "Queue order saved."}


def running():
    seen = []
    for folder in sorted(Q.running.iterdir()):
        if not folder.is_dir():
            continue
        file = next((p for p in folder.iterdir() if worker.NAME.fullmatch(p.name)), None)
        if file is None:
            continue
        job = read(file)
        outcome = read(folder / "outcome.json")
        progress = read(folder / "public-progress.json")
        stamp = (
            outcome.get("started_at")
            or datetime.datetime.fromtimestamp(
                folder.stat().st_mtime, datetime.timezone.utc
            ).isoformat()
        )
        begun = worker.scheduled_time(stamp)
        duration = job.get("duration_seconds", 3600)
        measurement = (progress.get("rows") or [{}])[0].get("measurement_started_at")
        eta = None
        mode = ""
        if job.get("kind") == "cooldown":
            eta = begun + job.get("timeout_seconds", 1800)
            mode = "timeout limit"
        elif job.get("profile") in ("fixed-work-v1", "littlebit-cpu"):
            cases = folder / "cases.jsonl"
            try:
                with cases.open() as f:
                    done = sum(1 for _ in f)
                if 0 < done < 99:
                    eta = begun + (time.time() - begun) * 99 / done
                    mode = f"rough estimate · {done}/99 cases"
            except OSError:
                pass
        elif measurement:
            eta = worker.scheduled_time(measurement) + duration
            mode = "hourly measurement"
        elif progress.get("status", "").lower().startswith("running"):
            eta = begun + duration
            mode = "estimate including startup"
        seen.append(
            {
                "id": folder.name,
                "name": file.name,
                "job": job,
                "status": progress.get("status", job.get("kind")),
                "started": begun,
                "eta": eta,
                "eta_mode": mode,
                "terminating": (folder / "terminate").exists(),
            }
        )
    for key, (unit, directory) in LEGACY.items():
        if not worker.service_active(unit):
            continue
        report = read(Path("/scratch/b65-model-testing") / directory / "results.json")
        row = (report.get("rows") or [{}])[-1]
        try:
            begun = worker.scheduled_time(row["measurement_started_at"])
        except (KeyError, ValueError):
            begun = time.time()
        seen.append(
            {
                "id": key,
                "name": directory,
                "job": {
                    "kind": "benchmark",
                    "model": row.get("model"),
                    "gpu": "b65" if key == "legacy-b65" else "r9700",
                },
                "status": report.get("status", "Loading"),
                "started": begun,
                "eta": begun + 3600 if row.get("status") == "running" else None,
                "eta_mode": "current measurement",
                "terminating": False,
            }
        )
    return seen


def terminate(data):
    if data.get("confirmed") is not True:
        raise ValueError("Confirm termination of this test")
    target = data.get("id")
    if isinstance(target, str) and target in LEGACY:
        unit = LEGACY[target][0]
        if not worker.service_active(unit):
            raise ValueError("Campaign has already stopped")
        subprocess.run(["systemctl", "stop", "--no-block", unit], check=True, timeout=8)
        return {"message": "Termination requested for the selected campaign."}
    if not mutable():
        raise ValueError("Queue controls activate after the current worker is upgraded.")
    if not isinstance(target, str) or not re.fullmatch("[0-9]{8}T[0-9]{6}Z-[0-9a-f]{10}", target):
        raise ValueError("Invalid running test ID")
    folder = Q.running / target
    try:
        (folder / "terminate").touch(exist_ok=True)
    except FileNotFoundError:
        raise ValueError("Test already finished")
    return {"message": "Termination requested. The worker will archive partial results."}


def pull_task(names):
    global PULL
    history = []
    name = names[0]
    try:
        with worker.schedule_lock(Q.root):
            state = read(worker.STATE)
            if (
                running()
                or worker.legacy_active()
                or (state.get("running") and state.get("expires", 0) > time.time())
            ):
                raise ValueError("A benchmark is active. Pull models after its current step.")
            subprocess.run(
                [
                    "systemd-run",
                    "--unit=" + PULL_UNIT,
                    "--collect",
                    "-p",
                    "User=ollama",
                    "-p",
                    "NoNewPrivileges=yes",
                    "-p",
                    "CapabilityBoundingSet=",
                    "-p",
                    "RuntimeMaxSec=7200",
                    "-p",
                    "TimeoutStopSec=10",
                    "-p",
                    "DevicePolicy=closed",
                    "--setenv=OLLAMA_HOST=127.0.0.1:11449",
                    "--setenv=OLLAMA_MODELS=/data/ollama/models",
                    "--setenv=OLLAMA_NO_CLOUD=1",
                    "/usr/bin/ollama",
                    "serve",
                ],
                check=True,
                timeout=15,
                capture_output=True,
            )
        for _ in range(20):
            try:
                get(11449, "/api/version")
                break
            except OSError:
                time.sleep(1)
        for index, name in enumerate(names):
            update = dict(model=name, index=index + 1, count=len(names), results=history.copy())
            try:
                status = "starting"
                PULL = dict(update, status=status)
                with local(
                    11449, "/api/pull", {"model": name, "stream": True}, timeout=180
                ) as response:
                    for line in response:
                        item = json.loads(line)
                        if item.get("error"):
                            raise RuntimeError(item["error"])
                        status = item["status"]
                        PULL = dict(
                            update,
                            status="finishing" if status == "success" else status,
                            **{k: item[k] for k in ("total", "completed") if k in item},
                        )
                if status != "success":
                    raise RuntimeError("Ollama did not report a completed pull")
                history.append(dict(model=name, status="success"))
            except Exception as error:
                history.append(dict(model=name, status="failed", error=str(error)[:500]))
        PULL = dict(
            status="failed" if any(item["status"] == "failed" for item in history) else "success",
            model=name,
            results=history,
        )
    except Exception as error:
        PULL = {"status": "failed", "model": name, "error": str(error)[:500], "results": history}
    finally:
        try:
            subprocess.run(
                ["systemctl", "stop", PULL_UNIT],
                timeout=20,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        finally:
            PULL_LOCK.release()


LITTLEBIT_WEIGHTS = Path("/data/models/littlebit-qwen3-8b-055")


def remove_littlebit():
    if LITTLEBIT_WEIGHTS.is_symlink() or not LITTLEBIT_WEIGHTS.is_dir():
        raise ValueError("LittleBit checkpoint folder is missing or invalid")
    subprocess.run(
        ["systemctl", "disable", "--now", "hal-littlebit.service"],
        check=True,
        timeout=12,
        capture_output=True,
    )
    shutil.rmtree(LITTLEBIT_WEIGHTS)


def delete_models(data):
    names = data.get("models")
    if (
        data.get("confirmed") is not True
        or not isinstance(names, list)
        or not 1 <= len(names) <= 12
        or any(not tuning.valid_model(name) for name in names)
    ):
        raise ValueError("Confirm deletion of 1 to 12 installed models")
    if not PULL_LOCK.acquire(blocking=False):
        raise ValueError("A model install or removal is in progress")
    try:
        with worker.schedule_lock(Q.root):
            if running() or worker.legacy_active():
                raise ValueError("Wait for the active benchmark to finish before deleting models")
            installed = {model["name"] for model in get(11434, "/api/tags")["models"]}
            queued = {job.get("model") for _, job in jobs()}
            native_config = Path("/etc/hal-llama/models.ini").read_text()
            native_blobs = set(re.findall(r"\bsha256-[0-9a-f]{64}\b", native_config))
            outcomes = []
            for name in dict.fromkeys(names):
                try:
                    if name in queued:
                        raise ValueError("This model is used by a pending benchmark")
                    if name == LITTLEBIT:
                        remove_littlebit()
                        outcomes.append({"model": name, "status": "deleted"})
                        continue
                    if name not in installed:
                        raise ValueError("This model is not installed in Ollama")
                    manifest = json.loads(model_manifest(name).read_bytes())
                    blobs = {
                        item["digest"].replace("sha256:", "sha256-") for item in manifest["layers"]
                    }
                    if blobs & native_blobs:
                        raise ValueError("A native llama.cpp preset references these weights")
                    with local(11434, "/api/delete", {"model": name}, timeout=10, method="DELETE"):
                        pass
                    outcomes.append({"model": name, "status": "deleted"})
                except (OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
                    outcomes.append({"model": name, "status": "failed", "error": str(error)[:300]})
            return {"results": outcomes}
    finally:
        PULL_LOCK.release()


def dispatch(data):
    global PULL
    if not isinstance(data, dict):
        raise ValueError("Expected a JSON object")
    operation = data.get("op")
    if operation in ("create", "reorder", "pull") and not mutable():
        raise ValueError("Queue controls activate after the current worker is upgraded.")
    if operation in ("replay-preview", "replay"):
        return replay(data)
    if operation == "create":
        return create(data)
    if operation == "reorder":
        return reorder(data)
    if operation == "terminate":
        return terminate(data)
    if operation == "model-delete":
        return delete_models(data)
    if operation == "model-card":
        return {"description": model_card(data)}
    if operation == "model-search":
        return {"matches": lookup(data)}
    if operation == "pull":
        names = data.get("models", [data.get("model")])
        if (
            not isinstance(names, list)
            or not 1 <= len(names) <= 12
            or any(
                not tuning.valid_model(name) or "cloud" in name.partition(":")[2].lower()
                for name in names
            )
        ):
            raise ValueError("Choose 1 to 12 local model tags")
        names = list(dict.fromkeys(names))
        if not PULL_LOCK.acquire(blocking=False):
            raise ValueError("A model pull is already in progress")
        PULL = {"status": "starting", "model": names[0], "count": len(names)}
        threading.Thread(target=pull_task, args=(names,), daemon=True).start()
        return {"message": "Model pull requested."}
    if operation != "state":
        raise ValueError("Unknown operation")
    models = [dict(model, purpose=model_purpose(model["name"])) for model in installed_models()]
    loaded = []
    for port, label in (
        (11434, "Ollama Auto"),
        (11444, "B65 chat"),
        (11446, "R9700 chat"),
        (11445, "Benchmark Vulkan"),
        (11453, "B65 benchmark"),
    ):
        try:
            for model in get(port, "/api/ps").get("models", []):
                loaded.append(
                    {"name": model["name"], "backend": label, "vram": model.get("size_vram")}
                )
        except (OSError, ValueError):
            pass
    try:
        cpu = get(11450, "/health")
        if cpu.get("loaded"):
            loaded.append({"name": LITTLEBIT, "backend": "LittleBit CPU"})
    except (OSError, ValueError):
        pass
    for item in native_models():
        if item.get("status", {}).get("value") == "loaded":
            loaded.append({"name": item["id"], "backend": "Native llama.cpp"})
    current = running()
    for run in current:
        if run["job"].get("profile") == "b65-sycl-hours":
            loaded.append({"name": run["job"]["model"], "backend": "Benchmark SYCL"})
    return {
        "pending": [
            dict(file=p.name, **{k: v for k, v in job.items() if k != "file"}) for p, job in jobs()
        ],
        "running": current,
        "models": models,
        "loaded": loaded,
        "pull": PULL,
        "options": tuning.OPTIONS,
        "cpu_options": tuning.cpu_options(),
        "editable": mutable(),
    }


class Handler(socketserver.StreamRequestHandler):
    def handle(self):
        _, uid, _ = struct.unpack(
            "3i", self.request.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
        )
        if uid not in (0, UID):
            return
        self.request.settimeout(15)
        try:
            raw = self.rfile.readline(65537)
            if not raw.endswith(b"\n") or len(raw) > 65536:
                raise ValueError("Request too large")
            response = {"ok": True, **dispatch(json.loads(raw))}
        except (
            ValueError,
            TypeError,
            KeyError,
            OSError,
            RuntimeError,
            StopIteration,
            subprocess.SubprocessError,
        ) as error:
            response = {"ok": False, "error": str(error)[:500]}
        self.wfile.write((json.dumps(response) + "\n").encode())


if __name__ == "__main__":
    with worker.schedule_lock(Q.root):
        recover_rewrite()
    SOCKET.unlink(missing_ok=True)
    with socketserver.ThreadingUnixStreamServer(str(SOCKET), Handler) as server:
        os.chown(SOCKET, 0, GID)
        SOCKET.chmod(0o660)
        server.serve_forever()
