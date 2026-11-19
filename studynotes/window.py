"""Main window: note list, Markdown editor, note actions."""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import urllib.parse
from pathlib import Path

from gi.repository import Adw, Gdk, Gio, GLib, Gtk

from common import (TMP_DIR, Worker, course_of, find_note_by_link, human_date, rename_speakers_text, load_config, note_date, parse_transcript, safe_name,
                    signed_in, split_note, transcript_speakers, unique_path, update_config, vault_dir,
                    vault_notes)
from importer import ImportDialog
from md import WIKILINK, MarkdownStyler
from record import RecordDialog


class RegenDialog(Adw.Dialog):
    """Re-run Claude over a note's saved transcript."""

    def __init__(self, window: "MainWindow", path: Path):
        super().__init__()
        self.win, self.path = window, path
        self.worker: Worker | None = None
        self.set_title("Regenerate Notes")
        self.set_content_width(520)
        self.set_content_height(560)
        self.connect("closed", lambda *_: self.worker and self.worker.cancel())
        tv = Adw.ToolbarView()
        tv.add_top_bar(Adw.HeaderBar())
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, margin_start=18, margin_end=18,
                      margin_bottom=18)
        self.status = Gtk.Label(label="Starting…", css_classes=["title-3"], xalign=0)
        self.detail = Gtk.Label(css_classes=["dim-label"], xalign=0, wrap=True)
        self.preview = Gtk.TextView(editable=False, wrap_mode=Gtk.WrapMode.WORD_CHAR, cursor_visible=False,
                                    left_margin=12, right_margin=12, top_margin=10, bottom_margin=10)
        sc = Gtk.ScrolledWindow(vexpand=True, css_classes=["card"])
        sc.set_child(self.preview)
        self.sc = sc
        for w in (self.status, self.detail, sc):
            box.append(w)
        tv.set_content(box)
        self.set_child(tv)
        self._run()

    def _run(self):
        text = self.path.read_text()
        fm, _head, tr = split_note(text)
        segs, names = parse_transcript(tr)
        if not segs:
            self.status.set_text("This note has no transcript to work from.")
            return
        data = {"duration": 0, "segments": segs, "speakers": sorted(names)}
        TMP_DIR.mkdir(parents=True, exist_ok=True)
        self.tmp = TMP_DIR / f"regen-{os.getpid()}.json"
        self.tmp.write_text(json.dumps(data))
        cfg = load_config()
        self.status.set_text("Writing notes…")
        self.worker = Worker(
            ["notes", "--transcript", str(self.tmp), "--vault", cfg["vault"],
             "--class", fm.get("course") or fm.get("class") or course_of(self.path), "--model", cfg["model"],
             "--out", str(self.path), "--speakers", json.dumps({str(k): v for k, v in names.items()}),
             "--budget", str(cfg["budget"]),
             *[x for e in cfg.get("extra_context", []) for x in ("--extra", e)]],
            self._event, {"ANTHROPIC_API_KEY": ""})

    def _event(self, ev):
        k = ev.get("event")
        if k == "progress":
            self.detail.set_text(ev.get("detail", ""))
        elif k == "text":
            b = self.preview.get_buffer()
            b.insert(b.get_end_iter(), ev["delta"])
            adj = self.sc.get_vadjustment()
            GLib.idle_add(lambda: adj.set_value(adj.get_upper()))
        elif k == "result":
            self._apply(ev["body"])
        elif k == "error":
            self.status.set_text("Could not write notes")
            self.detail.set_text(ev["message"])
        return False

    def _apply(self, body: str):
        text = self.path.read_text()
        fm, head, tr = split_note(text)
        m = re.match(r"\A---\n.*?\n---\n", text, re.S)
        meta = text[:m.end()] if m else ""
        model = load_config()["model"]
        meta = re.sub(r"^model: .*$", f"model: {model}", meta, flags=re.M)
        title = re.search(r"^# .*$", head, re.M)
        title_line = title[0] if title else f"# {self.path.stem}"
        self.path.write_text(f"{meta}{title_line}\n\n---\n\n{body}\n{tr}")
        try:
            self.tmp.unlink()
        except OSError:
            pass
        self.close()
        self.win.reload_current(force=True)
        self.win.toast("Notes regenerated")


class MainWindow(Adw.ApplicationWindow):
    def __init__(self, app):
        cfg = load_config()
        super().__init__(application=app, title="Study Notes", default_width=cfg["win_w"],
                         default_height=cfg["win_h"])
        self.current: Path | None = None
        self.loaded_mtime = 0.0
        self.loading = False
        self.save_id = 0
        self._refreshing = False

        self.toasts = Adw.ToastOverlay()
        self.split = Adw.NavigationSplitView(min_sidebar_width=270, max_sidebar_width=360,
                                             sidebar_width_fraction=0.28)
        self.toasts.set_child(self.split)
        self.set_content(self.toasts)
        bp = Adw.Breakpoint.new(Adw.BreakpointCondition.parse("max-width: 680sp"))
        bp.add_setter(self.split, "collapsed", True)
        self.add_breakpoint(bp)

        self._build_sidebar()
        self._build_content()
        self._build_actions()

        drop = Gtk.DropTarget.new(Gdk.FileList, Gdk.DragAction.COPY)
        drop.connect("drop", self._on_drop)
        self.add_controller(drop)

        self.connect("close-request", self._on_close)
        self.connect("notify::is-active", lambda *_: self.is_active() and self.refresh_notes())
        self.refresh_notes()
        last = cfg.get("last_note")
        if last and Path(last).exists():
            self.select_path(Path(last))

    # ------------------------------------------------------------------
    # layout
    # ------------------------------------------------------------------
    def _build_sidebar(self):
        sb = Adw.ToolbarView()
        hb = Adw.HeaderBar()
        rec = Gtk.Button(icon_name="media-record-symbolic", tooltip_text="New recording (Ctrl+R)",
                         css_classes=["suggested-action"])
        rec.set_action_name("app.record")
        imp = Gtk.Button(icon_name="document-save-symbolic", tooltip_text="Import recordings (Ctrl+I)")
        imp.set_icon_name("folder-download-symbolic")
        imp.set_action_name("app.import")
        hb.pack_start(rec)
        hb.pack_start(imp)
        menu = Gio.Menu()
        sec = Gio.Menu()
        sec.append("New Class…", "app.new-class")
        sec.append("Open Vault in Obsidian", "app.open-vault")
        sec.append("Show Vault in Files", "app.show-vault")
        menu.append_section(None, sec)
        sec2 = Gio.Menu()
        sec2.append("Preferences", "app.preferences")
        sec2.append("Keyboard Shortcuts", "app.shortcuts")
        sec2.append("About Study Notes", "app.about")
        menu.append_section(None, sec2)
        hb.pack_end(Gtk.MenuButton(icon_name="open-menu-symbolic", menu_model=menu, primary=True,
                                   tooltip_text="Main menu"))
        sb.add_top_bar(hb)
        self.search = Gtk.SearchEntry(placeholder_text="Search notes", margin_start=10, margin_end=10,
                                      margin_bottom=8)
        self.search.connect("search-changed", lambda *_: self.listbox.invalidate_filter())
        self.search.connect("stop-search", lambda *_: self.search.set_text(""))
        sb.add_top_bar(self.search)
        self.listbox = Gtk.ListBox(selection_mode=Gtk.SelectionMode.SINGLE, css_classes=["navigation-sidebar"])
        self.listbox.set_header_func(self._header)
        self.listbox.set_filter_func(self._filter)
        self.listbox.connect("row-selected", self._row_selected)
        sc = Gtk.ScrolledWindow(vexpand=True)
        sc.set_child(self.listbox)
        self.sidebar_stack = Gtk.Stack()
        self.sidebar_stack.add_named(sc, "list")
        self.sidebar_stack.add_named(Adw.StatusPage(
            title="No Notes Yet", icon_name="document-edit-symbolic", description="Your notes will show up here."),
            "empty")
        sb.set_content(self.sidebar_stack)
        self.split.set_sidebar(Adw.NavigationPage(child=sb, title="Notes"))

    def _build_content(self):
        cv = Adw.ToolbarView()
        self.title_widget = Adw.WindowTitle(title="", subtitle="")
        chb = Adw.HeaderBar(title_widget=self.title_widget)
        menu = Gio.Menu()
        a = Gio.Menu()
        a.append("Rename…", "win.rename")
        a.append("Rename Speakers…", "win.rename-speakers")
        a.append("Regenerate Notes…", "win.regenerate")
        menu.append_section(None, a)
        b = Gio.Menu()
        b.append("Copy as Markdown", "win.copy-markdown")
        b.append("Open in Obsidian", "win.open-obsidian")
        b.append("Show in Files", "win.show-in-files")
        menu.append_section(None, b)
        c = Gio.Menu()
        c.append("Move to Trash", "win.trash")
        menu.append_section(None, c)
        self.note_menu = Gtk.MenuButton(icon_name="view-more-symbolic", menu_model=menu, sensitive=False,
                                        tooltip_text="Note actions")
        chb.pack_end(self.note_menu)
        cv.add_top_bar(chb)

        self.view = Gtk.TextView(wrap_mode=Gtk.WrapMode.WORD_CHAR, top_margin=22, bottom_margin=64,
                                 left_margin=28, right_margin=28, pixels_below_lines=3,
                                 pixels_inside_wrap=4, css_classes=["note-editor"], accepts_tab=False)
        self.view.set_tooltip_text("Ctrl+click a [[link]] to open it")
        self.buffer = self.view.get_buffer()
        self.styler = MarkdownStyler(self.view)
        self.buffer.connect("changed", self._buffer_changed)
        keys = Gtk.EventControllerKey()
        keys.connect("key-pressed", self._on_key)
        self.view.add_controller(keys)
        click = Gtk.GestureClick(button=1)
        click.connect("pressed", self._on_click)
        self.view.add_controller(click)
        clamp = Adw.ClampScrollable(child=self.view, maximum_size=780, tightening_threshold=600)
        sc = Gtk.ScrolledWindow(vexpand=True)
        sc.set_child(clamp)

        empty = Adw.StatusPage(icon_name="audio-input-microphone-symbolic", title="Study Notes",
                               description="Record a class or import old recordings. They're transcribed "
                                           "on your NPU, then Claude writes notes that fit with the rest "
                                           "of your vault.")
        row = Gtk.Box(spacing=12, halign=Gtk.Align.CENTER)
        r = Gtk.Button(label="Record", css_classes=["suggested-action", "pill"])
        r.set_action_name("app.record")
        i = Gtk.Button(label="Import Recordings", css_classes=["pill"])
        i.set_action_name("app.import")
        row.append(r)
        row.append(i)
        empty.set_child(row)
        self.content_stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE)
        self.content_stack.add_named(empty, "empty")
        self.content_stack.add_named(sc, "editor")
        cv.set_content(self.content_stack)
        self.split.set_content(Adw.NavigationPage(child=cv, title="Note"))

    def _build_actions(self):
        for name, cb in (("rename", self.on_rename), ("rename-speakers", self.on_rename_speakers),
                         ("regenerate", self.on_regenerate), ("copy-markdown", self.on_copy),
                         ("open-obsidian", self.on_open_obsidian), ("show-in-files", self.on_show_in_files),
                         ("trash", self.on_trash), ("focus-search", lambda *_: self.search.grab_focus())):
            act = Gio.SimpleAction.new(name, None)
            act.connect("activate", cb)
            self.add_action(act)
        app = self.get_application()
        app.set_accels_for_action("win.rename", ["F2"])
        app.set_accels_for_action("win.trash", ["<Primary>Delete"])
        app.set_accels_for_action("win.focus-search", ["<Primary>f"])

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------
    def toast(self, msg: str, button: str | None = None, cb=None):
        t = Adw.Toast(title=msg, timeout=5 if button else 3)
        if button:
            t.set_button_label(button)
            t.connect("button-clicked", lambda *_: cb())
        self.toasts.add_toast(t)

    def _open_uri(self, uri: str, fail: str):
        try:
            Gio.AppInfo.launch_default_for_uri(uri, None)
        except GLib.Error:
            self.toast(fail)

    def _on_drop(self, _t, value, *_):
        paths = [Path(f.get_path()) for f in value.get_files() if f.get_path()]
        self.get_application().import_files(paths)
        return True

    def _on_close(self, *_):
        self._flush_save()
        w, h = self.get_default_size()
        update_config(win_w=w, win_h=h, last_note=str(self.current) if self.current else "")
        return False

    # ------------------------------------------------------------------
    # note list
    # ------------------------------------------------------------------
    def refresh_notes(self, select: Path | None = None):
        self._flush_save()
        vault = vault_dir()
        keep = select or self.current
        notes = vault_notes(vault)
        sig = (str(vault), tuple(sorted((str(p), p.stat().st_mtime_ns) for p in notes)))
        if sig == getattr(self, "_sig", None) and (select is None or select == self.current):
            self.reload_current()  # nothing new in the list; just pick up edits to the open note
            return
        self._sig = sig
        self._refreshing = True
        self.listbox.remove_all()
        groups: dict[str, list[tuple[dt.date, float, Path]]] = {}
        for p in notes:
            g = course_of(p, vault)
            groups.setdefault(g, []).append((note_date(p), p.stat().st_mtime, p))
        order = sorted(groups, key=lambda g: max(x[0] for x in groups[g]), reverse=True)
        target = None
        for g in order:
            for d, _m, p in sorted(groups[g], reverse=True):
                row = Adw.ActionRow(title=p.stem, subtitle=human_date(d), activatable=True)
                row.set_use_markup(False)
                row.set_title_lines(1)
                row.path, row.group, row.count = p, g, len(groups[g])
                self.listbox.append(row)
                if keep and p == keep:
                    target = row
        self.sidebar_stack.set_visible_child_name("list" if notes else "empty")
        if target:
            self.listbox.select_row(target)  # highlight only; never reloads the open note
        self._refreshing = False
        if select and select != self.current and target:
            self._open_row(target)
        elif self.current and not (self.current.exists()):
            self._clear_view()
        else:
            self.reload_current()

    def select_path(self, p: Path):
        r = self.listbox.get_first_child()
        while r:
            if getattr(r, "path", None) == p:
                self.listbox.select_row(r)
                return
            r = r.get_next_sibling()

    def note_created(self, path: Path):
        self.refresh_notes(select=path)
        self.toast("Note saved")

    def _header(self, row, before):
        if before is None or before.group != row.group:
            lbl = Gtk.Label(label=row.group, xalign=0, margin_start=12, margin_top=12, margin_bottom=4,
                            css_classes=["heading", "dim-label"])
            row.set_header(lbl)
        else:
            row.set_header(None)

    def _filter(self, row) -> bool:
        q = self.search.get_text().strip().lower()
        if not q:
            return True
        if q in row.path.stem.lower() or q in row.group.lower():
            return True
        return q in self._text(row.path)

    def _text(self, p: Path) -> str:
        """Lower-cased note text, cached by mtime so searching doesn't re-read every file per keystroke."""
        try:
            m = p.stat().st_mtime_ns
        except OSError:
            return ""
        cache = self.__dict__.setdefault("_text_cache", {})
        hit = cache.get(p)
        if not hit or hit[0] != m:
            try:
                hit = (m, p.read_text(errors="ignore").lower())
            except OSError:
                hit = (m, "")
            cache[p] = hit
        return hit[1]

    def _row_selected(self, _lb, row):
        if self._refreshing or row is None:
            return
        self._open_row(row)

    def _open_row(self, row):
        self._flush_save()
        self.current = row.path
        self._load(row.path)
        self.title_widget.set_title(row.path.stem)
        self.title_widget.set_subtitle(f"{row.group} · {human_date(note_date(row.path))}")
        self.note_menu.set_sensitive(True)
        self.content_stack.set_visible_child_name("editor")
        self.split.set_show_content(True)

    def _clear_view(self):
        self.current = None
        self.note_menu.set_sensitive(False)
        self.title_widget.set_title("")
        self.title_widget.set_subtitle("")
        self.content_stack.set_visible_child_name("empty")

    # ------------------------------------------------------------------
    # editor
    # ------------------------------------------------------------------
    def _load(self, p: Path, keep_cursor: int | None = None):
        try:
            text = p.read_text()
            self.loaded_mtime = p.stat().st_mtime
        except OSError as e:
            self.toast(f"Could not open note: {e}")
            return
        self.loading = True
        self.buffer.set_text(text)
        self.loading = False
        self.styler.apply()
        it = self.buffer.get_iter_at_offset(min(keep_cursor or 0, self.buffer.get_char_count()))
        self.buffer.place_cursor(it)

    def reload_current(self, force: bool = False):
        """Pick up edits made elsewhere (e.g. in Obsidian) when we have no unsaved changes."""
        if not self.current or self.save_id:
            return
        try:
            m = self.current.stat().st_mtime
        except OSError:
            return
        if force or m > self.loaded_mtime + 0.01:
            off = self.buffer.get_iter_at_mark(self.buffer.get_insert()).get_offset()
            self._load(self.current, keep_cursor=off)

    def _buffer_changed(self, *_):
        if self.loading or not self.current:
            return
        if self.save_id:
            GLib.source_remove(self.save_id)
        self.save_id = GLib.timeout_add(700, self._flush_save)

    def _flush_save(self):
        if self.save_id:
            GLib.source_remove(self.save_id)
            self.save_id = 0
        if self.current and not self.loading:
            b = self.buffer
            tmp = self.current.with_name("." + self.current.name + ".tmp")
            try:
                tmp.write_text(b.get_text(b.get_start_iter(), b.get_end_iter(), False))
                os.replace(tmp, self.current)  # atomic: a crash never leaves a half-written note
                self.loaded_mtime = self.current.stat().st_mtime
            except OSError as e:
                self.toast(f"Could not save: {e}")
        return False

    def _on_click(self, gesture, _n, x, y):
        if not gesture.get_current_event_state() & Gdk.ModifierType.CONTROL_MASK:
            return
        bx, by = self.view.window_to_buffer_coords(Gtk.TextWindowType.WIDGET, int(x), int(y))
        name = self.styler.link_at(bx, by)
        if name:
            self.open_link(name)

    def _on_key(self, _c, keyval, _code, state):
        if keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter) and state & Gdk.ModifierType.CONTROL_MASK:
            off = self.buffer.get_iter_at_mark(self.buffer.get_insert()).get_offset()
            s, e = self.buffer.get_bounds()
            for m in WIKILINK.finditer(self.buffer.get_text(s, e, False)):
                if m.start() <= off <= m.end():
                    self.open_link(m[1])
                    return True
        return False

    def open_link(self, name: str):
        p = find_note_by_link(name)
        if p:
            self.select_path(p)
        else:
            self.toast(f"No note called “{name}”")

    # ------------------------------------------------------------------
    # note actions
    # ------------------------------------------------------------------
    def _entry_dialog(self, heading, body, text, label, cb):
        entry = Gtk.Entry(text=text, activates_default=True)
        dlg = Adw.AlertDialog(heading=heading, body=body)
        dlg.set_extra_child(entry)
        dlg.add_response("cancel", "Cancel")
        dlg.add_response("ok", label)
        dlg.set_response_appearance("ok", Adw.ResponseAppearance.SUGGESTED)
        dlg.set_default_response("ok")
        dlg.connect("response", lambda _d, r: r == "ok" and cb(entry.get_text()))
        dlg.present(self)
        entry.grab_focus()
        entry.select_region(0, -1)

    def on_rename(self, *_):
        if not self.current:
            return
        old = self.current

        def do(name):
            name = safe_name(name)
            self._flush_save()
            new = old.with_name(name + ".md")
            if new == old:
                return
            if new.exists():
                self.toast("A note with that name already exists")
                return
            old.rename(new)
            self.current = new
            self.refresh_notes(select=new)

        self._entry_dialog("Rename Note", "", old.stem, "Rename", do)

    def on_rename_speakers(self, *_):
        if not self.current:
            return
        self._flush_save()
        text = self.current.read_text()
        speakers = transcript_speakers(text)
        if not speakers:
            self.toast("This note has no speaker labels")
            return
        dlg = Adw.AlertDialog(heading="Rename Speakers",
                              body="Changes the name everywhere in this note. Use the same name twice to merge.")
        group = Adw.PreferencesGroup()
        entries = {}
        for s in speakers:
            row = Adw.EntryRow(title=s, text=s)
            group.add(row)
            entries[s] = row
        dlg.set_extra_child(group)
        dlg.add_response("cancel", "Cancel")
        dlg.add_response("ok", "Rename")
        dlg.set_response_appearance("ok", Adw.ResponseAppearance.SUGGESTED)

        def done(_d, r):
            if r != "ok":
                return
            new = rename_speakers_text(text, {old: row.get_text() for old, row in entries.items()})
            if new != text:
                self.buffer.set_text(new)  # goes through the normal autosave path
                self.toast("Speakers renamed")

        dlg.connect("response", done)
        dlg.present(self)

    def on_regenerate(self, *_):
        if not self.current:
            return
        if not signed_in():
            self.toast("Sign in to Claude Code first (run `claude`, then /login)")
            return
        self._flush_save()
        RegenDialog(self, self.current).present(self)

    def on_copy(self, *_):
        if self.current:
            b = self.buffer
            self.get_clipboard().set(b.get_text(b.get_start_iter(), b.get_end_iter(), False))
            self.toast("Copied as Markdown")

    def on_open_obsidian(self, *_):
        target = self.current or vault_dir()
        self._open_uri(f"obsidian://open?path={urllib.parse.quote(str(target))}",
                       "Obsidian could not be opened. Open the vault folder in Obsidian once first.")

    def on_show_in_files(self, *_):
        folder = (self.current.parent if self.current else vault_dir())
        self._open_uri(Gio.File.new_for_path(str(folder)).get_uri(), "Could not open the folder")

    def on_trash(self, *_):
        if not self.current:
            return
        self._flush_save()
        p = self.current
        trash = vault_dir() / ".trash"
        trash.mkdir(parents=True, exist_ok=True)
        dest = unique_path(trash, p.stem)  # Obsidian's own trash folder
        p.rename(dest)
        self.current = None
        self._clear_view()
        self.refresh_notes()

        def undo():
            if not p.exists():
                p.parent.mkdir(parents=True, exist_ok=True)
                dest.rename(p)
                self.refresh_notes(select=p)

        self.toast(f"Moved “{p.stem}” to the trash", "Undo", undo)
