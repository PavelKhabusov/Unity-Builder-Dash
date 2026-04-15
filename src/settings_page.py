"""Settings page — edit config, manage projects (full page instead of dialog)."""
import os, subprocess
from gi.repository import Gtk, Adw, Gdk
from .constants import APP_NAME, APP_GITHUB, APK_DASH_GITHUB
from .config import save_config, find_unity, find_apk_dash, list_unity_versions


class SettingsPage(Gtk.Box):
    """Full-page settings with save button in header."""

    def __init__(self, cfg, on_save):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.cfg = cfg
        self.on_save = on_save
        self.proj_rows = []

        # Save button (exposed for header)
        self.save_btn = Gtk.Button(label="Save")
        self.save_btn.add_css_class("suggested-action")
        self.save_btn.connect("clicked", self._save)

        scroll = Gtk.ScrolledWindow(vexpand=True)
        page = Adw.PreferencesPage()

        # ── Projects ──
        self.proj_grp = Adw.PreferencesGroup(title="Projects")
        add_btn = Gtk.Button(icon_name="list-add-symbolic", valign=Gtk.Align.CENTER)
        add_btn.add_css_class("flat")
        add_btn.connect("clicked", self._add_project)
        self.proj_grp.set_header_suffix(add_btn)

        for p in cfg.get("projects", []):
            self._add_project_row(p)
        page.add(self.proj_grp)

        # ── General ──
        grp = Adw.PreferencesGroup(title="General")
        self.unity_row = Adw.EntryRow(title="Unity Editor (default)")
        self.unity_row.set_text(cfg.get("unity", ""))
        grp.add(self.unity_row)

        self.dash_row = Adw.EntryRow(title="APK Dash")
        self.dash_row.set_text(cfg.get("apk_dash", ""))
        grp.add(self.dash_row)

        detect = Adw.ActionRow(title="Auto-detect paths",
                               subtitle="Find Unity and APK Dash automatically")
        detect_btn = Gtk.Button(icon_name="emblem-synchronizing-symbolic",
                                valign=Gtk.Align.CENTER)
        detect_btn.add_css_class("flat")
        detect_btn.connect("clicked", self._auto_detect)
        detect.add_suffix(detect_btn)
        detect.set_activatable_widget(detect_btn)
        grp.add(detect)

        # Theme
        self.theme_row = Adw.ComboRow(title="Theme")
        self.theme_row.set_model(Gtk.StringList.new(["System", "Dark", "Light"]))
        theme_idx = {"system": 0, "dark": 1, "light": 2}
        self.theme_row.set_selected(theme_idx.get(cfg.get("theme", "system"), 0))
        self.theme_row.connect("notify::selected", self._on_theme_preview)
        grp.add(self.theme_row)
        page.add(grp)

        # ── Log Filters ──
        filter_grp = Adw.PreferencesGroup(title="Log Filters",
            description="Lines containing these strings will be hidden from build/test logs (with their stack traces)")
        add_filter_btn = Gtk.Button(icon_name="list-add-symbolic", valign=Gtk.Align.CENTER)
        add_filter_btn.add_css_class("flat")
        add_filter_btn.connect("clicked", self._add_filter_row)
        filter_grp.set_header_suffix(add_filter_btn)

        self._filter_grp = filter_grp
        self._filter_rows = []
        for f in cfg.get("log_filters", []):
            self._add_filter_row(None, f)
        page.add(filter_grp)

        # ── About ──
        about_grp = Adw.PreferencesGroup(title="About")

        # App info with icon
        icon_path = os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "icons", "ubd-app-icon.png")
        app_row = Adw.ActionRow(title=APP_NAME,
            subtitle="MIT License — 2026")
        if os.path.isfile(icon_path):
            icon_img = Gtk.Image.new_from_paintable(
                Gdk.Texture.new_from_filename(icon_path))
            icon_img.set_pixel_size(32)
            app_row.add_prefix(icon_img)
        about_grp.add(app_row)

        dev_row = Adw.ActionRow(title="Pavel Khabusov", subtitle="Developer — khabusov.ru")
        dev_row.set_activatable(True)
        dev_row.connect("activated", lambda _: subprocess.Popen(["xdg-open", "https://khabusov.ru"]))
        dev_row.add_suffix(Gtk.Image.new_from_icon_name("adw-external-link-symbolic"))
        about_grp.add(dev_row)

        for title, url in [(APP_NAME, APP_GITHUB), ("APK Dash", APK_DASH_GITHUB)]:
            row = Adw.ActionRow(title=title, subtitle=url)
            row.set_activatable(True)
            row.connect("activated", lambda _, u=url: subprocess.Popen(["xdg-open", u]))
            row.add_suffix(Gtk.Image.new_from_icon_name("adw-external-link-symbolic"))
            about_grp.add(row)
        page.add(about_grp)

        scroll.set_child(page)
        self.append(scroll)

    @staticmethod
    def _apply_theme(name):
        mgr = Adw.StyleManager.get_default()
        schemes = {"dark": Adw.ColorScheme.FORCE_DARK,
                   "light": Adw.ColorScheme.FORCE_LIGHT}
        mgr.set_color_scheme(schemes.get(name, Adw.ColorScheme.DEFAULT))

    def _on_theme_preview(self, *_):
        names = {0: "system", 1: "dark", 2: "light"}
        self._apply_theme(names.get(self.theme_row.get_selected(), "system"))

    def _add_project_row(self, proj=None):
        if proj is None:
            proj = {"name": "", "path": "", "desc": "", "build_dir": "", "targets": ["android"]}

        exp = Adw.ExpanderRow(title=proj.get("name") or "New Project")
        exp.set_subtitle(proj.get("desc", ""))

        name_row = Adw.EntryRow(title="Name")
        name_row.set_text(proj.get("name", ""))
        name_row.connect("changed", lambda r: exp.set_title(r.get_text() or "New Project"))
        exp.add_row(name_row)

        desc_row = Adw.EntryRow(title="Description")
        desc_row.set_text(proj.get("desc", ""))
        desc_row.connect("changed", lambda r: exp.set_subtitle(r.get_text()))
        exp.add_row(desc_row)

        path_row = Adw.EntryRow(title="Project path")
        path_row.set_text(proj.get("path", ""))
        exp.add_row(path_row)

        build_row = Adw.EntryRow(title="Build directory")
        build_row.set_text(proj.get("build_dir", ""))
        exp.add_row(build_row)

        unity_versions = list_unity_versions()
        unity_row = Adw.ComboRow(title="Unity version")
        labels = ["Default (global)"] + [v[0] for v in unity_versions]
        unity_row.set_model(Gtk.StringList.new(labels))
        proj_unity = proj.get("unity", "")
        sel = 0
        for i, (ver, path) in enumerate(unity_versions):
            if path == proj_unity: sel = i + 1
        unity_row.set_selected(sel)
        exp.add_row(unity_row)

        targets_row = Adw.ActionRow(title="Targets")
        android_sw = Gtk.CheckButton(label="Android")
        android_sw.set_active("android" in proj.get("targets", []))
        ios_sw = Gtk.CheckButton(label="iOS")
        ios_sw.set_active("ios" in proj.get("targets", []))
        targets_row.add_suffix(android_sw)
        targets_row.add_suffix(ios_sw)
        exp.add_row(targets_row)

        hide_adb_sw = Adw.SwitchRow(title="Hide ADB during build",
            subtitle="Saves ~2 min but may break some SDKs")
        hide_adb_sw.set_active(proj.get("hide_adb", False))
        exp.add_row(hide_adb_sw)

        # Upload section
        up = proj.get("upload", {})
        upload_exp = Adw.ExpanderRow(title="Upload (SCP)", show_enable_switch=False)

        up_host = Adw.EntryRow(title="Host")
        up_host.set_text(up.get("host", ""))
        upload_exp.add_row(up_host)

        up_user = Adw.EntryRow(title="User")
        up_user.set_text(up.get("user", ""))
        upload_exp.add_row(up_user)

        up_dir = Adw.EntryRow(title="Remote directory")
        up_dir.set_text(up.get("remote_dir", ""))
        upload_exp.add_row(up_dir)

        up_pattern = Adw.EntryRow(title="Rename ({name}, {build})")
        up_pattern.set_text(up.get("rename_pattern", "{name}_mq3_{build}.apk"))
        upload_exp.add_row(up_pattern)

        up_pass = Adw.PasswordEntryRow(title="Password (optional)")
        up_pass.set_text(up.get("password", ""))
        upload_exp.add_row(up_pass)

        exp.add_row(upload_exp)

        remove_row = Adw.ActionRow(title="Remove project")
        remove_btn = Gtk.Button(icon_name="user-trash-symbolic", valign=Gtk.Align.CENTER)
        remove_btn.add_css_class("flat")
        remove_btn.add_css_class("error")
        entry = {"exp": exp, "name": name_row, "path": path_row,
                 "desc": desc_row, "build_dir": build_row,
                 "android": android_sw, "ios": ios_sw, "hide_adb": hide_adb_sw,
                 "unity_combo": unity_row, "unity_versions": unity_versions,
                 "up_host": up_host, "up_user": up_user,
                 "up_dir": up_dir, "up_pattern": up_pattern,
                 "up_pass": up_pass}
        def do_remove(_, e=entry, x=exp):
            self.proj_grp.remove(x)
            self.proj_rows.remove(e)
        remove_btn.connect("clicked", do_remove)
        remove_row.add_suffix(remove_btn)
        remove_row.set_activatable_widget(remove_btn)
        exp.add_row(remove_row)

        self.proj_rows.append(entry)
        self.proj_grp.add(exp)

    def _add_filter_row(self, _, text=""):
        row = Adw.EntryRow(title="Exclude pattern")
        row.set_text(text)
        remove_btn = Gtk.Button(icon_name="user-trash-symbolic", valign=Gtk.Align.CENTER,
                                css_classes=["flat"])
        def do_remove(_, r=row):
            self._filter_grp.remove(r)
            self._filter_rows.remove(r)
        remove_btn.connect("clicked", do_remove)
        row.add_suffix(remove_btn)
        self._filter_rows.append(row)
        self._filter_grp.add(row)

    def _add_project(self, _):
        self._add_project_row()

    def _auto_detect(self, _):
        u = find_unity()
        if u: self.unity_row.set_text(u)
        d = find_apk_dash()
        if d: self.dash_row.set_text(d)

    def _save(self, _):
        theme_map = {0: "system", 1: "dark", 2: "light"}
        projects = []
        for r in self.proj_rows:
            targets = []
            if r["android"].get_active(): targets.append("android")
            if r["ios"].get_active(): targets.append("ios")
            sel = r["unity_combo"].get_selected()
            unity_path = ""
            if sel > 0 and sel - 1 < len(r["unity_versions"]):
                unity_path = r["unity_versions"][sel - 1][1]
            p = {
                "name": r["name"].get_text().strip(),
                "path": r["path"].get_text().strip(),
                "desc": r["desc"].get_text().strip(),
                "build_dir": r["build_dir"].get_text().strip(),
                "targets": targets,
            }
            if unity_path: p["unity"] = unity_path
            if r["hide_adb"].get_active(): p["hide_adb"] = True
            up_host = r["up_host"].get_text().strip()
            if up_host:
                up = {
                    "host": up_host,
                    "user": r["up_user"].get_text().strip(),
                    "remote_dir": r["up_dir"].get_text().strip(),
                    "rename_pattern": r["up_pattern"].get_text().strip() or "{name}_{build}.apk",
                }
                pw = r["up_pass"].get_text().strip()
                if pw: up["password"] = pw
                p["upload"] = up
            projects.append(p)
        log_filters = [r.get_text().strip() for r in self._filter_rows if r.get_text().strip()]
        self.cfg = {
            "unity": self.unity_row.get_text().strip(),
            "apk_dash": self.dash_row.get_text().strip(),
            "theme": theme_map.get(self.theme_row.get_selected(), "system"),
            "projects": projects,
            "log_filters": log_filters,
        }
        save_config(self.cfg)
        self.on_save(self.cfg)