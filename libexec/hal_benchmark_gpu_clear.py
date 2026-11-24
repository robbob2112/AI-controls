"""Release resident models from HAL inference services before GPU workloads."""

import time
from hal_model_catalog import request

PINNED = {"b65": 11444, "r9700": 11446}


def clear_gpu(target):
    targets = tuple(PINNED) if target == "both" else (target,)
    evicted = []
    try:
        if "b65" in targets:
            evicted += clear_native()
        for port in (11434, *(PINNED[gpu] for gpu in targets)):
            evicted += clear_ollama(port)
    except (OSError, ValueError, KeyError, IndexError, RuntimeError) as error:
        raise RuntimeError("GPU eviction preflight failed: " + str(error)) from error
    return evicted


def clear_native():
    released = []
    deadline = time.monotonic() + 22
    while True:
        models = request(11436, "/llama/models")["data"]
        active = [
            model
            for model in models
            if model.get("status", {}).get("value") in ("loaded", "loading", "unloading")
        ]
        if not active:
            return released
        if time.monotonic() >= deadline:
            raise RuntimeError("B65 llama.cpp remains active or holds weights")
        for model in active:
            status = model["status"]
            if status["value"] != "loaded":
                continue
            args = status["args"]
            child_port = int(args[args.index("--port") + 1])
            slots = request(child_port, "/llama/slots")
            if any(slot.get("is_processing") for slot in slots):
                continue
            if not request(11436, "/llama/models/unload", {"model": model["id"]}).get("success"):
                raise RuntimeError("llama.cpp could not unload " + model["id"])
            released.append({"service": "llama.cpp B65", "model": model["id"]})
        time.sleep(0.5)


def clear_ollama(port):
    # Auto does not expose per-card residency, so release its GPU-backed entries.
    def resident():
        return [
            model for model in request(port, "/api/ps")["models"] if model.get("size_vram", 0) > 0
        ]

    models = resident()
    for model in models:
        name = model["name"]
        reply = request(port, "/api/generate", {"model": name, "keep_alive": 0, "stream": False})
        if not reply.get("done"):
            raise RuntimeError(f"Ollama {port} did not unload {name}")
    deadline = time.monotonic() + 6
    while models and resident():
        if time.monotonic() >= deadline:
            raise RuntimeError(f"Ollama {port} still holds GPU weights")
        time.sleep(0.25)
    return [{"service": f"Ollama {port}", "model": model["name"]} for model in models]
