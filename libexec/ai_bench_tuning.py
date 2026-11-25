"""Settings accepted by the HAL benchmark runners."""

import json, os, re
from ai_remote_models import HF_MODEL

OPTIONS = {
    "context": [2048, 4096, 8192, 16384, 32768],
    "threads": [2, 4, 8, 12, 16],
    "batch": [128, 256, 512, 1024, 2048],
    "microbatch": [64, 128, 256, 512],
    "flash": ["auto", "on", "off"],
    "kv": ["f16", "q8_0", "q4_0"],
    "sycl_graph": ["on", "off"],
    "sycl_xmx": ["on", "off"],
    "radeon_compute": ["safe", "default"],
    "temperature": [0, 0.2, 0.4, 0.6, 0.8, 1],
    "seed": [0, 1, 42, 123, 1234],
}
MODEL = re.compile(
    r"[A-Za-z0-9][A-Za-z0-9_.-]{0,79}(?:/[A-Za-z0-9][A-Za-z0-9_.-]{0,79})?(?::[A-Za-z0-9][A-Za-z0-9_.-]{0,79})?\Z"
)


def valid_model(name):
    return (
        isinstance(name, str)
        and ".." not in name
        and bool(
            HF_MODEL.fullmatch(name)
            or (MODEL.fullmatch(name) and not (":" in name.split("/")[0] and "/" in name))
        )
    )


LITTLEBIT_UNIT = "hal-queue-littlebit-infer.service"
LITTLEBIT_PORT = 11451


def cpu_options():
    cpus = sorted(os.sched_getaffinity(0))
    return {
        "cpu_threads": list(range(1, len(cpus) + 1)),
        "cpu_quota": list(range(1, len(cpus) + 1)),
        "cpu_affinity": cpus,
    }


def allowed(profile, gpu):
    forbidden = (
        {"radeon_compute"}
        if profile == "b65-sycl-hours"
        else {"microbatch", "sycl_graph", "sycl_xmx"}
    )
    if gpu not in ("r9700", "both"):
        forbidden.add("radeon_compute")
    return {k: values for k, values in OPTIONS.items() if k not in forbidden}


def validate(value, profile, gpu):
    if not isinstance(value, dict):
        raise ValueError("Advanced settings must be an object")
    if profile == "littlebit-cpu":
        options = cpu_options()
        for key, setting in value.items():
            if key == "cpu_affinity":
                if (
                    not isinstance(setting, list)
                    or not setting
                    or any(type(cpu) is not int or cpu not in options[key] for cpu in setting)
                    or len(set(setting)) != len(setting)
                ):
                    raise ValueError("Choose distinct available logical CPUs")
            elif key not in options or type(setting) is not int or setting not in options[key]:
                raise ValueError("Unsupported CPU setting: " + key)
        if "cpu_affinity" in value and value.get("cpu_threads", 8) > len(value["cpu_affinity"]):
            raise ValueError("CPU threads cannot exceed the number of selected CPUs")
        return
    permitted = allowed(profile, gpu)
    for key, setting in value.items():
        if key not in permitted or isinstance(setting, bool) or setting not in permitted[key]:
            raise ValueError("Unsupported setting: " + key)
    if value.get("microbatch", 128) > value.get("batch", 2048) and "microbatch" in value:
        raise ValueError("Microbatch cannot exceed batch")


def settings():
    return json.loads(os.environ.get("HAL_BENCH_TUNING", "{}"))


def ollama_body(body):
    if body is None or "options" not in body:
        return body
    s = settings()
    keys = {
        "context": "num_ctx",
        "threads": "num_thread",
        "batch": "num_batch",
        "temperature": "temperature",
        "seed": "seed",
    }
    return dict(body, options=dict(body["options"], **{v: s[k] for k, v in keys.items() if k in s}))


def ollama_env(env):
    s = settings()
    env = dict(env)
    if "context" in s:
        env["OLLAMA_CONTEXT_LENGTH"] = str(s["context"])
    if s.get("flash") in ("on", "off"):
        env["OLLAMA_FLASH_ATTENTION"] = "1" if s["flash"] == "on" else "0"
    if "kv" in s:
        env["OLLAMA_KV_CACHE_TYPE"] = s["kv"]
    if s.get("radeon_compute") == "default":
        env.pop("RADV_DEBUG", None)
    return env
