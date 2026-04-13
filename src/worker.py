"""Build worker — runs Unity in background thread."""
import os, signal, subprocess, threading, time, datetime
from gi.repository import GLib
from .constants import TARGET_INFO, SKIP_PATTERNS, STAGE_PATTERNS, PROGRESS_RE
from .config import find_apk, get_unity_for_project, APP_DIR


class BuildWorker:
    def __init__(self, cfg, project, target, log_cb, done_cb, stage_cb, auto_increment=True):
        self.cfg = cfg
        self.unity = get_unity_for_project(cfg, project)
        self.project = project
        self.target = target
        self.log_cb = log_cb
        self.done_cb = done_cb
        self.stage_cb = stage_cb
        self.auto_increment = auto_increment
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

    def _save_log(self, lines):
        try:
            with open(self.log_path, "w") as f:
                f.writelines(lines)
            # Keep only last 10 logs per project
            logs_dir = os.path.dirname(self.log_path)
            prefix = self.project["name"] + "_"
            logs = sorted([f for f in os.listdir(logs_dir) if f.startswith(prefix)])
            for old in logs[:-10]:
                os.remove(os.path.join(logs_dir, old))
        except: pass

    def _run(self):
        path = self.project["path"]
        info = TARGET_INFO[self.target]
        method = info["method"] if self.auto_increment else info["method"] + "NoIncrement"
        build_target = "Android" if self.target == "android" else "iOS"
        cmd = [self.unity, "-quit", "-batchmode",
               "-buildTarget", build_target,        # pre-set target, skip platform switch
               "-disable-assembly-updater",         # skip API updater
               "-accept-apiupdate",                 # auto-accept updates
               "-DisableDirectConnection",          # skip ADB device scan
               "-skipMissingProjectID",             # skip cloud project ID check
               "-skipMissingUPID",                  # skip Unity analytics check
               "-projectPath", path,
               "-executeMethod", method, "-logFile", "-"]

        lock = os.path.join(path, "Temp", "UnityLockfile")
        if os.path.exists(lock): os.remove(lock)

        # Temporarily hide Unity's adb to skip 2+ min device scan
        unity_adb = os.path.join(os.path.dirname(self.unity),
            "Data/PlaybackEngines/AndroidPlayer/SDK/platform-tools/adb")
        adb_hidden = unity_adb + ".disabled"
        adb_was_hidden = False
        try:
            if os.path.exists(unity_adb):
                os.rename(unity_adb, adb_hidden)
                adb_was_hidden = True
        except: pass
        # Also kill system adb
        try: subprocess.run(["adb", "kill-server"], timeout=3, capture_output=True)
        except: pass

        GLib.idle_add(self.log_cb, f"  {path}\n\n")

        build_ok = False
        build_failed = False
        full_log = []

        # Prepare log file
        logs_dir = os.path.join(APP_DIR, "logs")
        os.makedirs(logs_dir, exist_ok=True)
        ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        self.log_path = os.path.join(logs_dir, f"{self.project['name']}_{ts}.log")

        try:
            self.process = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, bufsize=1, preexec_fn=os.setsid)
            for line in self.process.stdout:
                if self.cancelled: break
                full_log.append(line)
                s = line.strip()
                if not s: continue

                # Detect build result from log (more reliable than exit code)
                if "[Build] OK" in s: build_ok = True
                if "[Build] FAILED" in s or "Scripts have compiler errors" in s or "Aborting batchmode" in s:
                    build_failed = True

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

                # Stop reading once build result is known — but report done immediately
                if build_ok or build_failed:
                    # Report result now, let Unity finish cleanup in background
                    el = self.elapsed_str()
                    if build_ok and not self.cancelled:
                        apk = find_apk(self.project)
                        sz = f" ({os.path.getsize(apk)/(1024*1024):.0f} MB)" if apk else ""
                        GLib.idle_add(self.log_cb, f"\n  Done!{sz} {el}\n")
                        GLib.idle_add(self.done_cb, True)
                    elif not self.cancelled:
                        GLib.idle_add(self.log_cb, f"\n  Failed {el}\n")
                        GLib.idle_add(self.done_cb, False)
                    # Save log, restore adb, let Unity exit gracefully
                    self._save_log(full_log)
                    self.process.wait()
                    if adb_was_hidden and os.path.exists(adb_hidden):
                        try: os.rename(adb_hidden, unity_adb)
                        except: pass
                    try: subprocess.run(["adb", "start-server"], timeout=5, capture_output=True)
                    except: pass
                    return

            self.process.wait()
        except Exception as e:
            GLib.idle_add(self.log_cb, f"\n  Error: {e}\n")
        finally:
            self._save_log(full_log)
            # Restore adb
            if adb_was_hidden and os.path.exists(adb_hidden):
                try: os.rename(adb_hidden, unity_adb)
                except: pass
            # Restart adb server for deploy
            try: subprocess.run(["adb", "start-server"], timeout=5, capture_output=True)
            except: pass

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
