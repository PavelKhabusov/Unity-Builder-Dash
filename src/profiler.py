"""Profiler page — real-time device performance monitoring via ADB.

Collects metrics from:
- dumpsys gfxinfo (FPS, frame times, janky frames)
- dumpsys meminfo (RAM: PSS, native, java heap)
- top (CPU %)
- SurfaceFlinger / gfxinfo (GPU frame time)
- thermal zones / thermalservice (temperature)
- dumpsys battery (battery level + temp)
- logcat -s VrApi (Meta Quest: GPU/CPU level, stale frames, ASW, App time, etc.)
"""
import math, subprocess, threading, re
from collections import deque
from gi.repository import Gtk, Adw, GLib


def _adb_quick(*args, device=None, timeout=5):
    cmd = ["adb"]
    if device:
        cmd += ["-s", device]
    cmd += list(args)
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.stdout.strip() if r.returncode == 0 else ""
    except:
        return ""


def _parse_devices_simple():
    out = _adb_quick("devices", "-l")
    devices = []
    for line in out.splitlines()[1:]:
        parts = line.split()
        if len(parts) >= 2 and parts[1] == "device":
            props = {}
            for p in parts[2:]:
                if ":" in p:
                    k, v = p.split(":", 1)
                    props[k] = v
            devices.append({
                "id": parts[0],
                "model": props.get("model", parts[0]),
            })
    return devices


def _get_packages(device_id):
    out = _adb_quick("shell", "pm", "list", "packages", "-3", device=device_id)
    return sorted(l.replace("package:", "").strip()
                  for l in out.splitlines() if l.startswith("package:"))


# ── Data collectors ──

def _collect_fps(device_id, pkg):
    """Get FPS from gfxinfo. Falls back to 0 for VR apps (use VrApi instead)."""
    out = _adb_quick("shell", "dumpsys", "gfxinfo", pkg, "framestats", device=device_id)
    if not out:
        return None
    total = janky = 0
    for line in out.splitlines():
        if "Total frames rendered:" in line:
            m = re.search(r'(\d+)', line.split(":")[-1])
            if m: total = int(m.group(1))
        if "Janky frames:" in line:
            m = re.search(r'(\d+)', line.split(":")[-1])
            if m: janky = int(m.group(1))
    frame_times = []
    in_profile = False
    for line in out.splitlines():
        if "---PROFILEDATA---" in line:
            in_profile = not in_profile
            continue
        if in_profile and line.strip() and not line.startswith("Flags"):
            parts = line.split(",")
            if len(parts) >= 3:
                try:
                    start = int(parts[1])
                    end = int(parts[-1])
                    if start > 0 and end > start:
                        ms = (end - start) / 1_000_000
                        if 0 < ms < 200:
                            frame_times.append(ms)
                except (ValueError, IndexError):
                    pass
    avg_ms = sum(frame_times) / len(frame_times) if frame_times else 0
    fps = 1000 / avg_ms if avg_ms > 0 else 0
    return {"fps": min(fps, 120), "avg_ms": avg_ms, "total": total, "janky": janky}


def _collect_mem(device_id, pkg):
    out = _adb_quick("shell", "dumpsys", "meminfo", pkg, device=device_id)
    if not out:
        return None
    total_pss = native_heap = java_heap = 0
    for line in out.splitlines():
        if "TOTAL PSS:" in line or "TOTAL:" in line:
            m = re.search(r'TOTAL\s+(?:PSS:?\s+)?(\d+)', line)
            if m: total_pss = int(m.group(1))
        if "Native Heap" in line:
            parts = line.split()
            if len(parts) >= 3:
                try: native_heap = int(parts[1])
                except: pass
        if "Java Heap" in line or "Dalvik Heap" in line:
            parts = line.split()
            if len(parts) >= 3:
                try: java_heap = int(parts[1])
                except: pass
    return {"pss_mb": total_pss / 1024, "native_mb": native_heap / 1024,
            "java_mb": java_heap / 1024}


def _collect_cpu(device_id, pkg):
    """Get CPU% from top. Parses Quest/Android format without % symbol."""
    out = _adb_quick("shell", "top", "-n", "1", "-b", "-q", device=device_id, timeout=8)
    if not out:
        return None
    for line in out.splitlines():
        if pkg in line:
            parts = line.split()
            # Format: PID USER PR NI VIRT RES SHR S CPU% MEM% TIME+ ARGS
            # CPU% is typically column index 8 (after S column)
            # Find 'S' or 'R' state column, CPU% is next
            for i, p in enumerate(parts):
                if p in ("S", "R", "T", "Z", "D") and i + 1 < len(parts):
                    try:
                        return {"cpu_pct": float(parts[i + 1])}
                    except ValueError:
                        pass
            # Fallback: look for any float in reasonable position
            for p in parts:
                if "%" in p:
                    try: return {"cpu_pct": float(p.replace("%", ""))}
                    except: pass
    return {"cpu_pct": 0}


def _collect_gpu(device_id, pkg):
    """Get GPU utilization from sysfs (Adreno/Qualcomm) or SurfaceFlinger."""
    # Primary: Qualcomm/Adreno gpu_busy_percentage (works on Quest)
    out = _adb_quick("shell", "cat", "/sys/class/kgsl/kgsl-3d0/gpu_busy_percentage", device=device_id)
    if out:
        m = re.search(r'(\d+)', out)
        if m:
            pct = int(m.group(1))
            # Also get clock speed
            freq = _adb_quick("shell", "cat", "/sys/class/kgsl/kgsl-3d0/gpuclk", device=device_id)
            freq_mhz = int(freq) // 1_000_000 if freq and freq.isdigit() else 0
            return {"gpu_pct": pct, "freq_mhz": freq_mhz}

    # Fallback: SurfaceFlinger latency
    out = _adb_quick("shell", "dumpsys", "SurfaceFlinger", "--latency", device=device_id)
    frame_times_ms = []
    if out:
        for line in out.strip().splitlines()[1:]:
            parts = line.split()
            if len(parts) >= 3:
                try:
                    t1, t2 = int(parts[0]), int(parts[2])
                    if t1 > 0 and t2 > t1:
                        ms = (t2 - t1) / 1_000_000
                        if 0 < ms < 200:
                            frame_times_ms.append(ms)
                except (ValueError, IndexError):
                    pass
    if frame_times_ms:
        avg = sum(frame_times_ms) / len(frame_times_ms)
        return {"gpu_pct": 0, "avg_ms": avg, "freq_mhz": 0}

    # Fallback: gfxinfo Draw/Process/Execute
    out2 = _adb_quick("shell", "dumpsys", "gfxinfo", pkg, device=device_id)
    if out2:
        in_section = False
        for line in out2.splitlines():
            if "Draw\tPrepare\tProcess\tExecute" in line:
                in_section = True
                continue
            if in_section:
                parts = line.strip().split()
                if len(parts) >= 4:
                    try:
                        total = sum(float(p) for p in parts[:4])
                        if 0 < total < 200:
                            frame_times_ms.append(total)
                    except: pass
                elif frame_times_ms:
                    break
    if frame_times_ms:
        avg = sum(frame_times_ms) / len(frame_times_ms)
        return {"gpu_pct": 0, "avg_ms": avg, "freq_mhz": 0}
    return None


def _collect_thermal(device_id):
    temps = []
    for i in range(15):
        out = _adb_quick("shell", "cat", f"/sys/class/thermal/thermal_zone{i}/temp", device=device_id)
        if out:
            try:
                raw = int(out.strip())
                t = raw / 1000 if raw > 150 else float(raw)
                if 0 < t < 150:
                    temps.append(t)
            except: pass
    if not temps:
        out = _adb_quick("shell", "dumpsys", "thermalservice", device=device_id)
        if out:
            for line in out.splitlines():
                m = re.search(r'mValue=([\d.]+)', line)
                if m:
                    try:
                        val = float(m.group(1))
                        if 0 < val < 150:
                            temps.append(val)
                    except: pass
    return {"max_temp": max(temps) if temps else 0}


def _collect_battery(device_id):
    out = _adb_quick("shell", "dumpsys", "battery", device=device_id)
    level = temp = 0
    for line in out.splitlines():
        if "level:" in line:
            m = re.search(r'(\d+)', line)
            if m: level = int(m.group(1))
        if "temperature:" in line:
            m = re.search(r'(\d+)', line)
            if m: temp = int(m.group(1)) / 10
    return {"level": level, "temp": temp}


def _parse_vrapi_line(line):
    """Parse a single VrApi logcat line."""
    if "FPS=" not in line:
        return None
    d = {}
    m = re.search(r'FPS=(\d+)/(\d+)', line)
    if m:
        d["vr_fps"] = int(m.group(1))
        d["vr_target_fps"] = int(m.group(2))
    m = re.search(r'Stale=(\d+)', line)
    if m: d["stale"] = int(m.group(1))
    m = re.search(r'CPU\d*/GPU=(\d+)/(\d+)', line)
    if m:
        d["cpu_level"] = int(m.group(1))
        d["gpu_level"] = int(m.group(2))
    m = re.search(r'(\d+)/(\d+)MHz', line)
    if m:
        d["cpu_mhz"] = int(m.group(1))
        d["gpu_mhz"] = int(m.group(2))
    m = re.search(r'Fov=(\d+)', line)
    if m: d["foveation"] = int(m.group(1))
    m = re.search(r'App=([\d.]+)ms', line)
    if m: d["app_gpu_ms"] = float(m.group(1))
    m = re.search(r'GPU%=([\d.]+)', line)
    if m: d["gpu_util"] = float(m.group(1))
    m = re.search(r'CPU%=([\d.]+)', line)
    if m: d["cpu_util"] = float(m.group(1))
    m = re.search(r'Temp=([\d.]+)C', line)
    if m: d["soc_temp"] = float(m.group(1))
    m = re.search(r'Free=(\d+)MB', line)
    if m: d["free_mb"] = int(m.group(1))
    m = re.search(r'DSF=([\d.]+)', line)
    if m: d["dsf"] = float(m.group(1))
    m = re.search(r'TW=([\d.]+)ms', line)
    if m: d["timewarp_ms"] = float(m.group(1))
    return d if d else None


MAX_SAMPLES = 60


class ProfilerPage(Gtk.Box):
    """Full-page profiler with real-time charts."""

    def __init__(self):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=0)

        # ── Top controls ──
        top = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        top.set_margin_top(8)
        top.set_margin_start(12)
        top.set_margin_end(12)
        top.set_margin_bottom(4)

        top.append(Gtk.Label(label="Device:", css_classes=["dim-label"]))
        self._dev_dropdown = Gtk.DropDown.new_from_strings(["No devices"])
        self._dev_dropdown.set_selected(0)
        top.append(self._dev_dropdown)

        top.append(Gtk.Label(label="App:", css_classes=["dim-label"]))
        self._app_dropdown = Gtk.DropDown.new_from_strings(["Select app"])
        self._app_dropdown.set_selected(0)
        self._app_dropdown.set_hexpand(True)
        top.append(self._app_dropdown)

        refresh_btn = Gtk.Button(icon_name="view-refresh-symbolic", tooltip_text="Refresh")
        refresh_btn.connect("clicked", lambda _: self._refresh_devices())
        top.append(refresh_btn)

        self._start_btn = Gtk.Button(label="Start", css_classes=["suggested-action"])
        self._start_btn.connect("clicked", self._on_toggle)
        top.append(self._start_btn)

        self.append(top)

        # ── VrApi info bar (Quest-specific, shown when data available) ──
        self._vrapi_bar = Gtk.Label(label="", xalign=0, wrap=True,
                                     css_classes=["caption", "monospace"])
        self._vrapi_bar.set_margin_start(12)
        self._vrapi_bar.set_margin_end(12)
        self._vrapi_bar.set_visible(False)
        self.append(self._vrapi_bar)

        # ── Status ──
        self._status = Gtk.Label(label="Select device and app, then press Start",
                                 xalign=0, css_classes=["dim-label", "caption"])
        self._status.set_margin_start(12)
        self._status.set_margin_bottom(4)
        self.append(self._status)

        # ── Charts ──
        scroll = Gtk.ScrolledWindow(vexpand=True)
        charts_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        charts_box.set_margin_start(12)
        charts_box.set_margin_end(12)
        charts_box.set_margin_bottom(12)

        self._charts = {}
        chart_defs = [
            ("fps", "FPS", "#62a0ea"),
            ("frame_ms", "Frame Time (ms)", "#e5a50a"),
            ("gpu", "GPU %", "#c061cb"),
            ("ram", "RAM (MB)", "#2ec27e"),
            ("cpu", "CPU %", "#e01b24"),
            ("thermal", "Temperature", "#ff7800"),
        ]

        for chart_id, title, color in chart_defs:
            frame = Gtk.Frame()
            frame.add_css_class("card")
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)

            lbl_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            lbl_box.set_margin_start(10)
            lbl_box.set_margin_top(6)
            lbl_box.set_margin_end(10)
            title_lbl = Gtk.Label(label=title, xalign=0, css_classes=["caption"])
            title_lbl.set_hexpand(True)
            lbl_box.append(title_lbl)
            value_lbl = Gtk.Label(label="\u2014", xalign=1, css_classes=["caption", "monospace"])
            lbl_box.append(value_lbl)
            box.append(lbl_box)

            area = Gtk.DrawingArea()
            area.set_content_height(80)
            area.set_margin_start(4)
            area.set_margin_end(4)
            area.set_margin_bottom(4)
            box.append(area)

            frame.set_child(box)
            charts_box.append(frame)

            self._charts[chart_id] = {
                "area": area, "value_lbl": value_lbl,
                "color": color, "data": deque(maxlen=MAX_SAMPLES),
            }

        # Stats summary
        self._stats_label = Gtk.Label(label="", xalign=0, wrap=True,
                                       css_classes=["caption", "monospace"])
        self._stats_label.set_margin_top(4)
        charts_box.append(self._stats_label)

        scroll.set_child(charts_box)
        self.append(scroll)

        # State
        self._running = False
        self._timer_id = None
        self._devices_list = []
        self._packages_list = []
        self._selected_dev_id = None
        self._selected_pkg = None
        self._initialized = False
        self._vrapi_proc = None
        self._vrapi_latest = None

        self._dev_dropdown.connect("notify::selected", self._on_device_changed)

    def refresh(self):
        """Called when page becomes visible."""
        if not self._initialized:
            self._initialized = True
            self._refresh_devices()
        # Don't refresh if already running — would reset selection

    def _refresh_devices(self):
        # Save current selection
        self._save_selection()
        def do_scan():
            devs = _parse_devices_simple()
            GLib.idle_add(self._update_devices, devs)
        threading.Thread(target=do_scan, daemon=True).start()

    def _save_selection(self):
        dev_idx = self._dev_dropdown.get_selected()
        if dev_idx < len(self._devices_list):
            self._selected_dev_id = self._devices_list[dev_idx]["id"]
        app_idx = self._app_dropdown.get_selected()
        if app_idx < len(self._packages_list):
            self._selected_pkg = self._packages_list[app_idx]

    def _update_devices(self, devices):
        self._devices_list = devices
        items = [f"{d['model']} ({d['id'][:12]})" for d in devices] or ["No devices"]
        self._dev_dropdown.set_model(Gtk.StringList.new(items))

        # Restore selection
        sel = 0
        if self._selected_dev_id:
            for i, d in enumerate(devices):
                if d["id"] == self._selected_dev_id:
                    sel = i
                    break
        if devices:
            self._dev_dropdown.set_selected(sel)
            self._on_device_changed()

    def _on_device_changed(self, *_):
        idx = self._dev_dropdown.get_selected()
        if idx >= len(self._devices_list):
            return
        dev = self._devices_list[idx]
        self._status.set_text(f"Loading packages for {dev['model']}...")
        def do_load():
            pkgs = _get_packages(dev["id"])
            # Get running third-party apps
            running = set()
            out = _adb_quick("shell", "ps", "-A", "-o", "NAME", device=dev["id"])
            if out:
                running = set(l.strip() for l in out.splitlines()
                              if "." in l.strip() and l.strip().count(".") >= 2)
            running_pkgs = [p for p in pkgs if p in running]
            GLib.idle_add(self._update_packages, pkgs, running_pkgs)
        threading.Thread(target=do_load, daemon=True).start()

    def _update_packages(self, pkgs, running_pkgs=None):
        self._packages_list = pkgs
        items = pkgs or ["No apps"]
        self._app_dropdown.set_model(Gtk.StringList.new(items))

        # Restore previous selection, or pick first running app
        sel = 0
        if self._selected_pkg and self._selected_pkg in pkgs:
            sel = pkgs.index(self._selected_pkg)
        elif running_pkgs:
            sel = pkgs.index(running_pkgs[0])
        if pkgs:
            self._app_dropdown.set_selected(sel)
        self._status.set_text("Ready")

    def _on_toggle(self, _):
        if self._running:
            self._stop()
        else:
            self._start()

    def _start(self):
        dev_idx = self._dev_dropdown.get_selected()
        app_idx = self._app_dropdown.get_selected()
        if dev_idx >= len(self._devices_list) or app_idx >= len(self._packages_list):
            self._status.set_text("Select device and app first")
            return

        self._running = True
        self._start_btn.set_label("Stop")
        self._start_btn.remove_css_class("suggested-action")
        self._start_btn.add_css_class("destructive-action")

        for ch in self._charts.values():
            ch["data"].clear()

        dev_id = self._devices_list[dev_idx]["id"]
        pkg = self._packages_list[app_idx]
        self._selected_dev_id = dev_id
        self._selected_pkg = pkg
        _adb_quick("shell", "dumpsys", "gfxinfo", pkg, "reset", device=dev_id)

        self._poll_count = 0
        self._fps_zero_count = 0
        self._status.set_text(f"Profiling {pkg}...")

        # Start persistent VrApi logcat stream
        self._start_vrapi_stream(dev_id)

        self._timer_id = GLib.timeout_add(2000, self._poll)

    def _stop(self):
        self._running = False
        self._stop_vrapi_stream()
        if self._timer_id:
            GLib.source_remove(self._timer_id)
            self._timer_id = None
        self._start_btn.set_label("Start")
        self._start_btn.remove_css_class("destructive-action")
        self._start_btn.add_css_class("suggested-action")
        self._status.set_text("Stopped")

    def _start_vrapi_stream(self, dev_id):
        """Start background logcat -s VrApi stream, cache latest parsed line."""
        self._stop_vrapi_stream()
        self._vrapi_latest = None
        def reader():
            try:
                # Clear old logcat first
                subprocess.run(["adb", "-s", dev_id, "logcat", "-c", "-b", "main"],
                               capture_output=True, timeout=3)
                proc = subprocess.Popen(
                    ["adb", "-s", dev_id, "logcat", "-s", "VrApi:V"],
                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                    text=True, bufsize=1)
                self._vrapi_proc = proc
                for line in proc.stdout:
                    if self._vrapi_proc is None:
                        break
                    if "FPS=" in line:
                        parsed = _parse_vrapi_line(line)
                        if parsed:
                            self._vrapi_latest = parsed
            except: pass
        threading.Thread(target=reader, daemon=True).start()

    def _stop_vrapi_stream(self):
        proc = self._vrapi_proc
        self._vrapi_proc = None
        if proc:
            try: proc.kill()
            except: pass

    def _poll(self):
        if not self._running:
            return False

        dev_idx = self._dev_dropdown.get_selected()
        app_idx = self._app_dropdown.get_selected()
        if dev_idx >= len(self._devices_list) or app_idx >= len(self._packages_list):
            return False

        dev_id = self._devices_list[dev_idx]["id"]
        pkg = self._packages_list[app_idx]

        self._poll_count += 1

        def do_collect():
            fps = _collect_fps(dev_id, pkg)
            # Reset gfxinfo after reading so next poll gets only new frames
            _adb_quick("shell", "dumpsys", "gfxinfo", pkg, "reset", device=dev_id)
            mem = _collect_mem(dev_id, pkg)
            cpu = _collect_cpu(dev_id, pkg)
            gpu = _collect_gpu(dev_id, pkg)
            thermal = _collect_thermal(dev_id)
            battery = _collect_battery(dev_id)
            vrapi = self._vrapi_latest
            has_data = any([fps and fps["total"] > 0, mem and mem["pss_mb"] > 0, vrapi, gpu])
            GLib.idle_add(self._update_charts, fps, mem, cpu, gpu, thermal, battery, vrapi)
            if not has_data:
                GLib.idle_add(self._status.set_text,
                    f"Profiling {pkg} (#{self._poll_count}) — app may not be running or visible")

        threading.Thread(target=do_collect, daemon=True).start()
        return True

    def _update_charts(self, fps, mem, cpu, gpu, thermal, battery, vrapi):
        if not self._running:
            return

        # VrApi overrides FPS/GPU if available (more accurate on Quest)
        if vrapi:
            if "vr_fps" in vrapi:
                self._charts["fps"]["data"].append(vrapi["vr_fps"])
                self._charts["fps"]["value_lbl"].set_text(
                    f"{vrapi['vr_fps']}/{vrapi.get('vr_target_fps', '?')}")
                self._fps_zero_count = 0
            if "app_gpu_ms" in vrapi:
                self._charts["frame_ms"]["data"].append(vrapi["app_gpu_ms"])
                self._charts["frame_ms"]["value_lbl"].set_text(f"{vrapi['app_gpu_ms']:.1f} ms")
            if "gpu_util" in vrapi:
                self._charts["gpu"]["data"].append(vrapi["gpu_util"] * 100)
                self._charts["gpu"]["value_lbl"].set_text(f"{vrapi['gpu_util']*100:.0f}%")

            # VrApi info bar
            parts = []
            if "cpu_level" in vrapi:
                parts.append(f"CPU Lv:{vrapi['cpu_level']}")
            if "gpu_level" in vrapi:
                parts.append(f"GPU Lv:{vrapi['gpu_level']}")
            if "cpu_mhz" in vrapi:
                parts.append(f"{vrapi['cpu_mhz']}/{vrapi.get('gpu_mhz', '?')}MHz")
            if "stale" in vrapi:
                parts.append(f"Stale:{vrapi['stale']}")
            if "foveation" in vrapi:
                parts.append(f"FFR:{vrapi['foveation']}")
            if "dsf" in vrapi:
                parts.append(f"DSF:{vrapi['dsf']:.2f}")
            if "timewarp_ms" in vrapi:
                parts.append(f"TW:{vrapi['timewarp_ms']:.1f}ms")
            if "free_mb" in vrapi:
                parts.append(f"Free:{vrapi['free_mb']}MB")
            if "soc_temp" in vrapi:
                parts.append(f"SoC:{vrapi['soc_temp']:.1f}\u00b0C")
            if parts:
                self._vrapi_bar.set_text("  ".join(parts))
                self._vrapi_bar.set_visible(True)
            else:
                self._vrapi_bar.set_visible(False)
        else:
            self._vrapi_bar.set_visible(False)
            # Standard Android FPS/GPU
            if fps and fps["total"] > 0:
                self._charts["fps"]["data"].append(fps["fps"])
                self._charts["fps"]["value_lbl"].set_text(f"{fps['fps']:.0f}")
                self._charts["frame_ms"]["data"].append(fps["avg_ms"])
                self._charts["frame_ms"]["value_lbl"].set_text(f"{fps['avg_ms']:.1f} ms")
                self._fps_zero_count = 0
            elif fps:
                self._fps_zero_count += 1
                if self._fps_zero_count >= 3:
                    self._charts["fps"]["value_lbl"].set_text("N/A (VR)")
                    self._charts["frame_ms"]["value_lbl"].set_text("N/A (VR)")
            if gpu:
                pct = gpu.get("gpu_pct", 0)
                self._charts["gpu"]["data"].append(pct)
                freq = gpu.get("freq_mhz", 0)
                self._charts["gpu"]["value_lbl"].set_text(
                    f"{pct}%  {freq}MHz" if freq else f"{pct}%")

        # RAM
        if mem:
            self._charts["ram"]["data"].append(mem["pss_mb"])
            self._charts["ram"]["value_lbl"].set_text(f"{mem['pss_mb']:.0f}")

        # CPU
        if cpu:
            self._charts["cpu"]["data"].append(cpu["cpu_pct"])
            self._charts["cpu"]["value_lbl"].set_text(f"{cpu['cpu_pct']:.0f}%")

        # Thermal (prefer VrApi SoC temp if available)
        if vrapi and "soc_temp" in vrapi:
            self._charts["thermal"]["data"].append(vrapi["soc_temp"])
            self._charts["thermal"]["value_lbl"].set_text(f"{vrapi['soc_temp']:.1f}\u00b0C")
        elif thermal:
            self._charts["thermal"]["data"].append(thermal["max_temp"])
            self._charts["thermal"]["value_lbl"].set_text(f"{thermal['max_temp']:.1f}\u00b0C")

        # Battery (text only, in stats)

        # Stats summary
        parts = []
        if vrapi:
            if "vr_fps" in vrapi:
                parts.append(f"FPS:{vrapi['vr_fps']}/{vrapi.get('vr_target_fps','?')}")
            if "stale" in vrapi:
                parts.append(f"Stale:{vrapi['stale']}")
            if "app_gpu_ms" in vrapi:
                parts.append(f"App:{vrapi['app_gpu_ms']:.1f}ms")
            if "gpu_util" in vrapi:
                parts.append(f"GPU:{vrapi['gpu_util']*100:.0f}%")
            if "cpu_util" in vrapi:
                parts.append(f"CPU:{vrapi['cpu_util']*100:.0f}%")
        else:
            if fps:
                parts.append(f"FPS:{fps['fps']:.0f} Janky:{fps['janky']}")
            if gpu:
                parts.append(f"GPU:{gpu.get('gpu_pct', 0)}%")
            if cpu:
                parts.append(f"CPU:{cpu['cpu_pct']:.0f}%")
        if mem:
            parts.append(f"RAM:{mem['pss_mb']:.0f}MB")
        if battery:
            parts.append(f"Battery:{battery['level']}% {battery['temp']:.1f}\u00b0C")
        self._stats_label.set_text("  |  ".join(parts))

        # Redraw charts
        for ch in self._charts.values():
            ch["area"].set_draw_func(
                lambda area, cr, w, h, d=list(ch["data"]), col=ch["color"]: _draw_line_chart(cr, w, h, d, col))
            ch["area"].queue_draw()


def _draw_line_chart(cr, w, h, data, color_hex):
    if len(data) < 2:
        return

    r = int(color_hex[1:3], 16) / 255
    g = int(color_hex[3:5], 16) / 255
    b = int(color_hex[5:7], 16) / 255

    pad = 4
    cw = w - pad * 2
    ch = h - pad * 2

    min_v = min(data)
    max_v = max(data)
    val_range = max(max_v - min_v, 0.1)

    def x_for(i):
        return pad + (i / max(len(data) - 1, 1)) * cw

    def y_for(v):
        return pad + (1 - (v - min_v) / val_range) * ch

    points = [(x_for(i), y_for(v)) for i, v in enumerate(data)]

    # Fill
    cr.move_to(points[0][0], pad + ch)
    cr.line_to(*points[0])
    for i in range(len(points) - 1):
        x0, y0 = points[i]
        x1, y1 = points[i + 1]
        mx = (x0 + x1) / 2
        cr.curve_to(mx, y0, mx, y1, x1, y1)
    cr.line_to(points[-1][0], pad + ch)
    cr.close_path()
    cr.set_source_rgba(r, g, b, 0.12)
    cr.fill()

    # Line
    cr.move_to(*points[0])
    for i in range(len(points) - 1):
        x0, y0 = points[i]
        x1, y1 = points[i + 1]
        mx = (x0 + x1) / 2
        cr.curve_to(mx, y0, mx, y1, x1, y1)
    cr.set_source_rgba(r, g, b, 0.9)
    cr.set_line_width(2)
    cr.stroke()

    # Last point dot
    if points:
        lx, ly = points[-1]
        cr.arc(lx, ly, 3, 0, math.tau)
        cr.set_source_rgba(r, g, b, 1)
        cr.fill()

    # Min/max labels
    cr.set_font_size(9)
    cr.set_source_rgba(1, 1, 1, 0.4)
    cr.move_to(pad + 2, pad + 10)
    cr.show_text(f"{max_v:.0f}")
    cr.move_to(pad + 2, pad + ch - 2)
    cr.show_text(f"{min_v:.0f}")