import json, socket


def request(data):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.settimeout(4)
        s.connect("/run/hal-gpu-fan/control.sock")
        s.sendall(json.dumps(data).encode() + b"\n")
        out = b""
        while b"\n" not in out:
            chunk = s.recv(65536)
            if not chunk:
                break
            out += chunk
        return json.loads(out)


def get(handler):
    try:
        handler.send_json(200, request({"action": "status"}))
    except Exception:
        handler.send_json(503, {"ok": False, "error": "GPU fan helper unavailable"})


def post(handler):
    if (
        handler.headers.get("Origin") != "http://" + handler.headers.get("Host", "")
        or handler.headers.get("Sec-Fetch-Site") == "cross-site"
    ):
        handler.send_json(403, {"ok": False, "error": "Use the HAL monitor page"})
        return
    try:
        n = int(handler.headers.get("Content-Length", "0"))
        if not 0 < n <= 512 or handler.headers.get_content_type() != "application/json":
            raise ValueError("Invalid JSON request")
        d = json.loads(handler.rfile.read(n))
        if set(d) - {"action", "pci", "percent"} or d.get("action") not in (
            "auto",
            "manual",
            "curve",
        ):
            raise ValueError("Invalid action")
        result = request(d)
        handler.send_json(200 if result.get("ok") else 409, result)
    except (ValueError, TypeError, KeyError):
        handler.send_json(400, {"ok": False, "error": "Invalid fan request"})
    except Exception:
        handler.send_json(
            503, {"ok": False, "error": "GPU fan helper unavailable; request not applied"}
        )
