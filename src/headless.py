"""Headless build runner: same BuildWorker + iOS pipeline as the GUI, no GTK.

Started detached by the CLI when the GUI is not running. Serves the agent API
while the build runs, then exits with 0 on success.
"""
import os, sys, json, threading, subprocess

from gi.repository import GLib

from . import ios_remote, pipeline
from .config import load_config
from .constants import APP_NAME
from .worker import BuildWorker
from .agent_state import tracker
from .agent_api import AgentServer, Backend, api_port


class HeadlessRunner(Backend):
    owner = "headless"

    def __init__(self, cfg, params):
        self.cfg = cfg
        self.p = params
        self.loop = GLib.MainLoop()
        self.worker = None
        self.runner = None
        self.listener = None
        self.cancelled = False
        self.result = None

    # ── logging ──
    def log(self, t):
        sys.stdout.write(t); sys.stdout.flush()
        tracker.log(t)
        return False

    def log_lines(self, lines):
        sys.stdout.writelines(lines); sys.stdout.flush()
        tracker.log_lines(lines)
        return False

    def stage(self, text, frac=-1.0):
        tracker.stage(text, frac if frac is not None and frac >= 0 else None)
        return False

    def progress(self, frac):
        tracker.progress(frac)
        return False

    # ── Backend ──
    def start_build(self, req):
        return 409, {"ok": False, "error": "busy: headless build already running",
                     "status": tracker.snapshot()}

    def cancel(self):
        self.cancelled = True
        if self.worker:
            self.worker.cancel()
        if self.runner:
            self.runner.stop()
            threading.Thread(target=self._remote, args=("stop", None), daemon=True).start()
        self.log("\n  Cancelled by agent.\n")
        self._finish(False)
        return 200, {"ok": True, "cancelled": True}

    # ── flow ──
    def run(self):
        p = self.p
        proj, target = p["proj"], p["target"]
        action = p["ios_action"] if target == "ios" else ("aab" if p["aab"] else "apk")
        tracker.begin(proj["name"], target, action, "headless", pipeline.options_of(p))
        self.server = AgentServer(self, api_port(self.cfg))
        if not self.server.start():
            self.log(f"Agent API not started: {self.server.error}\n")
        if target == "ios":
            self.listener = ios_remote.ProgressListener(
                ios_remote.get_remote_cfg(self.cfg).get("progress_port", 8080),
                log_cb=self.log, log_bulk_cb=self.log_lines, progress_cb=self.progress)
            self.listener.start()
            GLib.idle_add(self._start_ios)
        else:
            GLib.idle_add(self._start_unity, target, self._android_done)
        self.loop.run()
        return self.result

    def _start_unity(self, target, done):
        self.worker = BuildWorker(
            self.cfg, self.p["proj"], target, self.log, done, self.stage,
            auto_increment=self.p["increment"], scripts_only=self.p["scripts_only"],
            aab=self.p["aab"], log_bulk_cb=self.log_lines)
        self.worker.start()
        return False

    def _android_done(self, ok):
        if self.cancelled:
            return False
        dev = self.p["install"]
        if ok and dev:
            def inst():
                iok = pipeline.install_apk(self.p["proj"], dev, lambda t: GLib.idle_add(self.log, t))
                GLib.idle_add(self._finish, iok)
            threading.Thread(target=inst, daemon=True).start()
        else:
            self._finish(ok)
        return False

    def _start_ios(self):
        cfg, p = self.cfg, self.p
        needs_unity, needs_zip, needs_scp, osa_arg, _label, status_text = \
            pipeline.resolve_ios_action(cfg, p["ios_action"], p["device"])
        self.stage(status_text)

        def post():
            threading.Thread(target=pipeline.ios_post_build, args=(
                cfg, p["proj"], needs_zip, needs_scp, osa_arg, self.log,
                lambda t: self.stage(t), self._remote, self._finish, status_text),
                daemon=True).start()

        if needs_unity:
            def after_unity(ok):
                if self.cancelled: return False
                if not ok:
                    self._finish(False)
                else:
                    post()
                return False
            self._start_unity("ios", after_unity)
        else:
            post()
        return False

    def _remote(self, osa_arg, done):
        self.runner = ios_remote.RemoteRunner(
            ios_remote.get_remote_cfg(self.cfg), log_cb=self.log, done_cb=done,
            progress_cb=self.progress, log_bulk_cb=self.log_lines)
        self.runner.run(osa_arg)

    def _finish(self, ok):
        if self.result is not None:
            return False
        self.result = bool(ok) and not self.cancelled
        start = self.worker.start_time if self.worker else tracker.snapshot().get("started_at")
        try:
            if not self.cancelled:
                pipeline.record_build(self.p["proj"], self.p["target"], self.result, start)
        except Exception as e:
            self.log(f"record_build error: {e}\n")
        tracker.finish(self.result, cancelled=self.cancelled)
        try:
            subprocess.Popen(["notify-send", "-i", "dialog-ok-apply" if self.result else "dialog-error",
                              APP_NAME, f"{self.p['proj']['name']} {self.p['target']}: "
                              f"{'done' if self.result else 'failed'} (headless)"])
        except Exception:
            pass
        GLib.timeout_add(4000 if self.cancelled else 1500, self._quit)
        return False

    def _quit(self):
        if self.listener:
            self.listener.stop()
        self.server.stop()
        self.loop.quit()
        return False


def main(req_json):
    cfg = load_config()
    err, params = pipeline.parse_build_request(cfg, json.loads(req_json))
    if err:
        print(f"error: {err}", file=sys.stderr)
        return 2
    reason = pipeline.busy_reason(project=params["proj"])
    if reason:
        print(f"busy: {reason}", file=sys.stderr)
        return 3
    return 0 if HeadlessRunner(cfg, params).run() else 1
