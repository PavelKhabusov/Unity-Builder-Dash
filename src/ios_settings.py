"""iOS remote-build settings widgets, shared between the main Settings page
and any other UI that needs them. Returns Adw.PreferencesGroup widgets that
can be added to any PreferencesPage or plain Box.
"""
import threading
from gi.repository import Gtk, Adw, GLib
from .ios_remote import (get_remote_cfg, copy_key_to_mac, generate_ssh_key,
                         install_mac_server)


def build_ios_settings_groups(cfg, save_cfg, log_cb=None):
    """Build the iOS server-config UI.

    Args:
        cfg:      full app config dict; ios_remote subkey is read/written.
        save_cfg: callable(cfg) -> None to persist changes.
        log_cb:   optional callable(str) for Install/Setup progress.
                  If None, a no-op sink is used.

    Returns:
        List of Adw.PreferencesGroup (Connection, Widget). Caller appends
        them wherever appropriate.
    """
    remote = get_remote_cfg(cfg)
    log = log_cb or (lambda _t: None)

    # ── Connection group (auth mode, user, work dir, password, buttons) ──
    conn_grp = Adw.PreferencesGroup(title="iOS Remote Build",
        description="Connection to the Mac build server (see server/README.md for setup)")

    auth_mode = Adw.ComboRow(title="Auth mode")
    auth_model = Gtk.StringList()
    auth_model.append("SSH key")
    auth_model.append("Password")
    auth_mode.set_model(auth_model)
    auth_mode.set_selected(0 if remote.get("mac_auth") == "key" else 1)
    def _on_mode_changed(*_a):
        mode = "key" if auth_mode.get_selected() == 0 else "password"
        cfg.setdefault("ios_remote", {})["mac_auth"] = mode
        save_cfg(cfg)
    auth_mode.connect("notify::selected", _on_mode_changed)
    conn_grp.add(auth_mode)

    user_row = Adw.EntryRow(title="Mac user")
    user_row.set_text(remote.get("mac_user", "pavel"))
    user_row.connect("changed", lambda r: (
        cfg.setdefault("ios_remote", {}).__setitem__("mac_user", r.get_text().strip()),
        save_cfg(cfg)))
    conn_grp.add(user_row)

    pw_row = Adw.PasswordEntryRow(title="Mac password")
    pw_row.set_text(remote.get("mac_password", ""))
    pw_row.connect("changed", lambda r: (
        cfg.setdefault("ios_remote", {}).__setitem__("mac_password", r.get_text()),
        save_cfg(cfg)))
    conn_grp.add(pw_row)

    work_row = Adw.EntryRow(title="Mac work folder")
    work_row.set_text(remote.get("mac_work_dir", "/Users/pavel/Desktop"))
    def _on_work_changed(_e):
        val = work_row.get_text().strip().rstrip("/") or "/Users/pavel/Desktop"
        ios = cfg.setdefault("ios_remote", {})
        ios["mac_work_dir"] = val
        ios.pop("mac_script_path", None)
        ios.pop("mac_zip_dest", None)
        save_cfg(cfg)
    work_row.connect("changed", _on_work_changed)
    conn_grp.add(work_row)

    gen_row = Adw.ActionRow(title="Generate SSH key",
        subtitle=f"Writes to {remote.get('mac_key_path','~/.ssh/id_ed25519')}")
    gen_btn = Gtk.Button(label="Generate", valign=Gtk.Align.CENTER)
    def _on_gen(_b):
        path = get_remote_cfg(cfg).get("mac_key_path", "~/.ssh/id_ed25519")
        threading.Thread(target=generate_ssh_key, args=(path, log), daemon=True).start()
    gen_btn.connect("clicked", _on_gen)
    gen_row.add_suffix(gen_btn)
    conn_grp.add(gen_row)

    setup_row = Adw.ActionRow(title="Install SSH key on Mac",
        subtitle="Generates the key if missing, then ssh-copy-id")
    setup_btn = Gtk.Button(label="Set up", css_classes=["suggested-action"],
                           valign=Gtk.Align.CENTER)
    def _on_setup(_b):
        pw = pw_row.get_text()
        r = get_remote_cfg(cfg)
        log("Installing SSH key on Mac...\n")
        def _do():
            ok = copy_key_to_mac(r, pw, log)
            if ok:
                cfg.setdefault("ios_remote", {})["mac_auth"] = "key"
                save_cfg(cfg)
                GLib.idle_add(auth_mode.set_selected, 0)
        threading.Thread(target=_do, daemon=True).start()
    setup_btn.connect("clicked", _on_setup)
    setup_row.add_suffix(setup_btn)
    conn_grp.add(setup_row)

    install_row = Adw.ActionRow(title="Install on Mac",
        subtitle="Copies ios_build.scpt + scripts to the work folder and patches them")
    install_btn = Gtk.Button(label="Install", valign=Gtk.Align.CENTER)
    def _on_install(_b):
        r = get_remote_cfg(cfg)
        threading.Thread(target=install_mac_server, args=(r, log), daemon=True).start()
    install_btn.connect("clicked", _on_install)
    install_row.add_suffix(install_btn)
    conn_grp.add(install_row)

    # ── Widget group ──
    widget_grp = Adw.PreferencesGroup(title="iOS Widget",
        description="Applied to ios_build.scpt and add_widget_dependency.rb on next Install")

    def _mk(title, key, fallback):
        row = Adw.EntryRow(title=title)
        row.set_text(remote.get(key, fallback))
        row.connect("changed", lambda r: (
            cfg.setdefault("ios_remote", {}).__setitem__(key, r.get_text().strip()),
            save_cfg(cfg)))
        return row

    widget_grp.add(_mk("Widget bundle ID",   "widget_bundle_id",    "com.example.myapp.widget"))
    widget_grp.add(_mk("Apple Team ID",      "widget_team_id",      "XXXXXXXXXX"))
    widget_grp.add(_mk("Widget target name", "widget_target_name",  "URLImageWidget"))
    widget_grp.add(_mk("Widget folder name", "widget_folder_name",  "kartoteka.widget"))
    widget_grp.add(_mk("App Group ID",       "widget_app_group_id", "group.com.example.myapp"))

    # ── Devices group ──
    devices_grp = Adw.PreferencesGroup(title="iOS Devices",
        description="Shown in the popup dropdown. 'Name' is passed to xcodebuild -destination.")
    add_dev_btn = Gtk.Button(icon_name="list-add-symbolic",
                             valign=Gtk.Align.CENTER, css_classes=["flat"])
    devices_grp.set_header_suffix(add_dev_btn)

    def _save_devices(rows):
        devs = []
        for name_row, disp_row in rows:
            name = name_row.get_text().strip()
            if not name: continue
            devs.append({
                "name": name,
                "display_name": disp_row.get_text().strip() or name,
            })
        cfg.setdefault("ios_remote", {})["devices"] = devs
        save_cfg(cfg)

    device_rows = []

    def _add_device_row(existing=None):
        exp = Adw.ExpanderRow(
            title=(existing or {}).get("display_name") or (existing or {}).get("name") or "New device")

        name_row = Adw.EntryRow(title="Name (xcodebuild destination)")
        name_row.set_text((existing or {}).get("name", ""))
        disp_row = Adw.EntryRow(title="Display name")
        disp_row.set_text((existing or {}).get("display_name", ""))

        def _on_row_changed(_e):
            exp.set_title(disp_row.get_text().strip() or name_row.get_text().strip() or "New device")
            _save_devices(device_rows)
        name_row.connect("changed", _on_row_changed)
        disp_row.connect("changed", _on_row_changed)

        rm_row = Adw.ActionRow()
        rm_btn = Gtk.Button(label="Remove", css_classes=["destructive-action", "flat"],
                            valign=Gtk.Align.CENTER)
        rm_btn.connect("clicked", lambda _b: _remove_device_row(exp, name_row, disp_row))
        rm_row.add_suffix(rm_btn)

        exp.add_row(name_row)
        exp.add_row(disp_row)
        exp.add_row(rm_row)

        devices_grp.add(exp)
        device_rows.append((name_row, disp_row))
        exp._name_row = name_row
        exp._disp_row = disp_row

    def _remove_device_row(exp, name_row, disp_row):
        devices_grp.remove(exp)
        try:
            device_rows.remove((name_row, disp_row))
        except ValueError:
            pass
        _save_devices(device_rows)

    for d in remote.get("devices", []):
        _add_device_row(d)
    add_dev_btn.connect("clicked", lambda _b: _add_device_row())

    return [conn_grp, widget_grp, devices_grp]
