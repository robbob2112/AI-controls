import datetime

"""Read-only public view of HAL's locally measured AI trials."""
import json, pathlib, time
from decimal import Decimal, InvalidOperation

ROOT = pathlib.Path("/scratch/b65-model-testing/results-20261113")
MODELS = {"qwen3coder": "Qwen3-Coder 30B-A3B", "qwen35": "Qwen3.5 35B-A3B", "qwen38": "Qwen3.8 27B"}


def load(path, default=None):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return default


def expanded_accuracy(root):
    rows = load(root / "expanded-quality-summary.json", []) or []
    campaign = root.parent / "expanded-20261113"
    models = {m["model"]: m["slug"] for m in load(campaign / "campaign-models.json", []) or []}
    for row in rows:
        if not row.get("benchmark", "").startswith("GSM8K") or row["model"] not in models:
            continue
        samples = load(campaign / (models[row["model"]] + "-reasoning-samples.json"), []) or []
        if len(samples) != row.get("examples") or any("error" in sample for sample in samples):
            continue
        correct = 0
        for sample in samples:
            try:
                correct += int(
                    sample.get("actual") is not None
                    and Decimal(sample["actual"]) == Decimal(sample["expected"])
                )
            except (InvalidOperation, ValueError, TypeError):
                pass
        row.update(
            score=correct / len(samples),
            correct=correct,
            grading="decimal-value-v2; 26.00 equals 26",
        )
        row["settings"] += "; numeric values compared independently of decimal formatting"
    for row in rows:
        if row.get("benchmark", "").startswith("HAL mixed"):
            parts = [
                p["score"]
                for p in rows
                if p["model"] == row["model"] and not p["benchmark"].startswith("HAL mixed")
            ]
            if len(parts) == 3:
                row["score"] = sum(parts) / 3
                row["grading"] = "Equal-weight categories; decimal-value-v2 reasoning"
    return rows


def live_payload():
    rows = []
    updated = 0
    for family, model in MODELS.items():
        path = ROOT / (family + "-summary.json")
        for r in load(path, []) or []:
            label = r["configuration"]
            devices = r.get("devices", "0")
            gpu = "Both B65s" if "," in devices else "B65 GPU " + devices
            settings = (
                ("MTP 3 · " if r.get("mtp") else "")
                + r.get("split_mode", "none")
                + " · FA "
                + r.get("flash_attention", "on")
            )
            trials = load(ROOT / (label + "-requests.json"), []) or []
            rows.append(
                {
                    "id": "sycl-" + label,
                    "model": model,
                    "gpu": gpu,
                    "backend": "SYCL",
                    "settings": settings,
                    "streams": 1,
                    "rate": r.get("median_tokens_per_second"),
                    "wall_rate": r.get("aggregate_wall_tokens_per_second"),
                    "tokens": r.get("tokens"),
                    "status": "failed" if r.get("error") else "measured",
                    "error": r.get("error"),
                    "trials": [x.get("timings", {}).get("predicted_per_second") for x in trials],
                }
            )
        if path.exists():
            updated = max(updated, path.stat().st_mtime)
    path = ROOT / "qwen3coder-throughput-summary.json"
    for r in load(path, []) or []:
        streams = 8 if r["configuration"].startswith("two") else 4
        rows.append(
            {
                "id": "sycl-" + r["configuration"],
                "model": MODELS["qwen3coder"],
                "gpu": "Both B65s · independent copies" if streams == 8 else "B65 GPU 0",
                "backend": "SYCL",
                "settings": "FA on · " + str(streams) + " streams",
                "streams": streams,
                "rate": r["aggregate_tokens_per_second"],
                "tokens": r["tokens"],
                "status": "measured",
                "trials": [b["aggregate_tokens_per_second"] for b in r["batches"]],
            }
        )
    if path.exists():
        updated = max(updated, path.stat().st_mtime)
    old = pathlib.Path("/var/log/hardware-validation/b65-matrix-20261113")
    for r in load(old / "summary.json", []) or []:
        config = r["configuration"]
        streams = r["parallel"]
        gpu = {"gpu0": "B65 GPU 0", "gpu1": "B65 GPU 1", "pair": "Both B65s · split model"}[
            config.split("-")[0]
        ]
        rows.append(
            {
                "id": "vulkan-" + config,
                "model": MODELS["qwen3coder"],
                "gpu": gpu,
                "backend": "Vulkan / Ollama",
                "settings": str(streams) + " stream" + ("s" if streams > 1 else ""),
                "streams": streams,
                "rate": (
                    r["median_stream_tokens_per_second"]
                    if streams == 1
                    else r["aggregate_tokens_per_second"]
                ),
                "tokens": r["tokens"],
                "status": "measured",
                "trials": [],
            }
        )
    for filename, streams in [("replicas-summary.json", 4)]:
        r = load(old / filename)
        if r:
            rows.append(
                {
                    "id": "vulkan-independent4",
                    "model": MODELS["qwen3coder"],
                    "gpu": "Both B65s · independent copies",
                    "backend": "Vulkan / Ollama",
                    "settings": "4 streams",
                    "streams": streams,
                    "rate": r["aggregate_tokens_per_second"],
                    "tokens": r["tokens"],
                    "status": "measured",
                    "trials": [],
                }
            )
    r = load(
        pathlib.Path("/var/log/hardware-validation/b65-pair-eight-20261113/replicas-summary.json")
    )
    if r:
        rows.append(
            {
                "id": "vulkan-independent8",
                "model": MODELS["qwen3coder"],
                "gpu": "Both B65s · independent copies",
                "backend": "Vulkan / Ollama",
                "settings": "8 streams",
                "streams": 8,
                "rate": r["aggregate_tokens_per_second"],
                "tokens": r["tokens"],
                "status": "measured",
                "trials": [],
            }
        )
    rows.append(
        {
            "id": "radeon-reference",
            "model": MODELS["qwen3coder"],
            "gpu": "Radeon AI PRO R9700",
            "backend": "Historical reference",
            "settings": "105-token short sample · not a matched sustained run",
            "streams": 1,
            "rate": 81.52,
            "tokens": 105,
            "status": "reference",
            "trials": [],
        }
    )
    # Additional models/backends can publish one record without changing this page.
    for path in sorted(ROOT.glob("trial-*.json")):
        record = load(path)
        if isinstance(record, dict) and all(
            k in record for k in ("id", "model", "gpu", "backend", "streams", "status")
        ):
            rows.append(record)
            updated = max(updated, path.stat().st_mtime)
    updated = max([updated] + [p.stat().st_mtime for p in ROOT.glob("*summary.json")])
    return {
        "time": time.time(),
        "last_result": updated,
        "rows": rows,
        "accuracy": (load(ROOT / "quality-summary.json", []) or []) + expanded_accuracy(ROOT),
        "standard_speed": load(ROOT / "llama-bench-summary.json", []) or [],
        "activity": load(ROOT / "activity.json", {}) or {},
    }


def payload():
    global ROOT
    candidates = sorted(pathlib.Path("/scratch/b65-model-testing").glob("results-*"))
    if candidates:
        ROOT = candidates[-1]
    data = live_payload()
    data["offload_campaign"] = (
        load(
            pathlib.Path("/scratch/b65-model-testing/cogito-offload-20261121/public-progress.json"),
            {},
        )
        or {}
    )
    data["paired_campaign"] = (
        load(
            pathlib.Path("/scratch/b65-model-testing/paired-hours-20261121/public-progress.json"),
            {},
        )
        or {}
    )
    if data["paired_campaign"]:
        data["activity"] = {
            "message": data["paired_campaign"].get("status", "Paired model campaign")
        }
    queue = load(pathlib.Path("/var/lib/hal-benchmarks/queue-status.json"), {}) or {}
    if queue.get("message"):
        data["activity"] = {
            "message": queue["message"] + " · " + str(queue.get("pending", 0)) + " pending"
        }
    state = load(pathlib.Path("/var/lib/hal-monitor/benchmark-state.json"), {}) or {}
    if state.get("running") and state.get("expires", 0) > time.time():
        data["activity"] = {"message": state.get("stage", "Benchmark running")}
    data["changes"] = (
        load(pathlib.Path("/scratch/b65-model-testing/software-changes.json"), []) or []
    )
    data["history"] = [
        r
        for p in sorted(pathlib.Path("/var/lib/hal-benchmarks/runs").glob("*.json"), reverse=True)
        if (r := load(p))
    ]

    def date_key(record):
        try:
            value = datetime.datetime.fromisoformat(record["recorded_at"])
            if value.tzinfo is None:
                value = value.replace(tzinfo=datetime.timezone.utc)
            return value.timestamp()
        except (ValueError, KeyError, TypeError):
            return 0

    data["history"].sort(key=date_key, reverse=True)
    return data
