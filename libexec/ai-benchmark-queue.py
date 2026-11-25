#!/usr/bin/python3
"""One durable, ordered queue for HAL benchmark and cooldown steps."""
import concurrent.futures
import datetime
from contextlib import contextmanager
import fcntl
import json
import logging
import os
from pathlib import Path
import re
import signal
import subprocess
import time
import uuid
import ai_bench_tuning
from ai_model_catalog import NATIVE_PREFIX
from ai_benchmark_gpu_clear import clear_gpu

ROOT = Path("/var/lib/hal-benchmark-queue")
PUBLIC = Path("/var/lib/hal-benchmarks")
STATE = Path("/var/lib/hal-monitor/benchmark-state.json")
SENSORS = Path("/run/hal-cooling/status.json")
COOLDOWN = Path("/run/hal-cooling/benchmark-cooldown")
REAR = Path("/run/hal-cooling/benchmark-rear.json")
PROFILES = Path("/usr/local/libexec/ai-benchmark-profiles")
UNIT = "hal-queue-radeon-infer.service"
MODELS = frozenset(("deepseek-r1:32b", "qwen3.5:35b-a3b", "gemma4:26b", "glm-4.7-flash:q4_K_M"))
NAME = re.compile(r"(\d+)(?:-[A-Za-z0-9_.-]+)?\.json\Z")
STOP = False


def write_json(path, value):
    tmp = path.with_name("." + path.name + "." + uuid.uuid4().hex)
    with tmp.open("x") as f:
        json.dump(value, f, allow_nan=False, indent=2)
        f.write("\n")
        f.flush()
        os.fsync(f.fileno())
    tmp.replace(path)


@contextmanager
def schedule_lock(root):
    with (root / "schedule.lock").open("a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        yield


def scheduled_time(value):
    if not isinstance(value, str):
        raise ValueError("Scheduled time must include a time zone")
    moment = datetime.datetime.fromisoformat(value)
    if moment.tzinfo is None:
        raise ValueError("Scheduled time must include a time zone")
    return moment.timestamp()


def read_json(path):
    with path.open() as f:
        return json.load(f)


def service_active(unit):
    return subprocess.run(["systemctl", "is-active", "--quiet", unit], timeout=8).returncode == 0


def legacy_active():
    return any(
        service_active(unit)
        for unit in (
            "hal-r9700-hours.service",
            "hal-b65-matched-hours.service",
            "hal-model-pull-worker.service",
        )
    )


def sensors(gpu_id="r9700"):
    if gpu_id == "both":
        return {
            f"{gpu}_{key}": value for gpu in ("b65", "r9700") for key, value in sensors(gpu).items()
        }
    pci, driver = {"r9700": ("0000:09:00.0", "amdgpu"), "b65": ("0000:04:00.0", "xe")}[gpu_id]
    state = read_json(SENSORS)
    if not state["ok"] or not state["sensors_fresh"] or time.time() - state["time"] > 4:
        raise RuntimeError("Cooling telemetry is unhealthy or stale")
    gpu = next(
        g for g in state["temperatures"]["gpus"] if g["pci"] == pci and g["driver"] == driver
    )
    result = {k: gpu[k] for k in ("pkg_c", "vram_c")}
    if not all(type(v) in (float, int) and 0 < v < 130 for v in result.values()):
        raise RuntimeError(f"Invalid temperatures on {gpu_id}")
    return result


def number(spec, key, default, low, high):
    value = spec.get(key, default)
    if type(value) != int or not low <= value <= high:
        raise ValueError(f"{key} must be an integer between {low} and {high}")
    return value


def validate(spec):
    if not isinstance(spec, dict) or spec.get("version") != 1:
        raise ValueError("Expected a version 1 job object")
    kind = spec.get("kind")
    common = {"version", "kind", "label", "notes", "not_before", "pair_id"}
    if "not_before" in spec:
        scheduled_time(spec["not_before"])
    if "pair_id" in spec and (
        not isinstance(spec["pair_id"], str) or not re.fullmatch("[a-f0-9]{32}", spec["pair_id"])
    ):
        raise ValueError("Invalid benchmark pair ID")
    if (
        "label" in spec
        and not isinstance(spec["label"], str)
        or "notes" in spec
        and not isinstance(spec["notes"], str)
    ):
        raise ValueError("label and notes must be text")
    if kind == "benchmark":
        profile = spec.get("profile")
        ai_bench_tuning.validate(spec.get("tuning", {}), profile, spec.get("gpu", "r9700"))
        common = common | {"tuning", "model_digest", "parallel"}
        if type(spec.get("parallel", False)) is not bool:
            raise ValueError("Invalid parallel benchmark setting")
        if spec.get("parallel") and spec.get("gpu", "r9700") not in ("b65", "r9700"):
            raise ValueError("Parallel jobs require one GPU")
        if "model_digest" in spec and (
            not isinstance(spec["model_digest"], str)
            or not re.fullmatch("[0-9a-f]{64}", spec["model_digest"])
        ):
            raise ValueError("Invalid model digest")
        if not ai_bench_tuning.valid_model(spec.get("model")):
            raise ValueError("Select a supported model")
        native = spec["model"].startswith(NATIVE_PREFIX)
        if native and profile != "b65-sycl-hours":
            raise ValueError("Native llama.cpp presets require the B65 SYCL profile")
        if profile == "radeon-vulkan":
            if set(spec) - common - {"profile", "model", "duration_seconds", "rear_percent"}:
                raise ValueError("Unknown hourly benchmark setting")
            spec["duration_seconds"] = number(spec, "duration_seconds", 3600, 10, 3600)
            if "rear_percent" in spec:
                number(spec, "rear_percent", None, 1, 100)
        elif profile == "dual-vulkan-hours":
            if (
                set(spec) - common - {"profile", "gpu", "model", "duration_seconds"}
                or spec.get("gpu") != "both"
            ):
                raise ValueError("Invalid dual-GPU Vulkan settings")
            spec["duration_seconds"] = number(spec, "duration_seconds", 3600, 10, 3600)
        elif profile == "b65-vulkan-hours":
            if (
                set(spec) - common - {"profile", "gpu", "model", "duration_seconds"}
                or spec.get("gpu") != "b65"
            ):
                raise ValueError("Invalid B65 Vulkan hourly settings")
            spec["duration_seconds"] = number(spec, "duration_seconds", 3600, 10, 3600)
        elif profile == "b65-sycl-hours":
            if (
                set(spec)
                - common
                - {"profile", "gpu", "model", "weight_digest", "llama_cpp_git", "duration_seconds"}
            ):
                raise ValueError("Unknown SYCL benchmark setting")
            if spec.get("gpu") != "b65":
                raise ValueError("SYCL profile requires B65-0")
            if native:
                if not spec.get("model_digest") or "weight_digest" in spec:
                    raise ValueError("Native SYCL benchmarks require a pinned local GGUF identity")
            elif not isinstance(spec.get("weight_digest"), str) or not re.fullmatch(
                "[0-9a-f]{64}", spec["weight_digest"]
            ):
                raise ValueError("Invalid GGUF weight digest")
            if not isinstance(spec.get("llama_cpp_git"), str) or not re.fullmatch(
                "[0-9a-f]{7,40}", spec["llama_cpp_git"]
            ):
                raise ValueError("Invalid llama.cpp revision")
            spec["duration_seconds"] = number(spec, "duration_seconds", 3600, 10, 3600)
        elif profile == "littlebit-cpu":
            if (
                set(spec) - common - {"profile", "gpu", "model", "timeout_seconds"}
                or spec.get("gpu") != "cpu"
                or spec.get("model") != "littlebit-qwen3:8b-0.55bpw"
            ):
                raise ValueError("LittleBit CPU requires the installed checkpoint")
            spec["timeout_seconds"] = number(spec, "timeout_seconds", 14400, 600, 28800)
        elif profile == "fixed-work-v1":
            if set(spec) - common - {"profile", "gpu", "model", "model_digest", "timeout_seconds"}:
                raise ValueError("Unknown fixed-work benchmark setting")
            if spec.get("gpu") not in ("r9700", "b65"):
                raise ValueError("gpu must be r9700 or b65")
            if not isinstance(spec.get("model_digest"), str) or not re.fullmatch(
                "[0-9a-f]{64}", spec["model_digest"]
            ):
                raise ValueError("model_digest must contain 64 hex digits")
            spec["timeout_seconds"] = number(spec, "timeout_seconds", 14400, 600, 28800)
        else:
            raise ValueError("Unknown benchmark profile")
    elif kind == "cooldown":
        if set(spec) - common - {"timeout_seconds", "minimum_seconds", "target_c", "gpu"}:
            raise ValueError("Unknown cooldown setting")
        spec["timeout_seconds"] = number(spec, "timeout_seconds", 1800, 10, 1800)
        spec["minimum_seconds"] = number(spec, "minimum_seconds", 10, 0, spec["timeout_seconds"])
        if spec.get("gpu", "r9700") not in ("r9700", "b65", "both"):
            raise ValueError("Invalid cooldown GPU")
        target = spec.get("target_c", "previous-start")
        keys = {"pkg_c", "vram_c"}
        if spec.get("gpu") == "both":
            keys = {f"{gpu}_{key}" for gpu in ("b65", "r9700") for key in keys}
        if target != "previous-start" and (
            not isinstance(target, dict)
            or set(target) != keys
            or not all(type(v) in (float, int) and 0 < v < 130 for v in target.values())
        ):
            raise ValueError("target_c must be previous-start or valid pkg_c and vram_c")
    else:
        raise ValueError("Expected benchmark or cooldown")


class Queue:
    def __init__(self, root=ROOT, public=PUBLIC):
        self.root = root
        self.public = public
        self.pending = root / "pending"
        self.running = root / "running"
        self.archive = root / "archive"
        for folder in (self.pending, self.running, self.archive):
            folder.mkdir(mode=0o750, parents=True, exist_ok=True)
        (public / "runs").mkdir(parents=True, exist_ok=True)
        self.last_message = None
        self.active_gpu = None
        self.background = False
        self.overlap = set()

    def announce(self, status, active=False):
        if self.background:
            return
        message = (status, len(self.candidates()))
        if message != self.last_message:
            write_json(
                self.public / "queue-status.json",
                {"message": status, "time": time.time(), "pending": message[1]},
            )
            self.last_message = message
        if active:
            write_json(
                STATE,
                {
                    "running": True,
                    "expires": time.time() + 30,
                    "stage": status,
                    "gpu": self.active_gpu,
                },
            )

    def candidates(self):
        found = []
        for file in self.pending.iterdir():
            match = NAME.fullmatch(file.name)
            if match and not file.is_symlink() and file.is_file():
                found.append((int(match[1]), file.name, file))
        return [item[2] for item in sorted(found)]

    def next_eligible(self, active=()):
        jobs = []
        for path in self.candidates():
            try:
                spec = read_json(path) if path.stat().st_size <= 65536 else {}
                if not isinstance(spec, dict):
                    spec = {}
            except (OSError, ValueError):
                spec = {}
            jobs.append((path, spec))
        paired = {spec.get("pair_id") for _, spec in jobs if spec.get("kind") == "benchmark"}
        paired.update(spec.get("pair_id") for spec in active)
        handoff = self.root / "next-cooldown-pair.json"
        if handoff.exists() and not active:
            try:
                pair = read_json(handoff)["pair_id"]
            except (OSError, ValueError, KeyError):
                pair = None
            for path, spec in jobs:
                if not pair or spec.get("kind") != "cooldown" or spec.get("pair_id") != pair:
                    continue
                try:
                    if "not_before" in spec and scheduled_time(spec["not_before"]) > time.time():
                        continue
                except (ValueError, OverflowError):
                    pass
                return path
        for path, spec in jobs:
            if "not_before" in spec:
                try:
                    if scheduled_time(spec["not_before"]) > time.time():
                        continue
                except (ValueError, OverflowError):
                    pass
            if spec.get("kind") == "cooldown" and spec.get("pair_id") and spec["pair_id"] in paired:
                continue
            if active:
                if len(active) != 1 or not all(job.get("parallel") for job in active):
                    return None
                if spec.get("kind") != "benchmark" or not spec.get("parallel"):
                    return None
                if {active[0].get("gpu", "r9700"), spec.get("gpu", "r9700")} != {"r9700", "b65"}:
                    continue
            return path
        return None

    def claim(self, source):
        folder = self.running / (
            datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            + "-"
            + uuid.uuid4().hex[:10]
        )
        folder.mkdir()
        try:
            source.rename(folder / source.name)
        except FileNotFoundError:
            folder.rmdir()
            return None
        return folder

    def benchmark(self, spec, folder, record, status):
        gpu = spec.get("gpu", "r9700")
        if gpu != "cpu":
            with (self.root / "gpu-clear.lock").open("a") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX)
                record["evicted_models"] = clear_gpu(gpu)
        if "rear_percent" in spec:
            if not read_json(SENSORS).get("benchmark_rear_supported"):
                if legacy_active():
                    raise RuntimeError("Legacy campaign is active")
                subprocess.run(
                    ["systemctl", "restart", "hal-cooling.service"], check=True, timeout=45
                )
            write_json(
                REAR,
                {
                    "duty": spec["rear_percent"],
                    "expires": time.monotonic() + spec["duration_seconds"] + 490,
                },
            )
            for _ in range(35):
                try:
                    state = read_json(SENSORS)
                    fan = state["fans"]["CHA_FAN1"]
                    if (
                        state["ok"]
                        and state["sensors_fresh"]
                        and time.time() - state["time"] < 4
                        and state["policy"].get("benchmark_rear_percent") == spec["rear_percent"]
                        and fan["applied"]
                        and all(p["Duty"] == spec["rear_percent"] for p in fan["curve"])
                    ):
                        break
                except (OSError, ValueError, KeyError):
                    pass
                time.sleep(1)
            else:
                raise RuntimeError("Rear benchmark duty was not applied")
        gpu = spec.get("gpu", "r9700")
        if gpu != "cpu":
            start = sensors(gpu)
            record["starting_temperatures_c"] = start
            write_json(self.root / "previous-start.json", {"gpu": gpu, "temperatures": start})
            if spec.get("pair_id"):
                write_json(
                    self.root / ("baseline-" + spec["pair_id"] + ".json"),
                    {"gpu": gpu, "temperatures": start},
                )
        write_json(folder / "outcome.json", record)
        clean = folder / "effective.json"
        write_json(clean, spec)
        env = dict(
            os.environ,
            HAL_QUEUE_EVIDENCE=str(folder),
            HAL_BENCH_TUNING=json.dumps(spec.get("tuning", {})),
        )
        unit = UNIT
        if gpu == "b65":
            unit = "hal-queue-b65-infer.service"
            env.update(HAL_BENCH_PORT="11453", HAL_BENCH_UNIT=unit)
        if gpu == "cpu":
            unit = ai_bench_tuning.LITTLEBIT_UNIT
        with (folder / "runner.log").open("w") as log:
            process = subprocess.Popen(
                ["/usr/bin/python3", str(PROFILES / (spec["profile"] + ".py")), str(clean)],
                stdout=log,
                stderr=subprocess.STDOUT,
                env=env,
                start_new_session=True,
            )
            deadline = (
                time.monotonic()
                + spec.get("duration_seconds", spec.get("timeout_seconds", 0))
                + 440
            )
            try:
                while (
                    process.poll() is None
                    and time.monotonic() < deadline
                    and not STOP
                    and not (folder / "terminate").exists()
                ):
                    try:
                        progress = read_json(folder / "public-progress.json").get("status")
                    except (OSError, ValueError):
                        progress = None
                    self.announce(progress or status, True)
                    time.sleep(2)
                if (folder / "terminate").exists():
                    raise RuntimeError("Terminated by operator")
                if process.poll() is None:
                    raise RuntimeError(
                        "Benchmark interrupted" if STOP else "Benchmark exceeded its time limit"
                    )
                if process.returncode:
                    result = read_json(folder / "results.json")
                    raise RuntimeError(
                        result.get("error") or f"Benchmark exited {process.returncode}"
                    )
            finally:
                if process.poll() is None:
                    os.killpg(process.pid, signal.SIGTERM)
                    self.stop_inference(unit)
                    try:
                        process.wait(15)
                    except subprocess.TimeoutExpired:
                        os.killpg(process.pid, signal.SIGKILL)
                        process.wait()
                self.stop_inference(unit)

    @staticmethod
    def stop_inference(unit):
        subprocess.run(
            ["systemctl", "stop", unit],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=20,
        )

    def cooldown(self, spec, folder, record, status):
        target = spec.get("target_c", "previous-start")
        gpu = spec.get("gpu", "r9700")
        if target == "previous-start":
            baseline = (
                "baseline-" + spec["pair_id"] + ".json"
                if spec.get("pair_id")
                else "previous-start.json"
            )
            previous = read_json(self.root / baseline)
            if "temperatures" in previous:
                if "gpu" in spec and gpu != previous["gpu"]:
                    raise ValueError("Cooldown GPU differs from previous benchmark")
                gpu = previous["gpu"]
                target = previous["temperatures"]
            else:
                target = previous
        record["gpu"] = gpu
        self.active_gpu = gpu
        record["target_c"] = target
        started = time.monotonic()
        deadline = started + spec["timeout_seconds"]
        staged = COOLDOWN.with_suffix(".queue-tmp")
        staged.write_text(str(deadline))
        staged.replace(COOLDOWN)
        stable = 0
        first_sample = None
        try:
            with (folder / "cooldown.jsonl").open("a") as log:
                while (
                    time.monotonic() < deadline and not STOP and not (folder / "terminate").exists()
                ):
                    temperature = sensors(gpu)
                    state = read_json(SENSORS)
                    fans = state["fans"]
                    full = all(
                        fans[n]["applied"] and all(p["Duty"] == 100 for p in fans[n]["curve"])
                        for n in ("CHA_FAN1", "CHA_FAN3", "CHA_FAN4", "CHA_FAN5")
                    )
                    entry = {
                        "time": time.time(),
                        "temps": temperature,
                        "full_applied": full,
                        "target": target,
                    }
                    log.write(json.dumps(entry) + "\n")
                    log.flush()
                    now = time.monotonic()
                    if first_sample is None:
                        first_sample = now
                        record["starting_temperatures_c"] = temperature
                    record["cooldown_elapsed_seconds"] = round(now - first_sample, 3)
                    record["ending_temperatures_c"] = temperature
                    reached = all(temperature[k] <= v for k, v in target.items())
                    stable = stable + 1 if reached and full else 0
                    if stable >= 3 and time.monotonic() - started >= spec["minimum_seconds"]:
                        return
                    self.announce(status, True)
                    time.sleep(2)
            if (folder / "terminate").exists():
                raise RuntimeError("Terminated by operator")
            raise RuntimeError(
                "Cooldown interrupted"
                if STOP
                else "Cooldown did not reach its target before timeout"
            )
        finally:
            COOLDOWN.unlink(missing_ok=True)

    def finish(self, folder, record, spec):
        if self.overlap:
            record["overlapping_benchmarks"] = sorted(self.overlap)
        record["finished_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
        write_json(folder / "outcome.json", record)
        try:
            report = read_json(folder / "results.json")
        except (OSError, ValueError):
            report = {}
        rows = report.get("rows", [])
        if not rows and spec.get("kind") == "benchmark":
            rows = [
                {
                    "model": spec.get("model", "Unknown"),
                    "gpu": {
                        "b65": "Intel Arc Pro B65-0",
                        "both": "R9700 + B65",
                        "cpu": "HAL CPU",
                    }.get(spec.get("gpu"), "Radeon AI PRO R9700"),
                    "backend": "Ollama / Vulkan",
                    "streams": 1,
                    "rate": None,
                    "settings": "Queue step",
                }
            ]
        for index, row in enumerate(rows):
            row["id"] = folder.name + "-" + str(index + 1)
            row["status"] = record["status"]
            if record.get("error"):
                row["error"] = record["error"]
        if spec.get("tuning"):
            for row in rows:
                row["tuning"] = spec["tuning"]
        if "rear_percent" in spec:
            for row in rows:
                row["settings"] += f" Rear fan {spec['rear_percent']}%."
        report.update(
            id=folder.name,
            recorded_at=record.get("started_at", record["finished_at"]),
            campaign=record["file"] + " · " + record["status"],
            rows=rows,
            queue_step=record,
        )
        destination = self.archive / folder.name
        write_json(folder / "website.json", report)
        folder.rename(destination)
        write_json(self.public / "runs" / (folder.name + ".json"), report)

    def execute(self, folder):
        job = next(folder.glob("*.json"))
        record = {
            "file": job.name,
            "started_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        }
        spec = {}
        try:
            if job.stat().st_size > 65536:
                raise ValueError("Job file exceeds 64 KiB")
            spec = read_json(job)
            validate(spec)
            self.active_gpu = spec.get("gpu", "r9700")
            status = "Queue · " + job.name + " · " + spec["kind"]
            self.announce(status, True)
            if spec["kind"] == "benchmark":
                (self.root / "previous-start.json").unlink(missing_ok=True)
                try:
                    self.benchmark(spec, folder, record, status)
                finally:
                    if self.active_gpu in ("r9700", "both"):
                        REAR.unlink(missing_ok=True)
            else:
                self.cooldown(spec, folder, record, status)
            record["status"] = "complete"
        except Exception as error:
            record.update(
                status="terminated" if (folder / "terminate").exists() else "failed",
                error=str(error),
            )
        saved_spec = spec if isinstance(spec, dict) else {}
        pair = saved_spec.get("pair_id")
        handoff = self.root / "next-cooldown-pair.json"
        if pair and saved_spec.get("kind") == "benchmark":
            write_json(handoff, {"pair_id": pair})
        elif pair and saved_spec.get("kind") == "cooldown" and handoff.exists():
            if read_json(handoff).get("pair_id") == pair:
                handoff.unlink()
        self.finish(folder, record, saved_spec)

    def recover_job(self, folder, reason):
        archived = self.archive / folder.name
        if archived.exists():
            report = read_json(archived / "website.json")
            write_json(self.public / "runs" / (folder.name + ".json"), report)
            return
        jobs = [path for path in folder.iterdir() if NAME.fullmatch(path.name)]
        if not jobs:
            if not any(folder.iterdir()):
                folder.rmdir()
            return
        if (folder / "website.json").is_file():
            report = read_json(folder / "website.json")
            folder.rename(archived)
            write_json(self.public / "runs" / (folder.name + ".json"), report)
            return
        try:
            record = read_json(folder / "outcome.json")
            if not isinstance(record, dict):
                record = {}
        except (OSError, ValueError):
            record = {}
        record.setdefault("file", jobs[0].name)
        try:
            spec = read_json(jobs[0])
        except (OSError, ValueError):
            spec = {}
        if not record.get("status"):
            record.update(status="interrupted", error=reason)
        self.finish(folder, record, spec if isinstance(spec, dict) else {})

    def try_recover_job(self, folder, reason):
        try:
            self.recover_job(folder, reason)
        except Exception:
            logging.exception("Could not archive or publish benchmark %s; will retry", folder.name)
            return False
        return True

    def recover(self):
        deferred = {}
        reason = "Queue worker restarted during this step"
        for folder in sorted(self.running.iterdir()):
            if folder.is_dir() and not self.try_recover_job(folder, reason):
                deferred[folder] = reason
        for archived in self.archive.iterdir():
            public = self.public / "runs" / (archived.name + ".json")
            if not public.exists() and (archived / "website.json").is_file():
                folder = self.running / archived.name
                if folder not in deferred and not self.try_recover_job(folder, reason):
                    deferred[folder] = reason
        return deferred

    def run(self):
        with (self.root / "queue.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            if not legacy_active():
                REAR.unlink(missing_ok=True)
            write_json(self.root / "control-worker.json", {"pid": os.getpid(), "version": 1})
            deferred = self.recover()
            next_recovery = 0.0
            active = {}
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                while not STOP:
                    for future in list(active):
                        if future.done():
                            _, _, folder = active.pop(future)
                            try:
                                future.result()
                            except Exception:
                                logging.exception("Benchmark worker failed for %s", folder.name)
                                deferred[folder] = (
                                    "Benchmark worker failed while recording this step"
                                )
                    if deferred and time.monotonic() >= next_recovery:
                        for folder, reason in list(deferred.items()):
                            if self.try_recover_job(folder, reason):
                                del deferred[folder]
                        next_recovery = time.monotonic() + 10
                    with schedule_lock(self.root):
                        legacy = legacy_active()
                        candidates = self.candidates()
                        if (
                            candidates
                            and not legacy
                            and not (self.root / "pending-rewrite.json").exists()
                        ):
                            source = self.next_eligible(tuple(item[0] for item in active.values()))
                            if source and (folder := self.claim(source)):
                                try:
                                    job = read_json(folder / source.name)
                                except (ValueError, OSError):
                                    job = {}
                                if not isinstance(job, dict):
                                    job = {}
                                lane = Queue(self.root, self.public)
                                lane.background = True
                                for prior, other, other_folder in active.values():
                                    other.overlap.add(folder.name)
                                    lane.overlap.add(other_folder.name)
                                gpu = job.get("gpu", "r9700")
                                self.active_gpu = "both" if active else gpu
                                self.announce("Queue · " + source.name, True)
                                active[pool.submit(lane.execute, folder)] = (job, lane, folder)
                    if active:
                        self.active_gpu = (
                            "both"
                            if len(active) == 2
                            else next(iter(active.values()))[1].active_gpu or "r9700"
                        )
                        stages = []
                        for job, lane, folder in active.values():
                            try:
                                stage = read_json(folder / "public-progress.json").get("status")
                            except (OSError, ValueError):
                                stage = None
                            stages.append(
                                stage or str(job.get("model", job.get("kind", "Benchmark")))
                            )
                        self.announce(" · ".join(stages), True)
                    elif not legacy:
                        current = {}
                        try:
                            current = read_json(STATE)
                        except (OSError, ValueError):
                            pass
                        if current.get("running"):
                            write_json(
                                STATE, {"running": False, "expires": 0, "stage": "Queue idle"}
                            )
                        self.announce(
                            "Queue waiting for scheduled start" if candidates else "Queue idle"
                        )
                    time.sleep(2)


def stop(*_):
    global STOP
    STOP = True


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    Queue().run()
