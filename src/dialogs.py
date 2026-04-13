"""History and scan dialogs."""
import math, os, subprocess, glob
from gi.repository import Gtk, Adw
from .config import load_builds_log, scan_project, APP_DIR


def show_history(parent):
    builds = load_builds_log()
    dlg = Adw.Dialog()
    dlg.set_title("Build History")
    dlg.set_content_width(650)
    dlg.set_content_height(650)

    tb = Adw.ToolbarView()
    header = Adw.HeaderBar()
    tb.add_top_bar(header)

    if not builds:
        tb.set_content(Adw.StatusPage(title="No builds yet"))
        dlg.set_child(tb)
        dlg.present(parent)
        return

    content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)

    # ── Filters ──
    projects = sorted(set(b.get("project", "") for b in builds))
    filter_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8,
                         halign=Gtk.Align.CENTER)
    filter_box.set_margin_top(8)
    filter_box.set_margin_bottom(4)

    state = {"project": None, "success_only": False}
    chart_area = [None]
    list_box = [None]

    proj_dropdown = Gtk.DropDown.new_from_strings(["All"] + projects)
    proj_dropdown.set_selected(0)
    filter_box.append(proj_dropdown)

    success_toggle = Gtk.CheckButton(label="Success only")
    filter_box.append(success_toggle)

    # Chart Y: Duration / Size / Both
    chart_mode = Gtk.DropDown.new_from_strings(["Duration + Size", "Duration", "Size"])
    chart_mode.set_selected(0)
    state["chart_mode"] = 0
    filter_box.append(chart_mode)

    # Chart X: Build / Time
    x_mode = Gtk.DropDown.new_from_strings(["Build #", "Time"])
    x_mode.set_selected(0)
    state["x_mode"] = 0
    filter_box.append(x_mode)

    content.append(filter_box)

    # ── Chart ──
    chart = Gtk.DrawingArea()
    chart.set_content_height(180)
    chart.set_margin_start(16)
    chart.set_margin_end(16)
    chart_area[0] = chart
    content.append(chart)

    # ── List ──
    scroll = Gtk.ScrolledWindow(vexpand=True)
    list_container = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
    list_box[0] = list_container
    scroll.set_child(list_container)
    content.append(scroll)

    def get_filtered():
        filtered = builds
        if state["project"]:
            filtered = [b for b in filtered if b.get("project") == state["project"]]
        if state["success_only"]:
            filtered = [b for b in filtered if b.get("success")]
        return filtered

    def refresh():
        filtered = get_filtered()
        # Update chart
        mode = state["chart_mode"]
        xm = state["x_mode"]
        chart_area[0].set_draw_func(
            lambda area, cr, w, h, m=mode, x=xm: _draw_chart(cr, w, h, filtered, m, x))
        chart_area[0].queue_draw()
        # Update list
        while (c := list_box[0].get_first_child()):
            list_box[0].remove(c)
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
        list_box[0].append(page)

    def on_project_changed(*_):
        idx = proj_dropdown.get_selected()
        state["project"] = None if idx == 0 else projects[idx - 1]
        refresh()

    proj_dropdown.connect("notify::selected", on_project_changed)
    success_toggle.connect("toggled", lambda b: (
        state.__setitem__("success_only", b.get_active()), refresh()))
    def on_chart_mode(*_):
        state["chart_mode"] = chart_mode.get_selected()
        refresh()
    chart_mode.connect("notify::selected", on_chart_mode)
    def on_x_mode(*_):
        state["x_mode"] = x_mode.get_selected()
        refresh()
    x_mode.connect("notify::selected", on_x_mode)

    refresh()

    tb.set_content(content)
    dlg.set_child(tb)
    dlg.present(parent)


def show_scan(parent, proj):
    issues, ok_items = scan_project(proj["path"])
    dlg = Adw.Dialog()
    dlg.set_title(f"{proj['name']} — Health Check")
    dlg.set_content_width(420)
    dlg.set_content_height(400)

    tb = Adw.ToolbarView()
    tb.add_top_bar(Adw.HeaderBar())
    page = Adw.PreferencesPage()

    if ok_items:
        grp = Adw.PreferencesGroup(title="OK")
        for text in ok_items:
            row = Adw.ActionRow(title=text)
            row.add_prefix(Gtk.Image.new_from_icon_name("object-select-symbolic"))
            grp.add(row)
        page.add(grp)

    if issues:
        grp = Adw.PreferencesGroup(title="Issues")
        icons = {"error": "dialog-error-symbolic", "warn": "dialog-warning-symbolic",
                 "info": "dialog-information-symbolic"}
        for sev, text in issues:
            row = Adw.ActionRow(title=text)
            row.add_prefix(Gtk.Image.new_from_icon_name(icons.get(sev, "dialog-information-symbolic")))
            grp.add(row)
        page.add(grp)

    if not issues and not ok_items:
        page.add(Adw.StatusPage(title="Nothing to report"))

    tb.set_content(page)
    dlg.set_child(tb)
    dlg.present(parent)


def _find_log(build_entry):
    """Find log file matching a build entry by project name and date."""
    logs_dir = os.path.join(APP_DIR, "logs")
    if not os.path.isdir(logs_dir):
        return None
    name = build_entry.get("project", "")
    date = build_entry.get("date", "")  # "2026-04-13 21:58"
    if not name or not date:
        return None
    # Convert "2026-04-13 21:58" → "20260413_2158"
    ts = date.replace("-", "").replace(" ", "_").replace(":", "")
    # Find closest match
    pattern = os.path.join(logs_dir, f"{name}_{ts}*.log")
    matches = sorted(glob.glob(pattern))
    if matches:
        return matches[-1]
    # Fallback: find by prefix + date part
    prefix = os.path.join(logs_dir, f"{name}_{ts[:8]}")
    matches = sorted(glob.glob(prefix + "*.log"))
    if matches:
        # Find closest by timestamp
        for m in reversed(matches):
            return m
    return None


# ── Chart ──

def _draw_smooth_line(cr, points):
    """Draw smooth line through points using monotone cubic interpolation."""
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
    """Draw filled area under smooth line."""
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
    """Draw chart. mode: 0=both, 1=duration, 2=size. x_mode: 0=build#, 1=date."""
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

    # Background
    cr.set_source_rgba(0.15, 0.15, 0.18, 1)
    cr.rectangle(0, 0, w, h)
    cr.fill()

    # Grid
    show_size = mode != 1
    show_dur = mode != 2

    cr.set_line_width(0.5)
    for i in range(5):
        y = pad_t + ch * i / 4
        cr.set_source_rgba(1, 1, 1, 0.06)
        cr.move_to(pad_l, y)
        cr.line_to(w - pad_r, y)
        cr.stroke()
        # Duration labels (left, blue)
        if show_dur:
            val = max_dur - dur_range * i / 4
            m, s = divmod(int(val), 60)
            cr.set_source_rgba(0.38, 0.63, 0.92, 0.6)
            cr.set_font_size(9)
            cr.move_to(2, y + 3)
            cr.show_text(f"{m}:{s:02d}")
        # Size labels (right, orange)
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

    # --- Size curve (orange, behind) ---
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

    # --- Duration curve (blue, front) ---
    dur_xy = [(x_for(i), y_dur(d)) for i, d in enumerate(durations)]
    if show_dur:
        _draw_smooth_fill(cr, dur_xy, bottom)
        cr.set_source_rgba(0.38, 0.63, 0.92, 0.12)
        cr.fill()
        _draw_smooth_line(cr, dur_xy)
        cr.set_source_rgba(0.38, 0.63, 0.92, 0.9)
        cr.set_line_width(2)
        cr.stroke()

        # Dots — green=success, red=failed
        for i, (x, y) in enumerate(dur_xy):
            if data[i].get("success"):
                cr.set_source_rgba(0.18, 0.76, 0.49, 1)
            else:
                cr.set_source_rgba(0.88, 0.11, 0.14, 1)
            cr.arc(x, y, 4, 0, math.tau)
            cr.fill()

    # Bottom labels
    pts = dur_xy if show_dur else [(x_for(i), 0) for i in range(len(data))]
    cr.set_source_rgba(1, 1, 1, 0.4)
    cr.set_font_size(9)
    step = max(1, len(data) // 6)
    for i in range(0, len(data), step):
        cr.move_to(pts[i][0] - 8, h - 5)
        if x_mode == 1:
            # Time: "2026-04-13 21:58" → "21:58"
            date = data[i].get("date", "")
            cr.show_text(date[11:16] if len(date) > 14 else date[-5:])
        else:
            cr.show_text(str(data[i].get("build", "")))
