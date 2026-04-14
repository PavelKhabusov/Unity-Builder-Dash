"""Scan dialog (health check). History moved to history_page.py."""
import os
from gi.repository import Gtk, Adw
from .config import scan_project


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