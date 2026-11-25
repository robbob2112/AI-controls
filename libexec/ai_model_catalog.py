"""Live installed-model inventory used by both HAL applications."""

import hashlib
import json
from pathlib import Path
import urllib.request
from functools import lru_cache

LITTLEBIT_MODEL = "littlebit-qwen3:8b-0.55bpw"
LITTLEBIT_DIGEST = "5b087e4c79c23697355014fdd00b083c91d0c731ddd80a16d6affbe156f17179"
NATIVE_PREFIX = "llama.cpp/"


def request(port, path, body=None):
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        data=None if body is None else json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=4) as response:
        return json.load(response)


@lru_cache(maxsize=128)
def ollama_completion(name, digest):
    return "completion" in request(11434, "/api/show", {"model": name}).get("capabilities", [])


def native_models():
    try:
        return request(11436, "/llama/models").get("data", [])
    except (OSError, ValueError):
        return []


def native_weight(preset):
    args = preset.get("status", {}).get("args", [])
    try:
        index = args.index("--model")
        path = Path(args[index + 1]).resolve(strict=True)
        if (
            not any(
                path.is_relative_to(root)
                for root in (
                    "/data/models",
                    "/data/ollama/models/blobs",
                    "/scratch/b65-model-testing",
                )
            )
            or not path.is_file()
        ):
            return None
        with path.open("rb") as stream:
            if stream.read(4) != b"GGUF":
                return None
        stat = path.stat()
        identity = [str(path), stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns]
        return path, hashlib.sha256(json.dumps(identity).encode()).hexdigest(), stat.st_size
    except (OSError, ValueError, IndexError):
        return None


def installed_models():
    models = []
    try:
        tags = request(11434, "/api/tags").get("models", [])
    except (OSError, ValueError):
        tags = []
    for tag in tags:
        name = tag["name"]
        try:
            if ollama_completion(name, tag["digest"]):
                models.append(dict(tag, source="Ollama", benchmark=True))
        except (OSError, ValueError, KeyError):
            continue

    ollama_names = {model["name"].casefold() for model in models}
    for item in native_models():
        name = item.get("id")
        if isinstance(name, str) and name.casefold() not in ollama_names:
            weight = native_weight(item)
            models.append(
                {
                    "name": NATIVE_PREFIX + name,
                    "label": name + " · llama.cpp / B65",
                    "source": "llama.cpp",
                    "benchmark": weight is not None,
                    **({"digest": weight[1], "size": weight[2]} if weight else {}),
                }
            )
    try:
        if request(11450, "/health").get("model") == LITTLEBIT_MODEL:
            models.append(
                {
                    "name": LITTLEBIT_MODEL,
                    "label": "LittleBit Qwen3 8B (0.55 bpw) · CPU, text only",
                    "digest": LITTLEBIT_DIGEST,
                    "size": 2968804064,
                    "source": "LittleBit",
                    "benchmark": True,
                }
            )
    except (OSError, ValueError):
        pass
    return models


def text_only(name):
    return name == LITTLEBIT_MODEL or name.startswith(NATIVE_PREFIX)
