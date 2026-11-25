#!/usr/bin/python3
"""Loopback text inference for the pinned LittleBit checkpoint."""
import json
import os
from pathlib import Path
import sys
import threading
import time
from argparse import Namespace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
sys.path.insert(0, "/scratch/b65-model-testing/littlebit-source")
import torch
from transformers import (
    AutoConfig,
    AutoTokenizer,
    GenerationConfig,
    StoppingCriteria,
    StoppingCriteriaList,
    TextIteratorStreamer,
)
from transformers.models.qwen3.modeling_qwen3 import Qwen3ForCausalLM
from quantization.utils.quant_util import apply_littlebit_patch, _load_and_process_state_dict
from quantization.modules import LittleBitLinear

ROOT = Path("/data/models/littlebit-qwen3-8b-055")
NAME = "littlebit-qwen3:8b-0.55bpw"
LOCK = threading.Lock()
MODEL = TOKENIZER = None
torch.set_num_threads(int(os.environ.get("HAL_LITTLEBIT_THREADS", "8")))
torch.set_num_interop_threads(1)


def load():
    global MODEL, TOKENIZER
    if MODEL is not None:
        return MODEL, TOKENIZER
    cfg = AutoConfig.from_pretrained(ROOT, trust_remote_code=False, local_files_only=True)
    if cfg.model_type != "qwen3":
        raise ValueError("Unexpected model architecture")
    with torch.device("meta"):
        model = Qwen3ForCausalLM(cfg)
    args = Namespace(**json.loads((ROOT / "littlebit_config.json").read_text()), model_id=str(ROOT))
    apply_littlebit_patch(model, args, do_train=False)
    for module in model.modules():
        if getattr(module, "inv_freq", None) is not None:
            module.__init__(config=cfg, device="cpu")
    state, packed = _load_and_process_state_dict(str(ROOT), torch.bfloat16)
    if not packed:
        raise ValueError("Expected LittleBit packed weights")
    missing, unexpected = model.load_state_dict(state, strict=False, assign=True)
    required = [
        key
        for key in missing
        if not key.endswith(("_eff_bit_target", "_eff_bit_actual", "_split_dim_final"))
    ]
    if required or unexpected:
        raise ValueError(
            f"Checkpoint mismatch: missing {required[:5]}, unexpected {unexpected[:5]}"
        )
    if any(param.is_meta for param in model.parameters()):
        raise ValueError("Checkpoint left model weights uninitialized")
    for module in model.modules():
        if isinstance(module, LittleBitLinear):
            module._binarized = True
    model.generation_config = GenerationConfig.from_pretrained(ROOT, local_files_only=True)
    tokenizer = AutoTokenizer.from_pretrained(ROOT, trust_remote_code=False, local_files_only=True)
    MODEL, TOKENIZER = model.eval(), tokenizer
    return MODEL, TOKENIZER


class Cancel(StoppingCriteria):
    def __init__(self, event):
        self.event = event

    def __call__(self, input_ids, scores, **kwargs):
        return self.event.is_set()


class Handler(BaseHTTPRequestHandler):
    def reply(self, data, code=200):
        payload = json.dumps(data).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        if self.path == "/health":
            self.reply({"model": NAME, "backend": "CPU Transformers", "loaded": MODEL is not None})
        else:
            self.reply({"error": "Not found"}, 404)

    def do_POST(self):
        if self.path != "/api/chat":
            self.reply({"error": "Not found"}, 404)
            return
        try:
            size = int(self.headers.get("Content-Length", 0))
            if not 0 < size <= 350000:
                raise ValueError("Invalid input size")
            data = json.loads(self.rfile.read(size))
            if data.get("model") != NAME:
                raise ValueError("Unknown model")
            messages = [
                {"role": m["role"], "content": str(m.get("content", ""))}
                for m in data["messages"]
                if m.get("role") in ("system", "user", "assistant")
            ]
            if not messages or not any(m["role"] == "user" for m in messages):
                raise ValueError("A user message is required")
            options = data.get("options", {})
            maximum = max(1, min(512, int(options.get("num_predict", 384))))
        except (ValueError, TypeError, KeyError, AttributeError) as error:
            self.reply({"error": str(error)}, 400)
            return
        if not LOCK.acquire(blocking=False):
            self.reply({"error": "LittleBit is serving another reply"}, 429)
            return
        started = False
        cancel = threading.Event()
        thread = None
        try:
            model, tokenizer = load()
            labels = {"system": "Instructions", "user": "Question", "assistant": "Answer"}
            prompt = (
                "\n\n".join(labels[m["role"]] + ": " + m["content"] for m in messages) + "\nAnswer:"
            )
            inputs = tokenizer(prompt, return_tensors="pt")
            if inputs["input_ids"].shape[1] > 4096:
                raise ValueError(
                    "LittleBit accepts up to 4096 input tokens. Shorten the chat or start a new one."
                )
            streamer = TextIteratorStreamer(
                tokenizer, skip_prompt=True, skip_special_tokens=True, timeout=180
            )
            problems = []
            stats = {}

            def generate():
                try:
                    with torch.inference_mode():
                        output = model.generate(
                            **inputs,
                            max_new_tokens=maximum,
                            do_sample=False,
                            no_repeat_ngram_size=3,
                            streamer=streamer,
                            stopping_criteria=StoppingCriteriaList([Cancel(cancel)]),
                        )
                        stats["output_tokens"] = output.shape[1] - inputs["input_ids"].shape[1]
                except Exception as error:
                    problems.append(str(error))
                    streamer.end()

            self.send_response(200)
            self.send_header("Content-Type", "application/x-ndjson")
            self.send_header("Connection", "close")
            self.end_headers()
            started = True

            def chunk(item):
                self.wfile.write((json.dumps(item, ensure_ascii=False) + "\n").encode())
                self.wfile.flush()

            clock = time.monotonic()
            thread = threading.Thread(target=generate, daemon=True)
            thread.start()
            for part in streamer:
                if part:
                    chunk({"message": {"role": "assistant", "content": part}, "done": False})
            thread.join()
            if problems:
                chunk({"error": problems[0]})
            else:
                chunk(
                    {
                        "message": {"role": "assistant", "content": ""},
                        "done": True,
                        "eval_count": stats["output_tokens"],
                        "prompt_eval_count": inputs["input_ids"].shape[1],
                        "eval_duration": round((time.monotonic() - clock) * 1e9),
                        "done_reason": "length" if stats["output_tokens"] >= maximum else "stop",
                    }
                )
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as error:
            if started:
                self.wfile.write((json.dumps({"error": str(error)}) + "\n").encode())
            else:
                self.reply({"error": str(error)}, 500)
        finally:
            cancel.set()
            if thread:
                thread.join()
            LOCK.release()


if __name__ == "__main__":
    if "--smoke" in sys.argv:
        model, tokenizer = load()
        x = tokenizer.apply_chat_template(
            [{"role": "user", "content": "Say hello in one sentence."}],
            tokenize=True,
            add_generation_prompt=True,
            return_dict=True,
            return_tensors="pt",
            enable_thinking=False,
        )
        with torch.inference_mode():
            out = model.generate(**x, max_new_tokens=12, do_sample=False)
        print(
            "CHECKPOINT_REPLY",
            tokenizer.decode(out[0][x["input_ids"].shape[1] :], skip_special_tokens=True),
        )
    else:
        ThreadingHTTPServer(
            ("127.0.0.1", int(os.environ.get("HAL_LITTLEBIT_PORT", "11450"))), Handler
        ).serve_forever()
