"""Build state shared between GUI, headless runner and CLI (no GTK)."""
import os, json, time, threading, datetime, subprocess

from .config import APP_DIR

LOGS_DIR = os.path.join(APP_DIR, "logs")
STATE_PATH = os.path.join(LOGS_DIR, "agent_status.json")
RUN_LOG_PREFIX = "run_"
KEEP_RUN_LOGS = 15


def read_state():
    try:
        with open(STATE_PATH) as f:
            return json.load(f)
    except Exception:
        return {}


def pid_alive(pid):
    if not pid:
        return False
    try:
        os.kill(int(pid), 0)
        return True
    except (ProcessLookupError, ValueError, TypeError):
        return False
    except PermissionError:
        return True


def unity_batch_processes():
    """Unity editors in -batchmode on this machine (any origin, not only ours)."""
    out = []
    try:
        r = subprocess.run(["ps", "-eo", "pid,args"], capture_output=True,
                           text=True, timeout=3)
        for line in r.stdout.splitlines()[1:]:
            pid, _, args = line.strip().partition(" ")
            if "/Editor/Unity" in args and "-batchmode" in args:
                out.append({"pid": int(pid), "args": args[:300]})
    except Exception:
        pass
    return out


def gui_processes():
    """Running GUI instances of this app (python3 .../build.py)."""
    out = []
    target = os.path.join(APP_DIR, "build.py")
    try:
        r = subprocess.run(["ps", "-eo", "pid,args"], capture_output=True,
                           text=True, timeout=3)
        for line in r.stdout.splitlines()[1:]:
            pid, _, args = line.strip().partition(" ")
            if int(pid) == os.getpid():
                continue
            if target in args or (args.endswith("build.py") and "python" in args
                                  and os.path.realpath(_proc_cwd(pid)) == APP_DIR):
                out.append(int(pid))
    except Exception:
        pass
    return out


def _proc_cwd(pid):
    try:
        return os.readlink(f"/proc/{pid}/cwd")
    except Exception:
        return ""


class Tracker:
    """Current build: status json + live log file. Thread-safe."""

    def __init__(self):
        self._lock = threading.Lock()
        self._fh = None
        self._state = {}
        self._last_write = 0.0

    @property
    def active(self):
        return bool(self._state.get("running"))

    def snapshot(self):
        with self._lock:
            return dict(self._state)

    def begin(self, project, target, action, source, options=None, pid=None):
        os.makedirs(LOGS_DIR, exist_ok=True)
        ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        log_path = os.path.join(LOGS_DIR, f"{RUN_LOG_PREFIX}{project}_{target}_{ts}.log")
        with self._lock:
            self._close()
            try:
                self._fh = open(log_path, "a", buffering=1)
            except OSError:
                self._fh = None
            self._state = {
                "running": True, "project": project, "target": target,
                "action": action, "source": source, "options": options or {},
                "stage": "Starting", "progress": 0.0,
                "started_at": time.time(), "finished_at": None, "result": None,
                "pid": pid or os.getpid(), "log_path": log_path,
            }
            self._write(force=True)
        self._prune()

    def log(self, text):
        if not self.active or not text:
            return
        with self._lock:
            if self._fh:
                try: self._fh.write(text if text.endswith("\n") else text + "\n")
                except Exception: pass

    def log_lines(self, lines):
        if not self.active:
            return
        with self._lock:
            if self._fh:
                try: self._fh.writelines(l if l.endswith("\n") else l + "\n" for l in lines)
                except Exception: pass

    def stage(self, text=None, frac=None):
        if not self.active:
            return
        with self._lock:
            changed = False
            if text and text != self._state.get("stage"):
                self._state["stage"] = text
                changed = True
            if frac is not None and frac >= 0:
                self._state["progress"] = round(float(frac), 3)
            self._write(force=changed)

    def progress(self, frac):
        self.stage(None, frac)

    def finish(self, ok, cancelled=False, extra=None):
        with self._lock:
            if not self._state.get("running"):
                return
            res = "cancelled" if cancelled else ("success" if ok else "failed")
            self._state.update(running=False, result=res, finished_at=time.time(),
                               stage={"success": "Done", "failed": "Failed",
                                      "cancelled": "Cancelled"}[res])
            if ok:
                self._state["progress"] = 1.0
            if extra:
                self._state.update(extra)
            if self._fh:
                try: self._fh.write(f"\n[ubd] result: {res}\n")
                except Exception: pass
            self._close()
            self._write(force=True)

    def _close(self):
        if self._fh:
            try: self._fh.close()
            except Exception: pass
            self._fh = None

    def _write(self, force=False):
        now = time.monotonic()
        if not force and now - self._last_write < 1.0:
            return
        self._last_write = now
        try:
            tmp = STATE_PATH + ".tmp"
            with open(tmp, "w") as f:
                json.dump(self._state, f, indent=2, ensure_ascii=False)
            os.replace(tmp, STATE_PATH)
        except Exception:
            pass

    def _prune(self):
        try:
            runs = sorted(f for f in os.listdir(LOGS_DIR) if f.startswith(RUN_LOG_PREFIX))
            for old in runs[:-KEEP_RUN_LOGS]:
                os.remove(os.path.join(LOGS_DIR, old))
        except Exception:
            pass


tracker = Tracker()
