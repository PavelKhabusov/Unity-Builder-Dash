"""Main application window with sidebar navigation."""
import os, subprocess, datetime, time, threading
from gi.repository import Gtk, Adw, GLib, Gio
from .constants import APP_NAME, TARGET_INFO, STAGE_PATTERNS
from .config import (load_config, load_history, save_history, save_build_entry,
                     load_builds_log, find_apk, get_version, get_build_number,
                     get_unity_for_project, upload_apk, save_test_entry, APP_DIR)
from .worker import BuildWorker
from .settings_page import SettingsPage
from .history_page import HistoryPage
from .devices import DevicesPage
from .dialogs import show_scan, show_screenshots
from .log_view import LogView
from .profiler import ProfilerPage


# Sidebar items: (id, icon, label)
SIDEBAR_ITEMS = [
    ("projects", "applications-system-symbolic", "Projects"),
    ("devices",  "phone-symbolic",      "Devices"),
    ("history",  "document-open-recent-symbolic", "History"),
    ("profiler", "org.gnome.SystemMonitor-symbolic", "Profiler"),
]
SIDEBAR_BOTTOM = ("settings", "emblem-system-symbolic", "Settings")


class BuilderWindow(Adw.ApplicationWindow):
    def __init__(self, app, cfg):
        super().__init__(application=app, title=APP_NAME,
                         default_width=1000, default_height=700)
        self.cfg = cfg
        self.worker = None
        self._build_queue = []
        self._elapsed_timer = None

        # ── Split view: sidebar + content ──
        self._split = Adw.NavigationSplitView()

        # ── Sidebar ──
        sidebar_page = Adw.NavigationPage(title=APP_NAME)
        sidebar_toolbar = Adw.ToolbarView()
        sidebar_header = Adw.HeaderBar()
        import os as _os
        icon_path = _os.path.join(_os.path.dirname(_os.path.dirname(
            _os.path.abspath(__file__))), "icons", "ubd-app-icon.png")
        center = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=7)
        center.set_margin_start(14)
        if _os.path.isfile(icon_path):
            from gi.repository import Gdk as _Gdk
            icon_img = Gtk.Image.new_from_paintable(
                _Gdk.Texture.new_from_filename(icon_path))
            icon_img.set_pixel_size(20)
            center.append(icon_img)
        title_lbl = Gtk.Label(xalign=0)
        title_lbl.set_markup(
            '<span size="8500" weight="bold" line_height="0.8">Unity Builder\n'
            '</span><span size="small" alpha="60%" line_height="0.8">Dash</span>')
        self._sidebar_title_lbl = title_lbl
        center.append(title_lbl)
        self._sidebar_title_center = center
        sidebar_header.set_show_title(False)
        sidebar_header.pack_start(center)
        self._sidebar_header_title = self._sidebar_title_lbl
        sidebar_toolbar.add_top_bar(sidebar_header)

        sidebar_content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)

        sidebar_list = Gtk.ListBox()
        sidebar_list.set_selection_mode(Gtk.SelectionMode.SINGLE)
        sidebar_list.add_css_class("navigation-sidebar")

        for item_id, icon, label in SIDEBAR_ITEMS:
            row = Gtk.ListBoxRow()
            box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
            box.set_margin_top(6)
            box.set_margin_bottom(6)
            box.set_margin_start(8)
            box.set_margin_end(8)
            img = Gtk.Image.new_from_icon_name(icon)
            lbl = Gtk.Label(label=label)
            box.append(img)
            box.append(lbl)
            row.set_child(box)
            row._page_id = item_id
            row._box = box
            row._label = lbl
            sidebar_list.append(row)

        sidebar_content.append(sidebar_list)

        # Spacer pushes Settings to bottom
        spacer = Gtk.Box()
        spacer.set_vexpand(True)
        sidebar_content.append(spacer)

        # Settings pinned to bottom
        settings_list = Gtk.ListBox()
        settings_list.set_selection_mode(Gtk.SelectionMode.SINGLE)
        settings_list.add_css_class("navigation-sidebar")

        item_id, icon, label = SIDEBAR_BOTTOM
        settings_row = Gtk.ListBoxRow()
        sbox = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        sbox.set_margin_top(6)
        sbox.set_margin_bottom(6)
        sbox.set_margin_start(8)
        sbox.set_margin_end(8)
        sbox.append(Gtk.Image.new_from_icon_name(icon))
        settings_lbl = Gtk.Label(label=label)
        sbox.append(settings_lbl)
        settings_row.set_child(sbox)
        settings_row._page_id = item_id
        settings_row._box = sbox
        settings_row._label = settings_lbl
        settings_list.append(settings_row)
        sidebar_content.append(settings_list)

        # Deselect other list when one is selected
        def _cross_deselect(active_list, other_list):
            def handler(lb, row):
                if row is not None:
                    other_list.unselect_all()
                self._on_sidebar_selected(lb, row)
            return handler

        sidebar_list.connect("row-selected", _cross_deselect(sidebar_list, settings_list))
        settings_list.connect("row-selected", _cross_deselect(settings_list, sidebar_list))

        # Collapse button
        collapse_list = Gtk.ListBox()
        collapse_list.set_selection_mode(Gtk.SelectionMode.NONE)
        collapse_list.add_css_class("navigation-sidebar")
        collapse_row = Gtk.ListBoxRow()
        collapse_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        collapse_box.set_margin_top(6)
        collapse_box.set_margin_bottom(6)
        collapse_box.set_margin_start(8)
        collapse_box.set_margin_end(8)
        collapse_box.append(Gtk.Image.new_from_icon_name("sidebar-show-symbolic"))
        self._collapse_label = Gtk.Label(label="Collapse")
        collapse_box.append(self._collapse_label)
        collapse_row.set_child(collapse_box)
        collapse_row._box = collapse_box
        collapse_row._label = self._collapse_label
        collapse_list.append(collapse_row)
        collapse_list.set_opacity(0.4)
        collapse_list.connect("row-activated", lambda *_: self._toggle_sidebar())
        sidebar_content.append(collapse_list)

        sidebar_toolbar.set_content(sidebar_content)
        sidebar_page.set_child(sidebar_toolbar)
        self._split.set_sidebar(sidebar_page)
        self._split.set_min_sidebar_width(110)
        self._split.set_max_sidebar_width(140)
        self._sidebar_list = sidebar_list
        self._settings_list = settings_list
        self._sidebar_collapsed = False
        self._sidebar_labels = []  # filled below
        self._sidebar_header_title = None

        # ── Content pages ──
        self._content_page = Adw.NavigationPage(title="Projects")
        content_toolbar = Adw.ToolbarView()
        self._content_header = Adw.HeaderBar()

        # Header left — build controls
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

        self._content_header.pack_start(build_all)
        self._content_header.pack_start(self.cancel_btn)
        self._content_header.pack_start(self.increment_toggle)

        # Header right — spinner + elapsed + log toggle
        self.spinner = Gtk.Spinner()
        self.elapsed_label = Gtk.Label(label="")
        self.elapsed_label.add_css_class("dim-label")

        self._log_toggle = Gtk.ToggleButton(icon_name="utilities-terminal-symbolic",
                                             tooltip_text="Show/hide build log")
        self._log_toggle.connect("toggled",
                                  lambda b: self._toggle_build_log(b.get_active()))

        self._settings_save_btn = Gtk.Button(label="Save", css_classes=["suggested-action"])
        self._settings_save_btn.set_visible(False)
        self._content_header.pack_end(self._settings_save_btn)
        self._content_header.pack_end(self._log_toggle)
        self._content_header.pack_end(self.spinner)
        self._content_header.pack_end(self.elapsed_label)

        # Settings save button (added after settings page is created)

        content_toolbar.add_top_bar(self._content_header)

        # Stack for content pages
        self._stack = Gtk.Stack()
        self._stack.set_transition_type(Gtk.StackTransitionType.CROSSFADE)
        self._stack.set_transition_duration(150)

        # Projects page
        self._projects_page = self._build_projects_page()
        self._stack.add_named(self._projects_page, "projects")

        # Devices page
        self._devices_page = DevicesPage()
        self._stack.add_named(self._devices_page, "devices")

        # History page
        self._history_page = HistoryPage()
        self._stack.add_named(self._history_page, "history")

        # Profiler page
        self._profiler_page = ProfilerPage()
        self._stack.add_named(self._profiler_page, "profiler")

        # Settings page
        self._settings_page = SettingsPage(cfg, self._apply_config)
        self._stack.add_named(self._settings_page, "settings")
        self._settings_save_btn.connect("clicked", self._settings_page._save)

        content_toolbar.set_content(self._stack)
        self._content_page.set_child(content_toolbar)
        self._split.set_content(self._content_page)

        self.set_content(self._split)

        # Select first item
        self._sidebar_list.select_row(self._sidebar_list.get_row_at_index(0))

        if not cfg.get("unity") or not cfg.get("projects"):
            self._settings_list.select_row(self._settings_list.get_row_at_index(0))

    # ── Sidebar ──

    def _toggle_sidebar(self):
        self._sidebar_collapsed = not self._sidebar_collapsed
        collapsed = self._sidebar_collapsed

        # Toggle labels, centering, header
        all_rows = []
        for i in range(10):
            r = self._sidebar_list.get_row_at_index(i)
            if r is None: break
            all_rows.append(r)
        r = self._settings_list.get_row_at_index(0)
        if r: all_rows.append(r)

        for row in all_rows:
            lbl = getattr(row, "_label", None)
            box = getattr(row, "_box", None)
            if lbl:
                lbl.set_visible(not collapsed)
            if box:
                box.set_halign(Gtk.Align.CENTER if collapsed else Gtk.Align.FILL)

        # Collapse button label
        if hasattr(self, "_collapse_label"):
            self._collapse_label.set_visible(not collapsed)

        # Header: hide text, center icon
        if hasattr(self, "_sidebar_title_lbl"):
            self._sidebar_title_lbl.set_visible(not collapsed)
        if hasattr(self, "_sidebar_title_center"):
            self._sidebar_title_center.set_halign(
                Gtk.Align.CENTER if collapsed else Gtk.Align.CENTER)

        # Set width immediately (no laggy animation)
        if collapsed:
            self._split.set_min_sidebar_width(48)
            self._split.set_max_sidebar_width(48)
        else:
            self._split.set_min_sidebar_width(110)
            self._split.set_max_sidebar_width(140)

    # ── Sidebar navigation ──

    def _on_sidebar_selected(self, listbox, row):
        if row is None:
            return
        page_id = row._page_id
        self._stack.set_visible_child_name(page_id)
        titles = {i[0]: i[2] for i in SIDEBAR_ITEMS}
        titles[SIDEBAR_BOTTOM[0]] = SIDEBAR_BOTTOM[2]
        title = titles.get(page_id, "")
        self._content_page.set_title(title)

        # Show/hide build controls based on page
        is_projects = page_id == "projects"
        self.build_all_btn.set_visible(is_projects)
        self.cancel_btn.set_visible(is_projects)
        self.increment_toggle.set_visible(is_projects)
        self._log_toggle.set_visible(is_projects)
        self._settings_save_btn.set_visible(page_id == "settings")

        # Refresh data on page switch
        if page_id == "history":
            self._history_page.refresh()
        elif page_id == "devices":
            self._devices_page.refresh()
        elif page_id == "profiler":
            self._profiler_page.refresh()

    # ── Projects page ──

    def _build_projects_page(self):
        """Build the projects content page."""
        paned = Gtk.Paned(orientation=Gtk.Orientation.VERTICAL)

        # ── Top: projects + progress ──
        top = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)

        # Projects list
        self.projects_list = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        self.projects_list.set_margin_top(8)
        self.projects_list.set_margin_start(12)
        self.projects_list.set_margin_end(12)
        self.cards = {}
        self._build_cards()

        proj_scroll = Gtk.ScrolledWindow(vexpand=True)
        proj_scroll.set_child(self.projects_list)
        top.append(proj_scroll)

        # Empty state
        self.empty = Adw.StatusPage(title="No projects configured",
            description="Open Settings to add Unity projects",
            icon_name="emblem-system-symbolic")
        self.empty.set_visible(not self.cfg.get("projects"))
        top.append(self.empty)

        # Progress
        pbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        pbox.set_margin_top(8)
        pbox.set_margin_start(16)
        pbox.set_margin_end(16)
        pbox.set_margin_bottom(4)

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
        top.append(pbox)

        paned.set_start_child(top)
        paned.set_resize_start_child(True)
        paned.set_shrink_start_child(False)

        # ── Bottom: log panel (hidden by default) ──
        self._build_log_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)

        log_close = Gtk.Button(icon_name="window-close-symbolic",
                               tooltip_text="Close log", css_classes=["flat"])

        self._log_widget = LogView(
            levels=["All", "Errors", "Warnings", "Stages"],
            get_tag=self._get_tag,
            extra_end=[log_close])
        self._build_log_box.append(self._log_widget)

        self._build_log_box.set_visible(False)

        log_close.connect("clicked", lambda _: self._toggle_build_log(False))

        paned.set_end_child(self._build_log_box)
        paned.set_resize_end_child(True)
        paned.set_shrink_end_child(False)
        self._projects_paned = paned

        return paned

    def _toggle_build_log(self, show):
        """Show or hide the build log panel."""
        self._build_log_box.set_visible(show)
        # Sync toggle button without triggering callback
        if self._log_toggle.get_active() != show:
            self._log_toggle.set_active(show)
        if show:
            h = self._projects_paned.get_allocated_height()
            self._projects_paned.set_position(h // 2)

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

        apk = find_apk(proj)
        if apk:
            mb = os.path.getsize(apk) / (1024 * 1024)
            sub.append(Gtk.Label(label=f"{mb:.0f} MB", css_classes=["dim-label", "caption"]))

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

        scan_btn = Gtk.Button(icon_name="security-medium-symbolic",
                              tooltip_text="Health check", css_classes=["flat"])
        scan_btn.connect("clicked", lambda _, p=proj: self._on_scan(p))
        actions.append(scan_btn)

        menu = Gio.Menu()
        proj_id = proj["name"].replace(" ", "_")

        has_upload = proj.get("upload", {}).get("host") or self.cfg.get("upload", {}).get("host")
        if has_upload:
            menu.append("Upload to Server", f"win.upload-{proj_id}")

        if "android" in proj.get("targets", []):
            menu.append("Push APK to Device", f"win.push-{proj_id}")

        open_section = Gio.Menu()
        open_section.append("Open in Unity", f"win.open-unity-{proj_id}")
        open_section.append("Open Build Folder", f"win.folder-{proj_id}")
        open_section.append("Open Project Folder", f"win.proj-folder-{proj_id}")
        menu.append_section(None, open_section)

        test_section = Gio.Menu()
        test_section.append("Run EditMode Tests", f"win.test-edit-{proj_id}")
        test_section.append("Run PlayMode Tests", f"win.test-play-{proj_id}")
        test_section.append("Select Tests…", f"win.test-dialog-{proj_id}")
        menu.append_section(None, test_section)

        clean_section = Gio.Menu()
        clean_section.append("Clear Build Cache", f"win.clear-cache-{proj_id}")
        clean_section.append("Clean Build (delete Library)", f"win.clean-{proj_id}")
        menu.append_section(None, clean_section)

        action_list = [
            (f"upload-{proj_id}", lambda *_, p=proj: self._on_upload(p)),
            (f"push-{proj_id}", lambda *_, p=proj: self._on_push_to_device(p)),
            (f"open-unity-{proj_id}", lambda *_, p=proj: self._open_in_unity(p)),
            (f"folder-{proj_id}", lambda *_, p=proj: subprocess.Popen(
                ["xdg-open", p.get("build_dir") or p["path"]])),
            (f"proj-folder-{proj_id}", lambda *_, p=proj: subprocess.Popen(
                ["xdg-open", p["path"]])),
            (f"test-edit-{proj_id}", lambda *_, p=proj: self._on_run_tests(p, "EditMode")),
            (f"test-play-{proj_id}", lambda *_, p=proj: self._on_run_tests(p, "PlayMode")),
            (f"test-dialog-{proj_id}", lambda *_, p=proj: self._show_test_dialog(p)),
            (f"clear-cache-{proj_id}", lambda *_, p=proj: self._on_clear_cache(p)),
            (f"clean-{proj_id}", lambda *_, p=proj: self._on_clean_build(p)),
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

    @staticmethod
    def _get_tag(s):
        s = s.strip()
        sl = s.lower()
        if "error" in sl or "FAILED" in s or "unable" in sl or "exception" in sl: return "error"
        if "warning" in sl or "please" in sl: return "warning"
        if "Done!" in s or "[Build] OK" in s: return "success"
        if s.startswith("[Stage]") or any(s.startswith(p[1] or "") for p in STAGE_PATTERNS if p[1]): return "stage"
        return None

    def _log(self, t):
        self._log_widget.append_line(t)

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
        self._log_widget.clear()
        self._toggle_build_log(True)
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
        GLib.timeout_add(2000, self._build_cards)

        if ok and self._build_queue:
            p, t = self._build_queue.pop(0)
            self._start(p, t)

    def _on_deploy(self, proj):
        apk = find_apk(proj)
        if not apk: return
        dash = self.cfg.get("apk_dash", "")
        if not dash or not os.path.exists(dash): return
        env = {k: v for k, v in os.environ.items()
               if not k.startswith(("GTK_", "GDK_", "GIO_", "DBUS_SESSION_BUS_PID"))}
        env["GTK_A11Y"] = "none"
        subprocess.Popen(["python3", dash, apk], env=env,
                         start_new_session=True,
                         stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL)

    def _on_push_to_device(self, proj):
        apk = find_apk(proj)
        if not apk:
            self._log("No APK found.\n")
            return
        self._log(f"Pushing {os.path.basename(apk)} to device /sdcard/Download/...\n")
        self.stage_label.set_text("Pushing to device...")
        self.progress_bar.pulse()
        def do_push():
            try:
                r = subprocess.run(
                    ["adb", "push", apk, f"/sdcard/Download/{os.path.basename(apk)}"],
                    capture_output=True, text=True, timeout=120)
                if r.returncode == 0:
                    GLib.idle_add(self._log, f"Pushed to /sdcard/Download/\n")
                    GLib.idle_add(self.stage_label.set_text, "Push complete")
                else:
                    GLib.idle_add(self._log, f"Push failed: {r.stderr.strip()}\n")
                    GLib.idle_add(self.stage_label.set_text, "Push failed")
            except Exception as e:
                GLib.idle_add(self._log, f"Push error: {e}\n")
            GLib.idle_add(self.progress_bar.set_fraction, 0)
        threading.Thread(target=do_push, daemon=True).start()

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

    def _on_cancel(self, _):
        self._build_queue = []
        if self.worker:
            self.worker.cancel()
            self.worker._restore_adb()
            self._set_building(False)
        if hasattr(self, '_test_proc') and self._test_proc:
            try:
                os.killpg(os.getpgid(self._test_proc.pid), 9)
            except ProcessLookupError:
                pass
            self._test_proc = None
            self._stop_test_timer()
            self.cancel_btn.set_sensitive(False)
        self.stage_label.set_text("Cancelled")
        self.progress_bar.set_fraction(0)
        self._log("\n  Cancelled by user.\n")

    def _get_known_fixtures(self, proj):
        """Extract test fixture names from previous XML results."""
        fixtures = {}
        for plat in ("editmode", "playmode"):
            xml_path = os.path.join(proj["path"], f"test-results-{plat}.xml")
            if not os.path.isfile(xml_path):
                continue
            try:
                import xml.etree.ElementTree as ET
                tree = ET.parse(xml_path)
                for suite in tree.iter("test-suite"):
                    if suite.get("type") == "TestFixture":
                        name = suite.get("name", "")
                        total = int(suite.get("total", 0))
                        passed = int(suite.get("passed", 0))
                        failed = int(suite.get("failed", 0))
                        plat_name = "EditMode" if plat == "editmode" else "PlayMode"
                        fixtures[f"{plat_name}:{name}"] = {
                            "platform": plat_name, "name": name,
                            "total": total, "passed": passed, "failed": failed
                        }
            except: pass
        return fixtures

    def _show_test_dialog(self, proj):
        """Show dialog to select test groups and platform."""
        fixtures = self._get_known_fixtures(proj)

        dlg = Adw.Dialog()
        dlg.set_title(f"{proj['name']} — Run Tests")
        dlg.set_content_width(420)
        dlg.set_content_height(560)

        tb = Adw.ToolbarView()
        tb.add_top_bar(Adw.HeaderBar())

        page = Adw.PreferencesPage()

        # Platform selection
        plat_group = Adw.PreferencesGroup(title="Platform")
        plat_row = Adw.ComboRow(title="Test Platform")
        plat_model = Gtk.StringList.new(["EditMode", "PlayMode"])
        plat_row.set_model(plat_model)
        plat_row.set_selected(1)
        plat_group.add(plat_row)
        page.add(plat_group)

        # Filter
        filter_group = Adw.PreferencesGroup(title="Filter",
            description="Leave empty to run all, or select fixtures below")
        filter_entry = Adw.EntryRow(title="Test filter (class or method name)")
        filter_group.add(filter_entry)
        page.add(filter_group)

        # Known fixtures — dynamic by platform
        checks = {}  # key → SwitchRow
        fixtures_group = Adw.PreferencesGroup(title="Fixtures (last run)")
        select_all_row = Adw.ActionRow(title="Select All / Deselect All")
        sel_btn = Gtk.Button(label="All", css_classes=["flat"], valign=Gtk.Align.CENTER)
        desel_btn = Gtk.Button(label="None", css_classes=["flat"], valign=Gtk.Align.CENTER)
        select_all_row.add_suffix(sel_btn)
        select_all_row.add_suffix(desel_btn)
        fixtures_group.add(select_all_row)
        page.add(fixtures_group)

        def rebuild_fixtures(*_):
            """Rebuild fixture switches when platform changes."""
            plat_name = plat_model.get_string(plat_row.get_selected())
            # Remove old switches (keep select_all_row)
            for key in list(checks.keys()):
                fixtures_group.remove(checks[key])
            checks.clear()

            items = {k: v for k, v in fixtures.items() if v["platform"] == plat_name}
            if items:
                for key, info in items.items():
                    status = f"{info['passed']}/{info['total']}"
                    if info["failed"]:
                        status += f"  ({info['failed']} failed)"
                    row = Adw.SwitchRow(title=info["name"], subtitle=status, active=True)
                    fixtures_group.add(row)
                    checks[key] = row
                fixtures_group.set_description("")
            else:
                fixtures_group.set_description(
                    f"No {plat_name} results yet — run once to see fixtures")

        def set_all(active):
            for row in checks.values():
                row.set_active(active)

        sel_btn.connect("clicked", lambda _: set_all(True))
        desel_btn.connect("clicked", lambda _: set_all(False))
        plat_row.connect("notify::selected", rebuild_fixtures)
        rebuild_fixtures()  # initial

        # Run button
        run_btn = Gtk.Button(label="Run Tests", css_classes=["suggested-action", "pill"],
                             halign=Gtk.Align.CENTER)
        run_btn.set_margin_top(12)
        run_btn.set_margin_bottom(12)

        def on_run(_):
            platform = plat_model.get_string(plat_row.get_selected())
            filter_text = filter_entry.get_text().strip()
            if not filter_text:
                active = [v["name"] for k, v in checks.items() if checks[k].get_active()]
                total = len(checks)
                # If all selected or none selected → run all (no filter)
                if active and len(active) < total:
                    filter_text = "|".join(active)
            dlg.close()
            self._on_run_tests(proj, platform, test_filter=filter_text or None)

        run_btn.connect("clicked", on_run)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        box.append(page)
        box.append(run_btn)

        scroll = Gtk.ScrolledWindow(vexpand=True)
        scroll.set_child(box)
        tb.set_content(scroll)
        dlg.set_child(tb)
        dlg.present(self)

    def _on_run_tests(self, proj, platform, test_filter=None):
        """Run Unity tests in batchmode and parse NUnit XML results."""
        unity = get_unity_for_project(self.cfg, proj)
        if not unity or not os.path.isfile(unity):
            self._log("Unity editor not found. Check Settings.\n")
            return

        self._log_widget.clear()
        self._toggle_build_log(True)
        self.stage_label.set_text(f"Running {platform} tests...")
        self.progress_bar.pulse()
        self._test_proc = None
        self.cancel_btn.set_sensitive(True)

        # Check if Unity Editor is open on this project
        import subprocess as _sp
        try:
            ps = _sp.run(["pgrep", "-af", "Unity.*" + proj["path"].replace("/", ".")[-20:]],
                         capture_output=True, text=True, timeout=3)
            for line in ps.stdout.splitlines():
                if "-batchmode" not in line and "Unity" in line and proj["path"][-15:] in line:
                    self._log("Warning: Unity Editor may be open for this project\n")
                    break
        except: pass

        # Kill any stale batchmode processes for this project
        try:
            ps = _sp.run(["pgrep", "-f", f"Unity.*-runTests.*{proj['path']}"],
                         capture_output=True, text=True, timeout=3)
            for pid in ps.stdout.strip().splitlines():
                pid = pid.strip()
                if pid.isdigit():
                    os.kill(int(pid), 9)
                    self._log(f"Killed stale test process (PID {pid})\n")
        except: pass
        self.status.set_text(f"{proj['name']} / {platform} Tests")

        # Timer + ETA
        import time as _time
        self._test_start = _time.time()
        test_key = f"{proj['name']}_test_{platform}"
        prev_duration = load_history().get(test_key)

        def tick_test():
            if not hasattr(self, '_test_start') or self._test_start is None:
                return False
            elapsed = int(_time.time() - self._test_start)
            m, s = divmod(elapsed, 60)
            if prev_duration and prev_duration > elapsed:
                rm, rs = divmod(prev_duration - elapsed, 60)
                self.elapsed_label.set_text(f"{m}:{s:02d}  ~{rm}:{rs:02d} left")
            else:
                self.elapsed_label.set_text(f"{m}:{s:02d}")
            return True

        self.spinner.start()
        self._test_timer_id = GLib.timeout_add(1000, tick_test)

        results_xml = os.path.join(proj["path"], f"test-results-{platform.lower()}.xml")
        # Remove old results
        if os.path.exists(results_xml):
            os.remove(results_xml)

        filter_label = f" ({test_filter})" if test_filter else ""
        self.stage_label.set_text(f"Running {platform} tests{filter_label}...")

        cmd = [unity, "-batchmode",
               "-disable-assembly-updater",
               "-accept-apiupdate",
               "-DisableDirectConnection",
               "-skipMissingProjectID",
               "-skipMissingUPID",
               "-projectPath", proj["path"],
               "-runTests",
               "-testPlatform", platform,
               "-testResults", results_xml,
               "-logFile", "-"]
        if test_filter:
            cmd.extend(["-testFilter", test_filter])
        # EditMode doesn't need graphics; PlayMode needs GPU for scene rendering
        if platform == "EditMode":
            cmd.insert(2, "-nographics")

        # Kill adb server to avoid conflicts
        try: subprocess.run(["adb", "kill-server"], timeout=3, capture_output=True)
        except: pass

        lock = os.path.join(proj["path"], "Temp", "UnityLockfile")
        if os.path.exists(lock):
            os.remove(lock)

        # Hide ADB to skip device scan (like build worker)
        unity_adb = os.path.join(os.path.dirname(unity),
            "Data/PlaybackEngines/AndroidPlayer/SDK/platform-tools/adb")
        adb_hidden = unity_adb + ".disabled"
        adb_was_hidden = False
        if proj.get("hide_adb") and os.path.exists(unity_adb):
            try:
                os.rename(unity_adb, adb_hidden)
                adb_was_hidden = True
            except: pass

        def restore_adb():
            if adb_was_hidden and not os.path.exists(unity_adb) and os.path.exists(adb_hidden):
                try: os.rename(adb_hidden, unity_adb)
                except: pass

        # Log file
        import datetime as _dt
        logs_dir = os.path.join(APP_DIR, "logs")
        os.makedirs(logs_dir, exist_ok=True)
        ts = _dt.datetime.now().strftime("%Y%m%d_%H%M%S")
        log_path = os.path.join(logs_dir, f"{proj['name']}_{ts}_test_{platform}.log")

        def run_tests():
            full_log = []
            try:
                proc = subprocess.Popen(
                    cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    text=True, bufsize=1, preexec_fn=os.setsid)
                self._test_proc = proc
                compiler_error = False
                test_done = 0
                test_completed = False
                last_line = ""
                repeat_count = 0
                screenshots = []
                for line in proc.stdout:
                    full_log.append(line)
                    s = line.strip()

                    # Suppress repeated lines (spam)
                    if s == last_line and s:
                        repeat_count += 1
                        if repeat_count == 3:
                            GLib.idle_add(self._log, f"  ... (repeating, suppressed)\n")
                        if repeat_count >= 3:
                            continue
                    else:
                        repeat_count = 0
                        last_line = s

                    # Detect fatal crash / corrupted library
                    if "Caught fatal signal" in s or "corrupted" in s.lower() or "Fatal Error" in s:
                        if not compiler_error:
                            compiler_error = True
                            if "corrupted" in s.lower():
                                GLib.idle_add(self._log, "\n  Library corrupted — use Clean Build\n")
                                GLib.idle_add(self.stage_label.set_text, "Library corrupted")
                            else:
                                GLib.idle_add(self._log, "\n  Unity crashed (fatal signal)\n")
                                GLib.idle_add(self.stage_label.set_text, "Unity crashed")
                            GLib.idle_add(self.progress_bar.set_fraction, 0)
                            try: os.killpg(os.getpgid(proc.pid), 9)
                            except: pass
                            break

                    # Detect Unity lockfile / editor open
                    if "Failed to write file" in s and "EditorUserBuildSettings" in s:
                        if repeat_count == 0:
                            GLib.idle_add(self._log, "\n  Unity Editor is open — close it and retry\n")
                            GLib.idle_add(self.stage_label.set_text, "Unity Editor is open")
                            compiler_error = True
                            try: os.killpg(os.getpgid(proc.pid), 9)
                            except: pass
                            break

                    GLib.idle_add(self._log, line)
                    # Collect screenshots from TestScreenshot.Capture
                    if "[Screenshot]" in s:
                        path = s.split("[Screenshot]")[-1].strip()
                        if os.path.isfile(path):
                            screenshots.append(path)
                    if "compiler errors" in s.lower() or "Aborting batchmode" in s:
                        compiler_error = True
                        GLib.idle_add(self._log, "\n  Compiler errors — aborting\n")
                        GLib.idle_add(self.stage_label.set_text, "Compiler errors")
                        GLib.idle_add(self.progress_bar.set_fraction, 0)
                        try: os.killpg(os.getpgid(proc.pid), 9)
                        except: pass
                        break
                    # Count completed tests from NUnit output
                    if any(k in s for k in ("Passed", "Failed", "Skipped", "##utp")):
                        test_done += 1
                        GLib.idle_add(self.stage_label.set_text,
                                      f"Running {platform} tests... ({test_done} done)")
                    # Detect test completion — don't wait for Unity to fully exit
                    if "Test run completed" in s:
                        test_completed = True
                        # Save log and show results immediately
                        try:
                            with open(log_path, "w") as f:
                                f.writelines(full_log)
                        except: pass
                        GLib.idle_add(self._stop_test_timer)
                        GLib.idle_add(self.cancel_btn.set_sensitive, False)
                        GLib.idle_add(self._parse_test_results, proj, platform, results_xml, 0, screenshots)
                        # Let Unity finish in background
                        break

                # Wait for process to actually exit
                proc.wait()
                self._test_proc = None
                restore_adb()

                # Save final log
                try:
                    with open(log_path, "w") as f:
                        f.writelines(full_log)
                except: pass

                if not test_completed:
                    GLib.idle_add(self._stop_test_timer)
                    GLib.idle_add(self.cancel_btn.set_sensitive, False)
                    if compiler_error:
                        GLib.idle_add(self._log, "\n  Tests aborted: compiler errors in project\n")
                        GLib.idle_add(self.stage_label.set_text, "Tests failed: compiler errors")
                        GLib.idle_add(self.progress_bar.set_fraction, 0)
                        GLib.idle_add(self.status.set_text, f"{proj['name']} — compiler errors")
                    else:
                        GLib.idle_add(self._parse_test_results, proj, platform, results_xml, proc.returncode, screenshots)
            except Exception as e:
                restore_adb()
                GLib.idle_add(self._stop_test_timer)
                GLib.idle_add(self._log, f"Error: {e}\n")
                GLib.idle_add(self.stage_label.set_text, "Test error")
                GLib.idle_add(self.progress_bar.set_fraction, 0)

        threading.Thread(target=run_tests, daemon=True).start()

    def _stop_test_timer(self):
        if hasattr(self, '_test_timer_id') and self._test_timer_id:
            GLib.source_remove(self._test_timer_id)
            self._test_timer_id = None
        self._test_start = None
        self.spinner.stop()
        self.elapsed_label.set_text("")

    def _parse_test_results(self, proj, platform, xml_path, exit_code, screenshots=None):
        """Parse NUnit XML test results and display summary."""
        self.progress_bar.set_fraction(1.0 if exit_code == 0 else 0)

        if not os.path.isfile(xml_path):
            self._log(f"\nNo results file generated (exit code {exit_code})\n")
            self.stage_label.set_text("Tests failed (no results)")
            self.progress_bar.set_fraction(0)
            self.status.set_text(f"{proj['name']} — no test results")
            return

        import xml.etree.ElementTree as ET
        try:
            tree = ET.parse(xml_path)
            root = tree.getroot()

            # NUnit3 format
            total = int(root.get("total", 0))
            passed = int(root.get("passed", 0))
            failed = int(root.get("failed", 0))
            skipped = int(root.get("skipped", 0))
            xml_duration = float(root.get("duration", 0))
            # Real elapsed time (includes Unity startup/compile)
            import time as _time
            if hasattr(self, '_test_start') and self._test_start:
                duration = int(_time.time() - self._test_start)
            else:
                duration = int(xml_duration)

            self._log(f"\n{'='*50}\n")
            self._log(f"  {platform} Test Results: {passed} passed")
            if failed:
                self._log(f", {failed} FAILED")
            if skipped:
                self._log(f", {skipped} skipped")
            dm, ds = divmod(duration, 60)
            self._log(f"  ({total} total, {dm}:{ds:02d} total, tests {xml_duration:.1f}s)\n")
            self._log(f"{'='*50}\n\n")

            # Collect all test case results
            test_cases = []
            for tc in root.iter("test-case"):
                name = tc.get("fullname", tc.get("name", "?"))
                result = tc.get("result", "?")
                tc_dur = tc.get("duration", "0")
                entry = {"name": name, "result": result, "duration": tc_dur}
                if result == "Failed":
                    failure = tc.find("failure")
                    if failure is not None:
                        msg_el = failure.find("message")
                        if msg_el is not None and msg_el.text:
                            entry["message"] = msg_el.text.strip().split("\n")[0]
                test_cases.append(entry)

            # Log failed tests
            failed_cases = [t for t in test_cases if t["result"] == "Failed"]
            if failed_cases:
                for t in failed_cases:
                    self._log(f"  FAIL: {t['name']}\n")
                    if t.get("message"):
                        self._log(f"        {t['message']}\n")
                self._log("\n")

            status = f"{passed}/{total} passed" if not failed else f"{failed} FAILED"
            self.stage_label.set_text(f"{platform}: {status}")
            dm, ds = divmod(duration, 60)
            self.status.set_text(f"{proj['name']} — {status} ({dm}:{ds:02d})")

            # Save to history (with test case details)
            save_test_entry(proj["name"], platform, passed, failed, skipped, total, duration,
                            test_cases=test_cases)
            # Save duration for ETA
            h = load_history()
            h[f"{proj['name']}_test_{platform}"] = int(duration)
            save_history(h)

            # Show screenshots gallery
            if screenshots:
                self._log(f"\n  Screenshots ({len(screenshots)}):\n")
                for sp in screenshots:
                    self._log(f"    {os.path.basename(sp)}\n")
                show_screenshots(self, screenshots, proj["name"], platform)

        except ET.ParseError as e:
            self._log(f"Failed to parse results: {e}\n")
            self.stage_label.set_text("Parse error")

    def _on_clear_cache(self, proj):
        """Delete only Bee/artifacts — fixes most compilation cache issues."""
        import shutil
        path = proj["path"]
        targets = [
            os.path.join(path, "Library", "Bee", "artifacts"),
            os.path.join(path, "Library", "ScriptAssemblies"),
        ]
        deleted = []
        for p in targets:
            if os.path.isdir(p):
                shutil.rmtree(p, ignore_errors=True)
                deleted.append(os.path.relpath(p, path))
        # Also remove lockfile
        lock = os.path.join(path, "Temp", "UnityLockfile")
        if os.path.exists(lock):
            try:
                os.remove(lock)
                deleted.append("Temp/UnityLockfile")
            except: pass
        if deleted:
            self._log(f"Cache cleared: {', '.join(deleted)}\n")
        else:
            self._log("Cache already clean\n")

    def _on_clean_build(self, proj):
        """Delete Library and Bee folders for a clean rebuild."""
        import shutil
        dlg = Adw.AlertDialog()
        dlg.set_heading(f"Clean {proj['name']}?")
        dlg.set_body("This will delete Library/ and Bee/ folders.\nNext build will be significantly slower.")
        dlg.add_response("cancel", "Cancel")
        dlg.add_response("ok", "Clean")
        dlg.set_response_appearance("ok", Adw.ResponseAppearance.DESTRUCTIVE)
        def on_resp(d, resp):
            if resp != "ok": return
            path = proj["path"]
            deleted = []
            for folder in ["Library", "Bee"]:
                p = os.path.join(path, folder)
                if os.path.isdir(p):
                    shutil.rmtree(p, ignore_errors=True)
                    deleted.append(folder)
            if deleted:
                self._log(f"Cleaned: {', '.join(deleted)}\n")
            else:
                self._log("Nothing to clean\n")
        dlg.connect("response", on_resp)
        dlg.present(self)

    def _apply_config(self, cfg):
        self.cfg = cfg
        self._build_cards()
        self.empty.set_visible(not cfg.get("projects"))