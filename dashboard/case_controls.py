"""Unprivileged dashboard adapter; no BMC credentials or arbitrary commands."""

import json
import socket


def call(request):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(90)
        client.connect("/run/hal-case-fans/control.sock")
        client.sendall(json.dumps(request).encode() + b"\n")
        raw = b""
        while b"\n" not in raw:
            chunk = client.recv(65536)
            if not chunk:
                raise OSError("Case-fan service disconnected")
            raw += chunk
            if len(raw) > 131072:
                raise ValueError("Oversized reply")
        return json.loads(raw)


def get(handler):
    try:
        result = call({"action": "get"})
        handler.send_json(200 if result.get("ok") else 503, result)
    except (OSError, ValueError):
        handler.send_json(503, {"ok": False, "error": "Case-fan service unavailable"})


def post(handler):
    if handler.headers.get("Origin") not in (None, "http://" + handler.headers.get("Host", "")):
        handler.send_json(403, {"ok": False, "error": "Use the HAL monitor page"})
        return
    try:
        length = int(handler.headers.get("Content-Length", "0"))
        if not 0 < length <= 1024 or handler.headers.get_content_type() != "application/json":
            raise ValueError()
        body = json.loads(handler.rfile.read(length))
        if set(body) != {"header", "percent"} or body["header"] not in (
            "CHA_FAN1",
            "CHA_FAN3",
            "CHA_FAN4",
            "CHA_FAN5",
        ):
            raise ValueError()
        if type(body["percent"]) is not int or body["percent"] not in range(10, 101, 10):
            raise ValueError()
    except (ValueError, KeyError, TypeError):
        handler.send_json(
            400, {"ok": False, "error": "Choose a chassis header and 10–100% in 10% steps"}
        )
        return
    try:
        result = call(dict(body, action="set"))
        handler.send_json(200 if result.get("ok") else 503, result)
    except (OSError, ValueError):
        handler.send_json(
            503,
            {"ok": False, "error": "Case-fan service unavailable; verify readback before retrying"},
        )
