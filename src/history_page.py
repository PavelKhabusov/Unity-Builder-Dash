"""History page — build & test history with tabs, charts, log viewer."""
import math, os, subprocess, glob
from gi.repository import Gtk, Adw
from .config import load_builds_log, APP_DIR
from .log_view import LogView


class HistoryPage(Gtk.Box):
    """Tabbed history: Builds and Tests with charts and lists."""

    def __init__(self):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=0)

        self._state = {"project": None, "success_only": False, "x_mode": 0}

        # ── Tabs + Filters (one row) ──
        filter_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8,
                             halign=Gtk.Align.CENTER)
        filter_box.set_margin_top(8)
        filter_box.set_margin_bottom(4)

        tab_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        tab_box.add_css_class("linked")
        self._tab_builds = Gtk.ToggleButton(label="Builds")
        self._tab_tests = Gtk.ToggleButton(label="Tests")
        self._tab_builds.set_active(True)
        self._tab_tests.set_group(self._tab_builds)
        self._tab_builds.connect("toggled", lambda _: self._redraw())
        self._tab_tests.connect("toggled", lambda _: self._redraw())
        tab_box.append(self._tab_builds)
        tab_box.append(self._tab_tests)
        filter_box.append(tab_box)

        self._proj_dropdown = Gtk.DropDown.new_from_strings(["All"])
        self._proj_dropdown.set_selected(0)
        self._proj_dropdown.connect("notify::selected", self._on_project_changed)
        filter_box.append(self._proj_dropdown)

        self._success_toggle = Gtk.CheckButton(label="Success only")
        self._success_toggle.connect("toggled", self._on_success_toggled)
        filter_box.append(self._success_toggle)

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
        self._builds = load_builds_log()
        self._projects = sorted(set(b.get("project", "") for b in self._builds))
        items = ["All"] + self._projects
        self._proj_dropdown.set_model(Gtk.StringList.new(items))
        self._proj_dropdown.set_selected(0)
        self._state["project"] = None
        self._redraw()

    def _is_tests_tab(self):
        return self._tab_tests.get_active()

    def _get_filtered(self):
        is_test = self._is_tests_tab()
        filtered = self._builds
        if is_test:
            filtered = [b for b in filtered if b.get("type") == "test"]
        else:
            filtered = [b for b in filtered if b.get("type") != "test"]
        if self._state["project"]:
            filtered = [b for b in filtered if b.get("project") == self._state["project"]]
        if self._state["success_only"]:
            filtered = [b for b in filtered if b.get("success")]
        return filtered

    def _redraw(self):
        filtered = self._get_filtered()
        xm = self._state["x_mode"]
        is_test = self._is_tests_tab()

        if is_test:
            self._chart.set_draw_func(
                lambda area, cr, w, h: _draw_test_chart(cr, w, h, filtered, xm))
        else:
            self._chart.set_draw_func(
                lambda area, cr, w, h: _draw_build_chart(cr, w, h, filtered, xm))
        self._chart.queue_draw()

        while (c := self._list_container.get_first_child()):
            self._list_container.remove(c)

        if not filtered:
            label = "No tests yet" if is_test else "No builds yet"
            self._list_container.append(Adw.StatusPage(title=label, vexpand=True))
            return

        page = Adw.PreferencesPage()

        if is_test:
            grp = Adw.PreferencesGroup(title=f"Tests ({len(filtered[-20:])})")
            for t in reversed(filtered[-20:]):
                passed = t.get("passed", 0)
                failed = t.get("failed", 0)
                total = t.get("total", 0)
                skipped = t.get("skipped", 0)
                dm, ds = divmod(t.get("duration", 0), 60)
                icon = "object-select-symbolic" if t.get("success") else "dialog-error-symbolic"

                result = f"{passed}/{total} passed"
                if failed:
                    result += f", {failed} failed"
                if skipped:
                    result += f", {skipped} skipped"

                test_cases = t.get("test_cases", [])
                if test_cases:
                    # Expandable row with test case details
                    exp = Adw.ExpanderRow(
                        title=f"{t['project']} — {t.get('target', '?').replace('test-', '')}",
                        subtitle=f"{t.get('date', '?')}  {dm}:{ds:02d}  {result}")
                    exp.add_prefix(Gtk.Image.new_from_icon_name(icon))

                    # Failed tests first, then passed
                    failed_tc = [tc for tc in test_cases if tc["result"] == "Failed"]
                    passed_tc = [tc for tc in test_cases if tc["result"] == "Passed"]
                    skipped_tc = [tc for tc in test_cases if tc["result"] not in ("Failed", "Passed")]

                    for tc in failed_tc:
                        short = tc["name"].split(".")[-1] if "." in tc["name"] else tc["name"]
                        msg = tc.get("message", "")
                        r = Adw.ActionRow(title=short, subtitle=msg or tc["name"])
                        r.set_subtitle_selectable(True)
                        r.add_prefix(Gtk.Image.new_from_icon_name("dialog-error-symbolic"))
                        copy_text = f"{tc['name']}\n{msg}" if msg else tc["name"]
                        cp_btn = Gtk.Button(icon_name="edit-copy-symbolic",
                                            tooltip_text="Copy", valign=Gtk.Align.CENTER,
                                            css_classes=["flat"])
                        cp_btn.connect("clicked", lambda _, t=copy_text: _copy_to_clipboard(self, t))
                        r.add_suffix(cp_btn)
                        exp.add_row(r)

                    for tc in passed_tc:
                        short = tc["name"].split(".")[-1] if "." in tc["name"] else tc["name"]
                        r = Adw.ActionRow(title=short, subtitle=tc["name"])
                        r.add_prefix(Gtk.Image.new_from_icon_name("object-select-symbolic"))
                        exp.add_row(r)

                    for tc in skipped_tc:
                        short = tc["name"].split(".")[-1] if "." in tc["name"] else tc["name"]
                        r = Adw.ActionRow(title=short, subtitle=f"{tc['result']} — {tc['name']}")
                        r.add_prefix(Gtk.Image.new_from_icon_name("dialog-warning-symbolic"))
                        exp.add_row(r)

                    log = _find_log(t)
                    if log:
                        log_btn = Gtk.Button(icon_name="document-open-symbolic",
                                             tooltip_text="Open log", valign=Gtk.Align.CENTER,
                                             css_classes=["flat"])
                        log_btn.connect("clicked", lambda _, p=log: _open_log_viewer(self, p))
                        exp.add_suffix(log_btn)
                    grp.add(exp)
                else:
                    row = Adw.ActionRow(
                        title=f"{t['project']} — {t.get('target', '?').replace('test-', '')}",
                        subtitle=f"{t.get('date', '?')}  {dm}:{ds:02d}  {result}")
                    row.add_prefix(Gtk.Image.new_from_icon_name(icon))
                    log = _find_log(t)
                    if log:
                        log_btn = Gtk.Button(icon_name="document-open-symbolic",
                                             tooltip_text="Open log", valign=Gtk.Align.CENTER,
                                             css_classes=["flat"])
                        log_btn.connect("clicked", lambda _, p=log: _open_log_viewer(self, p))
                        row.add_suffix(log_btn)
                    grp.add(row)
            page.add(grp)
        else:
            grp = Adw.PreferencesGroup(title=f"Builds ({len(filtered[-20:])})")
            for b in reversed(filtered[-20:]):
                icon = "object-select-symbolic" if b.get("success") else "dialog-error-symbolic"
                size = f"  {b['apk_size_mb']} MB" if b.get("apk_size_mb") else ""
                dm, ds = divmod(b.get("duration", 0), 60)
                row = Adw.ActionRow(
                    title=f"{b['project']} — {b.get('target', '?')}",
                    subtitle=f"{b.get('date', '?')}  {dm}:{ds:02d}{size}  build {b.get('build', '?')}")
                row.add_prefix(Gtk.Image.new_from_icon_name(icon))
                log = _find_log(b)
                if log:
                    log_btn = Gtk.Button(icon_name="document-open-symbolic",
                                         tooltip_text="Open log", valign=Gtk.Align.CENTER,
                                         css_classes=["flat"])
                    log_btn.connect("clicked", lambda _, p=log: _open_log_viewer(self, p))
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

    def _on_x_mode(self, *_):
        self._state["x_mode"] = self._x_mode.get_selected()
        self._redraw()


# ── Helpers ──

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


# ── Chart drawing ──

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


def _draw_build_chart(cr, w, h, builds, x_mode=0):
    """Draw build duration + APK size chart."""
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

    cr.set_line_width(0.5)
    for i in range(5):
        y = pad_t + ch * i / 4
        cr.set_source_rgba(1, 1, 1, 0.06)
        cr.move_to(pad_l, y)
        cr.line_to(w - pad_r, y)
        cr.stroke()
        val = max_dur - dur_range * i / 4
        m, s = divmod(int(val), 60)
        cr.set_source_rgba(0.38, 0.63, 0.92, 0.6)
        cr.set_font_size(9)
        cr.move_to(2, y + 3)
        cr.show_text(f"{m}:{s:02d}")
        if pos_sizes:
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

    # Size curve (orange)
    if pos_sizes:
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

    # Duration curve (blue)
    dur_xy = [(x_for(i), y_dur(d)) for i, d in enumerate(durations)]
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

    # Bottom labels
    cr.set_source_rgba(1, 1, 1, 0.4)
    cr.set_font_size(9)
    step = max(1, len(data) // 6)
    for i in range(0, len(data), step):
        cr.move_to(dur_xy[i][0] - 8, h - 5)
        if x_mode == 1:
            date = data[i].get("date", "")
            cr.show_text(date[11:16] if len(date) > 14 else date[-5:])
        else:
            cr.show_text(str(data[i].get("build", "")))


def _draw_test_chart(cr, w, h, builds, x_mode=0):
    """Draw test results: green=passed, red=failed stacked bars."""
    data = [b for b in builds if b.get("total", 0) > 0]
    if not data:
        return
    data = data[-20:]

    pad_l, pad_r, pad_t, pad_b = 45, 15, 20, 25
    cw = w - pad_l - pad_r
    ch = h - pad_t - pad_b

    max_total = max(b.get("total", 1) for b in data)

    cr.set_source_rgba(0.15, 0.15, 0.18, 1)
    cr.rectangle(0, 0, w, h)
    cr.fill()

    cr.set_line_width(0.5)
    for i in range(5):
        y = pad_t + ch * i / 4
        cr.set_source_rgba(1, 1, 1, 0.06)
        cr.move_to(pad_l, y)
        cr.line_to(w - pad_r, y)
        cr.stroke()
        val = max_total - max_total * i / 4
        cr.set_source_rgba(1, 1, 1, 0.4)
        cr.set_font_size(9)
        cr.move_to(2, y + 3)
        cr.show_text(f"{val:.0f}")

    bar_w = max(4, cw / len(data) * 0.6)
    gap = cw / max(len(data), 1)

    for i, b in enumerate(data):
        x = pad_l + i * gap + gap / 2 - bar_w / 2
        passed = b.get("passed", 0)
        failed = b.get("failed", 0)

        ph = (passed / max_total) * ch if max_total else 0
        cr.set_source_rgba(0.18, 0.76, 0.49, 0.9)
        cr.rectangle(x, pad_t + ch - ph, bar_w, ph)
        cr.fill()

        fh = (failed / max_total) * ch if max_total else 0
        if fh > 0:
            cr.set_source_rgba(0.88, 0.11, 0.14, 0.9)
            cr.rectangle(x, pad_t + ch - ph - fh, bar_w, fh)
            cr.fill()

        cr.set_source_rgba(1, 1, 1, 0.4)
        cr.set_font_size(9)
        if x_mode == 1:
            date = b.get("date", "")
            label = date[11:16] if len(date) > 14 else date[-5:]
        else:
            label = str(i + 1)
        cr.move_to(x, h - 5)
        cr.show_text(label)

def _open_log_viewer(parent, path):
    """Open a log file in a built-in viewer dialog with search, filter, copy."""
    try:
        with open(path, errors="replace") as f:
            content = f.read()
    except Exception as e:
        return

    name = os.path.basename(path)
    dlg = Adw.Dialog()
    dlg.set_title(name)
    dlg.set_content_width(800)
    dlg.set_content_height(600)

    tb = Adw.ToolbarView()
    tb.add_top_bar(Adw.HeaderBar())

    def get_log_tag(line):
        sl = line.strip().lower()
        if "error" in sl or "failed" in sl or "exception" in sl:
            return "error"
        if "warning" in sl:
            return "warning"
        if "[build] ok" in sl or "done!" in sl or "passed" in sl.split(":")[0]:
            return "success"
        if line.strip().startswith("[Stage]") or "stage" in sl[:20]:
            return "stage"
        return None

    lv = LogView(levels=["All", "Errors", "Warnings"], get_tag=get_log_tag, margin=8)

    # Load content
    for line in content.splitlines(keepends=True):
        lv.append_line(line)

    tb.set_content(lv)
    dlg.set_child(tb)
    dlg.present(parent.get_root())


def _copy_to_clipboard(widget, text):
    display = widget.get_display()
    if display:
        from gi.repository import Gdk
        display.get_clipboard().set(text)
