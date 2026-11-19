"""Import existing recordings (files or whole folders) as notes, in a queue."""
from __future__ import annotations

import datetime as dt
import re
import subprocess
import threading
from pathlib import Path

from gi.repository import Adw, Gdk, Gio, GLib, Gtk

from common import (AUDIO_EXT, TMP_DIR, Job, already_imported, convert_to_wav, fmt_dur, fmt_size, load_config,
                    safe_name, signed_in, vault_classes)

_DATEISH = re.compile(r"^[\d\-_ .:T]+$")


def find_audio(paths: list[Path]) -> list[Path]:
    """Expand files/folders into a sorted, de-duplicated list of audio/video files."""
    out: dict[Path, None] = {}
    for p in paths:
        if p.is_dir():
            for f in sorted(p.rglob("*")):
                if f.is_file() and f.suffix.lower() in AUDIO_EXT and not f.name.startswith("."):
                    out[f] = None
        elif p.is_file() and p.suffix.lower() in AUDIO_EXT:
            out[p] = None
    return sorted(out, key=lambda f: f.stat().st_mtime)


def source_key(p: Path) -> str:
    return f"{p.name} ({fmt_size(p.stat().st_size)})"


_LEAD_DATE = re.compile(r"^\d{4}[-_.]\d{2}[-_.]\d{2}[ _T.\-]*")


def title_for(p: Path) -> str:
    stem = _LEAD_DATE.sub("", p.stem.replace("_", " ")).strip()  # the note date already carries it
    return "" if not stem or _DATEISH.match(stem) else safe_name(stem)


class Item:
    def __init__(self, path: Path):
        self.path = path
        self.status = "queued"       # queued | working | done | failed | skipped
        self.detail = ""
        self.note: Path | None = None
        self.row: Adw.ActionRow | None = None
        self.icon: Gtk.Image | None = None
        self.spinner: Adw.Spinner | None = None


class ImportDialog(Adw.Dialog):
    def __init__(self, window, files: list[Path] | None = None):
        super().__init__()
        self.win = window
        self.cfg = load_config()
        self.items: list[Item] = []
        self.idx = -1
        self.job: Job | None = None
        self.running = False
        self.cancelled = False
        self.dead = False
        self.durations: dict[Path, float] = {}
        self.inhibit_cookie = 0
        self.set_title("Import Recordings")
        self.set_content_width(520)
        self.set_content_height(640)
        self.connect("close-attempt", self._close_attempt)
        self.connect("closed", self._closed)

        self.stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE)
        tv = Adw.ToolbarView()
        self.header = Adw.HeaderBar()
        tv.add_top_bar(self.header)
        tv.set_content(self.stack)
        self.set_child(tv)
        self._page_setup()
        self._page_run()
        if files:
            self.add_files(files)

    # ---- setup page ----------------------------------------------------
    def _page_setup(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14, margin_start=18, margin_end=18,
                      margin_bottom=18, margin_top=6)
        opts = Adw.PreferencesGroup()
        self.classes = vault_classes()
        self.class_row = Adw.ComboRow(title="Add to class")
        self.class_row.set_model(Gtk.StringList.new(self.classes + ["New class…"]))
        self.class_row.connect("notify::selected", self._class_changed)
        self.new_class = Adw.EntryRow(title="New class name", visible=not self.classes)
        self.ai_row = Adw.SwitchRow(
            title="Write notes with Claude", active=signed_in(), sensitive=signed_in(),
            subtitle="Off: save only the transcript" if signed_in()
            else "Sign in to Claude Code to enable (run `claude`, then /login)")
        self.spk_row = Adw.SwitchRow(
            title="Detect speakers", active=bool(self.cfg.get("detect_speakers", True)),
            subtitle="Labels who is speaking. Off is about 4x faster.")
        self.spk_row.connect("notify::active", lambda *_: self._update_eta())
        self.ai_row.connect("notify::active", lambda *_: self._update_eta())
        for w in (self.class_row, self.new_class, self.ai_row, self.spk_row):
            opts.add(w)
        box.append(opts)

        self.files_group = Adw.PreferencesGroup(title="Recordings")
        add = Gtk.Box(spacing=6)
        b1 = Gtk.Button(label="Add Files…", css_classes=["flat"])
        b1.connect("clicked", self._pick_files)
        b2 = Gtk.Button(label="Add Folder…", css_classes=["flat"])
        b2.connect("clicked", self._pick_folder)
        add.append(b1)
        add.append(b2)
        self.files_group.set_header_suffix(add)
        self.list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE, css_classes=["boxed-list"])
        self.empty = Adw.StatusPage(icon_name="folder-music-symbolic", title="No recordings yet",
                                    description="Add files or a folder, or drop them here.",
                                    vexpand=True)
        self.list_scroll = Gtk.ScrolledWindow(vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER)
        self.list_scroll.set_child(self.list)
        self.files_stack = Gtk.Stack(vexpand=True)
        self.files_stack.add_named(self.empty, "empty")
        self.files_stack.add_named(self.list_scroll, "list")
        box.append(self.files_group)
        box.append(self.files_stack)

        self.eta = Gtk.Label(css_classes=["dim-label"], wrap=True, justify=Gtk.Justification.CENTER)
        box.append(self.eta)
        self.start_button = Gtk.Button(label="Import", css_classes=["suggested-action", "pill"],
                                       halign=Gtk.Align.CENTER, sensitive=False)
        self.start_button.connect("clicked", self._start)
        box.append(self.start_button)
        self.stack.add_named(box, "setup")

        drop = Gtk.DropTarget.new(Gdk.FileList, Gdk.DragAction.COPY)
        drop.connect("drop", self._on_drop)
        box.add_controller(drop)

    def _on_drop(self, _t, value, *_):
        self.add_files([Path(f.get_path()) for f in value.get_files() if f.get_path()])
        return True

    def _class_changed(self, *_):
        self.new_class.set_visible(self.class_row.get_selected() == len(self.classes))

    def _selected_class(self) -> str:
        i = self.class_row.get_selected()
        if i < len(self.classes):
            return self.classes[i]
        t = self.new_class.get_text().strip()
        return safe_name(t) if t else "Imported"

    def _pick_files(self, *_):
        flt = Gtk.FileFilter(name="Audio and video")
        for ext in sorted(AUDIO_EXT):
            flt.add_suffix(ext.lstrip("."))
        store = Gio.ListStore.new(Gtk.FileFilter)
        store.append(flt)
        dlg = Gtk.FileDialog(title="Add recordings", filters=store, default_filter=flt)
        dlg.open_multiple(self.win, None, self._files_done)

    def _files_done(self, dlg, res):
        try:
            files = dlg.open_multiple_finish(res)
        except GLib.Error:
            return
        self.add_files([Path(files.get_item(i).get_path()) for i in range(files.get_n_items())])

    def _pick_folder(self, *_):
        Gtk.FileDialog(title="Add a folder of recordings").select_folder(self.win, None, self._folder_done)

    def _folder_done(self, dlg, res):
        try:
            f = dlg.select_folder_finish(res)
        except GLib.Error:
            return
        self.add_files([Path(f.get_path())])

    def add_files(self, paths: list[Path]):
        if self.running:
            return
        known = {i.path for i in self.items}
        added = [p for p in find_audio(paths) if p not in known]
        for p in added:
            it = Item(p)
            if already_imported(source_key(p)):
                it.status, it.detail = "skipped", "Already imported"
            self.items.append(it)
            self._make_row(it)
        self._probe([i.path for i in self.items if i.path not in self.durations])
        n = len(self.items)
        self.files_group.set_title(f"Recordings ({n})" if n else "Recordings")
        self.files_stack.set_visible_child_name("list" if n else "empty")
        todo = sum(1 for i in self.items if i.status == "queued")
        if todo > 10 and signed_in() and not getattr(self, "_big_warned", False):
            # each note sends vault context to Claude and counts against the plan: default to
            # transcripts for big batches (notes can be regenerated one by one afterwards)
            self._big_warned = True
            self.ai_row.set_active(False)
            self.spk_row.set_active(False)
            self.ai_row.set_subtitle(f"Turned off for {todo} files to save your plan. Turn on to write all of "
                                     "them, or use Regenerate Notes on the ones you care about later.")
            self.spk_row.set_subtitle("Turned off for a big batch: it is the slowest step. Turn on if you need labels.")
        self.start_button.set_sensitive(todo > 0)
        self.start_button.set_label(f"Import {todo} Recording{'s' if todo != 1 else ''}" if todo else "Import")
        if not added and paths:
            self.win.toast("No audio files found there")

    # ---- time estimate -------------------------------------------------
    def _probe(self, paths: list[Path]):
        """Read each file's duration with ffprobe off the main thread, then refresh the estimate."""
        def run():
            for p in paths:
                try:
                    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of",
                                        "csv=p=0", str(p)], capture_output=True, text=True, timeout=30)
                    d = float(r.stdout.strip() or 0)
                except (OSError, ValueError, subprocess.TimeoutExpired):
                    d = 0.0
                GLib.idle_add(self._got_duration, p, d)
        if paths:
            threading.Thread(target=run, daemon=True).start()

    def _got_duration(self, p: Path, d: float):
        if not self.dead:
            self.durations[p] = d
            self._update_eta()
        return False

    def _update_eta(self):
        todo = [i for i in self.items if i.status == "queued"]
        if not todo:
            self.eta.set_text("")
            return
        secs = sum(self.durations.get(i.path, 0.0) for i in todo)
        known = sum(1 for i in todo if i.path in self.durations)
        # measured on the NPU laptop: ~0.22x realtime with speaker detection, ~0.05x without
        # (+ ~10-25 s per file for Claude's notes)
        per = 0.22 if self.spk_row.get_active() else 0.05
        est = secs * per + len(todo) * (25 if self.ai_row.get_active() else 5)
        h, m = int(est // 3600), int(est % 3600 // 60)
        nice = (f"{h} h {m:02d} min" if h else f"{max(m, 1)} min")
        txt = f"{fmt_dur(secs)} of audio: about {nice}"
        if known < len(todo):
            txt += f" (measuring {len(todo) - known} more…)"
        try:
            on_ac = (Path("/sys/class/power_supply/ACAD/online").read_text().strip() == "1")
        except OSError:
            on_ac = True
        if est > 1800 and not on_ac:
            txt += "\nThis will use a lot of battery. Plugging in is recommended."
        self.eta.set_text(txt)

    def _make_row(self, it: Item):
        st = it.path.stat()
        date = dt.datetime.fromtimestamp(st.st_mtime).strftime("%b %-d, %Y")
        row = Adw.ActionRow(title=it.path.name, subtitle=f"{date} · {fmt_size(st.st_size)}"
                            + (f" · {it.detail}" if it.detail else ""))
        row.set_use_markup(False)
        row.set_title_lines(1)
        it.row = row
        it.icon = Gtk.Image.new_from_icon_name("audio-x-generic-symbolic")
        row.add_prefix(it.icon)
        if it.status == "queued":
            rm = Gtk.Button(icon_name="window-close-symbolic", valign=Gtk.Align.CENTER,
                            css_classes=["flat", "circular"], tooltip_text="Remove")
            rm.connect("clicked", lambda *_: self._remove(it))
            row.add_suffix(rm)
        self.list.append(row)

    def _remove(self, it: Item):
        if self.running:
            return
        self.list.remove(it.row)
        self.items.remove(it)
        self.add_files([])

    # ---- run page ------------------------------------------------------
    def _page_run(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, margin_start=18, margin_end=18,
                      margin_bottom=18, margin_top=6)
        self.run_title = Gtk.Label(label="Importing…", css_classes=["title-2"], xalign=0)
        self.run_detail = Gtk.Label(css_classes=["dim-label"], xalign=0, wrap=True)
        self.bar = Gtk.ProgressBar()
        self.run_list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE, css_classes=["boxed-list"])
        sc = Gtk.ScrolledWindow(vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER)
        sc.set_child(self.run_list)
        self.action_button = Gtk.Button(label="Stop After This One", css_classes=["pill"],
                                        halign=Gtk.Align.CENTER)
        self.action_button.connect("clicked", self._action)
        for w in (self.run_title, self.run_detail, self.bar, sc, self.action_button):
            box.append(w)
        self.stack.add_named(box, "run")

    def _start(self, *_):
        self.cls = self._selected_class()
        self.with_ai = self.ai_row.get_active() and signed_in()
        self.detect = self.spk_row.get_active()
        queue = [i for i in self.items if i.status == "queued"]
        self.items = queue + [i for i in self.items if i.status != "queued"]
        self.run_list.remove_all()
        for it in self.items:
            row = Adw.ActionRow(title=it.path.name, subtitle=it.detail or "Waiting")
            row.set_use_markup(False)
            row.set_title_lines(1)
            it.row = row
            it.icon = Gtk.Image.new_from_icon_name("content-loading-symbolic" if it.status == "queued"
                                                   else "object-select-symbolic")
            row.add_prefix(it.icon)
            self.run_list.append(row)
        self.running = True
        self.cancelled = False
        self.set_can_close(False)
        self.inhibit_cookie = self.win.get_application().inhibit(
            self.win, Gtk.ApplicationInhibitFlags.SUSPEND | Gtk.ApplicationInhibitFlags.IDLE,
            "Importing recordings")
        self.stack.set_visible_child_name("run")
        self.idx = -1
        self.total = len(queue)
        self.finished = 0
        self._next()

    def _action(self, *_):
        if self.running:
            self.cancelled = True
            self.action_button.set_sensitive(False)
            self.action_button.set_label("Stopping…")
        else:
            self.force_close()

    def _next(self):
        self.idx += 1
        while self.idx < len(self.items) and self.items[self.idx].status != "queued":
            self.idx += 1
        if self.cancelled or self.idx >= len(self.items):
            self._finish()
            return
        it = self.items[self.idx]
        self._set_status(it, "working", "Converting audio…")
        self.run_title.set_text(f"Importing {self.finished + 1} of {self.total}")
        self.bar.set_fraction(self.finished / max(1, self.total))
        mtime = dt.datetime.fromtimestamp(it.path.stat().st_mtime).date()
        TMP_DIR.mkdir(parents=True, exist_ok=True)
        wav = TMP_DIR / f"import-{id(it)}.wav"
        self.job = Job(wav, self.cls, title=title_for(it.path), date=mtime, source=source_key(it.path))
        self.job.detect_speakers = self.detect
        convert_to_wav(it.path, wav, lambda err: self._converted(it, err))

    def _converted(self, it: Item, err: str):
        if self.dead:
            return False
        if err:
            self._fail(it, err)
            return False
        self._set_status(it, "working", "Transcribing…")
        self.job.transcribe(lambda ev: self._on_transcribe(it, ev))
        return False

    def _on_transcribe(self, it: Item, ev):
        if self.dead:
            return False
        kind = ev.get("event")
        if kind == "progress":
            d = ev.get("detail", "")
            self.run_detail.set_text(f"{it.path.name}: {d}")
            if ev["stage"] == "transcribing":
                self._set_status(it, "working", d)
            elif ev["stage"] == "diarizing":
                self._set_status(it, "working", "Detecting speakers…")
        elif kind == "result":
            data = self.job.load()
            if not data["segments"]:
                self._skip(it, "No speech found")
            elif self.with_ai:
                self._set_status(it, "working", "Writing notes…")
                self.job.notes({}, lambda e: self._on_notes(it, e))
            else:
                self._done(it, "")
        elif kind == "error":
            self._fail(it, ev["message"])
        return False

    def _on_notes(self, it: Item, ev):
        if self.dead:
            return False
        kind = ev.get("event")
        if kind == "progress":
            self.run_detail.set_text(f"{it.path.name}: {ev.get('detail', '')}")
        elif kind == "result":
            self._done(it, ev["body"])
        elif kind == "error":
            # keep the work: save the transcript so nothing is lost, and say why notes failed
            self._done(it, "", note_error=ev["message"])
        return False

    def _done(self, it: Item, body: str, note_error: str = ""):
        it.note = self.job.save({}, body)
        self.job.cleanup(False)  # temp files only; the original recording is never touched
        self.finished += 1
        if note_error:
            self._set_status(it, "done", "Transcript saved - notes failed: " + note_error[:70])
            it.warn = True
        else:
            sk = (self.job.data or {}).get("skipped") or {}
            n = sk.get("windows", 0)
            self._set_status(it, "done", "Saved" + (f" · {n} silent section{'s' if n != 1 else ''} skipped" if n else ""))
        self.win.refresh_notes()
        self._next()

    def _skip(self, it: Item, why: str):
        self.job.cleanup(False)
        self.finished += 1
        self._set_status(it, "skipped", why)
        self._next()

    def _fail(self, it: Item, msg: str):
        if self.job:
            self.job.cleanup(False)
        self.finished += 1
        self._set_status(it, "failed", msg[:90])
        self._next()

    def _set_status(self, it: Item, status: str, detail: str):
        it.status, it.detail = status, detail
        it.row.set_subtitle(detail or " ")
        icons = {"working": "content-loading-symbolic", "done": "object-select-symbolic",
                 "failed": "dialog-error-symbolic", "skipped": "action-unavailable-symbolic"}
        if status in icons:
            it.icon.set_from_icon_name(icons[status])
        if status == "working" and not it.spinner:
            it.spinner = Adw.Spinner(width_request=16, height_request=16)
            it.row.add_suffix(it.spinner)
        elif status != "working" and it.spinner:
            it.row.remove(it.spinner)
            it.spinner = None

    def _finish(self):
        self.running = False
        self._uninhibit()
        self.set_can_close(True)
        done = [i for i in self.items if i.status == "done"]
        failed = [i for i in self.items if i.status == "failed"]
        left = [i for i in self.items if i.status == "queued"]
        self.bar.set_fraction(1.0 if not left else self.finished / max(1, self.total))
        if self.cancelled and left:
            self.run_title.set_text(f"Stopped - {len(done)} imported, {len(left)} not started")
        else:
            self.run_title.set_text(f"Imported {len(done)} recording{'s' if len(done) != 1 else ''}"
                                    + (f", {len(failed)} failed" if failed else ""))
        self.run_detail.set_text("Your original files were not changed.")
        self.action_button.set_sensitive(True)
        self.action_button.set_label("Done")
        self.action_button.add_css_class("suggested-action")
        if done:
            self.win.refresh_notes(select=done[-1].note)
            if not self.win.is_active():
                n = Gio.Notification.new("Import finished")
                n.set_body(f"{len(done)} note{'s' if len(done) != 1 else ''} added to your vault.")
                self.win.get_application().send_notification("import-done", n)

    # ---- closing -------------------------------------------------------
    def _uninhibit(self):
        if self.inhibit_cookie:
            self.win.get_application().uninhibit(self.inhibit_cookie)
            self.inhibit_cookie = 0

    def _close_attempt(self, *_):
        dlg = Adw.AlertDialog(heading="Stop importing?",
                              body="Notes already imported are kept. The current recording will be abandoned.")
        dlg.add_response("keep", "Keep Going")
        dlg.add_response("stop", "Stop")
        dlg.set_response_appearance("stop", Adw.ResponseAppearance.DESTRUCTIVE)
        dlg.connect("response", lambda _d, r: self.force_close() if r == "stop" else None)
        dlg.present(self)

    def force_close(self):
        self.cancelled = True
        self.set_can_close(True)
        self.close()

    def _closed(self, *_):
        self.dead = True
        self.cancelled = True
        self._uninhibit()
        if self.job:
            self.job.cancel()
