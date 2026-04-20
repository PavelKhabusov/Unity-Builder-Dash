"""Reusable log viewer widget with search, level filter, word wrap, and color tags."""
import re
from gi.repository import Gtk, Gio


# Matches Unity Bee / IL2CPP progress lines like:
#   "[ 491/1162  3s] C_iOS_arm64 Il2CppTempDirArtifacts/..."
# Back-to-back matches are collapsed into a single updating line in the buffer.
_BEE_PROGRESS_RE = re.compile(r'^\s*\[\s*\d+\s*/\s*\d+\s+\d+[ms]?\]')


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

    def __init__(self, levels=None, get_tag=None, margin=12, extra_start=None, extra_end=None, exclude_patterns=None):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, vexpand=True)
        if margin:
            self.set_margin_top(4)
            self.set_margin_start(margin)
            self.set_margin_end(margin)
            self.set_margin_bottom(margin)

        self._get_tag = get_tag or (lambda _: None)
        self._full_lines = []  # all raw lines for refilter
        self._paused = False
        self._exclude_patterns = exclude_patterns or []

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

        # Follow-tail toggle: pressed = auto-scroll (default), released = stay
        # at current position so user can read a specific line without
        # fighting the stream. Icon flips between "locked" and "unlocked".
        self._follow_toggle = Gtk.ToggleButton(
            icon_name="changes-prevent-symbolic",
            tooltip_text="Follow log tail (auto-scroll)",
            active=True)
        def _on_follow(b):
            b.set_icon_name("changes-prevent-symbolic" if b.get_active()
                            else "changes-allow-symbolic")
            if b.get_active():
                self.scroll_to_bottom()
        self._follow_toggle.connect("toggled", _on_follow)
        search_box.append(self._follow_toggle)

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

        # Trace folding
        self._trace_groups = []  # list of {"start_mark", "end_mark", "btn", "visible"}
        self._in_trace = False
        self._trace_ended_ago = 99
        self._buffer.create_tag("trace_filename", foreground="#6e9bcf", scale=0.8, invisible=True)

        # Track right-click position for context menu
        self._last_click_line = -1
        rclick = Gtk.GestureClick(button=3)
        rclick.connect("pressed", self._on_track_click)
        self._view.add_controller(rclick)

        # Context menu: "Show in context" for filtered view
        menu_model = Gio.Menu()
        menu_model.append("Show in Context", "logview.show-context")
        self._view.set_extra_menu(menu_model)

        self._ctx_action = Gio.SimpleAction.new("show-context", None)
        self._ctx_action.connect("activate", self._on_show_context)
        self._ctx_action.set_enabled(False)
        group = Gio.SimpleActionGroup()
        group.add_action(self._ctx_action)
        self._view.insert_action_group("logview", group)

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
        self._trace_groups = []
        self._in_trace = False
        self._trace_ended_ago = 99
        self._bee_mark = None
        self._bee_line_idx = None

    def set_exclude_patterns(self, patterns):
        """Update exclude patterns at runtime."""
        self._exclude_patterns = patterns or []

    def append_lines(self, lines):
        """Bulk-append a list of lines with a single GTK user-action — much
        cheaper than calling append_line N times for high-volume log streams
        (xcodebuild, IL2CPP). All the same trace/filter/exclude logic runs."""
        if not lines:
            return
        self._bulk = True  # suppress per-line scroll; one scroll at the end
        self._buffer.begin_user_action()
        try:
            for ln in lines:
                self.append_line(ln)
        finally:
            self._buffer.end_user_action()
            self._bulk = False
        follow = getattr(self, "_follow_toggle", None)
        if follow is None or follow.get_active():
            mk = self._buffer.create_mark(None, self._buffer.get_end_iter(), False)
            self._view.scroll_mark_onscreen(mk)
            self._buffer.delete_mark(mk)

    def append_line(self, text):
        """Append a line, respecting current filter. Stores raw line for refilter."""
        s = text.strip()

        # Skip lines matching exclude patterns (and suppress following trace)
        if self._exclude_patterns and s:
            for pat in self._exclude_patterns:
                if pat in s:
                    self._skip_until_non_trace = True
                    return
        # If skipping after excluded line, skip trace lines too
        if getattr(self, '_skip_until_non_trace', False):
            if not s or self._is_trace_line(text) or s.startswith("(Filename:"):
                return
            self._skip_until_non_trace = False

        # (Filename:...) — always fold into trace block (comes after trace + empty line)
        if s.startswith("(Filename:"):
            in_trace = getattr(self, '_in_trace', False)
            # Also check if recently exited trace (within last 2 lines)
            just_left_trace = getattr(self, '_trace_ended_ago', 0) <= 2
            if in_trace or just_left_trace:
                self._full_lines.append(text)
                if not self._paused:
                    end = self._buffer.get_end_iter()
                    if not self._buffer.get_tag_table().lookup("trace_hidden"):
                        self._buffer.create_tag("trace_hidden", invisible=True,
                                                 foreground="#888888", scale=0.85)
                    self._buffer.insert_with_tags_by_name(end, text, "trace_hidden")
                return
            # No recent trace: merge with previous visible line
            if self._full_lines:
                prev = self._full_lines[-1].rstrip("\n")
                merged = prev + "  " + s + "\n"
                self._full_lines[-1] = merged
                if not self._paused:
                    self._replace_last_line(merged)
            return

        # Bee / IL2CPP progress lines like "[ 491/1162  3s] C_iOS_arm64 …" —
        # collapse into a single rolling line. Other log lines between them
        # stay untouched; only the last Bee line is kept visible at a time.
        if _BEE_PROGRESS_RE.match(text):
            bm = getattr(self, "_bee_mark", None)
            bi = getattr(self, "_bee_line_idx", None)
            if bm and bi is not None and not self._paused:
                try:
                    start = self._buffer.get_iter_at_mark(bm)
                    end = start.copy()
                    end.forward_line()
                    self._buffer.delete(start, end)
                    self._buffer.delete_mark(bm)
                except Exception:
                    pass
                self._bee_mark = None
            if bi is not None and 0 <= bi < len(self._full_lines):
                try: self._full_lines.pop(bi)
                except IndexError: pass
            self._full_lines.append(text)
            self._bee_line_idx = len(self._full_lines) - 1
            if not self._paused and self._passes_filter(text):
                end_iter = self._buffer.get_end_iter()
                mark = self._buffer.create_mark(None, end_iter, True)  # left-gravity
                self._insert_tagged(text)
                self._bee_mark = mark
            return

        self._full_lines.append(text)
        if len(self._full_lines) > 10000:
            self._full_lines = self._full_lines[-7000:]
            # Shift bee index if we just trimmed
            if self._bee_line_idx is not None:
                self._bee_line_idx = max(0, self._bee_line_idx - 3000)
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

    def _replace_last_line(self, merged):
        """Replace the last displayed line with merged version."""
        # Find and remove last line in buffer
        end = self._buffer.get_end_iter()
        start = end.copy()
        # Go back to start of last line
        start.backward_line()
        # If last line had a child anchor (trace button), go back one more
        if start.get_child_anchor():
            start.backward_line()
        self._buffer.delete(start, end)
        # Re-insert merged
        if self._passes_filter(merged):
            self._insert_tagged(merged, scroll=True)

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

    @staticmethod
    def _is_trace_line(line):
        s = line.strip()
        if not s:
            return False
        if s.startswith("(at "):
            return True
        # Unity C#-style stack traces
        if (s.startswith("at ") or
                s.startswith("UnityEngine.") or s.startswith("System.") or
                s.startswith("Unity.") or s.startswith("Google.") or
                s.startswith("Firebase.") or s.startswith("KartAuth.") or
                (s.startswith("#") and " in " in s) or
                "--- End of" in s or
                (s.startswith("0x") and " in " in s) or
                ("(at " in s and ".cs:" in s)):
            return True
        # clang / Xcode warning/error continuation lines.
        if s.startswith("In file included from "):
            return True
        if ": note:" in s:
            return True
        # Source-code pointer lines from clang: "NNN | ...code..." and "  | ^"
        import re as _re
        if _re.match(r"^\s*\d+\s*\|", line) or _re.match(r"^\s*\|\s*[\^~]", line):
            return True
        # Xcodebuild dependency graph arrows
        if s.startswith("➜ "):
            return True
        # Xcodebuild action detail lines: the action header (like "CompileC ..."
        # or "CpResource ...") stays visible, but these implementation lines
        # that follow it — `cd`, tool invocation (/Applications/Xcode.app/…,
        # /Users/…/DerivedData/…, /bin/sh -c …, builtin-…), response files,
        # progress reports from rsync/Transfer — all go into the trace block.
        if s.startswith("cd /"):
            return True
        if s.startswith("/Applications/Xcode.app/Contents/Developer/"):
            return True
        if s.startswith("/Users/") and "/Library/Developer/Xcode/" in s:
            return True
        if s.startswith("/bin/sh -c ") or s.startswith("/bin/bash -c "):
            return True
        if s.startswith("builtin-") or s.startswith("Using response file:"):
            return True
        if s.startswith("Transfer starting:") or s.startswith("sent ") and " bytes " in s:
            return True
        if s.startswith("total size is ") or s.startswith("Ignoring --"):
            return True
        # Env-var dumps that Xcode emits before script phases:
        #   export PLATFORM_NAME=iphoneos
        #   export PATH\=/usr/bin:...
        # Also `setenv VAR value` alt form. Hundreds of these per build.
        if s.startswith("export ") and ("=" in s or "\\=" in s):
            return True
        if s.startswith("setenv ") and " " in s[7:]:
            return True
        # Unzip verbose output — one `inflating:`/`extracting:`/`creating:`
        # line per file. Hide the wall of text under the fold.
        if s.startswith("inflating:") or s.startswith("extracting:") or s.startswith("creating:"):
            return True
        return False

    def _insert_tagged(self, text, scroll=True):
        is_trace = self._is_trace_line(text)
        if is_trace:
            self._trace_ended_ago = 0
        else:
            self._trace_ended_ago = getattr(self, '_trace_ended_ago', 99) + 1

        # Detect trace block start: previous line was not trace, this one is
        if is_trace and not getattr(self, '_in_trace', False):
            self._in_trace = True
            # Insert toggle button via child anchor
            end = self._buffer.get_end_iter()
            anchor = self._buffer.create_child_anchor(end)
            btn = Gtk.Button(label="▸ trace", css_classes=["flat", "caption"])
            btn.set_opacity(0.6)
            btn.set_margin_start(4)
            # Mark start of trace
            start_mk = self._buffer.create_mark(None, self._buffer.get_end_iter(), True)
            group = {"start_mark": start_mk, "end_mark": None, "btn": btn, "visible": False}
            self._trace_groups.append(group)
            btn.connect("clicked", lambda _, g=group: self._toggle_trace(g))
            self._view.add_child_at_anchor(btn, anchor)
            # Newline after button
            end = self._buffer.get_end_iter()
            self._buffer.insert(end, "\n")

        if is_trace:
            end = self._buffer.get_end_iter()
            if not self._buffer.get_tag_table().lookup("trace_hidden"):
                self._buffer.create_tag("trace_hidden", invisible=True,
                                         foreground="#888888", scale=0.85)
            tag = self._get_tag(text)
            if tag:
                self._buffer.insert_with_tags_by_name(end, text, tag, "trace_hidden")
            else:
                self._buffer.insert_with_tags_by_name(end, text, "trace_hidden")
        else:
            # End of trace block (but not on empty lines — they appear inside traces)
            if getattr(self, '_in_trace', False) and text.strip():
                self._in_trace = False
                if self._trace_groups:
                    self._trace_groups[-1]["end_mark"] = self._buffer.create_mark(
                        None, self._buffer.get_end_iter(), True)

            # Empty line while in trace: hide it too
            if not text.strip() and getattr(self, '_in_trace', False):
                end = self._buffer.get_end_iter()
                if not self._buffer.get_tag_table().lookup("trace_hidden"):
                    self._buffer.create_tag("trace_hidden", invisible=True,
                                             foreground="#888888", scale=0.85)
                self._buffer.insert_with_tags_by_name(end, text, "trace_hidden")
            else:
                end = self._buffer.get_end_iter()
                tag = self._get_tag(text)
                if tag:
                    self._buffer.insert_with_tags_by_name(end, text, tag)
                else:
                    self._buffer.insert(end, text)

        # Only auto-scroll when the follow-tail lock is engaged AND we're
        # not in a bulk-append (append_lines does a single scroll at the end).
        follow = getattr(self, "_follow_toggle", None)
        in_bulk = getattr(self, "_bulk", False)
        if scroll and not in_bulk and (follow is None or follow.get_active()):
            mk = self._buffer.create_mark(None, self._buffer.get_end_iter(), False)
            self._view.scroll_mark_onscreen(mk)
            self._buffer.delete_mark(mk)

    def _toggle_trace(self, group):
        """Toggle visibility of a trace block."""
        group["visible"] = not group["visible"]
        visible = group["visible"]

        start = self._buffer.get_iter_at_mark(group["start_mark"])
        if group["end_mark"]:
            end = self._buffer.get_iter_at_mark(group["end_mark"])
        else:
            end = self._buffer.get_end_iter()

        # Find trace_hidden tag
        tag = self._buffer.get_tag_table().lookup("trace_hidden")
        if tag:
            tag_visible = self._buffer.get_tag_table().lookup("trace_visible")
            if not tag_visible:
                tag_visible = self._buffer.create_tag("trace_visible",
                    invisible=False, foreground="#888888", scale=0.85)

            if visible:
                self._buffer.remove_tag(tag, start, end)
                self._buffer.apply_tag(tag_visible, start, end)
                group["btn"].set_label("▾ trace")
            else:
                self._buffer.remove_tag(tag_visible, start, end)
                self._buffer.apply_tag(tag, start, end)
                group["btn"].set_label("▸ trace")

    def _rebuild(self):
        self._buffer.set_text("")
        self._trace_groups = []
        self._in_trace = False
        for line in self._full_lines:
            if self._passes_filter(line):
                self._insert_tagged(line, scroll=False)
        follow = getattr(self, "_follow_toggle", None)
        if follow is None or follow.get_active():
            self.scroll_to_bottom()

    def _on_filter(self, *_):
        self._rebuild()
        self._update_ctx_action()

    def _update_ctx_action(self):
        has_filter = bool(self._search.get_text().strip()) or self._level_filter.get_selected() != 0
        self._ctx_action.set_enabled(has_filter)

    def _on_copy(self, _):
        """Copy currently visible (filtered) log text to clipboard."""
        text = self._buffer.get_text(
            self._buffer.get_start_iter(), self._buffer.get_end_iter(), False)
        display = self._view.get_display()
        if display:
            clipboard = display.get_clipboard()
            from gi.repository import Gdk
            clipboard.set(text)

    def _on_track_click(self, gesture, n_press, x, y):
        """Track right-click position for context menu action."""
        bx, by = self._view.window_to_buffer_coords(Gtk.TextWindowType.WIDGET, int(x), int(y))
        ok, it = self._view.get_iter_at_location(bx, by)
        if ok:
            self._last_click_line = it.get_line()

    def _on_show_context(self, *_):
        """Show clicked line in full unfiltered log."""
        line_num = self._last_click_line
        if line_num < 0:
            return
        ok, start = self._buffer.get_iter_at_line(line_num)
        if not ok:
            return
        end = start.copy()
        end.forward_to_line_end()
        clicked_text = self._buffer.get_text(start, end, False).strip()
        if not clicked_text:
            return

        # Find this line in full log
        target_idx = None
        for i, full_line in enumerate(self._full_lines):
            if clicked_text in full_line:
                target_idx = i
                break
        if target_idx is None:
            return

        # Clear filters
        self._search.set_text("")
        self._level_filter.set_selected(0)
        self._rebuild()

        # Scroll after GTK layout update
        def do_scroll():
            ok, it = self._buffer.get_iter_at_line(target_idx)
            if ok:
                mk = self._buffer.create_mark("ctx", it, True)
                self._view.scroll_mark_onscreen(mk)
                end = it.copy()
                end.forward_to_line_end()
                self._buffer.select_range(it, end)
                self._buffer.delete_mark(mk)
            return False

        from gi.repository import GLib
        GLib.idle_add(do_scroll)

    def _on_wrap(self, btn):
        self._view.set_wrap_mode(
            Gtk.WrapMode.WORD_CHAR if btn.get_active() else Gtk.WrapMode.NONE)