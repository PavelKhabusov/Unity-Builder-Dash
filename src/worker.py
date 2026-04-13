"""Build worker — runs Unity in background thread."""
import os, signal, subprocess, threading, time
from gi.repository import GLib
from .constants import TARGET_INFO, SKIP_PATTERNS, STAGE_PATTERNS, PROGRESS_RE
from .config import find_apk


class BuildWorker:
    def __init__(self, unity, project, target, log_cb, done_cb, stage_cb):
        self.unity = unity
        self.project = project
        self.target = target
        self.log_cb = log_cb
        self.done_cb = done_cb
        self.stage_cb = stage_cb
        self.process = None
        self.cancelled = False
        self.start_time = None

    def start(self):
        self.cancelled = False
        self.start_time = time.time()
        threading.Thread(target=self._run, daemon=True).start()

    def cancel(self):
        self.cancelled = True
        if self.process:
            try: os.killpg(os.getpgid(self.process.pid), signal.SIGTERM)
            except ProcessLookupError: pass

    def elapsed_str(self):
        if not self.start_time: return ""
        m, s = divmod(int(time.time() - self.start_time), 60)
        return f"{m}:{s:02d}"

    def _run(self):
        path = self.project["path"]
        info = TARGET_INFO[self.target]
        cmd = [self.unity, "-quit", "-batchmode", "-projectPath", path,
               "-executeMethod", info["method"], "-logFile", "-"]

        lock = os.path.join(path, "Temp", "UnityLockfile")
        if os.path.exists(lock): os.remove(lock)

        GLib.idle_add(self.log_cb, f"  {path}\n\n")

        try:
            self.process = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, bufsize=1, preexec_fn=os.setsid)
            for line in self.process.stdout:
                if self.cancelled: break
                s = line.strip()
                if not s: continue
                if any(p in s for p in SKIP_PATTERNS): continue
                for pat, lbl in STAGE_PATTERNS:
                    if pat in s:
                        GLib.idle_add(self.stage_cb, lbl or s, -1.0)
                        break
                pm = PROGRESS_RE.search(s)
                if pm:
                    c, t = int(pm.group(1)), int(pm.group(2))
                    if t > 0: GLib.idle_add(self.stage_cb, None, min(c/t, 1.0))
                GLib.idle_add(self.log_cb, line)
            self.process.wait()
            code = self.process.returncode
        except Exception as e:
            GLib.idle_add(self.log_cb, f"\n  Error: {e}\n")
            code = -1

        el = self.elapsed_str()
        if self.cancelled:
            GLib.idle_add(self.done_cb, False)
        elif code == 0:
            apk = find_apk(self.project)
            sz = f" ({os.path.getsize(apk)/(1024*1024):.0f} MB)" if apk else ""
            GLib.idle_add(self.log_cb, f"\n  Done!{sz} {el}\n")
            GLib.idle_add(self.done_cb, True)
        else:
            GLib.idle_add(self.log_cb, f"\n  Failed (exit {code}) {el}\n")
            GLib.idle_add(self.done_cb, False)
