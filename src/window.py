"""Main application window."""
import os, subprocess, datetime, time, threading
from gi.repository import Gtk, Adw, GLib
from .constants import APP_NAME, TARGET_INFO, STAGE_PATTERNS
from .config import (load_config, load_history, save_history, save_build_entry,
                     load_builds_log, find_apk, get_version, get_build_number,
                     get_unity_for_project, scan_project, upload_apk)
from .worker import BuildWorker
from .settings_dialog import SettingsDialog


class BuilderWindow(Adw.ApplicationWindow):
    def __init__(self, app, cfg):
        super().__init__(application=app, title=APP_NAME,
                         default_width=900, default_height=700)
        self.cfg = cfg
        self.worker = None
        self._build_queue = []
        self._elapsed_timer = None

        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar()

        # Header left
        build_all = Gtk.Button(icon_name="media-playback-start-symbolic",
                               tooltip_text="Build All Android")
        build_all.add_css_class("suggested-action")
        build_all.connect("clicked", self._on_build_all)
        self.build_all_btn = build_all

        self.cancel_btn = Gtk.Button(icon_name="process-stop-symbolic", tooltip_text="Cancel")
        self.cancel_btn.add_css_class("destructive-action")
        self.cancel_btn.set_sensitive(False)
        self.cancel_btn.connect("clicked", self._on_cancel)

        header.pack_start(build_all)
        header.pack_start(self.cancel_btn)

        # Header right
        self.spinner = Gtk.Spinner()
        self.elapsed_label = Gtk.Label(label="")
        self.elapsed_label.add_css_class("dim-label")

        history_btn = Gtk.Button(icon_name="document-open-recent-symbolic",
                                 tooltip_text="Build History")
        history_btn.connect("clicked", self._on_history)

        settings_btn = Gtk.Button(icon_name="emblem-system-symbolic", tooltip_text="Settings")
        settings_btn.connect("clicked", self._on_settings)

        header.pack_end(settings_btn)
        header.pack_end(history_btn)
        header.pack_end(self.spinner)
        header.pack_end(self.elapsed_label)
        toolbar.add_top_bar(header)

        # Content
        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)

        # ── Projects list (vertical, like Unity Hub) ──
        self.projects_list = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        self.projects_list.set_margin_top(8)
        self.projects_list.set_margin_start(12)
        self.projects_list.set_margin_end(12)
        self.cards = {}
        self._build_cards()
        content.append(self.projects_list)

        # Empty state
        self.empty = Adw.StatusPage(title="No projects configured",
            description="Open Settings to add Unity projects",
            icon_name="emblem-system-symbolic")
        self.empty.set_visible(not cfg.get("projects"))
        content.append(self.empty)

        # ── Progress ──
        pbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        pbox.set_margin_top(8)
        pbox.set_margin_start(16)
        pbox.set_margin_end(16)

        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        self.stage_label = Gtk.Label(label="Ready")
        self.stage_label.set_halign(Gtk.Align.START)
        self.stage_label.set_hexpand(True)
        self.stage_label.add_css_class("caption")
        row.append(self.stage_label)
        self.status = Gtk.Label(label="")
        self.status.set_halign(Gtk.Align.END)
        self.status.add_css_class("dim-label")
        self.status.add_css_class("caption")
        row.append(self.status)
        pbox.append(row)
        self.progress_bar = Gtk.ProgressBar()
        self.progress_bar.set_show_text(False)
        pbox.append(self.progress_bar)
        content.append(pbox)

        # ── Log ──
        lbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, vexpand=True)
        lbox.set_margin_top(4)
        lbox.set_margin_start(12)
        lbox.set_margin_end(12)
        lbox.set_margin_bottom(12)

        self.log_buffer = Gtk.TextBuffer()
        self.log_view = Gtk.TextView(buffer=self.log_buffer, editable=False,
                                     cursor_visible=False, monospace=True)
        self.log_view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        self.log_view.set_top_margin(6)
        self.log_view.set_bottom_margin(6)
        self.log_view.set_left_margin(8)
        self.log_view.set_right_margin(8)

        # Search bar for log
        search_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        search_box.set_margin_bottom(4)
        self.search_entry = Gtk.SearchEntry(placeholder_text="Filter log...")
        self.search_entry.set_hexpand(True)
        self.search_entry.connect("search-changed", self._on_log_search)
        search_box.append(self.search_entry)
        lbox.append(search_box)

        scroll = Gtk.ScrolledWindow(vexpand=True)
        scroll.set_child(self.log_view)
        scroll.add_css_class("card")
        lbox.append(scroll)
        content.append(lbox)

        self._setup_log_tags()
        toolbar.set_content(content)
        self.set_content(toolbar)

        if not cfg.get("unity") or not cfg.get("projects"):
            GLib.idle_add(self._on_settings, None)

    # ── Project rows (Hub-style) ──

    def _build_cards(self):
        while (c := self.projects_list.get_first_child()):
            self.projects_list.remove(c)
        self.cards = {}
        for proj in self.cfg.get("projects", []):
            self.projects_list.append(self._make_row(proj))

    def _make_row(self, proj):
        """Create a horizontal project row like Unity Hub."""
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        row.add_css_class("card")
        row.set_margin_start(2)
        row.set_margin_end(2)

        inner = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        inner.set_margin_top(10)
        inner.set_margin_bottom(10)
        inner.set_margin_start(16)
        inner.set_margin_end(12)
        inner.set_hexpand(True)

        # Left: name + desc
        info = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        info.set_hexpand(True)

        name_lbl = Gtk.Label(label=proj["name"], xalign=0, css_classes=["heading"])
        info.append(name_lbl)

        sub = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        sub.append(Gtk.Label(label=proj.get("desc", ""), xalign=0,
                             css_classes=["dim-label", "caption"]))

        ver = Gtk.Label(label=get_version(proj["path"]), css_classes=["caption"])
        sub.append(ver)

        stat = Gtk.Label(label="", css_classes=["caption"])
        sub.append(stat)
        info.append(sub)
        inner.append(info)

        # Right: action buttons
        actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=2,
                          valign=Gtk.Align.CENTER)
        buttons = []

        for t_key in proj.get("targets", []):
            t_info = TARGET_INFO.get(t_key)
            if not t_info: continue
            btn = Gtk.Button(icon_name=t_info["icon"],
                             tooltip_text=f"Build {t_info['label']}", css_classes=["flat"])
            btn.connect("clicked", lambda _, p=proj, t=t_key: self._on_build(p, t))
            actions.append(btn)
            buttons.append(btn)

        deploy = None
        if "android" in proj.get("targets", []):
            deploy = Gtk.Button(icon_name="send-to-symbolic",
                                tooltip_text="Deploy to device", css_classes=["flat"])
            deploy.connect("clicked", lambda _, p=proj: self._on_deploy(p))
            deploy.set_sensitive(find_apk(proj) is not None)
            actions.append(deploy)
            buttons.append(deploy)

        # Upload button (per-project)
        has_upload = proj.get("upload", {}).get("host") or self.cfg.get("upload", {}).get("host")
        if has_upload and "android" in proj.get("targets", []):
            upload_btn = Gtk.Button(icon_name="go-up-symbolic",
                                   tooltip_text="Upload APK to server", css_classes=["flat"])
            upload_btn.connect("clicked", lambda _, p=proj: self._on_upload(p))
            upload_btn.set_sensitive(find_apk(proj) is not None)
            actions.append(upload_btn)
            buttons.append(upload_btn)

        # Open in Unity
        unity_btn = Gtk.Button(icon_name="application-x-executable-symbolic",
                               tooltip_text="Open in Unity", css_classes=["flat"])
        unity_btn.connect("clicked", lambda _, p=proj: self._open_in_unity(p))
        actions.append(unity_btn)

        # Health check
        scan_btn = Gtk.Button(icon_name="security-medium-symbolic",
                              tooltip_text="Health check", css_classes=["flat"])
        scan_btn.connect("clicked", lambda _, p=proj: self._on_scan(p))
        actions.append(scan_btn)

        # Folder
        fb = Gtk.Button(icon_name="folder-symbolic",
                        tooltip_text="Open build folder", css_classes=["flat"])
        fb.connect("clicked", lambda _, p=proj: subprocess.Popen(
            ["xdg-open", p.get("build_dir") or p["path"]]))
        actions.append(fb)

        inner.append(actions)
        row.append(inner)

        self.cards[proj["name"]] = {
            "buttons": buttons, "status": stat,
            "version": ver, "deploy": deploy,
        }
        return row

    # ── Build state ──

    def _set_building(self, on):
        for c in self.cards.values():
            for b in c["buttons"]: b.set_sensitive(not on)
        self.build_all_btn.set_sensitive(not on)
        self.cancel_btn.set_sensitive(on)
        if on:
            self.spinner.start()
            self._elapsed_timer = GLib.timeout_add(1000, self._tick)
        else:
            self.spinner.stop()
            self.elapsed_label.set_text("")
            if self._elapsed_timer:
                GLib.source_remove(self._elapsed_timer)
                self._elapsed_timer = None

    def _tick(self):
        if self.worker and self.worker.start_time:
            elapsed = int(time.time() - self.worker.start_time)
            m, s = divmod(elapsed, 60)
            key = f"{self.worker.project['name']}_{self.worker.target}"
            prev = load_history().get(key)
            if prev and prev > elapsed:
                rm, rs = divmod(prev - elapsed, 60)
                self.elapsed_label.set_text(f"{m}:{s:02d}  ~{rm}:{rs:02d} left")
            else:
                self.elapsed_label.set_text(f"{m}:{s:02d}")
        return True

    # ── Log ──

    def _setup_log_tags(self):
        self.log_buffer.create_tag("error", foreground="#e01b24")
        self.log_buffer.create_tag("warning", foreground="#e5a50a")
        self.log_buffer.create_tag("success", foreground="#2ec27e")
        self.log_buffer.create_tag("stage", foreground="#62a0ea", weight=700)
        self.log_buffer.create_tag("hidden", invisible=True)

    def _log(self, t):
        end = self.log_buffer.get_end_iter()
        tag = None
        s = t.strip()
        if "error" in s.lower() or "FAILED" in s:
            tag = "error"
        elif "warning" in s.lower():
            tag = "warning"
        elif "Done!" in s or "[Build] OK" in s:
            tag = "success"
        elif s.startswith("[Stage]") or any(s.startswith(p[1] or "") for p in STAGE_PATTERNS if p[1]):
            tag = "stage"

        if tag:
            self.log_buffer.insert_with_tags_by_name(end, t, tag)
        else:
            self.log_buffer.insert(end, t)
        mk = self.log_buffer.create_mark(None, self.log_buffer.get_end_iter(), False)
        self.log_view.scroll_mark_onscreen(mk)
        self.log_buffer.delete_mark(mk)

    def _on_log_search(self, entry):
        """Simple log filter — hides non-matching lines."""
        query = entry.get_text().lower().strip()
        if not query:
            # Show all
            self.log_buffer.remove_tag_by_name("hidden",
                self.log_buffer.get_start_iter(), self.log_buffer.get_end_iter())
            return
        start = self.log_buffer.get_start_iter()
        end = self.log_buffer.get_end_iter()
        self.log_buffer.apply_tag_by_name("hidden", start, end)
        # Show matching lines
        it = start.copy()
        while True:
            line_end = it.copy()
            line_end.forward_to_line_end()
            text = self.log_buffer.get_text(it, line_end, False)
            if query in text.lower():
                self.log_buffer.remove_tag_by_name("hidden", it, line_end)
            if not it.forward_line():
                break

    def _on_stage(self, text, frac):
        if text: self.stage_label.set_text(text)
        if frac >= 0: self.progress_bar.set_fraction(frac)
        elif text: self.progress_bar.pulse()

    # ── Actions ──

    def _on_build(self, proj, target_key):
        self._build_queue = []
        self._start(proj, target_key)

    def _on_build_all(self, _):
        q = [(p, t) for p in self.cfg["projects"] for t in p["targets"] if t == "android"]
        if not q: return
        f = q.pop(0)
        self._build_queue = q
        self._start(f[0], f[1])

    def _start(self, proj, target_key):
        unity = get_unity_for_project(self.cfg, proj)
        if not unity or not os.path.isfile(unity):
            self._log("Unity editor not found. Check Settings.\n")
            return
        self.log_buffer.set_text("")
        self._set_building(True)
        now = datetime.datetime.now().strftime("%H:%M:%S")
        info = TARGET_INFO[target_key]
        self.status.set_text(f"{proj['name']} / {info['label']}  {now}")
        self.cards[proj["name"]]["status"].set_text("Building...")
        self.progress_bar.set_fraction(0)
        self.stage_label.set_text("Starting Unity...")
        self.worker = BuildWorker(self.cfg, proj, target_key,
                                  self._log, self._on_done, self._on_stage)
        self.worker.start()

    def _on_done(self, ok):
        name = self.worker.project["name"]
        proj = self.worker.project
        c = self.cards[name]
        c["status"].set_text("Done" if ok else "Failed")
        c["version"].set_text(get_version(proj["path"]))
        if c["deploy"]:
            c["deploy"].set_sensitive(find_apk(proj) is not None)

        duration = int(time.time() - self.worker.start_time) if self.worker.start_time else 0
        if ok and self.worker.start_time:
            h = load_history()
            h[f"{name}_{self.worker.target}"] = duration
            save_history(h)

        apk = find_apk(proj) if ok else None
        save_build_entry(name, self.worker.target, ok, duration,
                         os.path.getsize(apk) if apk else None,
                         get_build_number(proj["path"]))

        # Notification
        try:
            icon = "dialog-ok-apply" if ok else "dialog-error"
            subprocess.Popen(["notify-send", "-i", icon, APP_NAME,
                              f"{name}: {'done' if ok else 'failed'} ({self.worker.elapsed_str()})"])
        except: pass

        el = self.worker.elapsed_str()
        self._set_building(False)
        self.status.set_text(f"{name}  {'done' if ok else 'failed'}  {el}")
        self.stage_label.set_text("Done" if ok else "Failed")
        self.progress_bar.set_fraction(1.0 if ok else 0)
        self.worker = None

        if ok and self._build_queue:
            p, t = self._build_queue.pop(0)
            self._start(p, t)

    def _on_deploy(self, proj):
        apk = find_apk(proj)
        if not apk: return
        dash = self.cfg.get("apk_dash", "")
        if not dash or not os.path.exists(dash): return
        # Launch in fully isolated process — clean env without parent GTK state
        env = {k: v for k, v in os.environ.items()
               if not k.startswith(("GTK_", "GDK_", "GIO_", "DBUS_SESSION_BUS_PID"))}
        env["GTK_A11Y"] = "none"  # prevent accessibility bus conflicts
        subprocess.Popen(["python3", dash, apk], env=env,
                         start_new_session=True,
                         stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL)

    def _on_upload(self, proj):
        apk = find_apk(proj)
        if not apk:
            self._log("No APK found.\n")
            return
        self._log(f"Uploading {os.path.basename(apk)}...\n")
        def do_upload():
            ok = upload_apk(self.cfg, proj, apk, lambda t: GLib.idle_add(self._log, t))
            GLib.idle_add(self._log, "Upload done.\n" if ok else "Upload failed.\n")
        threading.Thread(target=do_upload, daemon=True).start()

    def _open_in_unity(self, proj):
        unity = get_unity_for_project(self.cfg, proj)
        if not unity or not os.path.isfile(unity):
            self._log("Unity editor not found.\n")
            return
        subprocess.Popen([unity, "-projectPath", proj["path"]])
        self._log(f"Opening {proj['name']} in Unity...\n")

    def _on_scan(self, proj):
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
                row.add_prefix(Gtk.Image.new_from_icon_name("emblem-ok-symbolic"))
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
        dlg.present(self)

    def _on_history(self, _):
        builds = load_builds_log()
        dlg = Adw.Dialog()
        dlg.set_title("Build History")
        dlg.set_content_width(500)
        dlg.set_content_height(500)

        tb = Adw.ToolbarView()
        tb.add_top_bar(Adw.HeaderBar())
        page = Adw.PreferencesPage()

        if not builds:
            page.add(Adw.StatusPage(title="No builds yet"))
        else:
            grp = Adw.PreferencesGroup(title=f"Last {min(len(builds), 20)} builds")
            for b in reversed(builds[-20:]):
                icon = "emblem-ok-symbolic" if b.get("success") else "dialog-error-symbolic"
                size = f"  {b['apk_size_mb']} MB" if b.get("apk_size_mb") else ""
                dm, ds = divmod(b.get("duration", 0), 60)
                row = Adw.ActionRow(
                    title=f"{b['project']} — {b.get('target', '?')}",
                    subtitle=f"{b.get('date', '?')}  {dm}:{ds:02d}{size}  build {b.get('build', '?')}"
                )
                row.add_prefix(Gtk.Image.new_from_icon_name(icon))
                grp.add(row)
            page.add(grp)

        tb.set_content(page)
        dlg.set_child(tb)
        dlg.present(self)

    def _on_cancel(self, _):
        self._build_queue = []
        if self.worker: self.worker.cancel()

    def _on_settings(self, _):
        SettingsDialog(self.cfg, self._apply_config).present(self)

    def _apply_config(self, cfg):
        self.cfg = cfg
        self._build_cards()
        self.empty.set_visible(not cfg.get("projects"))
