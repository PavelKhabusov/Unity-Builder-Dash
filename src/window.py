"""Main application window."""
import os, subprocess, datetime, time, threading
from gi.repository import Gtk, Adw, GLib, Gio
from .constants import APP_NAME, TARGET_INFO, STAGE_PATTERNS
from .config import (load_config, load_history, save_history, save_build_entry,
                     load_builds_log, find_apk, get_version, get_build_number,
                     get_unity_for_project, upload_apk)
from .worker import BuildWorker
from .settings_dialog import SettingsDialog
from .dialogs import show_history, show_scan


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

        self.increment_toggle = Gtk.ToggleButton(icon_name="list-add-symbolic",
                                                   tooltip_text="Auto-increment build version")
        self.increment_toggle.set_active(cfg.get("auto_increment", False))

        header.pack_start(build_all)
        header.pack_start(self.cancel_btn)
        header.pack_start(self.increment_toggle)

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
        self.log_view.set_wrap_mode(Gtk.WrapMode.NONE)
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

        wrap_toggle = Gtk.ToggleButton(icon_name="format-justify-left-symbolic",
                                       tooltip_text="Word wrap", active=False)
        wrap_toggle.connect("toggled", lambda b: self.log_view.set_wrap_mode(
            Gtk.WrapMode.WORD_CHAR if b.get_active() else Gtk.WrapMode.NONE))
        search_box.append(wrap_toggle)
        lbox.append(search_box)

        self.log_scroll = Gtk.ScrolledWindow(vexpand=True)
        self.log_scroll.set_child(self.log_view)
        self.log_scroll.add_css_class("card")

        # Overlay with scroll-down button
        overlay = Gtk.Overlay()
        overlay.set_child(self.log_scroll)
        scroll_btn = Gtk.Button(icon_name="go-bottom-symbolic",
                                tooltip_text="Scroll to bottom",
                                css_classes=["circular", "osd"],
                                halign=Gtk.Align.END, valign=Gtk.Align.END)
        scroll_btn.set_margin_end(12)
        scroll_btn.set_margin_bottom(12)
        scroll_btn.connect("clicked", self._scroll_to_bottom)
        overlay.add_overlay(scroll_btn)
        overlay.set_vexpand(True)

        lbox.append(overlay)
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

        # APK size
        apk = find_apk(proj)
        if apk:
            mb = os.path.getsize(apk) / (1024 * 1024)
            sub.append(Gtk.Label(label=f"{mb:.0f} MB", css_classes=["dim-label", "caption"]))

        # Last build info
        builds = load_builds_log()
        proj_builds = [b for b in builds if b.get("project") == proj["name"]]
        if proj_builds:
            last = proj_builds[-1]
            dm, ds = divmod(last.get("duration", 0), 60)
            ok = last.get("success", False)
            icon = "object-select-symbolic" if ok else "dialog-error-symbolic"
            sub.append(Gtk.Image.new_from_icon_name(icon))
            sub.append(Gtk.Label(
                label=f"{last.get('date', '')}  {dm}:{ds:02d}",
                css_classes=["dim-label", "caption"]))

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

        # Health check icon
        scan_btn = Gtk.Button(icon_name="security-medium-symbolic",
                              tooltip_text="Health check", css_classes=["flat"])
        scan_btn.connect("clicked", lambda _, p=proj: self._on_scan(p))
        actions.append(scan_btn)

        # Context menu (three dots)
        menu = Gio.Menu()
        proj_id = proj["name"].replace(" ", "_")

        has_upload = proj.get("upload", {}).get("host") or self.cfg.get("upload", {}).get("host")
        if has_upload:
            menu.append("Upload to Server", f"win.upload-{proj_id}")

        menu.append("Open in Unity", f"win.open-unity-{proj_id}")
        menu.append("Open Build Folder", f"win.folder-{proj_id}")
        menu.append("Open Project Folder", f"win.proj-folder-{proj_id}")
        menu.append("Edit in Settings", f"win.edit-{proj_id}")

        # Register actions
        action_list = [
            (f"upload-{proj_id}", lambda *_, p=proj: self._on_upload(p)),
            (f"open-unity-{proj_id}", lambda *_, p=proj: self._open_in_unity(p)),
            (f"scan-{proj_id}", lambda *_, p=proj: self._on_scan(p)),
            (f"folder-{proj_id}", lambda *_, p=proj: subprocess.Popen(
                ["xdg-open", p.get("build_dir") or p["path"]])),
            (f"proj-folder-{proj_id}", lambda *_, p=proj: subprocess.Popen(
                ["xdg-open", p["path"]])),
            (f"edit-{proj_id}", lambda *_, p=proj: self._on_settings(None, expand=p["name"])),
        ]
        for action_name, callback in action_list:
            action = Gio.SimpleAction.new(action_name, None)
            action.connect("activate", callback)
            self.add_action(action)

        menu_btn = Gtk.MenuButton(icon_name="view-more-symbolic",
                                  menu_model=menu, css_classes=["flat"],
                                  valign=Gtk.Align.CENTER)
        actions.append(menu_btn)

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

    def _get_tag(self, s):
        s = s.strip()
        if "error" in s.lower() or "FAILED" in s: return "error"
        if "warning" in s.lower(): return "warning"
        if "Done!" in s or "[Build] OK" in s: return "success"
        if s.startswith("[Stage]") or any(s.startswith(p[1] or "") for p in STAGE_PATTERNS if p[1]): return "stage"
        return None

    def _insert_tagged(self, text, scroll=True):
        end = self.log_buffer.get_end_iter()
        tag = self._get_tag(text)
        if tag:
            self.log_buffer.insert_with_tags_by_name(end, text, tag)
        else:
            self.log_buffer.insert(end, text)
        if scroll:
            mk = self.log_buffer.create_mark(None, self.log_buffer.get_end_iter(), False)
            self.log_view.scroll_mark_onscreen(mk)
            self.log_buffer.delete_mark(mk)

    def _log(self, t):
        if not hasattr(self, '_full_log_text'): self._full_log_text = ""
        self._full_log_text += t
        self._insert_tagged(t)

    def _rebuild_log(self, text):
        """Rebuild log buffer with syntax highlighting."""
        self.log_buffer.set_text("")
        for line in text.splitlines(keepends=True):
            self._insert_tagged(line, scroll=False)

    def _on_log_search(self, entry):
        query = entry.get_text().lower().strip()
        if not query:
            if hasattr(self, '_full_log_text') and self._full_log_text:
                self._rebuild_log(self._full_log_text)
            self._scroll_to_bottom()
            return
        source = getattr(self, '_full_log_text', '') or self.log_buffer.get_text(
            self.log_buffer.get_start_iter(), self.log_buffer.get_end_iter(), False)
        filtered = "\n".join(l for l in source.splitlines() if query in l.lower())
        self._rebuild_log(filtered)

    def _scroll_to_bottom(self, *_):
        adj = self.log_scroll.get_vadjustment()
        adj.set_value(adj.get_upper())

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
        self._full_log_text = ""
        self._set_building(True)
        now = datetime.datetime.now().strftime("%H:%M:%S")
        info = TARGET_INFO[target_key]
        self.status.set_text(f"{proj['name']} / {info['label']}  {now}")
        self.cards[proj["name"]]["status"].set_text("Building...")
        self.progress_bar.set_fraction(0)
        self.stage_label.set_text("Starting Unity...")
        self.worker = BuildWorker(self.cfg, proj, target_key,
                                  self._log, self._on_done, self._on_stage,
                                  auto_increment=self.increment_toggle.get_active())
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
        self._build_cards()

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
        mb = os.path.getsize(apk) / (1024 * 1024)
        self._log(f"Uploading {os.path.basename(apk)} ({mb:.0f} MB)...\n")
        self.stage_label.set_text("Uploading...")
        self.progress_bar.set_fraction(0)
        def on_progress(frac):
            GLib.idle_add(self.progress_bar.set_fraction, frac)
            GLib.idle_add(self.stage_label.set_text, f"Uploading... {frac*100:.0f}%")
        def do_upload():
            ok = upload_apk(self.cfg, proj, apk,
                            log_cb=lambda t: GLib.idle_add(self._log, t),
                            progress_cb=on_progress)
            GLib.idle_add(self.stage_label.set_text, "Uploaded!" if ok else "Upload failed")
            GLib.idle_add(self.progress_bar.set_fraction, 1.0 if ok else 0)
        threading.Thread(target=do_upload, daemon=True).start()

    def _open_in_unity(self, proj):
        unity = get_unity_for_project(self.cfg, proj)
        if not unity or not os.path.isfile(unity):
            self._log("Unity editor not found.\n")
            return
        subprocess.Popen([unity, "-projectPath", proj["path"]])
        self._log(f"Opening {proj['name']} in Unity...\n")

    def _on_scan(self, proj):
        show_scan(self, proj)

    def _on_history(self, _):
        show_history(self)

    def _on_cancel(self, _):
        self._build_queue = []
        if self.worker: self.worker.cancel()

    def _on_settings(self, _, expand=None):
        dlg = SettingsDialog(self.cfg, self._apply_config, expand_project=expand)
        dlg.present(self)

    def _apply_config(self, cfg):
        self.cfg = cfg
        self._build_cards()
        self.empty.set_visible(not cfg.get("projects"))
