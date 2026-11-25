"""Host-owned Android web controls, behind the existing authenticated gateway."""

import json, pathlib, tempfile, urllib.parse
import ai_android
from ai_lab_proxy import reply


def api(h):
    path = urllib.parse.urlsplit(h.path).path
    if path not in ("/llama/android", "/llama/android/") and not path.startswith("/llama/android/"):
        return False
    try:
        if h.command == "GET":
            if path in ("/llama/android", "/llama/android/"):
                reply(
                    h,
                    pathlib.Path("/usr/local/share/hal-android.html").read_bytes(),
                    typ="text/html; charset=utf-8",
                )
            elif path.endswith("/status"):
                reply(h, ai_android.status())
            elif path.endswith("/screen.png"):
                reply(
                    h,
                    ai_android.adb(["exec-out", "screencap", "-p"], binary=True),
                    typ="image/png",
                )
            else:
                reply(h, {"error": "Unknown Android route"}, 404)
        elif h.command == "POST":
            length = int(h.headers.get("Content-Length", "0"))
            if path == "/llama/android/install":
                if not 0 < length <= 256 * 1024 * 1024:
                    raise ValueError("APK upload limit: 256 MiB")
                with tempfile.NamedTemporaryFile(suffix=".apk") as f:
                    remaining = length
                    while remaining:
                        chunk = h.rfile.read(min(1048576, remaining))
                        if not chunk:
                            raise ValueError("Incomplete upload")
                        f.write(chunk)
                        remaining -= len(chunk)
                    f.flush()
                    reply(h, ai_android.install(f.name))
            else:
                if not 0 < length <= 16000:
                    raise ValueError("Invalid request size")
                value = json.loads(h.rfile.read(length))
                if path == "/llama/android/power":
                    reply(h, ai_android.lifecycle(value["action"]))
                elif path == "/llama/android/input":
                    reply(h, ai_android.input_event(value["kind"], value["value"]))
                else:
                    reply(h, {"error": "Unknown Android route"}, 404)
        else:
            reply(h, {"error": "Method not allowed"}, 405)
    except Exception as error:
        reply(h, {"error": str(error)}, 400)
    return True
