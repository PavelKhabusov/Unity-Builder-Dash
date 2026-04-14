"""History page — build history as a full page instead of dialog."""
import math, os, subprocess, glob
from gi.repository import Gtk, Adw
from .config import load_builds_log, APP_DIR


class HistoryPage(Gtk.Box):
    """Full-page build history with chart and list."""

    def __init__(self):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=0)

        self._state = {"project": None, "success_only": False,
                       "chart_mode": 0, "x_mode": 0}

        # ── Filters ──
        filter_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8,
                             halign=Gtk.Align.CENTER)
        filter_box.set_margin_top(8)
        filter_box.set_margin_bottom(4)

        self._proj_dropdown = Gtk.DropDown.new_from_strings(["All"])
        self._proj_dropdown.set_selected(0)
        self._proj_dropdown.connect("notify::selected", self._on_project_changed)
        filter_box.append(self._proj_dropdown)

        self._success_toggle = Gtk.CheckButton(label="Success only")
        self._success_toggle.connect("toggled", self._on_success_toggled)
        filter_box.append(self._success_toggle)

        self._chart_mode = Gtk.DropDown.new_from_strings(
            ["Duration + Size", "Duration", "Size"])
        self._chart_mode.set_selected(0)
        self._chart_mode.connect("notify::selected", self._on_chart_mode)
        filter_box.append(self._chart_mode)

        self._x_mode = Gtk.DropDown.new_from_strings(["Build #", "Time"])
        self._x_mode.set_selected(0)
        self._x_mode.connect("notify::selected", self._on_x_mode)
        filter_box.append(self._x_mode)

        self.append(filter_box)

        # ── Chart ──
        self._chart = Gtk.DrawingArea()
        self._chart.set_content_height(180)
        self._chart.set_margin_start(16)
        self._chart.set_margin_end(16)
        self.append(self._chart)

        # ── List ──
        scroll = Gtk.ScrolledWindow(vexpand=True)
        self._list_container = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        scroll.set_child(self._list_container)
        self.append(scroll)

        self._projects = []
        self._builds = []

    def refresh(self):
        """Reload data and redraw."""
        self._builds = load_builds_log()
        self._projects = sorted(set(b.get("project", "") for b in self._builds))
        # Update dropdown
        items = ["All"] + self._projects
        self._proj_dropdown.set_model(Gtk.StringList.new(items))
        self._proj_dropdown.set_selected(0)
        self._state["project"] = None
        self._redraw()

    def _get_filtered(self):
        filtered = self._builds
        if self._state["project"]:
            filtered = [b for b in filtered if b.get("project") == self._state["project"]]
        if self._state["success_only"]:
            filtered = [b for b in filtered if b.get("success")]
        return filtered

    def _redraw(self):
        filtered = self._get_filtered()
        mode = self._state["chart_mode"]
        xm = self._state["x_mode"]
        self._chart.set_draw_func(
            lambda area, cr, w, h, m=mode, x=xm: _draw_chart(cr, w, h, filtered, m, x))
        self._chart.queue_draw()

        while (c := self._list_container.get_first_child()):
            self._list_container.remove(c)

        if not filtered:
            self._list_container.append(
                Adw.StatusPage(title="No builds yet", vexpand=True))
            return

        page = Adw.PreferencesPage()
        grp = Adw.PreferencesGroup(title=f"{len(filtered[-20:])} builds")
        for b in reversed(filtered[-20:]):
            icon = "object-select-symbolic" if b.get("success") else "dialog-error-symbolic"
            size = f"  {b['apk_size_mb']} MB" if b.get("apk_size_mb") else ""
            dm, ds = divmod(b.get("duration", 0), 60)
            row = Adw.ActionRow(
                title=f"{b['project']} — {b.get('target', '?')}",
                subtitle=f"{b.get('date', '?')}  {dm}:{ds:02d}{size}  build {b.get('build', '?')}"
            )
            row.add_prefix(Gtk.Image.new_from_icon_name(icon))
            log = _find_log(b)
            if log:
                log_btn = Gtk.Button(icon_name="document-open-symbolic",
                                     tooltip_text="Open log", valign=Gtk.Align.CENTER,
                                     css_classes=["flat"])
                log_btn.connect("clicked", lambda _, p=log: subprocess.Popen(["xdg-open", p]))
                row.add_suffix(log_btn)
            grp.add(row)
        page.add(grp)
        self._list_container.append(page)

    # ── Filter callbacks ──

    def _on_project_changed(self, *_):
        idx = self._proj_dropdown.get_selected()
        self._state["project"] = None if idx == 0 else self._projects[idx - 1]
        self._redraw()

    def _on_success_toggled(self, btn):
        self._state["success_only"] = btn.get_active()
        self._redraw()

    def _on_chart_mode(self, *_):
        self._state["chart_mode"] = self._chart_mode.get_selected()
        self._redraw()

    def _on_x_mode(self, *_):
        self._state["x_mode"] = self._x_mode.get_selected()
        self._redraw()


# ── Helpers (moved from dialogs.py) ──

def _find_log(build_entry):
    logs_dir = os.path.join(APP_DIR, "logs")
    if not os.path.isdir(logs_dir):
        return None
    name = build_entry.get("project", "")
    date = build_entry.get("date", "")
    if not name or not date:
        return None
    ts = date.replace("-", "").replace(" ", "_").replace(":", "")
    pattern = os.path.join(logs_dir, f"{name}_{ts}*.log")
    matches = sorted(glob.glob(pattern))
    if matches:
        return matches[-1]
    prefix = os.path.join(logs_dir, f"{name}_{ts[:8]}")
    matches = sorted(glob.glob(prefix + "*.log"))
    if matches:
        return matches[-1]
    return None


# ── Chart drawing (moved from dialogs.py) ──

def _draw_smooth_line(cr, points):
    if len(points) < 2:
        return
    cr.move_to(*points[0])
    if len(points) == 2:
        cr.line_to(*points[1])
        return
    for i in range(len(points) - 1):
        x0, y0 = points[i]
        x1, y1 = points[i + 1]
        mx = (x0 + x1) / 2
        cr.curve_to(mx, y0, mx, y1, x1, y1)


def _draw_smooth_fill(cr, points, baseline_y):
    if len(points) < 2:
        return
    cr.move_to(points[0][0], baseline_y)
    cr.line_to(*points[0])
    if len(points) == 2:
        cr.line_to(*points[1])
    else:
        for i in range(len(points) - 1):
            x0, y0 = points[i]
            x1, y1 = points[i + 1]
            mx = (x0 + x1) / 2
            cr.curve_to(mx, y0, mx, y1, x1, y1)
    cr.line_to(points[-1][0], baseline_y)
    cr.close_path()


def _draw_chart(cr, w, h, builds, mode=0, x_mode=0):
    data = [b for b in builds if b.get("duration", 0) > 0]
    if len(data) < 2:
        return
    data = data[-20:]

    pad_l, pad_r, pad_t, pad_b = 45, 45, 20, 25
    cw = w - pad_l - pad_r
    ch = h - pad_t - pad_b

    durations = [b["duration"] for b in data]
    sizes = [b.get("apk_size_mb") or 0 for b in data]
    max_dur = max(durations)
    dur_range = max(max_dur - min(durations), 30)
    pos_sizes = [s for s in sizes if s > 0]
    max_size = max(pos_sizes) if pos_sizes else 1
    size_range = max(max_size - min(pos_sizes), 10) if pos_sizes else 10
    size_base = max_size - size_range

    cr.set_source_rgba(0.15, 0.15, 0.18, 1)
    cr.rectangle(0, 0, w, h)
    cr.fill()

    show_size = mode != 1
    show_dur = mode != 2

    cr.set_line_width(0.5)
    for i in range(5):
        y = pad_t + ch * i / 4
        cr.set_source_rgba(1, 1, 1, 0.06)
        cr.move_to(pad_l, y)
        cr.line_to(w - pad_r, y)
        cr.stroke()
        if show_dur:
            val = max_dur - dur_range * i / 4
            m, s = divmod(int(val), 60)
            cr.set_source_rgba(0.38, 0.63, 0.92, 0.6)
            cr.set_font_size(9)
            cr.move_to(2, y + 3)
            cr.show_text(f"{m}:{s:02d}")
        if pos_sizes and show_size:
            sv = max_size - size_range * i / 4
            cr.set_source_rgba(0.92, 0.63, 0.18, 0.6)
            cr.move_to(w - pad_r + 4, y + 3)
            cr.show_text(f"{sv:.0f}M")

    def x_for(i):
        return pad_l + (i / max(len(data) - 1, 1)) * cw

    def y_dur(d):
        return pad_t + max(0, min(1, 1 - (d - (max_dur - dur_range)) / dur_range)) * ch

    def y_size(s):
        if size_range == 0: return pad_t + ch / 2
        return pad_t + max(0, min(1, 1 - (s - size_base) / size_range)) * ch

    bottom = pad_t + ch

    if pos_sizes and show_size:
        size_xy = [(x_for(i), y_size(s)) for i, s in enumerate(sizes) if s > 0]
        if len(size_xy) >= 2:
            _draw_smooth_fill(cr, size_xy, bottom)
            cr.set_source_rgba(0.92, 0.63, 0.18, 0.08)
            cr.fill()
            _draw_smooth_line(cr, size_xy)
            cr.set_source_rgba(0.92, 0.63, 0.18, 0.6)
            cr.set_line_width(1.5)
            cr.stroke()
            for x, y in size_xy:
                cr.set_source_rgba(0.92, 0.63, 0.18, 0.8)
                cr.arc(x, y, 3, 0, math.tau)
                cr.fill()

    dur_xy = [(x_for(i), y_dur(d)) for i, d in enumerate(durations)]
    if show_dur:
        _draw_smooth_fill(cr, dur_xy, bottom)
        cr.set_source_rgba(0.38, 0.63, 0.92, 0.12)
        cr.fill()
        _draw_smooth_line(cr, dur_xy)
        cr.set_source_rgba(0.38, 0.63, 0.92, 0.9)
        cr.set_line_width(2)
        cr.stroke()
        for i, (x, y) in enumerate(dur_xy):
            if data[i].get("success"):
                cr.set_source_rgba(0.18, 0.76, 0.49, 1)
            else:
                cr.set_source_rgba(0.88, 0.11, 0.14, 1)
            cr.arc(x, y, 4, 0, math.tau)
            cr.fill()

    pts = dur_xy if show_dur else [(x_for(i), 0) for i in range(len(data))]
    cr.set_source_rgba(1, 1, 1, 0.4)
    cr.set_font_size(9)
    step = max(1, len(data) // 6)
    for i in range(0, len(data), step):
        cr.move_to(pts[i][0] - 8, h - 5)
        if x_mode == 1:
            date = data[i].get("date", "")
            cr.show_text(date[11:16] if len(date) > 14 else date[-5:])
        else:
            cr.show_text(str(data[i].get("build", "")))