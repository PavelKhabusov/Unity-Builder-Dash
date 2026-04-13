"""Settings dialog — edit config, manage projects."""
import subprocess
from gi.repository import Gtk, Adw
from .constants import APP_NAME, APP_GITHUB, APK_DASH_GITHUB
from .config import save_config, find_unity, find_apk_dash, list_unity_versions


class SettingsDialog(Adw.Dialog):
    def __init__(self, cfg, on_save, expand_project=None):
        super().__init__()
        self.set_title("Settings")
        self.set_content_width(560)
        self.set_content_height(650)
        self.cfg = cfg
        self.on_save = on_save
        self.proj_rows = []
        self._saved = False
        self._expand_project = expand_project

        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar()
        save_btn = Gtk.Button(label="Save")
        save_btn.add_css_class("suggested-action")
        save_btn.connect("clicked", self._save)
        header.pack_end(save_btn)
        toolbar.add_top_bar(header)

        page = Adw.PreferencesPage()

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

        # Theme (live preview)
        self._original_theme = cfg.get("theme", "system")
        self.theme_row = Adw.ComboRow(title="Theme")
        self.theme_row.set_model(Gtk.StringList.new(["System", "Dark", "Light"]))
        theme_idx = {"system": 0, "dark": 1, "light": 2}
        self.theme_row.set_selected(theme_idx.get(self._original_theme, 0))
        self.theme_row.connect("notify::selected", self._on_theme_preview)
        grp.add(self.theme_row)
        self.connect("closed", self._on_closed)

        # ── Projects (first) ──
        self.proj_grp = Adw.PreferencesGroup(title="Projects")
        add_btn = Gtk.Button(icon_name="list-add-symbolic", valign=Gtk.Align.CENTER)
        add_btn.add_css_class("flat")
        add_btn.connect("clicked", self._add_project)
        self.proj_grp.set_header_suffix(add_btn)

        for p in cfg.get("projects", []):
            self._add_project_row(p)
        page.add(self.proj_grp)

        # ── General (after projects) ──
        page.add(grp)

        # ── About ──
        about_grp = Adw.PreferencesGroup(title="About")
        for title, url in [(APP_NAME, APP_GITHUB), ("APK Dash", APK_DASH_GITHUB)]:
            row = Adw.ActionRow(title=title, subtitle=url)
            row.set_activatable(True)
            row.connect("activated", lambda _, u=url: subprocess.Popen(["xdg-open", u]))
            row.add_suffix(Gtk.Image.new_from_icon_name("adw-external-link-symbolic"))
            about_grp.add(row)
        page.add(about_grp)

        toolbar.set_content(page)
        self.set_child(toolbar)


    @staticmethod
    def _apply_theme(name):
        mgr = Adw.StyleManager.get_default()
        schemes = {"dark": Adw.ColorScheme.FORCE_DARK,
                   "light": Adw.ColorScheme.FORCE_LIGHT}
        mgr.set_color_scheme(schemes.get(name, Adw.ColorScheme.DEFAULT))

    def _on_theme_preview(self, *_):
        names = {0: "system", 1: "dark", 2: "light"}
        self._apply_theme(names.get(self.theme_row.get_selected(), "system"))

    def _on_closed(self, *_):
        if not self._saved:
            self._apply_theme(self._original_theme)

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

        # Per-project Unity override
        unity_versions = list_unity_versions()
        unity_row = Adw.ComboRow(title="Unity version")
        labels = ["Default (global)"] + [v[0] for v in unity_versions]
        unity_row.set_model(Gtk.StringList.new(labels))
        # Select current
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

        # Upload section (nested expander)
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
        if self._expand_project and proj.get("name") == self._expand_project:
            exp.set_expanded(True)
            self._scroll_to = exp

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
            # Unity per-project
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
            # Per-project upload
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
        self.cfg = {
            "unity": self.unity_row.get_text().strip(),
            "apk_dash": self.dash_row.get_text().strip(),
            "theme": theme_map.get(self.theme_row.get_selected(), "system"),
            "projects": projects,
        }
        self._saved = True
        save_config(self.cfg)
        self.on_save(self.cfg)
        self.close()
