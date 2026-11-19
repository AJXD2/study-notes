"""Preferences dialog."""
from __future__ import annotations

from gi.repository import Adw, GLib, Gtk

from common import (ASR_CHOICES, BUDGETS, DEVICES, MODEL_CHOICES, load_config, save_config, signed_in)


class Preferences(Adw.PreferencesDialog):
    def __init__(self, window):
        super().__init__()
        self.win = window
        self.cfg = load_config()
        page = Adw.PreferencesPage(title="General", icon_name="preferences-system-symbolic")
        self.add(page)

        g = Adw.PreferencesGroup(title="Vault",
                                 description="Your Obsidian vault. Each class is a folder of Markdown notes.")
        self.vault_row = Adw.ActionRow(title="Vault folder", subtitle=self.cfg["vault"])
        self.vault_row.set_use_markup(False)
        b = Gtk.Button(label="Choose…", valign=Gtk.Align.CENTER)
        b.connect("clicked", self._pick_vault)
        self.vault_row.add_suffix(b)
        g.add(self.vault_row)
        page.add(g)

        self.extra_group = Adw.PreferencesGroup(
            title="Extra context",
            description="Read-only folders Claude can also learn from, like an older vault. Notes are only saved "
                        "to your main vault.")
        add = Gtk.Button(label="Add Folder…", css_classes=["flat"], valign=Gtk.Align.CENTER)
        add.connect("clicked", self._pick_extra)
        self.extra_group.set_header_suffix(add)
        self._extra_rows: list = []
        self._render_extra()
        page.add(self.extra_group)

        ok = signed_in()
        g = Adw.PreferencesGroup(
            title="Claude",
            description="Notes are written through the Claude Agent SDK using your Claude Code login, so "
                        "they use your Claude plan instead of an API key. The transcript and vault notes "
                        "are sent to Anthropic.")
        login = Adw.ActionRow(title="Claude Code account",
                              subtitle="Signed in" if ok else "Not signed in. Run `claude` in a terminal, then /login.")
        login.add_prefix(Gtk.Image.new_from_icon_name("emblem-ok-symbolic" if ok else "dialog-warning-symbolic"))
        g.add(login)
        g.add(self._combo("Model", [(c[1], c[2]) for c in MODEL_CHOICES], [c[0] for c in MODEL_CHOICES],
                          "model"))
        g.add(self._combo("Use vault as context", [(c[1], "Earlier notes help Claude link ideas" if c[0] else "")
                                                   for c in BUDGETS], [c[0] for c in BUDGETS], "budget"))
        page.add(g)

        g = Adw.PreferencesGroup(title="Transcription")
        g.add(self._combo("Speech model", [(c[1], c[2]) for c in ASR_CHOICES], [c[0] for c in ASR_CHOICES],
                          "asr"))
        g.add(self._combo("Run on", [(d, {"NPU": "Lowest power, quiet fans", "GPU": "Integrated graphics",
                                          "CPU": "Slowest"}[d]) for d in DEVICES], DEVICES, "device"))
        det = Adw.SwitchRow(title="Detect speakers", active=self.cfg.get("detect_speakers", True),
                            subtitle="Labels who is speaking. Off is about 4x faster (no Speaker 1/2 labels).")
        det.connect("notify::active", lambda r, _p: self._set("detect_speakers", r.get_active()))
        g.add(det)
        sp = Adw.SpinRow(title="Number of speakers", subtitle="0 detects automatically",
                         adjustment=Gtk.Adjustment(value=self.cfg["num_speakers"], lower=0, upper=8,
                                                   step_increment=1))
        sp.connect("notify::value", lambda r, _p: self._set("num_speakers", int(r.get_value())))
        g.add(sp)
        sw = Adw.SwitchRow(title="Keep recordings", subtitle="Otherwise audio is deleted once the note is saved",
                           active=self.cfg["keep_audio"])
        sw.connect("notify::active", lambda r, _p: self._set("keep_audio", r.get_active()))
        g.add(sw)
        page.add(g)

    def _combo(self, title, items, values, key):
        row = Adw.ComboRow(title=title)
        row.set_model(Gtk.StringList.new([i[0] for i in items]))
        cur = self.cfg.get(key)
        row.set_selected(values.index(cur) if cur in values else 0)

        def sub(r, _p):
            r.set_subtitle(items[r.get_selected()][1])
        row.connect("notify::selected", sub)
        sub(row, None)
        row.connect("notify::selected", lambda r, _p: self._set(key, values[r.get_selected()]))
        return row

    def _set(self, key, value):
        self.cfg[key] = value
        full = load_config()
        full[key] = value
        save_config(full)

    def _pick_vault(self, *_):
        Gtk.FileDialog(title="Choose vault folder").select_folder(self.win, None, self._vault_done)

    def _vault_done(self, dlg, res):
        try:
            f = dlg.select_folder_finish(res)
        except GLib.Error:
            return
        self._set("vault", f.get_path())
        self.vault_row.set_subtitle(f.get_path())
        self.win.refresh_notes()

    # ---- extra context folders -----------------------------------------
    def _render_extra(self):
        for r in self._extra_rows:
            self.extra_group.remove(r)
        self._extra_rows = []
        extras = load_config().get("extra_context", [])
        if not extras:
            r = Adw.ActionRow(title="None added", sensitive=False)
            self.extra_group.add(r)
            self._extra_rows.append(r)
        for path in extras:
            r = Adw.ActionRow(title=path.rsplit("/", 1)[-1], subtitle=path)
            r.set_use_markup(False)
            rm = Gtk.Button(icon_name="user-trash-symbolic", valign=Gtk.Align.CENTER,
                            css_classes=["flat", "circular"], tooltip_text="Remove")
            rm.connect("clicked", lambda _b, p=path: self._remove_extra(p))
            r.add_suffix(rm)
            self.extra_group.add(r)
            self._extra_rows.append(r)

    def _pick_extra(self, *_):
        Gtk.FileDialog(title="Add a context folder").select_folder(self.win, None, self._extra_done)

    def _extra_done(self, dlg, res):
        try:
            f = dlg.select_folder_finish(res)
        except GLib.Error:
            return
        extras = list(load_config().get("extra_context", []))
        if f.get_path() not in extras:
            extras.append(f.get_path())
            self._set("extra_context", extras)
        self._render_extra()

    def _remove_extra(self, path: str):
        self._set("extra_context", [e for e in load_config().get("extra_context", []) if e != path])
        self._render_extra()
