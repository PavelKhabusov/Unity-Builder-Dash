"""Scan dialog (health check) + screenshots gallery."""
import os, subprocess
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
