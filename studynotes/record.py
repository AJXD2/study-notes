"""Recording dialog: record -> transcribe -> name speakers -> notes -> save."""
from __future__ import annotations

import datetime as dt
from pathlib import Path

from gi.repository import Adw, GLib, Gst, Gtk

from common import (REC_DIR, Job, course_of, fmt_dur, load_config, safe_name, signed_in, vault_classes,
                    vault_dir)


class Waveform(Gtk.DrawingArea):
    """Voice-Memos-style rolling bar meter."""

    def __init__(self):
        super().__init__(content_height=64, hexpand=True)
        self.levels: list[float] = [0.0] * 90
        self.set_draw_func(self._draw)
        self.add_css_class("waveform")

    def push(self, v: float):
        self.levels = self.levels[1:] + [max(0.0, min(1.0, v))]
        self.queue_draw()

    def reset(self):
        self.levels = [0.0] * 90
        self.queue_draw()

    def _draw(self, _a, cr, w, h):
        fg = self.get_color()
        n = len(self.levels)
        bar, gap = max(2.0, w / n * 0.55), w / n * 0.45
        cr.set_source_rgba(fg.red, fg.green, fg.blue, 0.75)
        for i, v in enumerate(self.levels):
            bh = max(3.0, v * (h - 4))
            x = i * (bar + gap)
            y = (h - bh) / 2
            r = min(bar / 2, bh / 2)
            cr.new_sub_path()
            cr.arc(x + bar - r, y + r, r, -1.5708, 0)
            cr.arc(x + bar - r, y + bh - r, r, 0, 1.5708)
            cr.arc(x + r, y + bh - r, r, 1.5708, 3.1416)
            cr.arc(x + r, y + r, r, 3.1416, 4.7124)
            cr.close_path()
            cr.fill()


class RecordDialog(Adw.Dialog):
    def __init__(self, window):
        super().__init__()
        self.win = window
        self.cfg = load_config()
        self.pipeline = None
        self.paused = False
        self.job: Job | None = None
        self.inhibit_cookie = 0
        self.timer_id = 0
        self.elapsed = 0.0
        self._last_tick = 0.0
        self.retry_stage = None
        self.names: dict[str, str] = {}
        self.set_title("New Recording")
        self.set_content_width(480)
        self.set_content_height(580)
        self.connect("close-attempt", self._close_attempt)
        self.connect("closed", self._closed)

        self.stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE)
        tv = Adw.ToolbarView()
        tv.add_top_bar(Adw.HeaderBar())
        tv.set_content(self.stack)
        self.set_child(tv)
        self._page_record()
        self._page_working()
        self._page_speakers()
        self._page_error()

    # ---- pages ---------------------------------------------------------
    def _page_record(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18, margin_top=6,
                      margin_bottom=24, margin_start=18, margin_end=18)
        group = Adw.PreferencesGroup()
        self.classes = vault_classes()
        self.class_row = Adw.ComboRow(title="Class")
        self.class_row.set_model(Gtk.StringList.new(self.classes + ["New class…"]))
        last = self.cfg.get("last_note", "")
        for i, c in enumerate(self.classes):  # default to the class you used last
            if last and Path(last).exists() and course_of(Path(last)) == c:
                self.class_row.set_selected(i)
        self.class_row.connect("notify::selected", self._class_changed)
        self.new_class = Adw.EntryRow(title="New class name", visible=not self.classes)
        self.title_row = Adw.EntryRow(title="Topic (optional)")
        for w in (self.class_row, self.new_class, self.title_row):
            group.add(w)
        box.append(group)
        self.class_row.set_sensitive(bool(self.classes))
        box.append(Gtk.Box(vexpand=True))
        self.timer_label = Gtk.Label(label="0:00", css_classes=["timer"])
        box.append(self.timer_label)
        self.wave = Waveform()
        box.append(self.wave)
        controls = Gtk.Box(spacing=18, halign=Gtk.Align.CENTER)
        self.pause_button = Gtk.Button(icon_name="media-playback-pause-symbolic", valign=Gtk.Align.CENTER,
                                       css_classes=["circular", "large-button"], visible=False,
                                       tooltip_text="Pause")
        self.pause_button.connect("clicked", self._toggle_pause)
        self.rec_button = Gtk.Button(icon_name="media-record-symbolic",
                                     css_classes=["circular", "suggested-action", "record-button"],
                                     tooltip_text="Start recording")
        self.rec_button.connect("clicked", self._toggle_record)
        controls.append(self.pause_button)
        controls.append(self.rec_button)
        box.append(controls)
        self.hint = Gtk.Label(wrap=True, justify=Gtk.Justification.CENTER, css_classes=["dim-label"],
                              label="Records from the default microphone.")
        box.append(self.hint)
        box.append(Gtk.Box(vexpand=True))
        self.stack.add_named(box, "record")

    def _page_working(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, margin_start=24, margin_end=24,
                      margin_bottom=24, valign=Gtk.Align.CENTER)
        self.spinner = Adw.Spinner(width_request=48, height_request=48)
        self.work_title = Gtk.Label(label="Working…", css_classes=["title-2"])
        self.work_detail = Gtk.Label(css_classes=["dim-label"], wrap=True)
        self.preview = Gtk.TextView(editable=False, wrap_mode=Gtk.WrapMode.WORD_CHAR, cursor_visible=False,
                                    left_margin=12, right_margin=12, top_margin=10, bottom_margin=10)
        self.preview_scroll = Gtk.ScrolledWindow(min_content_height=230, vexpand=True, visible=False,
                                                 css_classes=["card"])
        self.preview_scroll.set_child(self.preview)
        for w in (self.spinner, self.work_title, self.work_detail, self.preview_scroll):
            box.append(w)
        self.stack.add_named(box, "working")

    def _page_speakers(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, margin_start=18, margin_end=18,
                      margin_bottom=18)
        box.append(Gtk.Label(label="Who was speaking?", css_classes=["title-2"], xalign=0))
        self.spk_help = Gtk.Label(
            label="Names help Claude attribute questions and announcements. Give two labels the "
                  "same name to merge them. You can change this later from the note menu.",
            wrap=True, xalign=0, css_classes=["dim-label"])
        box.append(self.spk_help)
        self.spk_group = Adw.PreferencesGroup()
        sc = Gtk.ScrolledWindow(vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER)
        sc.set_child(self.spk_group)
        box.append(sc)
        self.gen_button = Gtk.Button(label="Write Notes", css_classes=["suggested-action", "pill"],
                                     halign=Gtk.Align.CENTER)
        self.gen_button.connect("clicked", lambda *_: self._generate(True))
        skip = Gtk.Button(label="Save Transcript Only", css_classes=["flat"], halign=Gtk.Align.CENTER)
        skip.connect("clicked", lambda *_: self._generate(False))
        box.append(self.gen_button)
        box.append(skip)
        self.stack.add_named(box, "speakers")
        self._spk_widgets, self.spk_rows = [], {}

    def _page_error(self):
        self.err_page = Adw.StatusPage(icon_name="dialog-warning-symbolic", title="Something went wrong")
        row = Gtk.Box(spacing=12, halign=Gtk.Align.CENTER)
        self.retry_button = Gtk.Button(label="Try Again", css_classes=["suggested-action", "pill"])
        self.retry_button.connect("clicked", lambda *_: self.retry_stage and self.retry_stage())
        close = Gtk.Button(label="Close", css_classes=["pill"])
        close.connect("clicked", lambda *_: self._force_close())
        row.append(self.retry_button)
        row.append(close)
        self.err_page.set_child(row)
        self.stack.add_named(self.err_page, "error")

    # ---- recording -----------------------------------------------------
    def _inhibit(self):
        if not self.inhibit_cookie:
            self.inhibit_cookie = self.win.get_application().inhibit(
                self.win, Gtk.ApplicationInhibitFlags.SUSPEND | Gtk.ApplicationInhibitFlags.IDLE,
                "Recording and transcribing a class")

    def _uninhibit(self):
        if self.inhibit_cookie:
            self.win.get_application().uninhibit(self.inhibit_cookie)
            self.inhibit_cookie = 0

    def _class_changed(self, *_):
        self.new_class.set_visible(self.class_row.get_selected() == len(self.classes))

    def _selected_class(self) -> str:
        i = self.class_row.get_selected()
        if i < len(self.classes):
            return self.classes[i]
        return safe_name(self.new_class.get_text()) if self.new_class.get_text().strip() else "General"

    def _toggle_record(self, *_):
        self._start() if self.pipeline is None else self._stop()

    def _start(self):
        REC_DIR.mkdir(parents=True, exist_ok=True)
        wav = REC_DIR / f"{dt.datetime.now():%Y%m%d-%H%M%S}.wav"
        try:
            self.pipeline = Gst.parse_launch(
                "pipewiresrc ! audioconvert ! level name=lvl interval=60000000 post-messages=true ! "
                "audioresample ! audio/x-raw,format=S16LE,rate=16000,channels=1 ! wavenc ! "
                f'filesink location="{wav}"')
        except GLib.Error as e:
            self._fail(f"Could not open the microphone: {e.message}", retry=False)
            return
        self.job = Job(wav, self._selected_class(),
                       title=safe_name(self.title_row.get_text()) if self.title_row.get_text().strip() else "")
        bus = self.pipeline.get_bus()
        bus.add_signal_watch()
        bus.connect("message::eos", lambda *_: self._on_eos())
        bus.connect("message::error", self._on_gst_error)
        bus.connect("message::element", self._on_level)
        self.pipeline.set_state(Gst.State.PLAYING)
        self._inhibit()
        self.elapsed, self._last_tick = 0.0, GLib.get_monotonic_time() / 1e6
        self.timer_id = GLib.timeout_add(250, self._tick)
        self.rec_button.set_icon_name("media-playback-stop-symbolic")
        self.rec_button.remove_css_class("suggested-action")
        self.rec_button.add_css_class("destructive-action")
        self.rec_button.set_tooltip_text("Stop and transcribe")
        self.pause_button.set_visible(True)
        self.hint.set_text("Recording. The laptop stays awake, but closing the lid will still suspend it.")
        for w in (self.class_row, self.new_class, self.title_row):
            w.set_sensitive(False)
        self.set_can_close(False)

    def _on_level(self, _bus, msg):
        s = msg.get_structure()
        if s and s.get_name() == "level" and not self.paused:
            rms = s.get_value("rms")
            if rms:
                db = max(-60.0, min(0.0, float(rms[0])))
                self.wave.push(((db + 60) / 60) ** 1.6)

    def _tick(self):
        now = GLib.get_monotonic_time() / 1e6
        if not self.paused:
            self.elapsed += now - self._last_tick
        self._last_tick = now
        self.timer_label.set_text(fmt_dur(self.elapsed))
        return True

    def _toggle_pause(self, *_):
        self.paused = not self.paused
        self.pipeline.set_state(Gst.State.PAUSED if self.paused else Gst.State.PLAYING)
        self.pause_button.set_icon_name("media-playback-start-symbolic" if self.paused
                                        else "media-playback-pause-symbolic")
        self.pause_button.set_tooltip_text("Resume" if self.paused else "Pause")
        self.timer_label.set_css_classes(["timer", "dim-label"] if self.paused else ["timer"])
        self.hint.set_text("Paused." if self.paused else "Recording.")

    def _stop(self):
        if self.timer_id:
            GLib.source_remove(self.timer_id)
            self.timer_id = 0
        self.rec_button.set_sensitive(False)
        self.pause_button.set_sensitive(False)
        self.hint.set_text("Finishing…")
        if self.paused:
            self.pipeline.set_state(Gst.State.PLAYING)
        self.pipeline.send_event(Gst.Event.new_eos())  # lets wavenc finalise the header

    def _on_eos(self):
        self._teardown()
        self._transcribe()

    def _on_gst_error(self, _bus, msg):
        err, _ = msg.parse_error()
        self._teardown()
        self._fail(f"Recording error: {err.message}", retry=False)

    def _teardown(self):
        if self.pipeline:
            self.pipeline.get_bus().remove_signal_watch()
            self.pipeline.set_state(Gst.State.NULL)
            self.pipeline = None

    # ---- stages --------------------------------------------------------
    def _show_work(self, title: str, detail: str = ""):
        self.set_can_close(False)
        self.work_title.set_text(title)
        self.work_detail.set_text(detail)
        self.stack.set_visible_child_name("working")

    def _transcribe(self):
        self.retry_stage = self._transcribe
        self._show_work("Transcribing…", "Starting up")
        self.job.transcribe(self._on_transcribe)

    def _on_transcribe(self, ev):
        kind = ev.get("event")
        if kind == "progress":
            titles = {"loading": "Loading speech model…", "transcribing": "Transcribing…",
                      "diarizing": "Detecting speakers…"}
            self.work_title.set_text(titles.get(ev["stage"], "Working…"))
            self.work_detail.set_text(ev.get("detail", ""))
        elif kind == "result":
            data = self.job.load()
            if not data["segments"]:
                self._fail("No speech was detected in the recording.", retry=False)
            else:
                self._show_speakers(data)
        elif kind == "error":
            self._fail(ev["message"])
        return False

    def _show_speakers(self, data):
        self._uninhibit()
        self.set_can_close(True)
        for row in self._spk_widgets:
            self.spk_group.remove(row)
        self._spk_widgets, self.spk_rows = [], {}
        for spk in data["speakers"]:
            sample = next((s["text"] for s in data["segments"] if s["speaker"] == spk), "")
            row = Adw.ActionRow(title=f"Speaker {spk}", subtitle=f"“{sample[:90]}”")
            row.set_use_markup(False)
            row.set_subtitle_lines(2)
            entry = Gtk.Entry(placeholder_text=f"Speaker {spk}", valign=Gtk.Align.CENTER, width_chars=14)
            entry.connect("activate", lambda *_: self._generate(True))
            row.add_suffix(entry)
            self.spk_group.add(row)
            self._spk_widgets.append(row)
            self.spk_rows[spk] = entry
        sk = data.get("skipped") or {}
        n = (sk.get("windows") or 0) + (sk.get("segments") or 0)
        base = ("Names help Claude attribute questions and announcements. Give two labels the same name "
                "to merge them. You can change this later from the note menu.")
        self.spk_help.set_text(base + (f"\n\nSkipped {sk.get('windows', 0)} silent section(s) and dropped "
                                       f"{sk.get('segments', 0)} text fragment(s) with no speech under them."
                                       if n else ""))
        self.gen_button.set_sensitive(True)
        self.stack.set_visible_child_name("speakers")
        first = next(iter(self.spk_rows.values()), None)
        if first:
            first.grab_focus()

    def _generate(self, with_ai: bool):
        self.names = {str(k): e.get_text().strip() for k, e in self.spk_rows.items() if e.get_text().strip()}
        if not with_ai:
            self._save("")
            return
        if not signed_in():
            self.win.toast("Sign in to Claude Code first (run `claude` in a terminal, then /login)")
            return
        self.retry_stage = lambda: self._generate(True)
        self._inhibit()
        self._show_work("Writing notes…", "")
        self.preview.get_buffer().set_text("")
        self.preview_scroll.set_visible(True)
        self.notes_text = ""
        self.job.notes(self.names, self._on_notes)

    def _on_notes(self, ev):
        kind = ev.get("event")
        if kind == "progress":
            self.work_detail.set_text(ev.get("detail", ""))
        elif kind == "text":
            self.notes_text += ev["delta"]
            buf = self.preview.get_buffer()
            buf.insert(buf.get_end_iter(), ev["delta"])
            adj = self.preview_scroll.get_vadjustment()
            GLib.idle_add(lambda: adj.set_value(adj.get_upper()))
        elif kind == "result":
            self._save(ev["body"])
        elif kind == "error":
            self._fail(ev["message"])
        return False

    def _save(self, body: str):
        self._uninhibit()
        path = self.job.save(self.names, body)
        self.job.cleanup(self.cfg["keep_audio"])
        self.set_can_close(True)
        self.close()
        self.win.note_created(path)

    # ---- failure / closing ---------------------------------------------
    def _fail(self, message: str, retry: bool = True):
        self._uninhibit()
        self.set_can_close(True)
        self.err_page.set_description(message)
        self.retry_button.set_visible(retry and self.retry_stage is not None)
        self.stack.set_visible_child_name("error")

    def _close_attempt(self, *_):
        recording = self.pipeline is not None
        dlg = Adw.AlertDialog(
            heading="Stop recording?" if recording else "Cancel?",
            body="The audio is kept in ~/.local/share/study-notes/recordings." if recording
            else "Work in progress will be stopped.")
        dlg.add_response("keep", "Keep Going")
        dlg.add_response("stop", "Stop")
        dlg.set_response_appearance("stop", Adw.ResponseAppearance.DESTRUCTIVE)
        dlg.connect("response", lambda _d, r: self._force_close() if r == "stop" else None)
        dlg.present(self)

    def _force_close(self):
        self.set_can_close(True)
        self.close()

    def _closed(self, *_):
        self._uninhibit()
        if self.timer_id:
            GLib.source_remove(self.timer_id)
        self._teardown()
        if self.job:
            self.job.cancel()
