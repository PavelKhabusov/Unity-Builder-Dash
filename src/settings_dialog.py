"""Settings dialog — edit config, manage projects."""
import subprocess
from gi.repository import Gtk, Adw
from .constants import APP_NAME, APP_GITHUB, APK_DASH_GITHUB
from .config import save_config, find_unity, find_apk_dash


class SettingsDialog(Adw.Dialog):
    def __init__(self, cfg, on_save):
        super().__init__()
        self.set_title("Settings")
        self.set_content_width(560)
        self.set_content_height(600)
        self.cfg = cfg
        self.on_save = on_save
        self.proj_rows = []

        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar()
        save_btn = Gtk.Button(label="Save")
        save_btn.add_css_class("suggested-action")
        save_btn.connect("clicked", self._save)
        header.pack_end(save_btn)
        toolbar.add_top_bar(header)

        page = Adw.PreferencesPage()

        # General
        grp = Adw.PreferencesGroup(title="General")
        self.unity_row = Adw.EntryRow(title="Unity Editor")
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
        page.add(grp)

        # Projects
        self.proj_grp = Adw.PreferencesGroup(title="Projects")
        add_btn = Gtk.Button(icon_name="list-add-symbolic", valign=Gtk.Align.CENTER)
        add_btn.add_css_class("flat")
        add_btn.connect("clicked", self._add_project)
        self.proj_grp.set_header_suffix(add_btn)

        for p in cfg.get("projects", []):
            self._add_project_row(p)
        page.add(self.proj_grp)

        # About
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

    def _add_project_row(self, proj=None):
        if proj is None:
            proj = {"name": "", "path": "", "desc": "", "build_dir": "", "targets": ["android"]}

        exp = Adw.ExpanderRow(title=proj.get("name") or "New Project")
        exp.set_subtitle(proj.get("desc", ""))

        name_row = Adw.EntryRow(title="Name")
        name_row.set_text(proj.get("name", ""))
        name_row.connect("changed", lambda r: exp.set_title(r.get_text() or "New Project"))
        exp.add_row(name_row)

        path_row = Adw.EntryRow(title="Project path")
        path_row.set_text(proj.get("path", ""))
        exp.add_row(path_row)

        desc_row = Adw.EntryRow(title="Description")
        desc_row.set_text(proj.get("desc", ""))
        desc_row.connect("changed", lambda r: exp.set_subtitle(r.get_text()))
        exp.add_row(desc_row)

        build_row = Adw.EntryRow(title="Build directory")
        build_row.set_text(proj.get("build_dir", ""))
        exp.add_row(build_row)

        android_sw = Adw.SwitchRow(title="Android")
        android_sw.set_active("android" in proj.get("targets", []))
        exp.add_row(android_sw)

        ios_sw = Adw.SwitchRow(title="iOS")
        ios_sw.set_active("ios" in proj.get("targets", []))
        exp.add_row(ios_sw)

        remove_row = Adw.ActionRow(title="Remove project")
        remove_btn = Gtk.Button(icon_name="user-trash-symbolic", valign=Gtk.Align.CENTER)
        remove_btn.add_css_class("flat")
        remove_btn.add_css_class("error")
        entry = {"exp": exp, "name": name_row, "path": path_row,
                 "desc": desc_row, "build_dir": build_row,
                 "android": android_sw, "ios": ios_sw}
        def do_remove(_, e=entry, x=exp):
            self.proj_grp.remove(x)
            self.proj_rows.remove(e)
        remove_btn.connect("clicked", do_remove)
        remove_row.add_suffix(remove_btn)
        remove_row.set_activatable_widget(remove_btn)
        exp.add_row(remove_row)

        self.proj_rows.append(entry)
        self.proj_grp.add(exp)

    def _add_project(self, _):
        self._add_project_row()

    def _auto_detect(self, _):
        u = find_unity()
        if u: self.unity_row.set_text(u)
        d = find_apk_dash()
        if d: self.dash_row.set_text(d)

    def _save(self, _):
        projects = []
        for r in self.proj_rows:
            targets = []
            if r["android"].get_active(): targets.append("android")
            if r["ios"].get_active(): targets.append("ios")
            projects.append({
                "name": r["name"].get_text().strip(),
                "path": r["path"].get_text().strip(),
                "desc": r["desc"].get_text().strip(),
                "build_dir": r["build_dir"].get_text().strip(),
                "targets": targets,
            })
        self.cfg = {
            "unity": self.unity_row.get_text().strip(),
            "apk_dash": self.dash_row.get_text().strip(),
            "projects": projects,
        }
        save_config(self.cfg)
        self.on_save(self.cfg)
        self.close()
