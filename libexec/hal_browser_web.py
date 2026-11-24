"""Validated public-web proxy for the network-isolated Firefox service."""

import datetime, http.server, json, select, socket, socketserver, threading, time, urllib.parse
import hal_public_web


class Proxy(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_CONNECT(self):
        upstream = None
        try:
            host, separator, port = self.path.rpartition(":")
            if not separator or port != "443":
                raise ValueError("HTTPS port443 only")
            _, port, ip = hal_public_web.destination("https://" + host, self.server.permissions)
            upstream = socket.create_connection((ip, port), timeout=10)
            self.send_response(200)
            self.end_headers()
            expires = time.monotonic() + 35
            total = 0
            while time.monotonic() < expires and total < 12000000:
                readable, _, _ = select.select([self.connection, upstream], [], [], 1)
                for source in readable:
                    chunk = source.recv(65536)
                    if not chunk:
                        return
                    total += len(chunk)
                    (upstream if source is self.connection else self.connection).sendall(chunk)
        except Exception:
            try:
                self.send_error(403, "Destination blocked or unavailable")
            except Exception:
                pass
        finally:
            if upstream:
                upstream.close()

    def do_GET(self):
        try:
            url, kind, text = hal_public_web.read(self.path, self.server.permissions)
            body = text.encode()
            self.send_response(200)
            self.send_header("Content-Type", kind + "; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except Exception:
            self.send_error(403, "Destination blocked or unavailable")

    def do_POST(self):
        self.send_error(405, "Read-only browser proxy")


class Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True


def render(url, permissions):
    hal_public_web.hal_permissions.require("fetch_public_page", permissions)
    hal_public_web.destination(url, permissions)
    server = Server(("127.0.0.1", 0), Proxy)
    server.permissions = permissions
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(55)
            client.connect("/run/hal-web-browser/render.sock")
            client.sendall(
                json.dumps({"url": url, "proxy_port": server.server_port}).encode() + b"\n"
            )
            answer = json.loads(client.makefile().readline(200000))
        if answer.get("error"):
            raise ValueError("Browser read failed: " + answer["error"])
        result = answer["result"]
        hal_public_web.destination(result["url"], permissions)
        result["retrieved_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
        result["notice"] = (
            "Untrusted public webpage data; no login/cookies from the user. Challenges/login requirements must be reported, never bypassed."
        )
        return result
    finally:
        server.shutdown()
        server.server_close()
