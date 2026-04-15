"""Reusable log viewer widget with search, level filter, word wrap, and color tags."""
from gi.repository import Gtk


# Tag configs: (name, foreground_color)
DEFAULT_TAGS = {
    "error":   "#e01b24",
    "warning": "#e5a50a",
    "success": "#2ec27e",
    "stage":   "#62a0ea",
    # Logcat levels
    "E": "#e01b24",
    "W": "#e5a50a",
    "I": "#2ec27e",
    "D": "#62a0ea",
    "V": "#9a9996",
}


class LogView(Gtk.Box):
    """Log viewer with filter bar, colored tags, word wrap toggle, and scroll-to-bottom.

    Usage:
        lv = LogView(levels=["All", "Errors", "Warnings", "Stages"],
                     get_tag=my_tag_func)
        parent.append(lv)
        lv.append_line("some log line\\n")
        lv.clear()

    get_tag(line) should return a tag name string (e.g. "error") or None.
    """

    def __init__(self, levels=None, get_tag=None, margin=12, extra_start=None, extra_end=None):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, vexpand=True)
        if margin:
            self.set_margin_top(4)
            self.set_margin_start(margin)
            self.set_margin_end(margin)
            self.set_margin_bottom(margin)

        self._get_tag = get_tag or (lambda _: None)
        self._full_lines = []  # all raw lines for refilter
        self._paused = False

        # ── Filter bar ──
        search_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        search_box.set_margin_bottom(4)

        # Extra widgets at start
        for w in (extra_start or []):
            search_box.append(w)

        self._search = Gtk.SearchEntry(placeholder_text="Filter log...")
        self._search.set_hexpand(True)
        self._search.connect("search-changed", self._on_filter)
        search_box.append(self._search)

        level_items = levels or ["All"]
        self._level_filter = Gtk.DropDown.new_from_strings(level_items)
        self._level_filter.set_selected(0)
        self._level_filter.connect("notify::selected", lambda *_: self._on_filter())
        self._level_names = level_items
        search_box.append(self._level_filter)

        wrap_toggle = Gtk.ToggleButton(icon_name="format-justify-left-symbolic",
                                       tooltip_text="Word wrap", active=False)
        wrap_toggle.connect("toggled", self._on_wrap)
        search_box.append(wrap_toggle)

        copy_btn = Gtk.Button(icon_name="edit-copy-symbolic",
                              tooltip_text="Copy visible log", css_classes=["flat"])
        copy_btn.connect("clicked", self._on_copy)
        search_box.append(copy_btn)

        # Extra widgets at end
        for w in (extra_end or []):
            search_box.append(w)

        self.append(search_box)

        # ── Text view ──
        self._buffer = Gtk.TextBuffer()
        self._view = Gtk.TextView(buffer=self._buffer, editable=False,
                                  cursor_visible=False, monospace=True)
        self._view.set_wrap_mode(Gtk.WrapMode.NONE)
        self._view.set_top_margin(6)
        self._view.set_bottom_margin(6)
        self._view.set_left_margin(8)
        self._view.set_right_margin(8)

        # Create tags
        for tag_name, color in DEFAULT_TAGS.items():
            kwargs = {"foreground": color}
            if tag_name == "stage":
                kwargs["weight"] = 700
            self._buffer.create_tag(tag_name, **kwargs)

        self._scroll = Gtk.ScrolledWindow(vexpand=True)
        self._scroll.set_child(self._view)
        self._scroll.add_css_class("card")

        # Overlay with scroll-to-bottom button
        overlay = Gtk.Overlay()
        overlay.set_child(self._scroll)
        overlay.set_vexpand(True)
        scroll_btn = Gtk.Button(icon_name="go-bottom-symbolic",
                                tooltip_text="Scroll to bottom",
                                css_classes=["circular", "osd"],
                                halign=Gtk.Align.END, valign=Gtk.Align.END)
        scroll_btn.set_margin_end(12)
        scroll_btn.set_margin_bottom(12)
        scroll_btn.connect("clicked", self.scroll_to_bottom)
        overlay.add_overlay(scroll_btn)

        self.append(overlay)

    # ── Public API ──

    @property
    def buffer(self):
        return self._buffer

    def clear(self):
        """Clear all log content."""
        self._full_lines = []
        self._buffer.set_text("")

    def append_line(self, text):
        """Append a line, respecting current filter. Stores raw line for refilter."""
        self._full_lines.append(text)
        # Keep buffer manageable
        if len(self._full_lines) > 10000:
            self._full_lines = self._full_lines[-7000:]
        if not self._paused and self._passes_filter(text):
            self._insert_tagged(text)

    def get_full_text(self):
        """Return all raw lines joined."""
        return "".join(self._full_lines)

    def set_paused(self, paused):
        """Pause/resume live output. On unpause, rebuilds with filter."""
        self._paused = paused
        if not paused:
            self._rebuild()

    def scroll_to_bottom(self, *_):
        adj = self._scroll.get_vadjustment()
        adj.set_value(adj.get_upper())

    # ── Internal ──

    def _passes_filter(self, text):
        query = self._search.get_text().lower().strip()
        level_idx = self._level_filter.get_selected()
        level_name = self._level_names[level_idx] if level_idx < len(self._level_names) else "All"

        if level_name != "All":
            tag = self._get_tag(text)
            # Map level name to expected tags
            level_tag_map = {
                "Errors": ["error", "E"],
                "Error": ["error", "E"],
                "Warnings": ["error", "warning", "E", "W"],
                "Warning": ["error", "warning", "E", "W"],
                "Stages": ["stage"],
                "Info": ["error", "warning", "success", "stage", "E", "W", "I"],
                "Debug": ["error", "warning", "success", "stage", "E", "W", "I", "D"],
            }
            allowed = level_tag_map.get(level_name, [])
            if tag not in allowed:
                return False

        if query and query not in text.lower():
            return False
        return True

    def _insert_tagged(self, text, scroll=True):
        end = self._buffer.get_end_iter()
        tag = self._get_tag(text)
        if tag:
            self._buffer.insert_with_tags_by_name(end, text, tag)
        else:
            self._buffer.insert(end, text)
        if scroll:
            mk = self._buffer.create_mark(None, self._buffer.get_end_iter(), False)
            self._view.scroll_mark_onscreen(mk)
            self._buffer.delete_mark(mk)

    def _rebuild(self):
        self._buffer.set_text("")
        for line in self._full_lines:
            if self._passes_filter(line):
                self._insert_tagged(line, scroll=False)
        self.scroll_to_bottom()

    def _on_filter(self, *_):
        self._rebuild()

    def _on_copy(self, _):
        """Copy currently visible (filtered) log text to clipboard."""
        text = self._buffer.get_text(
            self._buffer.get_start_iter(), self._buffer.get_end_iter(), False)
        display = self._view.get_display()
        if display:
            clipboard = display.get_clipboard()
            from gi.repository import Gdk
            clipboard.set(text)

    def _on_wrap(self, btn):
        self._view.set_wrap_mode(
            Gtk.WrapMode.WORD_CHAR if btn.get_active() else Gtk.WrapMode.NONE)