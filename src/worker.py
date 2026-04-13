"""Build worker — runs Unity in background thread."""
import os, signal, subprocess, threading, time
from gi.repository import GLib
from .constants import TARGET_INFO, SKIP_PATTERNS, STAGE_PATTERNS, PROGRESS_RE
from .config import find_apk, get_unity_for_project


class BuildWorker:
    def __init__(self, cfg, project, target, log_cb, done_cb, stage_cb):
        self.cfg = cfg
        self.unity = get_unity_for_project(cfg, project)
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

        build_ok = False
        build_failed = False

        try:
            self.process = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, bufsize=1, preexec_fn=os.setsid)
            for line in self.process.stdout:
                if self.cancelled: break
                s = line.strip()
                if not s: continue

                # Detect build result from log (more reliable than exit code)
                if "[Build] OK" in s: build_ok = True
                if "[Build] FAILED" in s: build_failed = True

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

                # Stop reading once build result is known
                if build_ok or build_failed:
                    break

            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.process.kill()
        except Exception as e:
            GLib.idle_add(self.log_cb, f"\n  Error: {e}\n")

        el = self.elapsed_str()
        success = build_ok and not build_failed and not self.cancelled

        if self.cancelled:
            GLib.idle_add(self.done_cb, False)
        elif success:
            apk = find_apk(self.project)
            sz = f" ({os.path.getsize(apk)/(1024*1024):.0f} MB)" if apk else ""
            GLib.idle_add(self.log_cb, f"\n  Done!{sz} {el}\n")
            GLib.idle_add(self.done_cb, True)
        else:
            code = self.process.returncode if self.process else -1
            GLib.idle_add(self.log_cb, f"\n  Failed (exit {code}) {el}\n")
            GLib.idle_add(self.done_cb, False)
