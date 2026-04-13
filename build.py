#!/usr/bin/env python3
"""Unity Builder Dash — GTK4/Adwaita build tool for Unity projects."""

import gi
gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
from gi.repository import Adw

from src.constants import APP_ID
from src.config import load_config
from src.window import BuilderWindow


class App(Adw.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID)

    def do_activate(self):
        cfg = load_config()
        BuilderWindow(self, cfg).present()


if __name__ == "__main__":
    App().run()
