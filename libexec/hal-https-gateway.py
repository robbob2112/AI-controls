#!/usr/bin/python3
"""TLS-only entry point; authorization remains enforced by the loopback app."""
import re, http.server, ssl, urllib.request, urllib.error, urllib.parse, json, pathlib, socket, time, secrets, threading, base64, http.cookies

_hosts = set()
_hosts_until = 0


def local_hosts():
    global _hosts, _hosts_until
    if time.monotonic() >= _hosts_until:
        names = {
            "localhost",
            "hal",
            "hal.local",
            "hal9000",
            "hal9000.local",
            socket.gethostname().lower(),
            socket.getfqdn().lower(),
        }
        try:
            lines = pathlib.Path("/proc/net/fib_trie").read_text().splitlines()
            for previous, line in zip(lines, lines[1:]):
                if "/32 host LOCAL" in line:
                    address = re.search(r"\b(?:[0-9]{1,3}\.){3}[0-9]{1,3}\b", previous)
                    if address:
                        names.add(address.group())
        except OSError:
            names.update(_hosts)
        _hosts = names
        _hosts_until = time.monotonic() + 30
    return _hosts


SESSION_FILE = pathlib.Path("/var/lib/hal-gateway/sessions.json")
try:
    SESSIONS = json.loads(SESSION_FILE.read_text())
except (OSError, ValueError):
    SESSIONS = {}
SESSION_LOCK = threading.Lock()
LOGIN_PAGE = b'<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>{{AI_NAME}} chat sign in</title><style>body{font:18px system-ui;background:#101820;color:#eee;max-width:420px;margin:10vh auto;padding:24px}input,button{box-sizing:border-box;font:inherit;padding:12px;width:100%;margin:8px 0 20px}a{color:#73dfcd}</style><h1>Sign in to {{AI_NAME}} chat</h1><p>Use your existing {{AI_NAME}} chat account.</p><form action="/login" method="post"><label>Username<input name="username" autocomplete="username" required></label><label>Password<input name="password" type="password" autocomplete="current-password" required></label><label><input style="width:auto;margin:0 8px 20px 0" name="remember" type="checkbox" value="yes" checked>Keep me signed in for 30 days</label><button>Sign in</button></form></html>'


def interactive_priority():
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(18)
            client.connect("/run/hal-inference-priority.sock")
            client.sendall(b"interactive")
            client.recv(64)
    except OSError:
        pass


import hal_shared
from hal_branding import render as render_ai_name

hal_shared.setup()
hal_shared.start_worker()


class Handler(http.server.BaseHTTPRequestHandler):
    def end_headers(self):
        # Renew cookie attributes for existing remembered sessions as well.
        try:
            cookie = http.cookies.SimpleCookie()
            cookie.load(self.headers.get("Cookie", ""))
            token = cookie["hal_session"].value if "hal_session" in cookie else ""
            with SESSION_LOCK:
                session = SESSIONS.get(token)
            if session and session[0] > time.time():
                self.send_header(
                    "Set-Cookie",
                    "hal_session="
                    + token
                    + "; Path=/; Secure; HttpOnly; SameSite=Lax; Max-Age="
                    + str(max(1, int(session[0] - time.time()))),
                )
        except (http.cookies.CookieError, AttributeError):
            pass
        super().end_headers()

    def setup(self):
        super().setup()
        self.connection.settimeout(20)

    def proxy(self):
        host = self.headers.get("Host", "")
        try:
            authority = urllib.parse.urlsplit("//" + host)
            valid = (
                authority.hostname in local_hosts()
                and authority.port == 8443
                and authority.username is None
                and authority.password is None
                and not (authority.path or authority.query or authority.fragment)
            )
        except ValueError:
            valid = False
        if not valid:
            self.send_error(403)
            return
        if (
            self.path
            not in {
                "/activity",
                "/activity/status",
                "/",
                "/status",
                "/models",
                "/tools",
                "/chat",
                "/assign",
                "/login",
            }
            and not re.fullmatch(r"/artifacts/[a-f0-9]{32}\.(pdf|png|svg)", self.path)
            and not self.path.split("?", 1)[0].startswith("/llama")
        ):
            self.send_error(404)
            return
        origin = self.headers.get("Origin")
        if origin and origin.lower() != "https://" + host.lower():
            self.send_error(403)
            return
        if self.path == "/login":
            if self.command != "POST":
                cookie = http.cookies.SimpleCookie()
                try:
                    cookie.load(self.headers.get("Cookie", ""))
                except http.cookies.CookieError:
                    pass
                token = cookie["hal_session"].value if "hal_session" in cookie else ""
                with SESSION_LOCK:
                    session = SESSIONS.get(token)
                if session and session[0] > time.time():
                    self.send_response(303)
                    self.send_header("Location", "/llama/")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                self.login_page()
                return
            try:
                size = int(self.headers.get("Content-Length", "0"))
                if (
                    not 0 < size <= 4096
                    or self.headers.get_content_type() != "application/x-www-form-urlencoded"
                ):
                    raise ValueError()
                form = urllib.parse.parse_qs(self.rfile.read(size).decode())
                username = form.get("username", [""])[0]
                password = form.get("password", [""])[0]
                if username != "hal" or not password or len(password) > 1024:
                    raise ValueError()
                authorization = (
                    "Basic " + base64.b64encode((username + ":" + password).encode()).decode()
                )
                probe = urllib.request.Request(
                    "http://127.0.0.1:8088/status",
                    headers={"Host": "127.0.0.1:8088", "Authorization": authorization},
                )
                with urllib.request.urlopen(probe, timeout=10) as response:
                    if response.status != 200:
                        raise ValueError()
                duration = 2592000 if form.get("remember") == ["yes"] else 43200
                token = secrets.token_urlsafe(32)
                with SESSION_LOCK:
                    for key in list(SESSIONS):
                        if SESSIONS[key][0] <= time.time():
                            del SESSIONS[key]
                    if len(SESSIONS) >= 256:
                        del SESSIONS[next(iter(SESSIONS))]
                    SESSIONS[token] = (time.time() + duration, authorization)
                    temporary = SESSION_FILE.with_suffix(".tmp")
                    temporary.write_text(json.dumps(SESSIONS))
                    temporary.chmod(0o600)
                    temporary.replace(SESSION_FILE)
                self.send_response(303)
                self.send_header("Location", "/llama/" if form.get("next") == ["llama"] else "/")
                self.send_header("Cache-Control", "no-store")
                cookie = "hal_session=" + token + "; Path=/; Secure; HttpOnly; SameSite=Lax"
                if form.get("remember") == ["yes"]:
                    cookie += "; Max-Age=" + str(duration)
                self.send_header("Set-Cookie", cookie)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            except (ValueError, UnicodeError, urllib.error.HTTPError):
                self.login_page(True)
                return
            except (OSError, urllib.error.URLError):
                self.send_error(503, "Chat login service unavailable")
                return
        authorization = self.headers.get("Authorization", "")
        if not authorization:
            try:
                cookie = http.cookies.SimpleCookie()
                cookie.load(self.headers.get("Cookie", ""))
                token = cookie["hal_session"].value if "hal_session" in cookie else ""
                with SESSION_LOCK:
                    session = SESSIONS.get(token)
                    if session and session[0] > time.time():
                        authorization = session[1]
                    elif session:
                        del SESSIONS[token]
            except http.cookies.CookieError:
                pass
        if self.path.split("?", 1)[0].startswith("/llama"):
            self.llama_proxy(authorization)
            return
        if not authorization and self.path == "/" and self.command == "GET":
            self.login_page()
            return
        if authorization and self.path == "/" and self.command == "GET":
            self.send_response(302)
            self.send_header("Location", "/llama/")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        headers = {"Host": "127.0.0.1:8088"}
        if authorization:
            headers["Authorization"] = authorization
        if origin:
            headers["Origin"] = "http://127.0.0.1:8088"
        body = None
        if self.command == "POST" and self.path == "/chat" and authorization:
            try:
                probe = urllib.request.Request(
                    "http://127.0.0.1:8088/status",
                    headers={"Host": "127.0.0.1:8088", "Authorization": authorization},
                )
                with urllib.request.urlopen(probe, timeout=5) as response:
                    response.read()
                interactive_priority()
            except OSError:
                pass
        if self.command == "POST":
            try:
                size = int(self.headers.get("Content-Length", "0"))
                if not 0 < size <= (2000000 if self.path == "/chat" else 32000):
                    raise ValueError()
            except ValueError:
                self.send_error(400)
                return
            body = self.rfile.read(size)
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(
            "http://127.0.0.1:8088" + ("/" if self.path == "/tools" else self.path),
            body,
            headers,
            method=self.command,
        )
        try:
            try:
                response = urllib.request.urlopen(req, timeout=190)
            except urllib.error.HTTPError as error:
                response = error
            with response:
                data = response.read(2_000_001)
                if len(data) > 2_000_000:
                    self.send_error(502)
                    return
                self.send_response(response.status)
                for key in [
                    "Content-Disposition",
                    "Content-Type",
                    "Cache-Control",
                    "WWW-Authenticate",
                    "Content-Security-Policy",
                    "X-Content-Type-Options",
                ]:
                    if response.headers.get(key):
                        self.send_header(key, response.headers[key])
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
        except (OSError, urllib.error.URLError):
            self.send_error(503, "Local application unavailable")

    def llama_proxy(self, authorization):
        if not authorization:
            self.login_page(llama=True)
            return
        probe = urllib.request.Request(
            "http://127.0.0.1:8088/status",
            headers={"Host": "127.0.0.1:8088", "Authorization": authorization},
        )
        try:
            with urllib.request.urlopen(probe, timeout=10) as response:
                response.read()
        except urllib.error.HTTPError as error:
            if error.code == 401:
                self.login_page(failed=True, llama=True)
                return
            self.send_error(503, "HAL authentication service unavailable")
            return
        except OSError:
            self.send_error(503, "HAL authentication service unavailable")
            return
        if hal_shared.api(self):
            return
        if self.path == "/llama":
            self.send_response(302)
            self.send_header("Location", "/llama/")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        body = None
        if self.command == "POST":
            interactive_priority()
            try:
                state = json.load(
                    urllib.request.urlopen("http://127.0.0.1/api/benchmark-state", timeout=5)
                )
            except (OSError, ValueError):
                state = {}
            if state.get("running") and state.get("expires", 0) > time.time():
                body = b'{"error":{"message":"HAL GPU benchmarks are running. Model loading and generation will be available after the tests finish."}}'
                self.send_response(503)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            try:
                size = int(self.headers.get("Content-Length", "0"))
                if not 0 <= size <= 16000000:
                    raise ValueError()
            except ValueError:
                self.send_error(400)
                return
            body = self.rfile.read(size)
        headers = {
            "Content-Type": self.headers.get("Content-Type", "application/json"),
            "Accept": self.headers.get("Accept", "*/*"),
            "Accept-Encoding": self.headers.get("Accept-Encoding", "gzip"),
        }
        req = urllib.request.Request(
            "http://127.0.0.1:11436" + self.path, body, headers, method=self.command
        )
        try:
            try:
                response = urllib.request.urlopen(req, timeout=190)
            except urllib.error.HTTPError as error:
                response = error
            with response:
                self.send_response(response.status)
                for key in [
                    "Content-Type",
                    "Content-Encoding",
                    "Content-Length",
                    "Cache-Control",
                    "Location",
                ]:
                    if response.headers.get(key):
                        self.send_header(key, response.headers[key])
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("X-Frame-Options", "DENY")
                self.end_headers()
                while chunk := response.read1(65536):
                    self.wfile.write(chunk)
                    self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            return
        except OSError:
            self.send_error(503, "Native llama.cpp service unavailable")

    def login_page(self, failed=False, llama=False):
        body = render_ai_name(LOGIN_PAGE.decode()).encode()
        if llama:
            body = body.replace(
                b'<form action="/login" method="post">',
                b'<form action="/login" method="post"><input type="hidden" name="next" value="llama">',
                1,
            )
        if failed:
            body = body.replace(
                b"<form ",
                b'<p role="alert">Sign-in failed. Check your username and password.</p><form ',
                1,
            )
        self.send_response(200 if not failed else 401)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'",
        )
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    do_GET = proxy
    do_POST = proxy


context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
context.minimum_version = ssl.TLSVersion.TLSv1_2
context.load_cert_chain("/etc/hal-https/server-chain.pem", "/etc/hal-https/server.key")
server = http.server.ThreadingHTTPServer(("0.0.0.0", 8443), Handler)
server.socket = context.wrap_socket(server.socket, server_side=True)
server.serve_forever()
