#!/usr/bin/env python3
"""Study Notes - a GNOME (GTK4 + libadwaita) app.

Record or import classes -> transcribe on the NPU -> detect speakers -> Claude writes notes
using your Obsidian vault as context. UI runs on system Python; the AI work runs in worker.py
inside the project venv (JSON lines over a pipe).
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import gi  # noqa: E402

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Gst", "1.0")
from gi.repository import Adw, Gdk, Gio, GLib, Gst, Gtk  # noqa: E402

from common import APP_ID, classes_root, worker_python, load_config, safe_name, vault_dir  # noqa: E402
from importer import ImportDialog  # noqa: E402
from prefs import Preferences  # noqa: E402
from record import RecordDialog  # noqa: E402
from window import MainWindow  # noqa: E402

CSS = b"""
.record-button { min-width: 84px; min-height: 84px; padding: 0; }
.record-button image { -gtk-icon-size: 34px; }
.large-button { min-width: 52px; min-height: 52px; padding: 0; }
.timer { font-size: 44px; font-weight: 200; font-feature-settings: "tnum"; }
.waveform { color: @accent_color; }
textview.note-editor { background: transparent; }
"""


class App(Adw.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID,
                         flags=Gio.ApplicationFlags.HANDLES_OPEN | Gio.ApplicationFlags.DEFAULT_FLAGS)
        self.win: MainWindow | None = None
        self.pending_import: list[Path] = []

    def do_startup(self):
        Adw.Application.do_startup(self)
        Gst.init(None)
        prov = Gtk.CssProvider()
        prov.load_from_data(CSS)
        Gtk.StyleContext.add_provider_for_display(Gdk.Display.get_default(), prov,
                                                  Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        for name, cb, accel in (
                ("record", self.on_record, "<Primary>r"), ("import", self.on_import, "<Primary>i"),
                ("new-class", self.on_new_class, "<Primary><Shift>n"),
                ("preferences", self.on_prefs, "<Primary>comma"), ("shortcuts", self.on_shortcuts, "<Primary>question"),
                ("about", self.on_about, None), ("open-vault", self.on_open_vault, None),
                ("show-vault", self.on_show_vault, None), ("quit", lambda *_: self.quit(), "<Primary>q")):
            a = Gio.SimpleAction.new(name, None)
            a.connect("activate", cb)
            self.add_action(a)
            if accel:
                self.set_accels_for_action(f"app.{name}", [accel])

    def _ensure_window(self) -> MainWindow:
        if not self.win:
            vault_dir().mkdir(parents=True, exist_ok=True)
            self.win = MainWindow(self)
        return self.win

    def do_activate(self):
        self._ensure_window().present()

    def do_open(self, files, _n, _hint):  # "Open With Study Notes" from the file manager
        win = self._ensure_window()
        win.present()
        self.import_files([Path(f.get_path()) for f in files if f.get_path()])

    # ---- actions -------------------------------------------------------
    def _guard(self) -> MainWindow | None:
        win = self._ensure_window()
        if worker_python() is None:
            win.toast("The AI environment is missing. See README.md to rebuild it.")
            return None
        vault_dir().mkdir(parents=True, exist_ok=True)
        return win

    def on_record(self, *_):
        if win := self._guard():
            RecordDialog(win).present(win)

    def on_import(self, *_):
        if win := self._guard():
            ImportDialog(win).present(win)

    def import_files(self, paths):
        if win := self._guard():
            ImportDialog(win, paths).present(win)

    def on_new_class(self, *_):
        win = self._ensure_window()

        def make(name):
            name = safe_name(name)
            (classes_root() / name / "Lectures").mkdir(parents=True, exist_ok=True)
            win.toast(f"Class “{name}” created")

        win._entry_dialog("New Class", "A class is a course folder (with a Lectures folder) in your vault.", "", "Create", make)

    def on_prefs(self, *_):
        win = self._ensure_window()
        Preferences(win).present(win)

    def on_open_vault(self, *_):
        self._ensure_window().on_open_obsidian()

    def on_show_vault(self, *_):
        w = self._ensure_window()
        w._open_uri(Gio.File.new_for_path(str(vault_dir())).get_uri(), "Could not open the folder")

    def on_shortcuts(self, *_):
        win = self._ensure_window()
        try:
            dlg = Adw.ShortcutsDialog()
            sec = Adw.ShortcutsSection(title="General")
            for title, accel in (("New recording", "<Control>r"), ("Import recordings", "<Control>i"),
                                 ("New class", "<Control><Shift>n"), ("Search notes", "<Control>f"),
                                 ("Rename note", "F2"), ("Move note to trash", "<Control>Delete"),
                                 ("Open a [[link]]", "<Control>Return"), ("Preferences", "<Control>comma"),
                                 ("Quit", "<Control>q")):
                sec.add(Adw.ShortcutsItem.new(title, accel))
            dlg.add(sec)
            dlg.present(win)
        except Exception:  # noqa: BLE001 - older libadwaita
            win.toast("Ctrl+R record · Ctrl+I import · Ctrl+F search · F2 rename")

    def on_about(self, *_):
        about = Adw.AboutDialog(
            application_name="Study Notes", application_icon=APP_ID, version="1.0",
            developer_name="ajxd2",
            comments="Record or import classes, transcribe them on your NPU, and turn them into notes "
                     "with Claude using your Obsidian vault as context.",
            license_type=Gtk.License.MIT_X11)
        about.present(self._ensure_window())


if __name__ == "__main__":
    GLib.set_prgname(APP_ID)
    GLib.set_application_name("Study Notes")
    sys.exit(App().run(sys.argv))
