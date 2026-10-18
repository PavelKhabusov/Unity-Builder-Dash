"""Local HTTP API on 127.0.0.1 for agents/CLI (same style as home-kit-dash hub).

Served by the GUI or by the headless runner — whichever owns the build. Read
endpoints work off files; mutating ones are marshalled to the GLib main loop.
"""
import json, os, threading
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

from gi.repository import GLib

from .config import load_config, load_builds_log
from .agent_state import tracker, read_state

APP = "unity-builder-dash"
DEFAULT_PORT = 47816


def api_port(cfg=None):
    env = os.environ.get("UBD_API_PORT")
    if env and env.isdigit():
        return int(env)
    return int((cfg or load_config()).get("agent_api_port") or DEFAULT_PORT)


def manifest(port, owner):
    base = f"http://127.0.0.1:{port}"
    return {
        "app": APP, "version": 1, "base": base, "owner": owner,
        "commands": [
            {"name": "status", "doc": "Current build: project, target, stage, progress, time",
             "method": "GET", "path": "/status", "args": []},
            {"name": "projects", "doc": "Projects and their targets", "method": "GET",
             "path": "/projects", "args": []},
            {"name": "log", "doc": "Tail of the current/last build log", "method": "GET",
             "path": "/log", "args": [{"name": "tail", "type": "number", "doc": "lines, default 100"}]},
            {"name": "history", "doc": "Recent builds", "method": "GET", "path": "/history",
             "args": [{"name": "n", "type": "number", "doc": "how many, default 10"}]},
            {"name": "build", "doc": "Start a build (one at a time per machine)", "method": "POST",
             "path": "/build", "args": [
                 {"name": "project", "type": "string", "doc": "project name from /projects"},
                 {"name": "target", "type": "string", "doc": "android | ios"},
                 {"name": "scripts_only", "type": "boolean", "doc": "scripts only"},
                 {"name": "aab", "type": "boolean", "doc": "Android App Bundle"},
                 {"name": "increment", "type": "boolean", "doc": "bump the build number (default true)"},
                 {"name": "install", "type": "string", "doc": "android: adb serial; ios: xcodebuild device"},
                 {"name": "ios_action", "type": "string", "doc": "full | xcode | build_only | without_xcode"},
                 {"name": "ios_mode", "type": "string", "doc": "incremental | clean (default: config)"}]},
            {"name": "cancel", "doc": "Cancel the current build", "method": "POST",
             "path": "/cancel", "args": []},
        ],
    }


def status_payload():
    st = tracker.snapshot() or read_state()
    if st.get("started_at"):
        import time
        end = st.get("finished_at") or time.time()
        st["elapsed_sec"] = int(end - st["started_at"])
    return st


def tail_file(path, n):
    if not path or not os.path.isfile(path):
        return []
    with open(path, "rb") as f:
        f.seek(0, 2)
        size = f.tell()
        block = min(size, max(4096, n * 400))
        f.seek(size - block)
        data = f.read().decode("utf-8", "replace")
    return data.splitlines()[-n:] if n > 0 else data.splitlines()


class Backend:
    """Implemented by the GUI window and the headless runner. Main-thread only."""
    owner = "unknown"
    def start_build(self, req): raise NotImplementedError   # → (http_code, dict)
    def cancel(self): raise NotImplementedError             # → (http_code, dict)


def _on_main(fn, *args, timeout=15):
    box, ev = {}, threading.Event()
    def run():
        try: box["r"] = fn(*args)
        except Exception as e: box["r"] = (500, {"ok": False, "error": str(e)})
        ev.set()
        return False
    GLib.idle_add(run)
    if not ev.wait(timeout):
        return 504, {"ok": False, "error": "main loop did not respond"}
    return box["r"]


class AgentServer:
    def __init__(self, backend, port=None):
        self.backend = backend
        self.port = port or api_port()
        self.httpd = None
        self.error = None

    def start(self):
        backend, port = self.backend, self.port

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a): pass

            def _send(self, code, obj):
                body = json.dumps(obj, ensure_ascii=False, indent=1).encode()
                self.send_response(code)
                self.send_header("content-type", "application/json; charset=utf-8")
                self.send_header("content-length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                u = urlparse(self.path)
                q = {k: v[-1] for k, v in parse_qs(u.query).items()}
                if u.path == "/health":
                    return self._send(200, {"ok": True, "app": APP, "owner": backend.owner,
                                            "pid": os.getpid()})
                if u.path == "/commands.json":
                    return self._send(200, manifest(port, backend.owner))
                if u.path == "/status":
                    return self._send(200, status_payload())
                if u.path == "/projects":
                    cfg = load_config()
                    return self._send(200, {"projects": [
                        {"name": p["name"], "path": p["path"], "targets": p.get("targets", [])}
                        for p in cfg.get("projects", [])]})
                if u.path == "/log":
                    n = int(q.get("tail", 100) or 100)
                    st = status_payload()
                    return self._send(200, {"log_path": st.get("log_path"),
                                            "running": bool(st.get("running")),
                                            "lines": tail_file(st.get("log_path"), n)})
                if u.path == "/history":
                    n = int(q.get("n", 10) or 10)
                    return self._send(200, {"builds": load_builds_log()[-n:]})
                self._send(404, {"error": "not found"})

            def do_POST(self):
                u = urlparse(self.path)
                try:
                    ln = int(self.headers.get("content-length") or 0)
                    req = json.loads(self.rfile.read(ln) or b"{}") if ln else {}
                except Exception:
                    return self._send(400, {"ok": False, "error": "bad json"})
                if u.path == "/build":
                    return self._send(*_on_main(backend.start_build, req))
                if u.path == "/cancel":
                    return self._send(*_on_main(backend.cancel))
                self._send(404, {"error": "not found"})

        try:
            self.httpd = ThreadingHTTPServer(("127.0.0.1", port), H)
        except OSError as e:
            self.error = str(e)
            return False
        self.httpd.daemon_threads = True
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        return True

    def stop(self):
        if self.httpd:
            try:
                self.httpd.shutdown()
                self.httpd.server_close()
            except Exception:
                pass
            self.httpd = None
