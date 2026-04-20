"""Scan dialog (health check) + screenshots gallery + iOS remote popup."""
import os, subprocess, threading
from gi.repository import Gtk, Adw, GLib
from .config import scan_project
from .ios_remote import (get_devices, get_remote_cfg, test_connection,
                         generate_ssh_key, copy_key_to_mac, install_mac_server)


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


def show_screenshots(parent, paths, project_name, platform):
    """Show test screenshots in a dialog with clickable thumbnails."""
    dlg = Adw.Dialog()
    dlg.set_title(f"{project_name} — {platform} Screenshots")
    dlg.set_content_width(720)
    dlg.set_content_height(520)

    tb = Adw.ToolbarView()
    tb.add_top_bar(Adw.HeaderBar())

    scroll = Gtk.ScrolledWindow(vexpand=True)
    flow = Gtk.FlowBox(
        homogeneous=True, column_spacing=8, row_spacing=8,
        min_children_per_line=2, max_children_per_line=4,
        selection_mode=Gtk.SelectionMode.NONE)
    flow.set_margin_top(8)
    flow.set_margin_bottom(8)
    flow.set_margin_start(8)
    flow.set_margin_end(8)

    for path in paths:
        if not os.path.isfile(path):
            continue
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)

        try:
            from gi.repository import GdkPixbuf, Gdk
            pixbuf = GdkPixbuf.Pixbuf.new_from_file_at_scale(path, 300, 200, True)
            texture = Gdk.Texture.new_for_pixbuf(pixbuf)
            img = Gtk.Picture.new_for_paintable(texture)
            img.set_size_request(300, 200)
            img.set_content_fit(Gtk.ContentFit.CONTAIN)
        except Exception:
            img = Gtk.Label(label="(failed to load)")

        label = Gtk.Label(label=os.path.basename(path), ellipsize=3)
        label.add_css_class("caption")

        btn = Gtk.Button()
        btn.set_child(box)
        btn.add_css_class("flat")
        box.append(img)
        box.append(label)

        p = path
        btn.connect("clicked", lambda _, f=p: subprocess.Popen(["xdg-open", f]))
        flow.append(btn)

    if flow.get_first_child() is None:
        flow.append(Gtk.Label(label="No screenshots captured"))

    scroll.set_child(flow)
    tb.set_content(scroll)
    dlg.set_child(tb)
    dlg.present(parent)


def show_ios_popup(parent, proj, cfg, on_action, save_cfg, log_cb,
                   on_open_settings=None):
    """iOS remote-build action picker (port of CrazyMegaBuilder's iOS tab).

    Args:
        parent:           main window (for dlg.present)
        proj:             project dict
        cfg:              full config dict; ios_remote key is read/written
        on_action:        callback (action_id, device_target) for build buttons
        save_cfg:         callback to persist cfg (e.g. config.save_config)
        log_cb:           callback to append text to the main log view
        on_open_settings: optional callback to switch the window to Settings
                          (for the "Open Settings" link)
    """
    remote = get_remote_cfg(cfg)

    dlg = Adw.Dialog()
    dlg.set_title(f"{proj['name']} — iOS")
    dlg.set_content_width(560)

    # Inline log inside the popup — users don't have to close it to see
    # Connect / SSH / Set-up-key output. Forwards to the main LogView too.
    log_scroll = Gtk.ScrolledWindow(
        min_content_height=110, max_content_height=160,
        hscrollbar_policy=Gtk.PolicyType.NEVER,
        margin_start=12, margin_end=12, margin_top=6, margin_bottom=12,
        css_classes=["card"])
    log_tv = Gtk.TextView(editable=False, monospace=True, cursor_visible=False,
                          top_margin=6, bottom_margin=6,
                          left_margin=8, right_margin=8)
    log_tv.add_css_class("caption")
    log_scroll.set_child(log_tv)
    log_buf = log_tv.get_buffer()
    _closed = {"v": False}
    dlg.connect("closed", lambda _d: _closed.__setitem__("v", True))

    def popup_log(t):
        if not _closed["v"]:
            try:
                log_buf.insert(log_buf.get_end_iter(), t)
                adj = log_scroll.get_vadjustment()
                GLib.idle_add(lambda: adj.set_value(
                    adj.get_upper() - adj.get_page_size()) or False)
            except Exception:
                pass
        log_cb(t)

    def _fire(action_id, dev=None):
        on_action(action_id, dev)
        dlg.close()

    tb = Adw.ToolbarView()
    header = Adw.HeaderBar()
    tb.add_top_bar(header)

    # Kebab menu: Stop + Update Pod + Open SSH terminal + terminal toggle
    menu_btn = Gtk.MenuButton(icon_name="view-more-symbolic", css_classes=["flat"])
    menu_popover = Gtk.Popover()
    menu_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2,
                       margin_top=6, margin_bottom=6, margin_start=6, margin_end=6)

    stop_btn = Gtk.Button(label="Stop", css_classes=["flat", "destructive-action"])
    stop_btn.connect("clicked",
        lambda _: (on_action("stop", None), menu_popover.popdown()))
    menu_box.append(stop_btn)

    update_pod_btn = Gtk.Button(label="Update Pod", css_classes=["flat"])
    update_pod_btn.connect("clicked",
        lambda _: (_fire("update_pod"), menu_popover.popdown()))
    menu_box.append(update_pod_btn)

    open_terminal_btn = Gtk.Button(label="Open SSH terminal", css_classes=["flat"])
    def _open_ssh(_b):
        from . import ios_remote as _ir
        _ir.open_ssh_terminal(_ir.get_remote_cfg(cfg), popup_log)
        menu_popover.popdown()
    open_terminal_btn.connect("clicked", _open_ssh)
    menu_box.append(open_terminal_btn)

    menu_box.append(Gtk.Separator(margin_top=4, margin_bottom=4))

    # Run with test runner — off by default (plain install via devicectl,
    # app icon appears on device, no auto-launch). On = xcodebuild test:
    # auto-launches via xctest harness (more output but init quirks).
    with_test_check = Gtk.CheckButton(label="Run with test runner (auto-launch)",
        margin_start=6, margin_end=6, margin_top=4, margin_bottom=4)
    with_test_check.set_active(
        bool((cfg.get("ios_remote") or {}).get("run_with_test", False)))
    def _on_with_test_toggle(b):
        cfg.setdefault("ios_remote", {})["run_with_test"] = b.get_active()
        save_cfg(cfg)
    with_test_check.connect("toggled", _on_with_test_toggle)
    menu_box.append(with_test_check)

    terminal_check = Gtk.CheckButton(label="External terminal window",
        margin_start=6, margin_end=6, margin_top=4, margin_bottom=4)
    terminal_check.set_active(
        bool((cfg.get("ios_remote") or {}).get("external_terminal")))
    def _on_terminal_toggle(b):
        cfg.setdefault("ios_remote", {})["external_terminal"] = b.get_active()
        save_cfg(cfg)
    terminal_check.connect("toggled", _on_terminal_toggle)
    menu_box.append(terminal_check)

    menu_popover.set_child(menu_box)
    menu_btn.set_popover(menu_popover)
    header.pack_end(menu_btn)

    # Plain Box instead of Adw.PreferencesPage — the latter's default gap
    # between PreferencesGroups is too wide for this compact popup.
    page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10,
                   margin_start=12, margin_end=12, margin_top=4, margin_bottom=4)

    # ── Connection ──
    conn_grp = Adw.PreferencesGroup(title="Connection")
    ip_row = Adw.ActionRow(title="Mac IP")

    # Connection indicator: ● coloured blue (unknown) / green (ok) / red (fail)
    status_dot = Gtk.Label(valign=Gtk.Align.CENTER, use_markup=True, margin_end=4)
    def set_connected(ok, tip=None):
        color = "#26a269" if ok is True else "#c01c28" if ok is False else "#62a0ea"
        label = tip or ("Connected" if ok is True
                        else "Disconnected" if ok is False else "Not checked yet")
        status_dot.set_markup(f'<span foreground="{color}" font="16">●</span>')
        status_dot.set_tooltip_text(label)
    set_connected(None)

    ip_entry = Gtk.Entry(text=remote.get("mac_ip", ""), hexpand=True,
                         valign=Gtk.Align.CENTER, width_chars=16)
    connect_btn = Gtk.Button(label="Connect", css_classes=["suggested-action"],
                             valign=Gtk.Align.CENTER)

    def _on_ip_changed(_e):
        cfg.setdefault("ios_remote", {})["mac_ip"] = ip_entry.get_text().strip()
        save_cfg(cfg)
        set_connected(None, "IP changed — reconnect to verify")
    ip_entry.connect("changed", _on_ip_changed)

    def _do_test(r, notify):
        ok = test_connection(r, popup_log, notify=notify)
        GLib.idle_add(lambda: set_connected(ok))

    def _on_connect(_b):
        r = get_remote_cfg(cfg)
        r["mac_ip"] = ip_entry.get_text().strip()
        popup_log(f"Connecting to {r['mac_user']}@{r['mac_ip']}...\n")
        threading.Thread(target=_do_test, args=(r, True), daemon=True).start()
    connect_btn.connect("clicked", _on_connect)

    ip_row.add_prefix(status_dot)
    ip_row.add_suffix(ip_entry)
    ip_row.add_suffix(connect_btn)
    conn_grp.add(ip_row)

    # Auto-probe on popup open for key-auth setups — silent (no Mac
    # notification, no IP write) so the indicator can update without
    # spamming the Mac every time the popup is opened.
    if remote.get("mac_ip") and remote.get("mac_auth") == "key":
        threading.Thread(target=_do_test,
            args=(get_remote_cfg(cfg), False), daemon=True).start()

    # ── Quick-actions popover (🔑) — one-click Generate/Set up/Install ──
    # Mac password lives in full Settings (iOS tab); Set up reads it from cfg.
    gen_row = Adw.ActionRow(title="Generate SSH key",
        subtitle=f"{remote.get('mac_key_path','~/.ssh/id_ed25519')}")
    gen_btn = Gtk.Button(label="Generate", valign=Gtk.Align.CENTER)
    gen_btn.connect("clicked", lambda _b: threading.Thread(
        target=generate_ssh_key,
        args=(get_remote_cfg(cfg).get("mac_key_path", "~/.ssh/id_ed25519"),
              popup_log), daemon=True).start())
    gen_row.add_suffix(gen_btn)

    setup_row = Adw.ActionRow(title="Install SSH key on Mac",
        subtitle="Generate if missing, then ssh-copy-id")
    setup_btn = Gtk.Button(label="Set up", css_classes=["suggested-action"],
                           valign=Gtk.Align.CENTER)
    def _on_setup(_b):
        r = get_remote_cfg(cfg)
        r["mac_ip"] = ip_entry.get_text().strip()
        pw = r.get("mac_password", "")
        if not pw:
            popup_log("Mac password is empty — set it in full iOS settings first.\n")
            return
        popup_log("Installing SSH key on Mac...\n")
        def _do():
            ok = copy_key_to_mac(r, pw, popup_log)
            if ok:
                cfg.setdefault("ios_remote", {})["mac_auth"] = "key"
                save_cfg(cfg)
        threading.Thread(target=_do, daemon=True).start()
    setup_btn.connect("clicked", _on_setup)
    setup_row.add_suffix(setup_btn)

    install_row = Adw.ActionRow(title="Install on Mac",
        subtitle="Copy ios_build.scpt + patches to the Mac work folder")
    install_btn = Gtk.Button(label="Install", valign=Gtk.Align.CENTER)
    install_btn.connect("clicked", lambda _b: threading.Thread(
        target=install_mac_server,
        args=(get_remote_cfg(cfg), popup_log), daemon=True).start())
    install_row.add_suffix(install_btn)

    quick_group = Adw.PreferencesGroup()
    quick_group.add(gen_row)
    quick_group.add(setup_row)
    quick_group.add(install_row)

    # Link to full Settings page (auth mode, user, work folder, widget identity)
    settings_group = Adw.PreferencesGroup()
    settings_row = Adw.ActionRow(title="Full iOS settings",
        subtitle="Mode, user, work folder, widget identity…")
    settings_row.set_activatable(True)
    settings_row.add_suffix(
        Gtk.Image.new_from_icon_name("adw-external-link-symbolic"))
    def _open_settings(_r):
        quick_popover.popdown()
        dlg.close()
        if on_open_settings: on_open_settings()
    settings_row.connect("activated", _open_settings)
    settings_group.add(settings_row)

    quick_popover = Gtk.Popover()
    quick_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10,
        margin_top=6, margin_bottom=6, margin_start=6, margin_end=6)
    quick_box.set_size_request(420, -1)
    quick_box.append(quick_group)
    quick_box.append(settings_group)
    quick_popover.set_child(quick_box)

    quick_btn = Gtk.MenuButton(
        icon_name="dialog-password-symbolic",
        always_show_arrow=True,
        tooltip_text="SSH key + install",
        popover=quick_popover,
        css_classes=["flat"],
        valign=Gtk.Align.CENTER)
    ip_row.add_suffix(quick_btn)

    page.append(conn_grp)

    # ── Build ──
    build_grp = Adw.PreferencesGroup(title="Build")
    build_row = Adw.ActionRow(title="Device")

    devices_list = get_devices(cfg) or [("iPhone 12 mini", "iPhone 12 mini")]
    dev_dropdown = Gtk.DropDown.new_from_strings([lbl for lbl, _n in devices_list])
    dev_dropdown.set_selected(0)
    dev_dropdown.set_valign(Gtk.Align.CENTER)

    def _dev_target():
        idx = dev_dropdown.get_selected()
        if 0 <= idx < len(devices_list):
            return devices_list[idx][1]
        return devices_list[0][1]

    build_row.add_suffix(dev_dropdown)
    for lbl, action_id, css, tip in [
        ("Full",       "full",          ["suggested-action"], "Unity build → zip → scp → unpack (pods+widget) → build on Mac"),
        ("Xcode",      "xcode",         [],                    "Skip Unity: unpack (pods+widget) + build on Mac with existing zip"),
        ("Build",      "build_only",    [],                    "Just xcodebuild on existing iOS/ — no unpack, no pods, no widget"),
        ("No Xcode",   "without_xcode", [],                    "Unity build → zip → scp → unpack only (no build)"),
    ]:
        b = Gtk.Button(label=lbl, css_classes=css, valign=Gtk.Align.CENTER,
                       tooltip_text=tip)
        b.connect("clicked",
            lambda _w, a=action_id: _fire(a, _dev_target()))
        build_row.add_suffix(b)
    build_grp.add(build_row)
    page.append(build_grp)

    # ── Archive ──
    arch_grp = Adw.PreferencesGroup(title="Archive")
    arch_row = Adw.ActionRow()
    arch_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6,
                       valign=Gtk.Align.CENTER, hexpand=True, homogeneous=True,
                       margin_top=6, margin_bottom=6,
                       margin_start=12, margin_end=12)
    for lbl, action_id in [
        ("Pack",   "archive"),
        ("Unpack", "unpack"),
        ("All",    "all"),
    ]:
        b = Gtk.Button(label=lbl)
        b.connect("clicked", lambda _w, a=action_id: _fire(a))
        arch_box.append(b)
    arch_row.set_child(arch_box)
    arch_grp.add(arch_row)
    page.append(arch_grp)

    # ── Extras ──
    extra_grp = Adw.PreferencesGroup(title="Extras")
    extra_row = Adw.ActionRow()
    extra_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6,
                        valign=Gtk.Align.CENTER, hexpand=True, homogeneous=True,
                        margin_top=6, margin_bottom=6,
                        margin_start=12, margin_end=12)
    for lbl, action_id in [
        ("Clear .pcm cache", "clear_cache"),
        ("Add widget",       "add_widget"),
        ("Clean build",      "clear_build"),
    ]:
        b = Gtk.Button(label=lbl)
        b.connect("clicked", lambda _w, a=action_id: _fire(a))
        extra_box.append(b)
    extra_row.set_child(extra_box)
    extra_grp.add(extra_row)
    page.append(extra_grp)

    content_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
    page.set_vexpand(True)
    content_box.append(page)
    content_box.append(log_scroll)
    tb.set_content(content_box)
    dlg.set_child(tb)
    dlg.present(parent)
    return dlg
