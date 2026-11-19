"""Lightweight live Markdown styling for a Gtk.TextView (headings, bold, callouts, links)."""
from __future__ import annotations

import re

from gi.repository import Adw, Gdk, GLib, Gtk, Pango

WIKILINK = re.compile(r"\[\[([^\[\]|#]+)(?:#[^\[\]|]*)?(?:\|[^\[\]]*)?\]\]")
BOLD = re.compile(r"(\*\*)(.+?)(\*\*)")
ITALIC = re.compile(r"(?<![\*\w])(\*|_)(?!\s)(.+?)(?<!\s)\1(?![\*\w])")
CODE = re.compile(r"(`)([^`\n]+)(`)")
HEADING = re.compile(r"^(#{1,4}) .*$", re.M)
BULLET = re.compile(r"^(\s*)([-*+]|\d+\.) ", re.M)
QUOTE = re.compile(r"^> ?.*$", re.M)
FM = re.compile(r"\A---\n.*?\n---\n", re.S)


class MarkdownStyler:
    def __init__(self, view: Gtk.TextView):
        self.view = view
        self.buf = view.get_buffer()
        self._id = 0
        self._build_tags()
        self.buf.connect("changed", self._schedule)
        sm = Adw.StyleManager.get_default()
        sm.connect("notify::dark", self.restyle_soon)
        sm.connect("notify::accent-color-rgba", self.restyle_soon)
        view.connect("map", self.restyle_soon)

    # ---- tags ----------------------------------------------------------
    def _build_tags(self):
        t = self.buf.create_tag
        self.h = {1: t("h1", scale=1.55, weight=Pango.Weight.BOLD, pixels_above_lines=14, pixels_below_lines=6),
                  2: t("h2", scale=1.28, weight=Pango.Weight.BOLD, pixels_above_lines=14, pixels_below_lines=4),
                  3: t("h3", scale=1.12, weight=Pango.Weight.BOLD, pixels_above_lines=10, pixels_below_lines=2),
                  4: t("h4", weight=Pango.Weight.BOLD, pixels_above_lines=8)}
        self.bold = t("bold", weight=Pango.Weight.BOLD)
        self.italic = t("italic", style=Pango.Style.ITALIC)
        self.code = t("code", family="monospace", scale=0.92)
        self.mark = t("mark")          # the #, **, [[ ]] characters themselves
        self.quote = t("quote", left_margin=22, scale=0.94)
        self.link = t("link", underline=Pango.Underline.SINGLE)
        self.fm = t("fm", scale=0.85)
        self.bullet = t("bullet")
        self.speaker = t("speaker", weight=Pango.Weight.BOLD)
        self._restyle_tags()

    def restyle_soon(self, *_):
        """Recompute tag colours a moment after a theme change.

        The signal fires before GTK has resolved the new foreground colour, so reading it
        immediately returns the old theme's colour (invisible frontmatter in light mode).
        """
        GLib.timeout_add(80, lambda: (self._restyle_tags(), False)[1])

    def _fg(self) -> Gdk.RGBA:
        return self.view.get_color()

    def _restyle_tags(self):
        fg = self._fg()
        dim = Gdk.RGBA()
        dim.red, dim.green, dim.blue, dim.alpha = fg.red, fg.green, fg.blue, 0.42
        mid = Gdk.RGBA()
        mid.red, mid.green, mid.blue, mid.alpha = fg.red, fg.green, fg.blue, 0.68
        accent = Adw.StyleManager.get_default().get_accent_color_rgba()
        self.mark.set_property("foreground-rgba", dim)
        self.fm.set_property("foreground-rgba", dim)
        self.quote.set_property("foreground-rgba", mid)
        self.bullet.set_property("foreground-rgba", accent)
        self.link.set_property("foreground-rgba", accent)
        self.speaker.set_property("foreground-rgba", accent)
        bg = Gdk.RGBA()
        bg.red, bg.green, bg.blue, bg.alpha = fg.red, fg.green, fg.blue, 0.08
        self.code.set_property("background-rgba", bg)

    # ---- applying ------------------------------------------------------
    def _schedule(self, *_):
        if self._id:
            GLib.source_remove(self._id)
        self._id = GLib.timeout_add(120, self.apply)

    def apply(self):
        self._id = 0
        buf = self.buf
        start, end = buf.get_bounds()
        buf.remove_all_tags(start, end)
        text = buf.get_text(start, end, False)
        # offsets in GTK are characters, which equals Python str indices
        def tag(t, a, b):
            buf.apply_tag(t, buf.get_iter_at_offset(a), buf.get_iter_at_offset(b))

        fm = FM.match(text)
        fm_end = fm.end() if fm else 0
        if fm:
            tag(self.fm, 0, fm_end)
        for m in HEADING.finditer(text, fm_end):
            lvl = len(m[1])
            tag(self.h[min(lvl, 4)], m.start(), m.end())
            tag(self.mark, m.start(), m.start() + lvl + 1)
        for m in QUOTE.finditer(text, fm_end):
            tag(self.quote, m.start(), m.end())
            tag(self.mark, m.start(), m.start() + 1)
        for m in BULLET.finditer(text, fm_end):
            tag(self.bullet, m.start(2), m.end(2))
        for rx, t in ((BOLD, self.bold), (ITALIC, self.italic), (CODE, self.code)):
            for m in rx.finditer(text, fm_end):
                tag(t, m.start(), m.end())
                tag(self.mark, m.start(1), m.end(1))
                tag(self.mark, m.start(3) if m.lastindex >= 3 else m.end() - len(m[1]), m.end())
        for m in WIKILINK.finditer(text, fm_end):
            tag(self.link, m.start(), m.end())
        for m in re.finditer(r"^> (\*\*.+?\*\*) \[[\d:]+\]$", text, re.M):
            tag(self.speaker, m.start(1), m.end(1))
        return False

    # ---- links ---------------------------------------------------------
    def link_at(self, x: int, y: int) -> str | None:
        ok, it = self.view.get_iter_at_location(x, y)
        if not ok:
            return None
        if not it.has_tag(self.link):
            return None
        s = it.copy()
        if not s.starts_tag(self.link):
            s.backward_to_tag_toggle(self.link)
        e = s.copy()
        e.forward_to_tag_toggle(self.link)
        m = WIKILINK.fullmatch(self.buf.get_text(s, e, False))
        return m[1].strip() if m else None
