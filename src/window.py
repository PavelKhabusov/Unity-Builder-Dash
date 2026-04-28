"""Main application window with sidebar navigation."""
import os, subprocess, datetime, time, threading
from gi.repository import Gtk, Adw, GLib, Gio, Gdk
from .constants import APP_NAME, TARGET_INFO, STAGE_PATTERNS, SKIP_PATTERNS
from .config import (load_config, load_history, save_history, save_build_entry,
                     load_builds_log, find_apk, get_version, get_build_number,
                     get_unity_for_project, upload_apk, save_test_entry, APP_DIR)
from .worker import BuildWorker
from .settings_page import SettingsPage
from .history_page import HistoryPage
from .devices import DevicesPage
from .dialogs import show_scan, show_screenshots, show_ios_popup
from .log_view import LogView
from .profiler import ProfilerPage
from . import ios_remote
from .config import save_config


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

        self.scripts_only_toggle = Gtk.ToggleButton(icon_name="media-seek-forward-symbolic",
                                                    tooltip_text="Scripts Only — fast rebuild of C# without full IL2CPP/gradle/Xcode")
        self.scripts_only_toggle.set_active(cfg.get("scripts_only", False))

        self._content_header.pack_start(build_all)
        self._content_header.pack_start(self.cancel_btn)
        self._content_header.pack_start(self.increment_toggle)
        self._content_header.pack_start(self.scripts_only_toggle)

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
        self._devices_page = DevicesPage(cfg)
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

        # Start iOS progress listener early if Mac is configured — so even
        # actions triggered from the Devices page (before the popup opens)
        # have a live TCP:8080 server ready to receive Mac-side logs.
        ios_cfg = ios_remote.get_remote_cfg(cfg)
        if ios_cfg.get("mac_ip"):
            def _ios_progress_log(t):
                # Auto-reveal the log panel on any incoming Mac-side byte so
                # streamed output is never silently hidden.
                if not self._build_log_box.get_visible():
                    self._toggle_build_log(True)
                self._log(t)
            def _ios_progress_bulk(lines):
                # Bulk path: hand the whole flush to LogView so it can insert
                # under a single user-action (one GTK layout pass per flush).
                # Per-line _log would re-layout thousands of times per build.
                # Only toggle the panel if it's hidden — idempotent set_visible
                # is cheap but set_position re-lays out the Paned every time.
                if not self._build_log_box.get_visible():
                    self._toggle_build_log(True)
                self._log_widget.append_lines(lines)
                self._scan_for_alerts(lines)
            self._ios_progress_listener = ios_remote.ProgressListener(
                ios_cfg.get("progress_port", 8080),
                log_cb=_ios_progress_log,
                log_bulk_cb=_ios_progress_bulk,
                progress_cb=self.progress_bar.set_fraction)
            self._ios_progress_listener.start()

        # Stop listener (close socket) when the window closes so the port
        # frees immediately on quit — no orphan TCP:8080 across restarts.
        self.connect("close-request", self._on_window_close)

    def _on_window_close(self, *_a):
        # Stop TCP listener so port 8080 frees up immediately
        lst = getattr(self, "_ios_progress_listener", None)
        if lst:
            try: lst.stop()
            except Exception: pass
        # Kill any in-flight remote SSH subprocess so it doesn't become
        # a zombie after the window closes.
        runner = getattr(self, "_ios_runner", None)
        if runner:
            try: runner.stop()
            except Exception: pass
        # Cancel any Unity BuildWorker too (subprocess group kill)
        w = getattr(self, "worker", None)
        if w:
            try: w.cancel()
            except Exception: pass
        # Force the Gtk.Application loop to exit. Without this, a hung
        # subprocess pipe or an open Adw.Dialog can keep the interpreter
        # alive even though the main window is gone.
        app = self.get_application()
        if app:
            try: app.quit()
            except Exception: pass
        return False  # allow close
        return False  # allow close

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
        self.scripts_only_toggle.set_visible(is_projects)
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
            extra_end=[log_close],
            exclude_patterns=self.cfg.get("log_filters", []))
        self._build_log_box.append(self._log_widget)

        self._build_log_box.set_visible(False)

        log_close.connect("clicked", lambda _: self._toggle_build_log(False))

        paned.set_end_child(self._build_log_box)
        paned.set_resize_end_child(True)
        paned.set_shrink_end_child(False)
        self._projects_paned = paned

        return paned

    def _toggle_build_log(self, show):
        """Show or hide the build log panel. Idempotent — skips work if the
        panel is already in the requested state so high-frequency callers
        (log-stream batches) don't re-lay out the Paned 20x/second."""
        if self._build_log_box.get_visible() == show:
            if self._log_toggle.get_active() != show:
                self._log_toggle.set_active(show)
            return
        self._build_log_box.set_visible(show)
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

        ver = Gtk.Label(label=get_version(proj["path"], proj.get("targets")), css_classes=["caption"])
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
            if t_key == "ios":
                btn.connect("clicked", lambda _, p=proj: self._show_ios_popup(p))
                # Right-click → quick build variants (4 combinations of
                # increment/scripts-only) that bypass the remote-pipeline popup.
                gesture = Gtk.GestureClick()
                gesture.set_button(Gdk.BUTTON_SECONDARY)
                gesture.connect("pressed",
                    lambda _g, _n, _x, _y, b=btn, p=proj: self._show_ios_quick_menu(b, p))
                btn.add_controller(gesture)
            else:
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
        test_section.append("Select Tests…", f"win.test-pick-{proj_id}")
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
            (f"test-pick-{proj_id}", lambda *_, p=proj: self._show_test_picker(p)),
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
        # Unzip and other verbose progress output — always neutral.
        if s.startswith(("inflating:", "extracting:", "creating:", " extracting:",
                         "  inflating:", "  extracting:")):
            return None
        # Require real error markers, not random substrings in paths/identifiers.
        if ("error:" in sl or ": error:" in sl or "error cs" in sl
                or sl.startswith("error ") or sl.startswith("error:")
                or "FAILED" in s or " unable to " in sl
                or "exception:" in sl or " exception " in sl):
            return "error"
        if ("warning:" in sl or ": warning:" in sl or "warning cs" in sl
                or sl.startswith("warning ") or sl.startswith("warning:")):
            return "warning"
        if "Done!" in s or "[Build] OK" in s: return "success"
        if s.startswith("[Stage]") or any(s.startswith(p[1] or "") for p in STAGE_PATTERNS if p[1]): return "stage"
        return None

    def _log(self, t):
        self._log_widget.append_line(t)
        self._scan_for_alerts((t,))

    def _on_stage(self, text, frac):
        if text: self.stage_label.set_text(text)
        if frac >= 0: self.progress_bar.set_fraction(frac)
        elif text: self.progress_bar.pulse()

    # Positive signals that xcodebuild has moved past Run Destination
    # Preflight — i.e. the device got unlocked and the build resumed.
    # All four are observed in real iOS-test runs right after the user
    # unlocks the phone (see commit context for the captured xcodebuild log):
    #   - "DVTDevice: Error locating DeviceSupport"  xcodebuild side: device handshake done
    #   - "Initialize engine version"                Unity engine starting on device
    #   - "UIApplicationMain"                        host process main() running
    #   - "Test Suite '"                              xctest runner began
    # Any one of them is sufficient — we only check while a lock alert is
    # active, so they can't false-trigger when there's nothing to clear.
    _UNLOCK_RESUME_SIGNS = (
        "DVTDevice: Error locating",
        "Initialize engine version",
        "UIApplicationMain",
        "Test Suite '",
    )

    def _scan_for_alerts(self, lines):
        """Watch Mac-side log output for operator-attention prompts and
        surface them as stage-label + notify-send. Currently handles
        xcodebuild's "Unlock <device> to Continue" (Error Domain
        com.apple.dt.deviceprep Code=-3): the build hangs on Run Destination
        Preflight until the phone is unlocked, and the user won't see it
        unless the log panel is open.

        Auto-clears event-driven: once a line matching any
        _UNLOCK_RESUME_SIGNS pattern shows up after we surfaced the alert,
        the build has clearly resumed → restore the prior stage label and
        fire a follow-up "unlocked" notification. No timers — we react to
        the actual log stream the way xcodebuild signals progress."""
        now = time.time()
        locked_dev = None
        saw_resume = False
        for ln in lines:
            if "Unlock " in ln and " to Continue" in ln:
                try:
                    locked_dev = ln.split("Unlock ", 1)[1].split(" to Continue", 1)[0].strip()
                except Exception:
                    locked_dev = "device"
            elif any(s in ln for s in self._UNLOCK_RESUME_SIGNS):
                saw_resume = True

        if locked_dev:
            last_notify = getattr(self, "_lock_notified_at", 0)
            # First lock event in this build OR >30s since last → fire UI +
            # system notification. Repeated prompts inside the same lock
            # event are silently absorbed.
            if now - last_notify >= 30:
                self._lock_notified_at = now
                self._locked_device = locked_dev
                self._pre_lock_stage = self.stage_label.get_text() or ""
                self.stage_label.set_text(f"⚠ Unlock {locked_dev} to continue")
                try:
                    subprocess.Popen(
                        ["notify-send", "-u", "critical", "-i", "dialog-warning",
                         APP_NAME, f"Unlock {locked_dev} — Xcode is waiting"])
                except Exception:
                    pass
            return

        # No "Unlock" in this batch. If we have an active alert AND we saw
        # any resume signal, the device got unlocked — clear the banner.
        if saw_resume and getattr(self, "_locked_device", None):
            self._clear_lock_alert()

    def _clear_lock_alert(self):
        """Clear the ⚠ device-locked banner. Triggered when log output shows
        the build has moved past Run Destination Preflight."""
        dev = getattr(self, "_locked_device", None)
        if not dev:
            return
        self._locked_device = None
        self._lock_notified_at = 0  # let a fresh lock event notify immediately
        # Only restore prior stage if our warning is still on screen — if
        # build code already updated stage_label, don't clobber it.
        cur = self.stage_label.get_text() or ""
        if cur.startswith("⚠ Unlock"):
            prior = getattr(self, "_pre_lock_stage", "") or "Resumed"
            self.stage_label.set_text(prior)
        try:
            subprocess.Popen(
                ["notify-send", "-u", "low", "-i", "dialog-ok-apply",
                 APP_NAME, f"{dev} unlocked — build resumed"])
        except Exception:
            pass

    # ── Actions ──

    def _on_build(self, proj, target_key):
        self._build_queue = []
        self._start(proj, target_key)

    # ── iOS remote pipeline (CrazyMegaBuilder integration) ──

    def _show_ios_quick_menu(self, anchor, proj):
        """Right-click on iOS button → same 4 build actions as the popup's Build row.
        Device defaults to first in cfg; open the popup if a different device is needed."""
        devices = ios_remote.get_devices(self.cfg) or [("iPhone 12 mini", "iPhone 12 mini")]
        default_device = devices[0][1]
        popover = Gtk.Popover()
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, margin_top=4,
                      margin_bottom=4, margin_start=4, margin_end=4)
        variants = [
            ("Full",     "full",          "Unity build → zip → scp → unpack (pods+widget) → build on Mac"),
            ("Xcode",    "xcode",         "Skip Unity: unpack (pods+widget) + build on Mac with existing zip"),
            ("Build",    "build_only",    "Just xcodebuild on existing iOS/ — no unpack, no pods, no widget"),
            ("No Xcode", "without_xcode", "Unity build → zip → scp → unpack only (no build)"),
        ]
        for label, action_id, tip in variants:
            lbl = Gtk.Label(label=label, xalign=0)
            b = Gtk.Button(child=lbl, css_classes=["flat"], tooltip_text=tip)
            b.connect("clicked",
                lambda _, p=proj, a=action_id, d=default_device, pop=popover: (
                    pop.popdown(), self._on_ios_action(p, a, d)))
            box.append(b)
        popover.set_child(box)
        popover.set_parent(anchor)
        popover.set_position(Gtk.PositionType.BOTTOM)
        popover.popup()

    def _show_ios_popup(self, proj):
        if not hasattr(self, "_ios_progress_listener"):
            port = ios_remote.get_remote_cfg(self.cfg).get("progress_port", 8080)
            def _bulk(lines):
                self._toggle_build_log(True)
                self._log_widget.append_lines(lines)
                self._scan_for_alerts(lines)
            self._ios_progress_listener = ios_remote.ProgressListener(
                port, log_cb=self._log,
                log_bulk_cb=_bulk,
                progress_cb=self.progress_bar.set_fraction)
            self._ios_progress_listener.start()

        # Auto-show the log panel whenever the popup writes something so users
        # see connection/SCP/SSH output without manually expanding the panel.
        def log_with_panel(t):
            self._toggle_build_log(True)
            self._log(t)

        show_ios_popup(
            self, proj, self.cfg,
            on_action=lambda aid, dt, p=proj: self._on_ios_action(p, aid, dt),
            save_cfg=save_config,
            log_cb=log_with_panel,
            on_open_settings=self._open_settings_ios)

    def _open_settings_ios(self):
        """Switch the main window to Settings and select the iOS tab."""
        self._stack.set_visible_child_name("settings")
        if hasattr(self, "_settings_list"):
            first = self._settings_list.get_row_at_index(0)
            if first: self._settings_list.select_row(first)
        if hasattr(self, "_sidebar_list"):
            self._sidebar_list.unselect_all()
        if hasattr(self, "_settings_page"):
            self._settings_page.select_tab("ios")

    # action_id → (needs_unity, needs_zip, needs_scp, osa_arg_of_device, label)
    # For run/runFull/build_only the prefix (`run:` vs `install:`) is swapped
    # in _on_ios_action based on cfg.ios_remote.run_with_test.
    _IOS_ACTIONS = {
        "full":          (True,  True,  True,  lambda dev: f"{{PREFIX}}Full:{dev}", "Full build"),
        "xcode":         (False, False, False, lambda dev: f"{{PREFIX}}Full:{dev}", "Xcode build"),
        "build_only":    (False, False, False, lambda dev: f"{{PREFIX}}:{dev}",     "Build & install (no refresh)"),
        "without_xcode": (True,  True,  True,  lambda _: "unpack",                  "Build without Xcode"),
        "archive":       (False, True,  False, lambda _: None,                      "Pack zip"),
        "unpack":        (False, False, False, lambda _: "unpack",                  "Unpack on Mac"),
        "all":           (False, True,  True,  lambda _: "unpack",                  "Pack & unpack"),
        "stop":          (False, False, False, lambda _: "stop",                    "Stop"),
        "clear_cache":   (False, False, False, lambda _: "clearCache",              "Clear .pcm cache"),
        "add_widget":    (False, False, False, lambda _: "addWidget",               "Add widget"),
        "clear_build":   (False, False, False, lambda _: "clearBuild",              "Clean build"),
        "update_pod":    (False, False, False, lambda _: "updatePod",               "Update Pod"),
        "open_xcode":    (False, False, False, lambda _: "openXcode",               "Open in Xcode"),
    }

    def _on_ios_action(self, proj, action_id, device_target):
        spec = self._IOS_ACTIONS.get(action_id)
        if not spec:
            self._log(f"Unknown iOS action: {action_id}\n")
            return
        needs_unity, needs_zip, needs_scp, osa_fn, label = spec
        osa_arg = osa_fn(device_target)
        # Pick command prefix based on cfg.ios_remote.run_with_test toggle
        # (kebab menu checkbox). Default off = install-only via devicectl;
        # on = xcodebuild test (auto-launches app through xctest harness).
        if osa_arg and "{PREFIX}" in osa_arg:
            prefix = "run" if (self.cfg.get("ios_remote") or {}).get("run_with_test") else "install"
            osa_arg = osa_arg.replace("{PREFIX}", prefix)

        # Show what's running in the main status bar so it's visible after the
        # popup closes.
        dev_label = ""
        for lbl, name in ios_remote.get_devices(self.cfg):
            if name == device_target:
                dev_label = f" — {lbl}"
                break
        status_text = f"iOS: {label}{dev_label}"

        # Stop kills the currently running remote SSH (and cancels unity if any)
        if action_id == "stop":
            if self.worker:
                self.worker.cancel()
            runner = getattr(self, "_ios_runner", None)
            if runner:
                runner.stop()
            self.stage_label.set_text("iOS: stop")
            # Still send "stop" to Mac in case a Terminal job is running there
            self._ios_run_remote(osa_arg)
            return

        remote = ios_remote.get_remote_cfg(self.cfg)
        if (needs_scp or osa_arg) and not remote.get("mac_ip"):
            self._log("Mac IP is empty — set it in the iOS popup.\n")
            return

        self._toggle_build_log(True)
        self.status.set_text(f"{proj['name']}  {status_text}")
        self.stage_label.set_text(status_text)

        if needs_unity:
            # Run Unity iOS build first, then chain zip/scp/ssh on success
            self._start_ios_build(proj, needs_zip, needs_scp, osa_arg, status_text)
        else:
            # Pure post-build pipeline (runs off the UI thread)
            threading.Thread(
                target=self._ios_post_build,
                args=(proj, needs_zip, needs_scp, osa_arg, False, status_text),
                daemon=True).start()

    def _start_ios_build(self, proj, needs_zip, needs_scp, osa_arg, status_text):
        """Run Unity iOS build, then chain into zip/scp/ssh."""
        unity = get_unity_for_project(self.cfg, proj)
        if not unity or not os.path.isfile(unity):
            self._log("Unity editor not found. Check Settings.\n")
            return
        # Fully reset device-lock alert state for the new build.
        self._lock_notified_at = 0
        self._locked_device = None
        self._pre_lock_stage = ""
        self._log_widget.clear()
        self._set_building(True)
        self.cards[proj["name"]]["status"].set_text("Building...")
        self.progress_bar.set_fraction(0)
        self.stage_label.set_text(f"{status_text} — Unity")

        def after_unity(ok):
            if not ok:
                self._on_done(False)
                return
            # Keep "Building..." state through the remote phase
            threading.Thread(
                target=self._ios_post_build,
                args=(proj, needs_zip, needs_scp, osa_arg, True, status_text),
                daemon=True).start()

        self.worker = BuildWorker(
            self.cfg, proj, "ios",
            self._log, after_unity, self._on_stage,
            auto_increment=self.increment_toggle.get_active(),
            scripts_only=self.scripts_only_toggle.get_active(),
            log_bulk_cb=self._log_widget.append_lines)
        self.worker.start()

    def _ios_post_build(self, proj, needs_zip, needs_scp, osa_arg,
                        already_building=False, status_text="iOS remote"):
        """Zip → scp → ssh osascript. Runs on a background thread."""
        if not already_building:
            GLib.idle_add(self._set_building, True)
            GLib.idle_add(self.progress_bar.set_fraction, 0)
        GLib.idle_add(self.stage_label.set_text, status_text)

        done = (self._on_done if already_building else self._ios_cleanup)

        ok = True
        build_dir = proj.get("build_dir") or os.path.join(proj["path"], "Builds")

        if needs_zip:
            GLib.idle_add(self.stage_label.set_text, f"{status_text} — zipping")
            try:
                ios_remote.make_ios_zip(build_dir, log_cb=self._log)
            except Exception as e:
                GLib.idle_add(self._log, f"Zip failed: {e}\n")
                ok = False

        if ok and needs_scp:
            GLib.idle_add(self.stage_label.set_text, f"{status_text} — uploading")
            remote = ios_remote.get_remote_cfg(self.cfg)
            zip_path = os.path.join(build_dir, "iOS.zip")
            ok = ios_remote.scp_to_mac(zip_path, remote, log_cb=self._log)

        if ok and osa_arg is not None:
            GLib.idle_add(self.stage_label.set_text, f"{status_text} — Mac")
            # RemoteRunner already invokes done_cb via GLib.idle_add on the
            # main thread — passing `done` directly. An intermediate
            # `lambda rok: GLib.idle_add(done, rok)` double-schedules AND
            # (worse) returns the source-ID int → GLib treats the lambda as
            # a repeating idle source and re-fires it every tick, calling
            # _on_done in an infinite loop until AttributeError trips on
            # self.worker=None (cleared by the first successful call).
            self._ios_run_remote(osa_arg, on_remote_done=done)
            return  # done fires from RemoteRunner callback

        GLib.idle_add(done, ok)

    def _ios_run_remote(self, osa_arg, on_remote_done=None):
        remote = ios_remote.get_remote_cfg(self.cfg)
        def _bulk(lines):
            self._log_widget.append_lines(lines)
            self._scan_for_alerts(lines)
        runner = ios_remote.RemoteRunner(
            remote, log_cb=self._log,
            done_cb=on_remote_done,
            progress_cb=self.progress_bar.set_fraction,
            log_bulk_cb=_bulk)
        self._ios_runner = runner
        runner.run(osa_arg)

    def _ios_cleanup(self, ok):
        """UI reset after a pure-remote (non-Unity) iOS action."""
        self._set_building(False)
        self.stage_label.set_text("Done" if ok else "Failed")
        self.progress_bar.set_fraction(1.0 if ok else 0)

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
                                  auto_increment=self.increment_toggle.get_active(),
                                  scripts_only=self.scripts_only_toggle.get_active(),
                                  log_bulk_cb=self._log_widget.append_lines)
        self.worker.start()

    def _on_done(self, ok):
        try:
            name = self.worker.project["name"]
            proj = self.worker.project
            c = self.cards[name]
            c["status"].set_text("Done" if ok else "Failed")
            c["version"].set_text(get_version(proj["path"], proj.get("targets")))
            if c["deploy"]:
                c["deploy"].set_sensitive(find_apk(proj) is not None)

            duration = int(time.time() - self.worker.start_time) if self.worker.start_time else 0
            if ok and self.worker.start_time:
                h = load_history()
                h[f"{name}_{self.worker.target}"] = duration
                save_history(h)

            apk = find_apk(proj) if ok else None
            try:
                apk_size = os.path.getsize(apk) if apk and os.path.isfile(apk) else None
            except OSError:
                apk_size = None
            save_build_entry(name, self.worker.target, ok, duration,
                             apk_size, get_build_number(proj["path"], self.worker.target))

            try:
                icon = "dialog-ok-apply" if ok else "dialog-error"
                subprocess.Popen(["notify-send", "-i", icon, APP_NAME,
                                  f"{name}: {'done' if ok else 'failed'} ({self.worker.elapsed_str()})"])
            except: pass

            el = self.worker.elapsed_str()
            self.status.set_text(f"{name}  {'done' if ok else 'failed'}  {el}")

            if ok and self._build_queue:
                p, t = self._build_queue.pop(0)
                self._start(p, t)
        except Exception as e:
            print(f"[_on_done] ERROR: {e}")
            import traceback; traceback.print_exc()
        finally:
            # ALWAYS stop building state
            self._set_building(False)
            self.stage_label.set_text("Done" if ok else "Failed")
            self.progress_bar.set_fraction(1.0 if ok else 0)
            self.worker = None
            GLib.timeout_add(2000, self._build_cards)

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
            def _log_cb(t):
                GLib.idle_add(self._log, t)
                # Must return None/False — if upload_apk ever invokes log_cb
                # through idle_add, a truthy return (source-ID int) would
                # make GLib reschedule it forever. See the _on_done loop bug
                # in _ios_post_build for the same failure mode.
            ok = upload_apk(self.cfg, proj, apk,
                            log_cb=_log_cb,
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
        # Stop in-flight iOS remote SSH + send "stop" to Mac so xcodebuild in
        # the Terminal window on Mac actually dies. Without this, the top-bar
        # Cancel button only killed Unity/Android/test-run but left the Mac
        # side grinding away.
        runner = getattr(self, "_ios_runner", None)
        if runner:
            try: runner.stop()
            except Exception: pass
        if (self.cfg.get("ios_remote") or {}).get("mac_ip"):
            threading.Thread(target=self._ios_run_remote, args=("stop",),
                             daemon=True).start()
            self._set_building(False)
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
        print("[test_picker] calling rebuild_fixtures initially")
        rebuild_fixtures()
        print("[test_picker] initial call done")  # initial

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

    def _scan_tests(self, proj_path, platform):
        """Scan .cs test files for [Test]/[UnityTest] methods."""
        import re as _re
        import glob as _glob
        test_dir = os.path.join(proj_path, "Assets")
        cs_files = set()
        for pat in ["**/Tests/**/*.cs"]:
            cs_files.update(_glob.glob(os.path.join(test_dir, pat), recursive=True))
        
        # Filter by platform based on directory
        filtered = set()
        for f in cs_files:
            if platform == "EditMode" and ("Editor" in f or "EditMode" in f):
                filtered.add(f)
            elif platform == "PlayMode" and "Editor" not in f:
                filtered.add(f)
        
        tests = []
        for cs_file in sorted(filtered):
            try:
                with open(cs_file, errors="replace") as f:
                    lines = f.readlines()
            except: continue
            current_class = ""
            next_is_test = False
            for line in lines:
                cm = _re.match(r'.*class\s+(\w+)', line)
                if cm:
                    current_class = cm.group(1)
                if "[Test]" in line or "[UnityTest]" in line:
                    next_is_test = True
                    continue
                if next_is_test:
                    mm = _re.match(r'\s+public\s+\S+\s+(\w+)\s*\(', line)
                    if mm:
                        tests.append({
                            "class": current_class,
                            "method": mm.group(1),
                            "full": f"{current_class}.{mm.group(1)}",
                            "file": os.path.basename(cs_file),
                        })
                    next_is_test = False
        return tests

    def _show_test_picker(self, proj, platform=None):
        """Show dialog to select platform and tests to run."""
        dlg = Adw.Dialog()
        dlg.set_title(f"{proj['name']} — Run Tests")
        dlg.set_content_width(520)
        dlg.set_content_height(650)

        tb = Adw.ToolbarView()
        tb.add_top_bar(Adw.HeaderBar())

        page = Adw.PreferencesPage()

        # Platform selector
        plat_grp = Adw.PreferencesGroup(title="Platform")
        plat_row = Adw.ComboRow(title="Test Platform")
        plat_row.set_model(Gtk.StringList.new(["EditMode", "PlayMode"]))
        plat_row.set_selected(1 if platform == "PlayMode" else 0)
        plat_grp.add(plat_row)
        filter_entry = Adw.EntryRow(title="Test filter (class or method name)")
        plat_grp.add(filter_entry)
        page.add(plat_grp)

        fixture_grp = [None]  # mutable ref
        checks = []
        state = {"tests": []}

        def rebuild_fixtures(*_):
          print(f"[rebuild_fixtures] CALLED, platform idx={plat_row.get_selected()}")
          try:
            # Remove old group
            if fixture_grp[0]:
                page.remove(fixture_grp[0])
            checks.clear()

            sel_platform = ["EditMode", "PlayMode"][plat_row.get_selected()]
            grp = Adw.PreferencesGroup(title=f"Fixtures ({sel_platform})")

            # Try XML results first
            xml_path = os.path.join(proj["path"], f"test-results-{sel_platform.lower()}.xml")
            test_names = []
            if os.path.isfile(xml_path):
                import xml.etree.ElementTree as ET
                try:
                    tree = ET.parse(xml_path)
                    for tc in tree.getroot().iter("test-case"):
                        name = tc.get("fullname", tc.get("name", ""))
                        result = tc.get("result", "")
                        if name:
                            test_names.append({"name": name, "result": result})
                except: pass

            if not test_names:
                scanned = self._scan_tests(proj["path"], sel_platform)
                test_names = [{"name": t["full"], "result": ""} for t in scanned]
                if not scanned:
                    print(f"[test_picker] WARNING: 0 tests found for {sel_platform} in {proj['path']}")

            if not test_names:
                grp.set_description(f"No {sel_platform} tests found — run once to see fixtures")
            else:
                grp.set_description(f"{len(test_names)} tests")

            state["tests"] = test_names

            # Group by class
            by_class = {}
            for t in test_names:
                cls = t["name"].rsplit(".", 1)[0] if "." in t["name"] else "Other"
                by_class.setdefault(cls, []).append(t)

            for cls, class_tests in by_class.items():
                passed = sum(1 for t in class_tests if t["result"] == "Passed")
                failed = sum(1 for t in class_tests if t["result"] == "Failed")
                total = len(class_tests)
                sub = f"{passed}/{total} passed" if passed else f"{total} tests"
                if failed: sub += f", {failed} failed"

                exp = Adw.ExpanderRow(title=cls, subtitle=sub)
                if failed:
                    exp.add_prefix(Gtk.Image.new_from_icon_name("dialog-error-symbolic"))
                elif passed == total and total > 0:
                    exp.add_prefix(Gtk.Image.new_from_icon_name("object-select-symbolic"))

                class_check = Gtk.CheckButton(active=True, valign=Gtk.Align.CENTER)
                exp.add_suffix(class_check)

                method_checks = []
                for t in class_tests:
                    method = t["name"].split(".")[-1] if "." in t["name"] else t["name"]
                    row = Adw.SwitchRow(title=method, active=True)
                    if t["result"] == "Passed":
                        row.add_prefix(Gtk.Image.new_from_icon_name("object-select-symbolic"))
                    elif t["result"] == "Failed":
                        row.add_prefix(Gtk.Image.new_from_icon_name("dialog-error-symbolic"))
                    exp.add_row(row)
                    checks.append((row, t["name"], row))
                    method_checks.append(row)

                # Class checkbox toggles all methods
                def toggle_class(btn, mc=method_checks):
                    active = btn.get_active()
                    for r in mc:
                        r.set_active(active)
                class_check.connect("toggled", toggle_class)

                grp.add(exp)

            # Select all / none
            sel_row = Adw.ActionRow(title="Select All / Deselect All")
            all_btn = Gtk.Button(label="All", css_classes=["flat"], valign=Gtk.Align.CENTER)
            none_btn = Gtk.Button(label="None", css_classes=["flat"], valign=Gtk.Align.CENTER)
            all_btn.connect("clicked", lambda _: [r.set_active(True) for c, n, r in checks])
            none_btn.connect("clicked", lambda _: [r.set_active(False) for c, n, r in checks])
            sel_row.add_suffix(all_btn)
            sel_row.add_suffix(none_btn)
            grp.add(sel_row)

            fixture_grp[0] = grp
            page.add(grp)
          except Exception as e:
            print(f"[rebuild_fixtures] ERROR: {e}")
            import traceback; traceback.print_exc()

        plat_row.connect("notify::selected", rebuild_fixtures)
        print("[test_picker] calling rebuild_fixtures initially")
        rebuild_fixtures()
        print("[test_picker] initial call done")

        scroll = Gtk.ScrolledWindow(vexpand=True)
        scroll.set_child(page)

        # Run button
        run_btn = Gtk.Button(label="Run Tests", css_classes=["suggested-action"],
                             halign=Gtk.Align.CENTER)
        run_btn.set_margin_top(12)
        run_btn.set_margin_bottom(16)

        def on_run(_):
            sel_platform = ["EditMode", "PlayMode"][plat_row.get_selected()]
            manual_filter = filter_entry.get_text().strip()
            if manual_filter:
                test_filter = manual_filter
            else:
                selected = [name for ch, name, row in checks if row.get_active()]
                if not selected or len(selected) == len(state["tests"]):
                    test_filter = None
                else:
                    test_filter = "||".join(selected)
            dlg.close()
            self._on_run_tests(proj, sel_platform, test_filter=test_filter)

        run_btn.connect("clicked", on_run)

        vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        vbox.append(scroll)
        vbox.append(run_btn)
        tb.set_content(vbox)
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
            self._log(f"Filter: {test_filter}\n\n")
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

                    # Detect fatal crash
                    if "Caught fatal signal" in s:
                        if not compiler_error:
                            compiler_error = True
                            GLib.idle_add(self._log, "\n  Unity crashed (fatal signal)\n")
                            GLib.idle_add(self.stage_label.set_text, "Unity crashed")
                            GLib.idle_add(self.progress_bar.set_fraction, 0)
                            try: os.killpg(os.getpgid(proc.pid), 9)
                            except: pass
                            break
                    # VirtualArtifacts corrupted — warn but don't kill, let Unity try to continue
                    if "Fatal Error!" in s and "corrupted" in s.lower():
                        GLib.idle_add(self.stage_label.set_text, "VirtualArtifacts error (continuing...)")

                    # Detect Unity lockfile / editor open
                    if "Failed to write file" in s and "EditorUserBuildSettings" in s:
                        if repeat_count == 0:
                            GLib.idle_add(self._log, "\n  Unity Editor is open — close it and retry\n")
                            GLib.idle_add(self.stage_label.set_text, "Unity Editor is open")
                            compiler_error = True
                            try: os.killpg(os.getpgid(proc.pid), 9)
                            except: pass
                            break

                    # Skip noisy Unity lines
                    if any(p in s for p in SKIP_PATTERNS):
                        full_log.append(line)
                        continue

                    GLib.idle_add(self._log, line)
                    # Collect screenshots from TestScreenshot.Capture
                    if "[Screenshot]" in s:
                        path = s.split("[Screenshot]")[-1].strip()
                        if os.path.isfile(path):
                            screenshots.append(path)
                    if "compiler errors" in s.lower() or "Aborting batchmode due to failure" in s:
                        compiler_error = True
                        GLib.idle_add(self._log, "\n  Compiler errors — aborting\n")
                        GLib.idle_add(self.stage_label.set_text, "Compiler errors")
                    elif "Aborting batchmode due to fatal error" in s:
                        compiler_error = True
                        GLib.idle_add(self._log, "\n  Fatal error — Library may be corrupted, try Clean Build\n")
                        GLib.idle_add(self.stage_label.set_text, "Fatal error")
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
        # Save elapsed before clearing
        import time as _time
        if hasattr(self, '_test_start') and self._test_start:
            self._test_elapsed = int(_time.time() - self._test_start)
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
            # Real elapsed time (saved before timer was stopped)
            duration = getattr(self, '_test_elapsed', 0) or int(xml_duration)

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
        self._log_widget.set_exclude_patterns(cfg.get("log_filters", []))