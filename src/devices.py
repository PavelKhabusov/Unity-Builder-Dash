"""Device manager page — ADB device management UI."""
import os, re, socket, subprocess, threading
from gi.repository import Gtk, Adw, GLib, Gio, Gdk
from .log_view import LogView
from . import ios_remote


# logcat -v threadtime line prefix: "MM-DD HH:MM:SS.mmm  PID  TID  "
# Stripped when the user toggles Compact view on the LogView toolbar.
LOGCAT_PREFIX_RE = re.compile(
    r"^\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d+\s+\d+\s+\d+\s+")


def _adb(*args, device=None, timeout=15):
    """Run adb command, return (success, stdout, stderr)."""
    cmd = ["adb"]
    if device:
        cmd += ["-s", device]
    cmd += list(args)
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.returncode == 0, r.stdout.strip(), r.stderr.strip()
    except Exception as e:
        return False, "", str(e)


def _adb_daemon_alive(timeout=0.5):
    """Pure TCP probe to 127.0.0.1:5037 (adb server socket). Returns True
    if a daemon is listening, False otherwise — no auto-start side effect
    (unlike `adb devices` which forks a daemon on miss), no env-var hacks
    (ANDROID_ADB_SERVER_START is undocumented and not honored on the
    Arch platform-tools build), and instant — ~1ms either way."""
    try:
        with socket.create_connection(("127.0.0.1", 5037), timeout=timeout):
            return True
    except (OSError, socket.timeout):
        return False


def _find_stuck_adb_clients(min_age_seconds=30):
    """Return labels of `adb` client processes (push/pull/shell/logcat/
    install) that have been running longer than min_age_seconds. A long
    push is normal; a multi-minute one usually means the daemon wedged
    mid-command. `ps -o pid,etimes,args` gives elapsed time in seconds."""
    stuck = []
    try:
        r = subprocess.run(
            ["ps", "-eo", "pid,etimes,args"],
            capture_output=True, text=True, timeout=2)
        if r.returncode != 0:
            return stuck
        for line in r.stdout.splitlines()[1:]:
            parts = line.strip().split(None, 2)
            if len(parts) < 3:
                continue
            pid, etimes, cmd = parts
            if not cmd.startswith("adb "):
                continue
            # Skip server itself, only count clients waiting on the daemon.
            if "fork-server" in cmd or "start-server" in cmd:
                continue
            # Filter to commands that block on the daemon.
            if not any(x in cmd for x in (
                    " push ", " pull ", " install ", " shell ",
                    " logcat", " devices")):
                continue
            try:
                if int(etimes) < min_age_seconds:
                    continue
            except ValueError:
                continue
            stuck.append(f"pid {pid} ({int(etimes)}s): {cmd[:60]}")
    except Exception:
        pass
    return stuck


def _usb_has_android_device():
    """True if `lsusb` shows a known Android / VR device VID. Used to
    detect the MTP-conflict case: device physically connected but adb
    sees zero — usually gvfsd-mtp / gvfsd-gphoto2 grabbed it first."""
    try:
        r = subprocess.run(["lsusb"], capture_output=True, text=True, timeout=2)
        if r.returncode != 0:
            return False
        # Common Android USB vendor IDs:
        #   18d1 Google Pixel/Nexus    04e8 Samsung    2833 Oculus/Meta
        #   2717 Xiaomi                12d1 Huawei     0bb4 HTC
        #   22b8 Motorola
        return any(vid in r.stdout.lower() for vid in (
            "18d1", "04e8", "2833", "2717", "12d1", "0bb4", "22b8"))
    except Exception:
        return False


def _probe_adb_health():
    """Single-shot health probe for the toolbar indicator.

    Returns a dict with: alive, responsive, device_count, stuck (list of
    descriptors), usb_present (USB shows an Android device), _devices."""
    info = {"alive": False, "responsive": False, "device_count": 0,
            "stuck": [], "usb_present": False, "_devices": []}
    info["alive"] = _adb_daemon_alive(timeout=0.5)
    if info["alive"]:
        try:
            r = subprocess.run(["adb", "devices", "-l"],
                               capture_output=True, text=True, timeout=2)
            info["responsive"] = (r.returncode == 0)
            if info["responsive"]:
                # Reuse _parse_devices output format via the same parser by
                # feeding it our own quick call (avoid double timeout cost).
                devs = []
                for line in r.stdout.strip().splitlines()[1:]:
                    line = line.strip()
                    if not line or "List of" in line:
                        continue
                    parts = line.split()
                    if len(parts) < 2:
                        continue
                    props = {}
                    for p in parts[2:]:
                        if ":" in p:
                            k, v = p.split(":", 1)
                            props[k] = v
                    devs.append({
                        "id": parts[0], "state": parts[1],
                        "model": props.get("model", "?"),
                        "product": props.get("product", "?"),
                        "transport_id": props.get("transport_id", "?"),
                        "wireless": ":" in parts[0],
                    })
                info["_devices"] = devs
                info["device_count"] = sum(
                    1 for d in devs if d["state"] == "device")
        except subprocess.TimeoutExpired:
            info["responsive"] = False
        except Exception:
            info["responsive"] = False
    info["stuck"] = _find_stuck_adb_clients()
    info["usb_present"] = _usb_has_android_device()
    return info


def _parse_devices(timeout=5):
    """Parse `adb devices -l` output into list of dicts.

    Default timeout dropped from 15s to 5s — `adb devices -l` returns in
    ~50ms when the daemon's up. A 15s wait was masking the symptom of a
    stalled daemon (post-build kill-server) and blocking polling for
    seconds on each tick."""
    ok, out, _ = _adb("devices", "-l", timeout=timeout)
    # First call after a kill-server prints daemon-startup chatter to stderr
    # and exits 0 — treat that as "daemon up" even though it took a couple
    # of seconds. Caller can use _adb_daemon_alive() if it needs a finer
    # check.
    if not ok:
        return []
    devices = []
    for line in out.splitlines()[1:]:
        line = line.strip()
        if not line or "List of" in line:
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        dev_id = parts[0]
        state = parts[1]
        props = {}
        for p in parts[2:]:
            if ":" in p:
                k, v = p.split(":", 1)
                props[k] = v
        is_wireless = ":" in dev_id
        devices.append({
            "id": dev_id,
            "state": state,
            "model": props.get("model", "?"),
            "product": props.get("product", "?"),
            "transport_id": props.get("transport_id", "?"),
            "wireless": is_wireless,
        })
    return devices


def _get_running_apps(device_id):
    ok, out, _ = _adb("shell", "ps", "-A", "-o", "NAME", device=device_id)
    if not ok:
        return []
    return sorted(set(
        l.strip() for l in out.splitlines()
        if "." in l.strip() and not l.strip().startswith("[")
           and l.strip().count(".") >= 2
    ))


def _get_installed_packages(device_id):
    ok, out, _ = _adb("shell", "pm", "list", "packages", "-3", device=device_id)
    if not ok:
        return []
    return sorted(
        l.replace("package:", "").strip()
        for l in out.splitlines() if l.startswith("package:")
    )


def _get_app_permissions(device_id, package):
    import re as _re
    ok, out, _ = _adb("shell", "dumpsys", "package", package, device=device_id, timeout=10)
    if not ok:
        return []
    perms = []
    in_section = False
    for line in out.splitlines():
        stripped = line.strip()
        if stripped.startswith("runtime permissions:"):
            in_section = True
            continue
        if in_section:
            if not line.startswith("    ") and stripped:
                break
            m = _re.match(r'^([^:]+):\s*granted=(true|false)', stripped)
            if m:
                perms.append((m.group(1), m.group(2) == "true"))
    return sorted(perms, key=lambda x: x[0])


def _get_apps_info(device_id, packages):
    """Get version and install date for packages via single shell call."""
    if not packages:
        return {}
    # Single adb shell call with inline loop — much faster than per-package calls
    script = 'for p in ' + ' '.join(packages[:30]) + '; do '
    script += 'v=$(dumpsys package $p 2>/dev/null | grep -m1 versionName= | sed "s/.*=//"); '
    script += 'u=$(dumpsys package $p 2>/dev/null | grep -m1 lastUpdateTime= | sed "s/.*=//"); '
    script += 'i=$(dumpsys package $p 2>/dev/null | grep -m1 firstInstallTime= | sed "s/.*=//"); '
    script += 'echo "$p|$v|$u|$i"; done'
    ok, out, _ = _adb("shell", script, device=device_id, timeout=15)
    info = {}
    if out:
        for line in out.splitlines():
            parts = line.split("|")
            if len(parts) >= 4 and parts[0] in packages:
                info[parts[0]] = {
                    "version": parts[1].strip(),
                    "updated": parts[2].strip().split(" ")[0] if parts[2].strip() else "",
                    "installed": parts[3].strip().split(" ")[0] if parts[3].strip() else "",
                }
    return info



def _get_logcat_tag(line):
    """Extract the priority letter (E/W/I/D/V). Works on both the raw
    threadtime form ("MM-DD HH:MM:SS.mmm  PID  TID  W TAG: ...") and the
    compact form with the timestamp prefix already stripped — LogView's
    level filter calls this on the visible string."""
    parts = line.split(None, 5)
    # Raw: parts[4] is the level. Compact: parts[0] is the level.
    for idx in (4, 0):
        if idx < len(parts):
            lvl = parts[idx][:1]
            if lvl in ("E", "W", "I", "D", "V"):
                return lvl
    return None


class DevicesPage(Gtk.Box):
    """Full-page device manager with ADB controls."""

    def __init__(self, cfg=None):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self._cfg = cfg or {}
        self._mac_online = None  # None=unknown, True=ok, False=offline

        # ── Top bar ──
        # Drastically simplified: just the ADB state indicator + a kebab
        # popover for the rarely-used wireless `adb connect` flow. There's
        # no Refresh button — auto-poll picks up plug/unplug within 3s.
        # No Restart ADB / Kill MTP buttons — those recoveries fire from
        # the polling loop automatically when the indicator detects the
        # corresponding bad state.
        top = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        top.set_margin_top(8)
        top.set_margin_start(12)
        top.set_margin_end(12)
        top.set_margin_bottom(4)

        # ADB state indicator. Polling auto-detects daemon problems and
        # recovers in the background:
        #   down         → adb start-server
        #   wedged       → pkill -9 adb fork-server + start-server
        #   mtp_blocked  → killall gvfsd-mtp + start-server (frees USB
        #                  endpoint when Files/Nautilus grabbed it via MTP)
        #   stuck        → kill -9 the hung adb client pids
        # Click bypasses the cooldown for immediate recovery.
        self._adb_state_lbl = Gtk.Button(label="● ADB ?", css_classes=["flat", "dim-label"],
                                          tooltip_text="ADB daemon state (click for immediate recovery)")
        self._adb_state_lbl.connect("clicked", self._on_adb_state_clicked)
        top.append(self._adb_state_lbl)
        self._adb_state = None
        self._adb_recovery_at = {}  # state -> last auto-recovery timestamp

        spacer = Gtk.Box()
        spacer.set_hexpand(True)
        top.append(spacer)

        # Wireless-ADB popover. `adb connect <ip>:5555` is niche (USB is
        # the common path) — tucked behind one icon button so it doesn't
        # clutter the bar.
        wireless_btn = Gtk.MenuButton(icon_name="network-wireless-symbolic",
                                       css_classes=["flat"],
                                       tooltip_text="Wireless ADB (connect by IP)")
        popover = Gtk.Popover()
        pbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6,
                       margin_top=8, margin_bottom=8,
                       margin_start=12, margin_end=12)
        self._ip_entry = Gtk.Entry(placeholder_text="192.168.1.x:5555")
        self._ip_entry.set_width_chars(20)
        pbox.append(self._ip_entry)
        connect_btn = Gtk.Button(label="Connect")
        connect_btn.add_css_class("suggested-action")
        connect_btn.connect("clicked",
            lambda b: (self._on_connect(b), popover.popdown()))
        pbox.append(connect_btn)
        popover.set_child(pbox)
        wireless_btn.set_popover(popover)
        top.append(wireless_btn)

        self.append(top)

        # ── Status ──
        self._status = Gtk.Label(label="", xalign=0, css_classes=["dim-label", "caption"])
        self._status.set_margin_start(12)
        self._status.set_margin_bottom(4)
        self.append(self._status)

        # ── Paned: top = devices, bottom = logcat ──
        self._paned = Gtk.Paned(orientation=Gtk.Orientation.VERTICAL)
        self._paned.set_vexpand(True)

        # Top: device list
        device_scroll = Gtk.ScrolledWindow()
        self._device_list = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self._device_list.set_margin_start(12)
        self._device_list.set_margin_end(12)
        self._device_list.set_margin_top(4)
        self._device_list.set_margin_bottom(12)
        device_scroll.set_child(self._device_list)
        self._paned.set_start_child(device_scroll)
        self._paned.set_resize_start_child(True)
        self._paned.set_shrink_start_child(False)

        # Bottom: inline logcat (hidden until activated)
        self._logcat_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)

        # App filter (goes at start of LogView filter bar)
        self._logcat_app_filter = Gtk.DropDown.new_from_strings(["All apps"])
        self._logcat_app_filter.set_selected(0)
        self._logcat_app_filter.connect("notify::selected", self._on_logcat_app_changed)

        # Action buttons (go at end of LogView filter bar)
        self._logcat_clear = Gtk.Button(icon_name="edit-clear-symbolic", tooltip_text="Clear",
                                        css_classes=["flat"])
        self._logcat_pause = Gtk.ToggleButton(icon_name="media-playback-pause-symbolic",
                                              tooltip_text="Pause", css_classes=["flat"])
        self._logcat_close = Gtk.Button(icon_name="window-close-symbolic",
                                        tooltip_text="Close logcat", css_classes=["flat"])

        self._logcat_view = LogView(
            levels=["All", "Error", "Warning", "Info", "Debug"],
            get_tag=_get_logcat_tag, margin=8,
            extra_start=[self._logcat_app_filter],
            extra_end=[self._logcat_clear, self._logcat_pause, self._logcat_close],
            compact_re=LOGCAT_PREFIX_RE)
        self._logcat_box.append(self._logcat_view)

        self._paned.set_end_child(self._logcat_box)
        self._paned.set_resize_end_child(True)
        self._paned.set_shrink_end_child(False)

        # Initially hide logcat
        self._logcat_box.set_visible(False)

        self._logcat_clear.connect("clicked", lambda _: self._logcat_view.clear())
        self._logcat_pause.connect("toggled",
                                   lambda b: self._logcat_view.set_paused(b.get_active()))
        self._logcat_close.connect("clicked", lambda _: self._stop_logcat())

        self.append(self._paned)

        self._devices = []
        self._logcat_proc = None
        self._logcat_dev = None

        # Drag & drop APK
        drop = Gtk.DropTarget.new(Gio.File, Gdk.DragAction.COPY)
        drop.connect("drop", self._on_drop_file)
        self.add_controller(drop)
        self._logcat_pkg_pids = {}  # pkg -> set of pids

        # Auto-poll device presence while this page is visible. Plug/unplug
        # is detected within ~3s without the user having to click refresh.
        # Tied to window-level start/stop_polling() calls from
        # _on_sidebar_selected so we don't churn adb in the background when
        # the user is on a different tab.
        self._poll_timer = None
        self._last_device_ids = None  # None = "haven't probed yet"

    def _stop_logcat(self):
        """Stop logcat stream and hide panel."""
        proc = self._logcat_proc
        self._logcat_proc = None
        self._logcat_dev = None
        self._logcat_filter_pkg = None
        self._logcat_filter_pids = set()
        t = getattr(self, "_logcat_pid_timer", None)
        if t is not None:
            GLib.source_remove(t)
            self._logcat_pid_timer = None
        if proc:
            try: proc.kill()
            except: pass
        self._logcat_box.set_visible(False)

    def refresh(self):
        # Re-entrancy guard. Builds do `adb kill-server` and the daemon takes
        # 1-2s to come back; meanwhile `adb devices -l` blocks waiting for
        # the smartsocket. If the user hammers refresh — or if a build's
        # restart races with it — concurrent calls stack up to dozens of
        # stuck adb clients that the system can't drain quickly. One scan
        # at a time keeps the queue at zero.
        if getattr(self, "_refresh_running", False):
            return
        self._refresh_running = True
        self._status.set_text("Scanning...")
        def do_scan():
            try:
                alive = _adb_daemon_alive(timeout=1)
                GLib.idle_add(self._set_adb_state, "up" if alive else "down")
                devs = _parse_devices()
                # After _parse_devices the daemon is definitely up (it
                # auto-spawns on first call). Reflect that.
                GLib.idle_add(self._set_adb_state, "up")
                for dev in devs:
                    if dev["state"] == "device":
                        dev["running_apps"] = _get_running_apps(dev["id"])
                        dev["installed_apps"] = _get_installed_packages(dev["id"])
                    else:
                        dev["running_apps"] = []
                        dev["installed_apps"] = []
                # Remember the set so the lightweight poller can decide if
                # anything actually changed and skip the heavy re-render.
                self._last_device_ids = {(d["id"], d["state"]) for d in devs}
                GLib.idle_add(self._update_list, devs)
            finally:
                self._refresh_running = False
        threading.Thread(target=do_scan, daemon=True).start()

    def start_polling(self):
        """Begin background plug/unplug detection. Called when this page
        becomes the active tab. Cheap — `adb devices -l` only; full refresh
        (running apps + packages per device) fires only when the device set
        actually changes."""
        if self._poll_timer is not None:
            return
        # Kick an immediate scan so the user sees current state without
        # waiting for the first 3s tick. refresh() is idempotent and uses
        # _refresh_running so the immediate + scheduled call don't race.
        self.refresh()
        self._poll_timer = GLib.timeout_add_seconds(3, self._poll_devices)

    def stop_polling(self):
        if self._poll_timer is not None:
            GLib.source_remove(self._poll_timer)
            self._poll_timer = None

    def _poll_devices(self):
        """Lightweight presence check + ADB health probe."""
        if getattr(self, "_refresh_running", False):
            return True
        win = self.get_root()
        if win and getattr(win, "worker", None) is not None:
            GLib.idle_add(self._set_adb_state, "paused")
            return True
        def probe():
            info = _probe_adb_health()
            devs = info.pop("_devices", [])
            ids = {(d["id"], d["state"]) for d in devs}
            def react():
                self._set_adb_state(info)
                if ids != self._last_device_ids:
                    self.refresh()
                return False
            GLib.idle_add(react)
        threading.Thread(target=probe, daemon=True).start()
        return True

    def _run_recovery(self, state, info):
        """Background recovery actions per state. See `_set_adb_state`
        docstring for state → action mapping."""
        import time as _t
        try:
            if state == "down":
                # If start-server times out, the OS-level USB stack is
                # likely wedged (Quest in bad state after reboot, cable
                # half-disconnected, etc.). Record that so the indicator
                # can surface a more actionable "replug device" hint
                # instead of looping silently on "down".
                try:
                    subprocess.run(["adb", "start-server"], timeout=5,
                                   capture_output=True)
                    self._adb_start_server_hung = False
                except subprocess.TimeoutExpired:
                    self._adb_start_server_hung = True
                    # Kill the hung start-server so the next attempt isn't
                    # blocked by its leftover process holding USB ioctls.
                    subprocess.run(["pkill", "-9", "-f", "adb start-server"],
                                   timeout=3, capture_output=True)
            elif state == "wedged":
                # Can't use `adb kill-server` — it tries to talk to the
                # same wedged socket and hangs. pkill the fork-server
                # process directly, then auto-spawn a fresh one.
                subprocess.run(["pkill", "-9", "-f", "adb.*fork-server"],
                               timeout=3, capture_output=True)
                _t.sleep(0.3)
                subprocess.run(["adb", "start-server"], timeout=5,
                               capture_output=True)
            elif state == "mtp_blocked":
                # Frees the USB endpoint that gvfsd-mtp/gphoto2 holds.
                # Files app's MTP browse breaks until next replug — fair
                # trade for getting adb back.
                for proc in ("gvfsd-mtp", "gvfsd-gphoto2"):
                    subprocess.run(["killall", proc],
                                   capture_output=True, timeout=2)
                _t.sleep(0.5)
                subprocess.run(["adb", "kill-server"], timeout=3,
                               capture_output=True)
                _t.sleep(0.3)
                subprocess.run(["adb", "start-server"], timeout=5,
                               capture_output=True)
            elif state == "stuck":
                for line in info.get("stuck", []):
                    # "pid <N> (<etimes>s): <cmd>"
                    parts = line.split()
                    if len(parts) >= 2:
                        try:
                            subprocess.run(["kill", "-9", parts[1]],
                                           timeout=2, capture_output=True)
                        except Exception:
                            pass
        except Exception:
            pass
        # Trigger an immediate re-poll so the indicator updates.
        def _one_shot():
            self._poll_devices()
            return False
        GLib.idle_add(_one_shot)

    def _set_adb_state(self, info):
        """Update the toolbar ADB indicator from a state dict OR a string
        state name for fast paths ("starting", "paused").

        Dict keys:
          alive:       bool  — TCP probe to 127.0.0.1:5037 succeeded
          responsive:  bool  — `adb devices` returned within timeout
          device_count int   — number of devices (only meaningful if responsive)
          stuck:       list  — pids of long-running `adb push|pull|shell|logcat`
                               client processes (indicates wedged commands)
        """
        # Fast-path string states: starting / paused. Keep simple, no probe.
        if isinstance(info, str):
            fast = {
                "starting": ("● ADB starting", "warning",   "ADB daemon starting up..."),
                "paused":   ("● ADB (build)",  "dim-label", "Polling paused while build runs"),
            }
            if info in fast:
                text, css, tip = fast[info]
                self._adb_state = info
                # Invalidate the dedup signature — otherwise the next probe
                # tick computing the SAME (state, count, stuck) tuple as
                # before this fast-path call would skip the label update,
                # freezing the indicator on "starting" forever.
                self._adb_state_signature = None
                self._render_adb_label(text, css, tip)
                return
            return

        alive = info.get("alive", False)
        responsive = info.get("responsive", False)
        device_count = info.get("device_count", 0)
        stuck = info.get("stuck", [])
        usb_present = info.get("usb_present", False)
        # Derive the logical state from the probe result.
        if not alive:
            new_state = "down"
        elif not responsive:
            new_state = "wedged"
        elif stuck:
            new_state = "stuck"
        elif device_count > 0:
            new_state = "ok"
        elif usb_present:
            # Daemon up, no devices, but USB shows an Android VID — most
            # likely gvfsd-mtp / gvfsd-gphoto2 grabbed the USB endpoint
            # before adb could.
            new_state = "mtp_blocked"
        else:
            new_state = "empty"
        # Avoid re-rendering when nothing changed — except on state-bearing
        # subtleties (e.g. device count went from 0→1). Use a tuple cache.
        signature = (new_state, device_count, len(stuck))
        if getattr(self, "_adb_state_signature", None) == signature:
            return
        self._adb_state_signature = signature
        self._adb_state = new_state

        if new_state == "down":
            if getattr(self, "_adb_start_server_hung", False):
                text = "● ADB stuck (replug?)"
                css = "error"
                tip = ("`adb start-server` is hanging — the USB stack is "
                       "wedged (common after a Quest reboot or a yanked\n"
                       "cable). Unplug the device, wait a second, plug back\n"
                       "in. Click here to retry the start.")
            else:
                text = "● ADB down"
                css = "error"
                tip = ("ADB daemon not listening on tcp:5037.\n"
                       "Click to run `adb start-server`.")
        elif new_state == "wedged":
            text = "● ADB wedged"
            css = "error"
            tip = ("Daemon listens on tcp:5037 but `adb devices` times out — "
                   "wedged state, normal kill-server can't recover.\n"
                   "Click to force-kill the daemon process and restart it.")
        elif new_state == "stuck":
            text = f"● ADB up · {len(stuck)} stuck"
            css = "warning"
            tip = ("Daemon healthy but some adb client commands are stuck:\n  "
                   + "\n  ".join(stuck) + "\n\nClick to kill stuck processes.")
        elif new_state == "mtp_blocked":
            text = "● ADB · MTP blocking"
            css = "warning"
            tip = ("USB shows an Android/VR device but adb sees nothing — "
                   "gvfsd-mtp / gvfsd-gphoto2 likely grabbed the endpoint.\n"
                   "Auto-recovery will killall the MTP services shortly; "
                   "click to do it now.")
        elif new_state == "ok":
            text = f"● ADB up · {device_count} dev"
            css = "success"
            tip = f"ADB daemon healthy, {device_count} device(s) attached"
        else:  # empty
            text = "● ADB up · 0 dev"
            css = "warning"
            tip = "ADB daemon up but no devices attached"
        self._render_adb_label(text, css, tip)

    def _render_adb_label(self, text, css, tip):
        self._adb_state_lbl.set_label(text)
        for c in ("success", "error", "warning", "dim-label"):
            self._adb_state_lbl.remove_css_class(c)
        self._adb_state_lbl.add_css_class(css)
        self._adb_state_lbl.set_tooltip_text(tip)

    def _on_adb_state_clicked(self, _btn):
        """Manual recovery trigger. Single-flight: if a recovery thread is
        already running, ignore the click — otherwise spam-clicking
        accumulates competing `adb start-server` processes that fight over
        the USB stack and wedge the daemon for good."""
        if getattr(self, "_recovery_in_flight", False):
            return
        state = self._adb_state or "down"
        self._recovery_in_flight = True
        self._set_adb_state("starting")
        info = {"stuck": _find_stuck_adb_clients()} if state == "stuck" else {}
        def do():
            try: self._run_recovery(state, info)
            finally: self._recovery_in_flight = False
        threading.Thread(target=do, daemon=True).start()

    def _update_list(self, devices):
        self._devices = devices
        while (c := self._device_list.get_first_child()):
            self._device_list.remove(c)

        # Mac card (only if an IP is configured)
        r = ios_remote.get_remote_cfg(self._cfg)
        if r.get("mac_ip"):
            self._device_list.append(self._make_mac_card(r))
            # Async probe to refresh the indicator
            threading.Thread(target=self._probe_mac, args=(r,), daemon=True).start()

        if not devices:
            if not r.get("mac_ip"):
                self._status.set_text("No devices connected")
                self._device_list.append(
                    Adw.StatusPage(title="No devices", icon_name="phone-symbolic",
                                   description="Connect a device via USB or WiFi"))
            else:
                self._status.set_text("Mac configured; no Android devices")
            return

        self._status.set_text(f"{len(devices)} device(s)")
        for dev in devices:
            self._device_list.append(self._make_device_card(dev))

    def _probe_mac(self, remote):
        ok = ios_remote.test_connection(remote, log_cb=None, notify=False)
        self._mac_online = ok
        GLib.idle_add(self._refresh_mac_status)

    def _refresh_mac_status(self):
        """Update the Mac card's status label in place without full rebuild."""
        status_lbl = getattr(self, "_mac_status_lbl", None)
        if not status_lbl: return False
        ok = self._mac_online
        txt = "Connected" if ok is True else "Disconnected" if ok is False else "Checking..."
        color = "success" if ok is True else "error" if ok is False else "dim-label"
        status_lbl.set_text(txt)
        for c in ("success", "error", "dim-label"):
            status_lbl.remove_css_class(c)
        status_lbl.add_css_class(color)
        return False

    def _make_mac_card(self, remote):
        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0,
                       css_classes=["card"])
        inner = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6,
                        margin_top=12, margin_bottom=12,
                        margin_start=16, margin_end=16)

        # Header
        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        title = Gtk.Label(label="Mac (iOS build server)", xalign=0,
                          css_classes=["heading"], hexpand=True)
        header.append(title)

        status_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        status_box.append(Gtk.Image.new_from_icon_name("computer-apple-ipad-symbolic"))
        self._mac_status_lbl = Gtk.Label(label="Checking...", css_classes=["caption", "dim-label"])
        status_box.append(self._mac_status_lbl)
        header.append(status_box)
        inner.append(header)

        sub = Gtk.Label(
            label=f"{remote['mac_user']}@{remote['mac_ip']}  •  {remote['mac_work_dir']}",
            xalign=0, css_classes=["dim-label", "caption"])
        inner.append(sub)

        # Action buttons
        actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=2,
                          margin_top=6)
        for tooltip, icon, cb in [
            ("Reconnect (notify Mac)", "emblem-synchronizing-symbolic",
             lambda _: self._mac_reconnect()),
            ("Open SSH terminal", "utilities-terminal-symbolic",
             lambda _: ios_remote.open_ssh_terminal(
                 ios_remote.get_remote_cfg(self._cfg), log_cb=None)),
            ("Install server files", "document-save-symbolic",
             lambda _: threading.Thread(
                 target=ios_remote.install_mac_server,
                 args=(ios_remote.get_remote_cfg(self._cfg), self._log_to_status),
                 daemon=True).start()),
            ("Open iOS settings", "emblem-system-symbolic",
             lambda _: self._open_ios_settings()),
        ]:
            btn = Gtk.Button(icon_name=icon, tooltip_text=tooltip, css_classes=["flat"])
            btn.connect("clicked", cb)
            actions.append(btn)
        inner.append(actions)

        card.append(inner)
        return card

    def _mac_reconnect(self):
        """Explicit reconnect: SSH + notify on Mac, update indicator."""
        r = ios_remote.get_remote_cfg(self._cfg)
        self._status.set_text(f"Reconnecting to {r['mac_user']}@{r['mac_ip']}...")
        def do():
            ok = ios_remote.test_connection(r, log_cb=None, notify=True)
            self._mac_online = ok
            GLib.idle_add(self._refresh_mac_status)
            GLib.idle_add(self._status.set_text,
                "Mac: connected" if ok else "Mac: connection failed")
        threading.Thread(target=do, daemon=True).start()

    def _log_to_status(self, text):
        """Route iOS-module logs through the status bar label (one-line)."""
        GLib.idle_add(self._status.set_text, text.strip().split("\n")[-1][:120])

    def _open_ios_settings(self):
        win = self.get_root()
        if win and hasattr(win, "_open_settings_ios"):
            win._open_settings_ios()

    def _make_device_card(self, dev):
        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        card.add_css_class("card")

        inner = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        inner.set_margin_top(12)
        inner.set_margin_bottom(12)
        inner.set_margin_start(16)
        inner.set_margin_end(16)

        # Header
        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        name = dev["model"].replace("_", " ")
        title = Gtk.Label(label=name, xalign=0, css_classes=["heading"])
        title.set_hexpand(True)
        header.append(title)

        conn_type = "WiFi" if dev["wireless"] else "USB"
        state_icon = "network-wireless-symbolic" if dev["wireless"] else "media-removable-symbolic"
        online = dev["state"] == "device"
        status_color = "success" if online else "error"

        status_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        status_box.append(Gtk.Image.new_from_icon_name(state_icon))
        status_box.append(Gtk.Label(
            label=f"{conn_type} • {dev['state']}", css_classes=["caption", status_color]))
        header.append(status_box)
        inner.append(header)

        sub = Gtk.Label(
            label=f"{dev['id']}  •  {dev['product']}  •  transport_id:{dev['transport_id']}",
            xalign=0, css_classes=["dim-label", "caption"])
        inner.append(sub)

        if not online:
            card.append(inner)
            return card

        # ── Action buttons (single row) ──
        actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=2)
        actions.set_margin_top(6)

        for tooltip, icon, cb in [
            ("Install APK", "document-save-symbolic", self._on_install),
            ("Push File", "send-to-symbolic", self._on_push),
            ("Screenshot", "camera-photo-symbolic", self._on_screenshot),
            ("Cast (scrcpy)", "video-display-symbolic", self._on_cast),
            ("Shell", "utilities-terminal-symbolic", self._on_shell),
            ("Logcat", "document-open-recent-symbolic", self._on_logcat),
            ("Files", "folder-symbolic", self._on_files),
            ("Device Info", "dialog-information-symbolic", self._on_device_info),
            ("WiFi On/Off", "network-wireless-symbolic", self._on_toggle_wifi),
            ("Airplane On/Off", "airplane-mode-symbolic", self._on_toggle_airplane),
        ]:
            btn = Gtk.Button(icon_name=icon, tooltip_text=tooltip, css_classes=["flat"])
            btn.connect("clicked", lambda _, d=dev, c=cb: c(d))
            actions.append(btn)

        if dev["wireless"]:
            disc_btn = Gtk.Button(icon_name="network-offline-symbolic",
                                  tooltip_text="Disconnect", css_classes=["flat"])
            disc_btn.connect("clicked", lambda _, d=dev: self._on_disconnect(d))
            actions.append(disc_btn)

        inner.append(actions)

        # ── Apps section ──
        running = dev.get("running_apps", [])
        installed = dev.get("installed_apps", [])
        installed_set = set(installed)
        running_3p = [p for p in running if p in installed_set]

        if installed:
            sep = Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL)
            sep.set_margin_top(6)
            inner.append(sep)

            # Search bar for apps
            app_search = Gtk.SearchEntry(placeholder_text="Filter apps...")
            app_search.set_margin_top(6)
            inner.append(app_search)

            # Merged app list: running first, then rest
            all_apps = []
            for pkg in running_3p:
                all_apps.append((pkg, True))
            for pkg in installed:
                if pkg not in set(running_3p):
                    all_apps.append((pkg, False))

            app_scroll = Gtk.ScrolledWindow()
            app_scroll.set_max_content_height(220)
            app_scroll.set_propagate_natural_height(True)
            app_scroll.set_margin_top(4)

            app_list = Gtk.ListBox()
            app_list.set_selection_mode(Gtk.SelectionMode.NONE)
            app_list.add_css_class("boxed-list")

            for pkg, is_running in all_apps:
                row = self._make_app_row(dev, pkg, is_running=is_running, is_installed=True)
                row._pkg_name = pkg
                app_list.append(row)

            # Filter function
            def on_app_filter(entry, lst=app_list):
                query = entry.get_text().lower().strip()
                row = lst.get_first_child()
                while row:
                    visible = not query or query in getattr(row, '_pkg_name', '').lower()
                    row.set_visible(visible)
                    row = row.get_next_sibling()

            app_search.connect("search-changed", on_app_filter)

            app_scroll.set_child(app_list)
            inner.append(app_scroll)

            count_lbl = Gtk.Label(
                label=f"{len(running_3p)} running, {len(installed)} installed",
                xalign=0, css_classes=["caption", "dim-label"])
            count_lbl.set_margin_top(2)
            inner.append(count_lbl)

            # Lazy-load version/date info in background
            self._load_apps_info_async(dev, app_list, installed)

        card.append(inner)
        return card

    def _load_apps_info_async(self, dev, app_list, packages):
        """Load app info in background and update labels."""
        def do_load():
            info = _get_apps_info(dev["id"], packages)
            GLib.idle_add(self._apply_apps_info, app_list, info)
        threading.Thread(target=do_load, daemon=True).start()

    def _apply_apps_info(self, app_list, info):
        """Apply loaded info to existing app rows."""
        i = 0
        while True:
            row = app_list.get_row_at_index(i)
            if row is None:
                break
            i += 1
            pkg = getattr(row, "_pkg_name", "")
            lbl = getattr(row, "_info_label", None)
            if pkg and lbl and pkg in info:
                d = info[pkg]
                parts = []
                if d.get("version"):
                    parts.append(f"v{d['version']}")
                if d.get("updated"):
                    parts.append(f"upd {d['updated']}")
                elif d.get("installed"):
                    parts.append(f"inst {d['installed']}")
                if parts:
                    lbl.set_text("  ".join(parts))
                    lbl.set_visible(True)
            row = row.get_next_sibling()

    def _make_app_row(self, dev, pkg, is_running, is_installed, info=None):
        row = Gtk.ListBoxRow(selectable=False)
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        box.set_margin_top(6)
        box.set_margin_bottom(6)
        box.set_margin_start(10)
        box.set_margin_end(6)

        if is_running:
            box.append(Gtk.Image.new_from_icon_name("media-playback-start-symbolic"))
        else:
            box.append(Gtk.Image.new_from_icon_name("media-playback-stop-symbolic"))

        # Name + version/date subtitle
        text_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1)
        text_box.set_hexpand(True)
        lbl = Gtk.Label(label=pkg, xalign=0, css_classes=["caption", "monospace"])
        lbl.set_ellipsize(3)
        text_box.append(lbl)

        info = info or {}
        sub_parts = []
        if info.get("version"):
            sub_parts.append(f"v{info['version']}")
        if info.get("updated"):
            sub_parts.append(f"upd {info['updated']}")
        elif info.get("installed"):
            sub_parts.append(f"inst {info['installed']}")
        if sub_parts:
            sub = Gtk.Label(label="  ".join(sub_parts), xalign=0,
                            css_classes=["dim-label"], ellipsize=3)

            text_box.append(sub)

        box.append(text_box)

        # Kill button (inline, always visible for running)
        if is_running:
            kill = Gtk.Button(icon_name="process-stop-symbolic",
                              tooltip_text="Force stop",
                              css_classes=["flat", "circular"])
            kill.connect("clicked",
                         lambda _, d=dev, p=pkg: self._run_async(
                             f"Kill {p}", lambda: _adb("shell", "am", "force-stop", p, device=d["id"]),
                             lambda ok: self.refresh()))
            box.append(kill)

        # Clear data (inline)
        if is_installed:
            clear = Gtk.Button(icon_name="edit-clear-symbolic",
                               tooltip_text="Clear app data",
                               css_classes=["flat", "circular"])
            clear.connect("clicked",
                          lambda _, d=dev, p=pkg: self._run_async(
                              f"Clear {p}", lambda: _adb("shell", "pm", "clear", p, device=d["id"])))
            box.append(clear)

        # Three-dot menu for less common actions
        if is_installed:
            menu = Gio.Menu()
            action_id = pkg.replace(".", "_")

            menu.append("Manage Permissions", f"app.perm-{action_id}")
            menu.append("Profile", f"app.profile-{action_id}")
            menu.append("Uninstall", f"app.uninst-{action_id}")

            # Register actions on the window
            win = self.get_root()
            if win:
                for act_name, cb in [
                    (f"perm-{action_id}", lambda *_, d=dev, p=pkg: self._on_permissions(d, p)),
                    (f"profile-{action_id}", lambda *_, p=pkg, d=dev: self._on_open_profiler(d, p)),
                    (f"uninst-{action_id}", lambda *_, d=dev, p=pkg: self._confirm_and_run(
                        f"Uninstall {p}?", f"Uninstall {p}",
                        lambda: _adb("uninstall", p, device=d["id"]))),
                ]:
                    action = Gio.SimpleAction.new(act_name, None)
                    action.connect("activate", cb)
                    # Use app-level actions to avoid duplicates on rebuild
                    app = win.get_application()
                    if app:
                        # Remove old if exists
                        if app.lookup_action(act_name):
                            app.remove_action(act_name)
                        app.add_action(action)

            menu_btn = Gtk.MenuButton(icon_name="view-more-symbolic",
                                      menu_model=menu, css_classes=["flat", "circular"],
                                      valign=Gtk.Align.CENTER)
            box.append(menu_btn)

        row.set_child(box)
        return row

    def _on_open_profiler(self, dev, pkg):
        """Switch to profiler tab with this device + app selected."""
        win = self.get_root()
        if not win:
            return
        # Set profiler selections
        profiler = win._profiler_page
        profiler._selected_dev_id = dev["id"]
        profiler._selected_pkg = pkg
        profiler._initialized = False  # force refresh
        # Switch to profiler tab
        win._sidebar_list.select_row(
            win._sidebar_list.get_row_at_index(3))  # Profiler is index 3

    def _on_drop_file(self, target, value, x, y):
        """Handle drag & drop of APK file."""
        path = value.get_path()
        if not path or not path.endswith(".apk"):
            self._status.set_text("Only .apk files supported")
            return False
        # Install on first connected device
        if not self._devices:
            self._status.set_text("No device connected")
            return False
        dev = next((d for d in self._devices if d["state"] == "device"), None)
        if not dev:
            self._status.set_text("No online device")
            return False
        import os
        self._run_async(f"Install {os.path.basename(path)}",
                        lambda: _adb("install", "-r", path, device=dev["id"]),
                        lambda ok: self.refresh())
        return True

    def _confirm_and_run(self, heading, label, func):
        dlg = Adw.AlertDialog()
        dlg.set_heading(heading)
        dlg.add_response("cancel", "Cancel")
        dlg.add_response("ok", "OK")
        dlg.set_response_appearance("ok", Adw.ResponseAppearance.DESTRUCTIVE)
        def on_resp(d, resp):
            if resp == "ok":
                self._run_async(label, func, lambda ok: self.refresh())
        dlg.connect("response", on_resp)
        dlg.present(self.get_root())

    def _on_permissions(self, dev, pkg):
        self._status.set_text(f"Loading permissions for {pkg}...")
        def fetch():
            perms = _get_app_permissions(dev["id"], pkg)
            GLib.idle_add(self._present_permissions_dialog, dev, pkg, perms)
        threading.Thread(target=fetch, daemon=True).start()

    def _present_permissions_dialog(self, dev, pkg, permissions):
        self._status.set_text("")

        dlg = Adw.Dialog()
        dlg.set_title(f"Permissions: {pkg.split('.')[-1]}")
        dlg.set_content_width(450)
        dlg.set_content_height(500)

        tb = Adw.ToolbarView()
        header = Adw.HeaderBar()
        grant_all = Gtk.Button(label="Grant All", css_classes=["suggested-action"])
        revoke_all = Gtk.Button(label="Revoke All", css_classes=["destructive-action"])
        header.pack_start(grant_all)
        header.pack_end(revoke_all)
        tb.add_top_bar(header)

        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        sub_label = Gtk.Label(label=pkg, xalign=0,
                              css_classes=["caption", "dim-label", "monospace"])
        sub_label.set_margin_start(12)
        sub_label.set_margin_top(8)
        content.append(sub_label)

        if not permissions:
            content.append(Adw.StatusPage(title="No runtime permissions", vexpand=True))
            tb.set_content(content)
            dlg.set_child(tb)
            dlg.present(self.get_root())
            return

        scroll = Gtk.ScrolledWindow(vexpand=True)
        page = Adw.PreferencesPage()
        grp = Adw.PreferencesGroup()
        switch_rows = []

        for perm_name, granted in permissions:
            short = perm_name.split(".")[-1] if "." in perm_name else perm_name
            row = Adw.SwitchRow(title=short, subtitle=perm_name)
            row.set_active(granted)
            row._perm_name = perm_name

            def on_toggle(row, _pspec, d=dev, p=pkg):
                new_state = row.get_active()
                action = "grant" if new_state else "revoke"
                pn = row._perm_name
                self._run_async(f"{action.title()} {pn.split('.')[-1]}",
                                lambda: _adb("shell", "pm", action, p, pn, device=d["id"]))

            row.connect("notify::active", on_toggle)
            grp.add(row)
            switch_rows.append(row)

        page.add(grp)
        scroll.set_child(page)
        content.append(scroll)

        grant_all.connect("clicked", lambda _: [r.set_active(True) for r in switch_rows if not r.get_active()])
        revoke_all.connect("clicked", lambda _: [r.set_active(False) for r in switch_rows if r.get_active()])

        tb.set_content(content)
        dlg.set_child(tb)
        dlg.present(self.get_root())

    # ── Actions ──

    def _run_async(self, label, func, on_done=None):
        self._status.set_text(f"{label}...")
        def worker():
            ok, out, err = func()
            msg = out if ok else (err or "Failed")
            GLib.idle_add(self._status.set_text, f"{label}: {msg[:80]}")
            if on_done:
                GLib.idle_add(on_done, ok)
        threading.Thread(target=worker, daemon=True).start()

    def _on_connect(self, _):
        ip = self._ip_entry.get_text().strip()
        if not ip:
            return
        if ":" not in ip:
            ip += ":5555"
        self._run_async(f"Connect {ip}",
                        lambda: _adb("connect", ip),
                        lambda ok: self.refresh())

    def _on_disconnect(self, dev):
        self._run_async(f"Disconnect {dev['id']}",
                        lambda: _adb("disconnect", dev["id"]),
                        lambda ok: self.refresh())

    def _on_install(self, dev):
        dialog = Gtk.FileDialog()
        dialog.set_title("Select APK")
        f = Gtk.FileFilter()
        f.set_name("APK files")
        f.add_pattern("*.apk")
        filters = _make_filter_list([f])
        dialog.set_filters(filters)
        dialog.open(self.get_root(), None, lambda d, r: self._do_install(d, r, dev))

    def _do_install(self, dialog, result, dev):
        try:
            f = dialog.open_finish(result)
            path = f.get_path()
            self._run_async(f"Install {os.path.basename(path)}",
                            lambda: _adb("install", "-r", path, device=dev["id"]),
                            lambda ok: self.refresh())
        except: pass

    def _on_push(self, dev):
        dialog = Gtk.FileDialog()
        dialog.set_title("Select file to push")
        dialog.open(self.get_root(), None, lambda d, r: self._do_push(d, r, dev))

    def _do_push(self, dialog, result, dev):
        try:
            f = dialog.open_finish(result)
            path = f.get_path()
            remote = f"/sdcard/Download/{os.path.basename(path)}"
            self._run_async(f"Push {os.path.basename(path)}",
                            lambda: _adb("push", path, remote, device=dev["id"]))
        except: pass

    def _on_screenshot(self, dev):
        tmp = "/tmp/ubd_screenshot.png"
        def do_screenshot():
            ok1, _, e1 = _adb("shell", "screencap", "-p", "/sdcard/screenshot.png", device=dev["id"])
            if not ok1: return False, "", e1
            ok2, _, e2 = _adb("pull", "/sdcard/screenshot.png", tmp, device=dev["id"])
            if not ok2: return False, "", e2
            _adb("shell", "rm", "/sdcard/screenshot.png", device=dev["id"])
            subprocess.Popen(["xdg-open", tmp])
            return True, tmp, ""
        self._run_async("Screenshot", do_screenshot)

    def _on_shell(self, dev):
        try:
            subprocess.Popen(["gnome-terminal", "--", "adb", "-s", dev["id"], "shell"])
        except FileNotFoundError:
            try:
                subprocess.Popen(["xterm", "-e", f"adb -s {dev['id']} shell"])
            except:
                self._status.set_text("No terminal emulator found")

    def _on_logcat_app_changed(self, *_):
        """Restart logcat with new app filter."""
        if not self._logcat_dev:
            return
        dev_id = self._logcat_dev
        # Find device dict
        dev = next((d for d in self._devices if d["id"] == dev_id), None)
        if dev:
            self._start_logcat_stream(dev)

    def _on_logcat(self, dev):
        """Start/toggle inline logcat panel."""
        if self._logcat_dev == dev["id"] and self._logcat_proc:
            self._stop_logcat()
            return

        if self._logcat_proc:
            self._stop_logcat()

        self._logcat_view.clear()
        self._logcat_box.set_visible(True)
        self._logcat_dev = dev["id"]
        self._logcat_pause.set_active(False)
        self._paned.set_position(self._paned.get_allocated_height() // 2)

        # Populate app filter dropdown
        installed = dev.get("installed_apps", [])
        items = ["All apps"] + installed
        self._logcat_app_filter.set_model(Gtk.StringList.new(items))
        self._logcat_app_filter.set_selected(0)

        self._start_logcat_stream(dev)

    def _start_logcat_stream(self, dev):
        """Start or restart logcat. Filter-by-app uses client-side PID
        matching with a background poller that refreshes the PID set every
        2s — survives app restart (new PID picked up) and lets us catch
        lines from an app that wasn't running when logcat attached."""
        # Kill previous + stop previous PID poller
        proc = self._logcat_proc
        self._logcat_proc = None
        if proc:
            try: proc.kill()
            except: pass
        t = getattr(self, "_logcat_pid_timer", None)
        if t is not None:
            GLib.source_remove(t)
            self._logcat_pid_timer = None

        self._logcat_view.clear()

        # Get selected app
        idx = self._logcat_app_filter.get_selected()
        installed = dev.get("installed_apps", [])
        selected_pkg = installed[idx - 1] if idx > 0 and idx - 1 < len(installed) else None
        self._logcat_filter_pkg = selected_pkg
        self._logcat_filter_pids = set()

        if selected_pkg:
            # Initial PID snapshot (runs synchronously on the GTK thread — it's
            # one cheap adb call; we want the user to see filtered lines from
            # the first one if the app's already running).
            self._refresh_logcat_pids(dev["id"])
            if not self._logcat_filter_pids:
                GLib.idle_add(self._logcat_view.append_line,
                              f"⏳ Waiting for {selected_pkg} to start...\n")
            # Re-probe pidof every 2s so app restart / new child processes are
            # picked up without restarting logcat.
            def _tick(dev_id=dev["id"], pkg=selected_pkg):
                if self._logcat_filter_pkg != pkg or self._logcat_proc is None:
                    return False
                threading.Thread(
                    target=lambda: self._refresh_logcat_pids(dev_id),
                    daemon=True).start()
                return True
            self._logcat_pid_timer = GLib.timeout_add_seconds(2, _tick)

        def reader_thread():
            try:
                cmd = ["adb", "-s", dev["id"], "logcat", "-v", "threadtime"]
                proc = subprocess.Popen(
                    cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    text=True, bufsize=1)
                self._logcat_proc = proc

                # Batch lines at ~20 Hz before sending to GTK. Logcat emits
                # thousands of lines per second on a busy device — per-line
                # idle_add floods the main thread and freezes the UI hard
                # ("not responding") the instant we attach. Same pattern as
                # BuildWorker / ProgressListener / RemoteRunner.
                import time as _t
                pending = []
                last_flush = [0.0]
                def flush():
                    if not pending: return
                    batch = pending[:]
                    pending.clear()
                    last_flush[0] = _t.monotonic()
                    def _deliver(lines=batch):
                        try: self._logcat_view.append_lines(lines)
                        except Exception: pass
                        return False
                    GLib.idle_add(_deliver)

                for line in proc.stdout:
                    if self._logcat_proc is None:
                        break
                    # Client-side app filter. PID is the 3rd whitespace token
                    # of `-v threadtime` lines:
                    #   "MM-DD HH:MM:SS.mmm  PID  TID  LEVEL TAG: MSG"
                    # When no app filter is active (_logcat_filter_pkg is None)
                    # everything passes through.
                    if self._logcat_filter_pkg:
                        parts = line.split(None, 4)
                        if len(parts) >= 4:
                            pid = parts[2]
                            if pid not in self._logcat_filter_pids:
                                continue
                        else:
                            # Non-conforming line (probably logcat banner) —
                            # let it through so users see startup output.
                            pass
                    pending.append(line)
                    if _t.monotonic() - last_flush[0] >= 0.05:
                        flush()
                flush()
            except Exception as e:
                GLib.idle_add(self._logcat_view.append_line, f"Error: {e}\n")

        threading.Thread(target=reader_thread, daemon=True).start()

    def _refresh_logcat_pids(self, dev_id):
        """Update _logcat_filter_pids for the current _logcat_filter_pkg.
        `pidof -s` returns single PID, plain `pidof` returns all matching —
        Quest's BusyBox pidof emits space-separated PIDs for processes with
        the same package (rare but possible: app + isolated service). Also
        scan `ps` for child processes whose comm starts with the package."""
        pkg = self._logcat_filter_pkg
        if not pkg:
            return
        new_pids = set()
        ok, out, _ = _adb("shell", "pidof", pkg, device=dev_id, timeout=5)
        if ok and out.strip():
            new_pids.update(out.strip().split())
        # Fallback / supplement: scan `ps` for processes whose name contains
        # the package (covers package:childN sub-processes some apps spawn).
        ok2, ps_out, _ = _adb("shell", "ps", "-A", "-o", "PID,NAME",
                              device=dev_id, timeout=5)
        if ok2:
            for ln in ps_out.splitlines():
                p = ln.strip().split(None, 1)
                if len(p) == 2 and pkg in p[1]:
                    new_pids.add(p[0])
        # Detect newly appeared PIDs (app just launched).
        was_empty = not self._logcat_filter_pids
        appeared = new_pids - self._logcat_filter_pids
        self._logcat_filter_pids = new_pids
        if was_empty and appeared:
            GLib.idle_add(self._logcat_view.append_line,
                          f"▶ {pkg} started (PID {', '.join(sorted(appeared))})\n")

    def _on_files(self, dev):
        """Open native file manager via MTP or adb pull to tmp."""
        # Try to open device in native file manager via gvfs/MTP
        mtp_path = f"/run/user/{os.getuid()}/gvfs/"
        if os.path.isdir(mtp_path):
            # Find device mount
            for d in os.listdir(mtp_path):
                if dev["model"].lower() in d.lower() or "mtp" in d.lower():
                    subprocess.Popen(["xdg-open", os.path.join(mtp_path, d)])
                    return
        # Fallback: open gvfs root (Nautilus will show MTP devices)
        subprocess.Popen(["xdg-open", mtp_path if os.path.isdir(mtp_path) else "/"])


    def _on_cast(self, dev):
        """Launch scrcpy for screen mirroring."""
        import shutil
        if not shutil.which("scrcpy"):
            self._status.set_text("scrcpy not installed (pacman -S scrcpy)")
            return
        self._status.set_text(f"Starting cast for {dev['model']}...")
        subprocess.Popen(["scrcpy", "-s", dev["id"]],
                         start_new_session=True,
                         stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL)

    def _on_device_info(self, dev):
        """Show device info popup."""
        self._status.set_text("Loading device info...")
        def do_load():
            info = {}
            for key, cmd in [
                ("Model", ["shell", "getprop", "ro.product.model"]),
                ("Device", ["shell", "getprop", "ro.product.device"]),
                ("Android", ["shell", "getprop", "ro.build.version.release"]),
                ("SDK", ["shell", "getprop", "ro.build.version.sdk"]),
                ("Build", ["shell", "getprop", "ro.build.display.id"]),
                ("Serial", ["shell", "getprop", "ro.serialno"]),
                ("CPU", ["shell", "getprop", "ro.product.board"]),
            ]:
                ok, out, _ = _adb(*cmd, device=dev["id"])
                if ok and out:
                    info[key] = out
            # Storage
            ok, out, _ = _adb("shell", "df", "-h", "/data", device=dev["id"])
            if ok:
                lines = out.strip().splitlines()
                if len(lines) >= 2:
                    parts = lines[1].split()
                    if len(parts) >= 4:
                        info["Storage"] = f"{parts[2]} used / {parts[1]} total ({parts[4]})"
            # Display
            ok, out, _ = _adb("shell", "wm", "size", device=dev["id"])
            if ok and out:
                info["Display"] = out.replace("Physical size: ", "")
            GLib.idle_add(self._show_device_info, dev, info)
        threading.Thread(target=do_load, daemon=True).start()

    def _show_device_info(self, dev, info):
        self._status.set_text("")
        dlg = Adw.Dialog()
        dlg.set_title(f"{dev['model'].replace('_', ' ')}")
        dlg.set_content_width(380)
        dlg.set_content_height(400)
        tb = Adw.ToolbarView()
        tb.add_top_bar(Adw.HeaderBar())
        page = Adw.PreferencesPage()
        grp = Adw.PreferencesGroup()
        for key, val in info.items():
            grp.add(Adw.ActionRow(title=key, subtitle=val))
        page.add(grp)
        tb.set_content(page)
        dlg.set_child(tb)
        dlg.present(self.get_root())

    def _on_toggle_wifi(self, dev):
        def do_toggle():
            ok, out, _ = _adb("shell", "settings", "get", "global", "wifi_on", device=dev["id"])
            if not ok:
                return False, "", "Failed to read WiFi state"
            is_on = out.strip() == "1"
            new_state = "disable" if is_on else "enable"
            return _adb("shell", "svc", "wifi", new_state, device=dev["id"])
        self._run_async("Toggle WiFi", do_toggle)

    def _on_toggle_airplane(self, dev):
        def do_toggle():
            ok, out, _ = _adb("shell", "settings", "get", "global", "airplane_mode_on", device=dev["id"])
            if not ok:
                return False, "", "Failed to read airplane state"
            is_on = out.strip() == "1"
            new_val = "0" if is_on else "1"
            ok1, _, e1 = _adb("shell", "settings", "put", "global", "airplane_mode_on", new_val, device=dev["id"])
            if not ok1:
                return False, "", e1
            return _adb("shell", "am", "broadcast", "-a",
                        "android.intent.action.AIRPLANE_MODE", "--ez", "state",
                        "false" if is_on else "true", device=dev["id"])
        self._run_async("Toggle Airplane", do_toggle)


def _make_filter_list(filters):
    from gi.repository import Gio
    store = Gio.ListStore.new(Gtk.FileFilter)
    for f in filters:
        store.append(f)
    return store