"""Reusable log viewer widget with search, level filter, word wrap, and color tags."""
import re
from gi.repository import Gtk, Gio, GLib


# Matches Unity Bee / IL2CPP progress lines like:
#   "[ 491/1162  3s] C_iOS_arm64 Il2CppTempDirArtifacts/..."
# Back-to-back matches are collapsed into a single updating line in the buffer.
_BEE_PROGRESS_RE = re.compile(r'^\s*\[\s*\d+\s*/\s*\d+\s+\d+[ms]?\]')

# Dedup normalization: replace hex runs, long alphanumeric identifiers, and
# number runs with "#" so two lines that only differ in GUIDs, mangled
# IL2CPP names, or IL2CPP-generated object file names (e.g. ss2j8xu5o4di.o)
# look identical after normalization. Order matters — broadest last so
# intermediate "#" placeholders don't get re-normalized.
_DEDUP_HEX_RE = re.compile(r'[0-9a-fA-F]{8,}')
_DEDUP_NUM_RE = re.compile(r'\d{2,}')
_DEDUP_ALNUM_RE = re.compile(r'[A-Za-z0-9]{10,}')  # base36-ish hashes, mangled names


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

        # Global trace-visibility toggle. Replaces per-block inline Gtk.Button
        # anchors — those froze GTK when xcodebuild emitted hundreds of
        # trace transitions (each created a new widget in the TextView).
        # Now we just flip the `trace_hidden` tag's `invisible` property.
        self._trace_toggle = Gtk.ToggleButton(
            icon_name="view-more-horizontal-symbolic",
            tooltip_text="Show verbose trace lines",
            active=False)
        self._trace_toggle.connect("toggled", self._on_trace_toggle)
        search_box.append(self._trace_toggle)

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

        # Trace folding — no inline widgets (Gtk.Button at every trace block
        # froze GTK when xcodebuild emitted hundreds of sections). Instead:
        #   • each trace block gets a plain-text marker line "⋯ trace ⋯"
        #     (just tagged text, no widget → zero extra layout cost)
        #   • hidden trace lines carry the `trace_hidden` tag (invisible=True)
        #   • right-click on a marker → context-menu "Expand/Collapse Trace"
        #     toggles visibility of that specific block
        #   • toolbar toggle flips ALL blocks globally
        self._trace_groups = []  # [{"marker_start", "content_start", "content_end", "expanded"}]
        self._in_trace = False
        self._trace_ended_ago = 99
        self._buffer.create_tag("trace_filename", foreground="#6e9bcf", scale=0.8, invisible=True)
        self._buffer.create_tag("trace_marker", foreground="#6a6a6a", scale=0.9)

        # Dedup state. `_dedup_hist` keeps the last few inserted normal lines
        # (norm, text, line_start_mark) so we can detect single or pair
        # repeats and retract them into a collapsed group. `_dedup_active`
        # is the group we're currently extending (or None).
        self._dedup_hist = []
        self._dedup_active = None

        # Chunked-drain queue for append_lines. Xcodebuild startup emits
        # thousands of env-var/build-setting lines in one burst — processing
        # them all synchronously in a single GTK user-action would block the
        # main loop past GTK's "not responding" watchdog. Instead we enqueue
        # the batch and drain CHUNK lines per GLib idle tick, so GTK processes
        # input/redraws between chunks and the UI stays live.
        self._pending_lines = []
        self._drain_scheduled = False

        # Track right-click position for context menu
        self._last_click_line = -1
        rclick = Gtk.GestureClick(button=3)
        rclick.connect("pressed", self._on_track_click)
        self._view.add_controller(rclick)

        # Context menu: "Show in context" for filtered view + per-block
        # trace toggle (enabled only when right-clicked on a marker line).
        menu_model = Gio.Menu()
        menu_model.append("Expand/Collapse Trace", "logview.toggle-trace")
        menu_model.append("Show in Context", "logview.show-context")
        self._view.set_extra_menu(menu_model)

        self._ctx_action = Gio.SimpleAction.new("show-context", None)
        self._ctx_action.connect("activate", self._on_show_context)
        self._ctx_action.set_enabled(False)
        self._trace_action = Gio.SimpleAction.new("toggle-trace", None)
        self._trace_action.connect("activate", self._on_toggle_trace_here)
        self._trace_action.set_enabled(False)
        group = Gio.SimpleActionGroup()
        group.add_action(self._ctx_action)
        group.add_action(self._trace_action)
        self._view.insert_action_group("logview", group)

        self._scroll = Gtk.ScrolledWindow(vexpand=True)
        self._scroll.set_child(self._view)
        self._scroll.add_css_class("card")
        # Lazy-load older lines when user scrolls near the top. Index of
        # buffer-line-0 inside self._full_lines; advances when we trim the
        # TextView, rewinds when we prepend older content on scroll.
        self._buffer_first_idx = 0
        self._loading_older = False  # re-entrancy guard
        self._scroll.get_vadjustment().connect("value-changed", self._on_scroll)

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
        self._buffer_first_idx = 0
        self._pending_lines = []

    def set_exclude_patterns(self, patterns):
        """Update exclude patterns at runtime."""
        self._exclude_patterns = patterns or []

    # Hard cap on lines held in the TextView buffer. GTK re-lays out the
    # buffer on every insert; at tens of thousands of lines the main thread
    # can't service events inside the 5s watchdog window and the user sees
    # "Force quit or wait". Full log is still kept in self._full_lines and
    # saved to disk by the worker — this only bounds the live view.
    _BUFFER_CAP_LINES = 3000
    _BUFFER_TRIM_KEEP = 2000  # trim down to this size when cap is exceeded

    _DRAIN_CHUNK = 100          # upper bound on lines per GLib idle tick
    _DRAIN_TIME_BUDGET = 0.05   # seconds — hard cap on wall time per tick
    _PENDING_CAP = 3000         # hard cap on queued lines awaiting drain
    _PENDING_KEEP = 2000        # when cap exceeded, trim down to this tail

    def append_lines(self, lines):
        """Enqueue lines for chunked drain. A huge flush (xcodebuild startup
        dumps thousands of env/build-setting lines at once) is split across
        GLib idle ticks so GTK's main loop stays responsive between chunks —
        otherwise the "not responding / force quit" watchdog fires.

        If the window is backgrounded while logs keep streaming, the queue
        is capped at _PENDING_CAP and trimmed to the tail _PENDING_KEEP so
        the user sees recent output on restore, not minutes-old prelude —
        and drain finishes in bounded time. Full log still persists in
        worker.py's `_save_log`, so nothing is truly lost."""
        if not lines:
            return
        self._pending_lines.extend(lines)
        if len(self._pending_lines) > self._PENDING_CAP:
            dropped = len(self._pending_lines) - self._PENDING_KEEP
            del self._pending_lines[:dropped]
            # Tell the user once; don't re-announce for every overflow tick.
            self._pending_lines.insert(0,
                f"  ⋯ {dropped} older lines dropped (window was inactive) ⋯\n")
        if not self._drain_scheduled:
            self._drain_scheduled = True
            GLib.idle_add(self._drain_pending_chunk)

    def _drain_pending_chunk(self):
        if not self._pending_lines:
            self._drain_scheduled = False
            return False
        # Time-budgeted drain: process up to _DRAIN_CHUNK lines OR until
        # wall-time budget is exhausted, whichever comes first. Prevents a
        # single expensive batch (dedup regex + GTK inserts) from blocking
        # the main loop beyond the compositor "not responding" window.
        import time as _time
        deadline = _time.monotonic() + self._DRAIN_TIME_BUDGET
        self._bulk = True  # suppress per-line scroll; one scroll at the end
        self._buffer.begin_user_action()
        processed = 0
        try:
            limit = min(self._DRAIN_CHUNK, len(self._pending_lines))
            while processed < limit and _time.monotonic() < deadline:
                self.append_line(self._pending_lines[processed])
                processed += 1
            self._trim_buffer_if_needed()
        finally:
            if processed:
                del self._pending_lines[:processed]
            self._buffer.end_user_action()
            self._bulk = False
        # Skip scroll when the widget isn't on-screen. scroll_mark_onscreen
        # forces TextView layout up to the mark — cheap when the window is
        # mapped and already laid out, painfully expensive when GTK has been
        # skipping layout in the background and the buffer has grown by
        # thousands of lines. Just redraw the tail when the window comes back.
        follow = getattr(self, "_follow_toggle", None)
        if (follow is None or follow.get_active()) and self._view.get_mapped():
            mk = self._buffer.create_mark(None, self._buffer.get_end_iter(), False)
            self._view.scroll_mark_onscreen(mk)
            self._buffer.delete_mark(mk)
        if self._pending_lines:
            return True  # reschedule on next idle; GTK processes events between
        self._drain_scheduled = False
        return False

    def _trim_buffer_if_needed(self):
        """If the TextView buffer exceeds _BUFFER_CAP_LINES, delete the
        oldest lines so only the last _BUFFER_TRIM_KEEP remain. Any trace
        groups whose marks fall in the deleted range get dropped from
        _trace_groups. Bee-coalescing marks get invalidated safely — the
        next Bee line will just start a fresh one.

        The dropped content is NOT lost — it stays in self._full_lines, and
        scrolling near the top of the view will lazy-load it back via
        _prepend_older."""
        total = self._buffer.get_line_count()
        if total <= self._BUFFER_CAP_LINES:
            return
        drop = total - self._BUFFER_TRIM_KEEP
        # Count trace markers whose line falls inside [0, drop) — those are
        # decorations we inserted, not entries in _full_lines, so we must
        # subtract them from `drop` when advancing _buffer_first_idx.
        markers_in_dropped = 0
        for g in self._trace_groups:
            ms = g.get("marker_start")
            if ms and not ms.get_deleted():
                if self._buffer.get_iter_at_mark(ms).get_line() < drop:
                    markers_in_dropped += 1
        start = self._buffer.get_start_iter()
        end = start.copy()
        if not end.forward_lines(drop):
            end = self._buffer.get_end_iter()
        self._buffer.delete(start, end)
        self._buffer_first_idx += (drop - markers_in_dropped)
        # Clean up trace groups whose marks were inside the deleted range
        self._trace_groups = [
            g for g in self._trace_groups
            if g.get("marker_start") and not g["marker_start"].get_deleted()
        ]
        # If the in-progress trace group got trimmed, reset state so the next
        # trace line starts a fresh block with its own marker.
        if self._in_trace and not self._trace_groups:
            self._in_trace = False
        # Bee mark — if it was in the deleted range, it's gone. Clear ref so
        # next Bee line starts a new rolling line.
        bm = getattr(self, "_bee_mark", None)
        if bm and bm.get_deleted():
            self._bee_mark = None

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
            # Shift bee index and buffer-first-idx since 3000 entries left
            # the front of _full_lines.
            if self._bee_line_idx is not None:
                self._bee_line_idx = max(0, self._bee_line_idx - 3000)
            self._buffer_first_idx = max(0, self._buffer_first_idx - 3000)
        if not self._paused and self._passes_filter(text):
            # Dedup path — collapse repeating patterns into a marker group.
            # Returns True if the line was absorbed into a dedup group
            # (nothing else to do); False if it should be inserted normally.
            if self._dedup_try_absorb(text):
                return
            self._insert_tagged(text)
            self._dedup_record_last_line(text)

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

        # Trace block start: drop a plain-text marker line before the hidden
        # content. Marker is a normal line of tagged text — no widget, no
        # child anchor, cheap to render at scale. Right-click on it toggles
        # the block via the "toggle-trace" context-menu action.
        if is_trace and not getattr(self, '_in_trace', False):
            self._in_trace = True
            if not self._buffer.get_tag_table().lookup("trace_hidden"):
                self._buffer.create_tag("trace_hidden", invisible=True,
                                         foreground="#888888", scale=0.85)
            end = self._buffer.get_end_iter()
            marker_start = self._buffer.create_mark(None, end, True)
            self._buffer.insert_with_tags_by_name(end, "  ⋯ trace ⋯\n", "trace_marker")
            content_start = self._buffer.create_mark(
                None, self._buffer.get_end_iter(), True)
            self._trace_groups.append({
                "marker_start": marker_start,
                "content_start": content_start,
                "content_end": None,
                "expanded": False,
            })

        if is_trace:
            end = self._buffer.get_end_iter()
            tag = self._get_tag(text)
            if tag:
                self._buffer.insert_with_tags_by_name(end, text, tag, "trace_hidden")
            else:
                self._buffer.insert_with_tags_by_name(end, text, "trace_hidden")
            # Track end of current block so right-click toggle knows the range
            if self._trace_groups:
                grp = self._trace_groups[-1]
                end_iter = self._buffer.get_end_iter()
                if grp["content_end"]:
                    self._buffer.move_mark(grp["content_end"], end_iter)
                else:
                    grp["content_end"] = self._buffer.create_mark(
                        None, end_iter, False)  # right-gravity: grows with appends
        else:
            # End of trace block (but not on empty lines — they appear inside traces)
            if getattr(self, '_in_trace', False) and text.strip():
                self._in_trace = False
                if self._trace_groups and not self._trace_groups[-1]["content_end"]:
                    self._trace_groups[-1]["content_end"] = self._buffer.create_mark(
                        None, self._buffer.get_end_iter(), True)

            # Empty line while in trace: hide it too
            if not text.strip() and getattr(self, '_in_trace', False):
                end = self._buffer.get_end_iter()
                if not self._buffer.get_tag_table().lookup("trace_hidden"):
                    self._buffer.create_tag("trace_hidden", invisible=True,
                                             foreground="#888888", scale=0.85)
                self._buffer.insert_with_tags_by_name(end, text, "trace_hidden")
                if self._trace_groups and self._trace_groups[-1]["content_end"]:
                    self._buffer.move_mark(
                        self._trace_groups[-1]["content_end"],
                        self._buffer.get_end_iter())
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

    def _on_trace_toggle(self, btn):
        """Flip the global `trace_hidden` tag's `invisible` property — shows
        or hides every trace line at once. Constant-time regardless of how
        many trace sections exist (unlike per-block widget toggles)."""
        tag = self._buffer.get_tag_table().lookup("trace_hidden")
        if tag:
            tag.set_property("invisible", not btn.get_active())

    def _rebuild(self):
        self._buffer.set_text("")
        self._trace_groups = []
        self._in_trace = False
        self._dedup_hist = []
        self._dedup_active = None
        self._buffer_first_idx = 0
        for line in self._full_lines:
            if self._passes_filter(line):
                if self._dedup_try_absorb(line):
                    continue
                self._insert_tagged(line, scroll=False)
                self._dedup_record_last_line(line)
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
        """Track right-click position for context menu action. Also decides
        whether "Expand/Collapse Trace" is enabled — only if click landed on
        a trace marker line."""
        bx, by = self._view.window_to_buffer_coords(Gtk.TextWindowType.WIDGET, int(x), int(y))
        ok, it = self._view.get_iter_at_location(bx, by)
        if ok:
            self._last_click_line = it.get_line()
            grp = self._find_trace_group_by_line(self._last_click_line)
            self._trace_action.set_enabled(grp is not None)
        else:
            self._last_click_line = -1
            self._trace_action.set_enabled(False)

    def _find_trace_group_by_line(self, line):
        """Return the trace group whose marker line is `line`, or None."""
        for grp in self._trace_groups:
            ms = grp.get("marker_start")
            if ms and not ms.get_deleted():
                it = self._buffer.get_iter_at_mark(ms)
                if it.get_line() == line:
                    return grp
        return None

    @staticmethod
    def _dedup_norm(text):
        """Normalize a line for dedup comparison — collapse hex runs, long
        alphanumeric identifiers, and digit runs so lines that only differ
        in mangled/hashed parts match."""
        n = _DEDUP_HEX_RE.sub('#', text)
        n = _DEDUP_ALNUM_RE.sub('#', n)
        n = _DEDUP_NUM_RE.sub('#', n)
        return n.strip()

    def _dedup_record_last_line(self, text):
        """After a regular insert, remember this line in _dedup_hist with a
        mark at its start so we can later retract it if a pattern emerges."""
        # The line was just inserted at end of buffer. Mark its start: walk
        # back one line from end.
        end = self._buffer.get_end_iter()
        start = end.copy()
        start.backward_line()
        mk = self._buffer.create_mark(None, start, True)  # left-gravity
        self._dedup_hist.append((self._dedup_norm(text), text, mk))
        # Keep history bounded; drop marks we no longer need.
        while len(self._dedup_hist) > 4:
            old = self._dedup_hist.pop(0)
            try: self._buffer.delete_mark(old[2])
            except Exception: pass

    def _dedup_try_absorb(self, text):
        """Either extend the active dedup group, start a new one, or do
        nothing. Returns True if the line was handled (don't insert
        normally)."""
        norm = self._dedup_norm(text)

        # Extend active group?
        act = self._dedup_active
        if act is not None:
            norms = act["norms"]
            expected = norms[act["phase"]]
            if norm == expected:
                # Matches — append this line into the hidden content.
                tag_table = self._buffer.get_tag_table()
                if not tag_table.lookup("trace_hidden"):
                    self._buffer.create_tag("trace_hidden", invisible=True,
                                             foreground="#888888", scale=0.85)
                end = self._buffer.get_end_iter()
                self._buffer.insert_with_tags_by_name(end, text, "trace_hidden")
                ce = act["group"].get("content_end")
                if ce:
                    self._buffer.move_mark(ce, self._buffer.get_end_iter())
                act["phase"] = (act["phase"] + 1) % len(norms)
                if act["phase"] == 0:
                    act["count"] += 1
                    self._dedup_update_marker(act)
                return True
            # Pattern broken — finalize active, fall through to maybe start new.
            self._dedup_active = None

        # Start new group?
        hist = self._dedup_hist
        # Single-line repeat: current == last
        if len(hist) >= 1 and hist[-1][0] == norm:
            self._dedup_start(text, norm, pattern_len=1)
            return True
        # Pair repeat: current == line 2-back (and line-1 differs, so it's
        # alternating A/B/A/B — not just AAA which was caught above)
        if len(hist) >= 2 and hist[-2][0] == norm and hist[-1][0] != norm:
            self._dedup_start(text, norm, pattern_len=2)
            return True
        return False

    def _dedup_start(self, text, norm, pattern_len):
        """Retract the last `pattern_len` inserted lines from the buffer,
        insert a collapsed `⋯ N similar ⋯` marker in their place, re-insert
        them hidden, then append `text` hidden. Records state in
        _dedup_active and adds the group to _trace_groups for right-click
        toggle."""
        if pattern_len < 1 or len(self._dedup_hist) < pattern_len:
            return
        retracted = self._dedup_hist[-pattern_len:]
        # Retract visible lines: delete from start of first retracted line
        # to end of buffer. retracted[0][2] is its start mark.
        try:
            start_mark = retracted[0][2]
            if start_mark.get_deleted():
                return
            retract_iter = self._buffer.get_iter_at_mark(start_mark)
        except Exception:
            return
        end_iter = self._buffer.get_end_iter()
        self._buffer.delete(retract_iter, end_iter)
        # Drop these entries' marks (they're deleted now along with the text)
        for (_, _, m) in retracted:
            try: self._buffer.delete_mark(m)
            except Exception: pass
        self._dedup_hist = self._dedup_hist[:-pattern_len]

        # Insert collapsed marker at the retraction point
        tag_table = self._buffer.get_tag_table()
        if not tag_table.lookup("trace_hidden"):
            self._buffer.create_tag("trace_hidden", invisible=True,
                                     foreground="#888888", scale=0.85)
        end = self._buffer.get_end_iter()
        marker_start = self._buffer.create_mark(None, end, True)
        count = 2  # retracted pattern_len lines + current = at least 2 of the pattern
        # For pattern_len=1 (AA): count = 2 initially (prev+current), both same norm
        # For pattern_len=2 (AB AB): count = 1 pair initially, we'll increment on cycles
        if pattern_len == 1:
            label = self._dedup_marker_label(count, 1)
        else:
            label = self._dedup_marker_label(1, 2)
        self._buffer.insert_with_tags_by_name(end, label, "trace_marker")
        content_start = self._buffer.create_mark(
            None, self._buffer.get_end_iter(), True)
        # Re-insert retracted lines (hidden) and the current line
        all_lines = [t for (_, t, _) in retracted] + [text]
        for line in all_lines:
            end = self._buffer.get_end_iter()
            self._buffer.insert_with_tags_by_name(end, line, "trace_hidden")
        content_end = self._buffer.create_mark(
            None, self._buffer.get_end_iter(), False)

        group = {
            "marker_start": marker_start,
            "content_start": content_start,
            "content_end": content_end,
            "expanded": False,
        }
        self._trace_groups.append(group)

        norms = (norm,) if pattern_len == 1 else (retracted[-1][0], norm)
        # After appending all_lines, phase should point to what norm is
        # expected NEXT. For pattern_len=1, always expect same norm → phase=0.
        # For pattern_len=2, we just inserted the B line, so next expected = A.
        next_phase = 0
        self._dedup_active = {
            "group": group,
            "norms": norms,
            "phase": next_phase,
            "count": count if pattern_len == 1 else 1,
            "pattern_len": pattern_len,
        }

    @staticmethod
    def _dedup_marker_label(count, pattern_len):
        """Text shown for the collapsed marker. Updated as count grows."""
        unit = "lines" if pattern_len == 1 else "pairs"
        return f"  ⋯ {count} similar {unit} ⋯\n"

    def _dedup_update_marker(self, act):
        """Rewrite the collapsed marker line to show the current count.
        Finds the marker's line via marker_start mark, deletes that line,
        re-inserts with new label."""
        grp = act["group"]
        ms = grp.get("marker_start")
        if not ms or ms.get_deleted():
            return
        start = self._buffer.get_iter_at_mark(ms)
        end = start.copy()
        if not end.forward_line():
            end = self._buffer.get_end_iter()
        self._buffer.delete(start, end)
        new_label = self._dedup_marker_label(act["count"], act["pattern_len"])
        insert_iter = self._buffer.get_iter_at_mark(ms)
        self._buffer.insert_with_tags_by_name(insert_iter, new_label, "trace_marker")

    def _on_scroll(self, adj):
        """Lazy-load older lines when the user scrolls near the top.
        Trigger at ~half a page-height above 0 so content appears before
        they actually hit the boundary."""
        if self._loading_older or self._buffer_first_idx <= 0:
            return
        if adj.get_value() < adj.get_page_size() * 0.5:
            self._prepend_older()

    def _prepend_older(self, count=500):
        """Take up to `count` older entries from self._full_lines and insert
        them at the top of the buffer, preserving the user's scroll
        position. Older trace runs get their own marker + content marks
        inserted at the front so per-block toggles keep working."""
        if self._buffer_first_idx <= 0:
            return
        self._loading_older = True
        try:
            start_idx = max(0, self._buffer_first_idx - count)
            chunk = self._full_lines[start_idx : self._buffer_first_idx]
            if not chunk:
                return
            adj = self._scroll.get_vadjustment()
            prev_upper = adj.get_upper()
            prev_value = adj.get_value()
            buf = self._buffer
            if not buf.get_tag_table().lookup("trace_hidden"):
                buf.create_tag("trace_hidden", invisible=True,
                               foreground="#888888", scale=0.85)
            buf.begin_user_action()
            try:
                # Group into (kind, lines) segments so each trace run gets
                # one marker up front, matching the live-append UX.
                segments = []
                cur_kind = None
                cur = []
                for line in chunk:
                    kind = "trace" if self._is_trace_line(line) else "visible"
                    if kind != cur_kind:
                        if cur: segments.append((cur_kind, cur))
                        cur_kind = kind
                        cur = []
                    cur.append(line)
                if cur: segments.append((cur_kind, cur))

                offset = 0
                new_groups = []  # prepend to _trace_groups in order
                for kind, lines in segments:
                    if kind == "trace":
                        it = buf.get_iter_at_offset(offset)
                        marker_start = buf.create_mark(None, it, True)
                        marker_text = "  ⋯ trace ⋯\n"
                        buf.insert_with_tags_by_name(it, marker_text, "trace_marker")
                        offset += len(marker_text)
                        content_start_offset = offset
                        for line in lines:
                            it = buf.get_iter_at_offset(offset)
                            tag = self._get_tag(line)
                            if tag:
                                buf.insert_with_tags_by_name(it, line, tag, "trace_hidden")
                            else:
                                buf.insert_with_tags_by_name(it, line, "trace_hidden")
                            offset += len(line)
                        cs = buf.create_mark(None,
                            buf.get_iter_at_offset(content_start_offset), True)
                        ce = buf.create_mark(None,
                            buf.get_iter_at_offset(offset), False)
                        new_groups.append({
                            "marker_start": marker_start,
                            "content_start": cs,
                            "content_end": ce,
                            "expanded": False,
                        })
                    else:
                        for line in lines:
                            it = buf.get_iter_at_offset(offset)
                            tag = self._get_tag(line)
                            if tag:
                                buf.insert_with_tags_by_name(it, line, tag)
                            else:
                                buf.insert(it, line)
                            offset += len(line)
                # Prepend preserving visual order
                self._trace_groups = new_groups + self._trace_groups
            finally:
                buf.end_user_action()
            self._buffer_first_idx = start_idx
            # Preserve scroll: new_value = prev_value + (new_upper - prev_upper)
            def _restore():
                adj2 = self._scroll.get_vadjustment()
                new_upper = adj2.get_upper()
                adj2.set_value(prev_value + (new_upper - prev_upper))
                return False
            GLib.idle_add(_restore)
        finally:
            self._loading_older = False

    def _on_toggle_trace_here(self, *_):
        """Expand or collapse the specific trace block whose marker was
        right-clicked. Flips `trace_hidden` tag on/off over the block range."""
        grp = self._find_trace_group_by_line(self._last_click_line)
        if not grp:
            return
        tag = self._buffer.get_tag_table().lookup("trace_hidden")
        cs = grp.get("content_start")
        ce = grp.get("content_end")
        if not tag or not cs or not ce or cs.get_deleted() or ce.get_deleted():
            return
        start = self._buffer.get_iter_at_mark(cs)
        end = self._buffer.get_iter_at_mark(ce)
        if grp["expanded"]:
            self._buffer.apply_tag(tag, start, end)
            grp["expanded"] = False
        else:
            self._buffer.remove_tag(tag, start, end)
            grp["expanded"] = True

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