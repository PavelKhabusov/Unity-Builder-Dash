#!/usr/bin/env python3
"""Unity Builder Dash — GTK4/Adwaita build tool for Unity projects."""

import gi
gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
from gi.repository import Gtk, Gdk, Adw

from src.constants import APP_ID, ICONS_DIR
from src.config import load_config
from src.window import BuilderWindow


def apply_theme(cfg):
    theme = cfg.get("theme", "system")
    mgr = Adw.StyleManager.get_default()
    schemes = {"dark": Adw.ColorScheme.FORCE_DARK,
               "light": Adw.ColorScheme.FORCE_LIGHT}
    mgr.set_color_scheme(schemes.get(theme, Adw.ColorScheme.DEFAULT))


class App(Adw.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID)
        self.win = None

    def do_activate(self):
        # Register custom icons
        display = Gdk.Display.get_default()
        if display:
            Gtk.IconTheme.get_for_display(display).add_search_path(ICONS_DIR)

        cfg = load_config()
        apply_theme(cfg)

        if self.win is None:
            self.win = BuilderWindow(self, cfg)
            orig = self.win._apply_config
            def on_config(c):
                orig(c)
                apply_theme(c)
            self.win._apply_config = on_config
        self.win.present()


if __name__ == "__main__":
    App().run()
