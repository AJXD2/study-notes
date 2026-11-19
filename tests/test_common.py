"""Tests for the pure logic in studynotes/common.py. Needs PyGObject (system Python):
    python3 -m unittest discover -s tests -v
"""
import datetime as dt
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "studynotes"))
try:
    import common
except ImportError:  # no PyGObject (e.g. running under the venv): nothing to test here
    common = None


@unittest.skipIf(common is None, "PyGObject not available")
class TextHelpers(unittest.TestCase):
    def test_rename_speakers_swap_and_word_boundaries(self):
        text = "A said hi. B said bye. Speaker 10 stays."
        self.assertEqual(common.rename_speakers_text(text, {"A": "B", "B": "A"}),
                         "B said hi. A said bye. Speaker 10 stays.")
        out = common.rename_speakers_text("Speaker 1 / Speaker 10", {"Speaker 1": "Ms Lee"})
        self.assertEqual(out, "Ms Lee / Speaker 10")

    def test_rename_ignores_blank_and_unchanged(self):
        self.assertEqual(common.rename_speakers_text("A B", {"A": "", "B": "B"}), "A B")

    def test_note_filename(self):
        self.assertEqual(common.note_filename(dt.date(2026, 1, 29)), "1-29-26")
        self.assertEqual(common.note_filename(dt.date(2026, 11, 8), "Circuits"), "11-8-26 - Circuits")

    def test_find_note_by_link(self):
        notes = [Path("/v/2025-09-01 Ohm's Law.md"), Path("/v/2025-11-01 Ohm's Law.md"),
                 Path("/v/2025-10-03 lecture 3.md")]
        self.assertEqual(common.find_note_by_link("2025-09-01 Ohm's Law", notes), notes[0])
        self.assertEqual(common.find_note_by_link("Ohm's Law", notes), notes[1])  # newest wins
        self.assertEqual(common.find_note_by_link("LECTURE", notes), notes[2])    # unique substring
        self.assertIsNone(common.find_note_by_link("nothing", notes))

    def test_fmt_helpers(self):
        self.assertEqual(common.fmt_dur(65), "1:05")
        self.assertEqual(common.fmt_dur(3725), "1:02:05")
        self.assertEqual(common.safe_name('a/b:c*"d"#e'), "abcde")
        self.assertEqual(common.safe_name("..."), "Untitled")


@unittest.skipIf(common is None, "PyGObject not available")
class VaultLayout(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.vault = Path(self.tmp.name) / "Vault"
        for d in ("School/EET-145/Lectures", "School/PSY-201/Notes", "Homelab", "Templates", ".obsidian"):
            (self.vault / d).mkdir(parents=True)
        (self.vault / "School/EET-145/Lectures/8-24-25.md").write_text("---\ndate: 2025-08-24\n---\n# x\n")
        (self.vault / "School/PSY-201/Notes/1-29-26.md").write_text("# y\n")
        (self.vault / "Homelab/Setup.md").write_text("# h\n")
        (self.vault / "Templates/Lecture Note.md").write_text("# t\n")
        (self.vault / "CLAUDE.md").write_text("# instructions\n")
        (self.vault / ".obsidian/x.md").write_text("hidden\n")
        self._orig = common.CONFIG
        common.CONFIG = Path(self.tmp.name) / "cfg.json"
        common.CONFIG.write_text(json.dumps({"vault": str(self.vault)}))
        self.addCleanup(lambda: setattr(common, "CONFIG", self._orig))

    def test_classes_root_and_classes(self):
        self.assertEqual(common.classes_root(), self.vault / "School")
        self.assertEqual(common.vault_classes(), ["EET-145", "PSY-201"])

    def test_classes_root_falls_back_to_vault(self):
        bare = Path(self.tmp.name) / "Bare"
        (bare / "Physics").mkdir(parents=True)
        self.assertEqual(common.classes_root(bare), bare)
        self.assertEqual(common.vault_classes(bare), ["Physics"])

    def test_notes_subdir_prefers_lectures_then_notes(self):
        root = common.classes_root()
        self.assertEqual(common.notes_subdir(root / "EET-145"), root / "EET-145/Lectures")
        self.assertEqual(common.notes_subdir(root / "PSY-201"), root / "PSY-201/Notes")
        (root / "NEW-100").mkdir()
        self.assertEqual(common.notes_subdir(root / "NEW-100"), root / "NEW-100/Lectures")

    def test_vault_notes_skips_hidden_templates_agent_files(self):
        names = sorted(p.name for p in common.vault_notes())
        self.assertEqual(names, ["1-29-26.md", "8-24-25.md", "Setup.md"])

    def test_course_of(self):
        self.assertEqual(common.course_of(self.vault / "School/EET-145/Lectures/8-24-25.md"), "EET-145")
        self.assertEqual(common.course_of(self.vault / "Homelab/Setup.md"), "Homelab")

    def test_note_date_sources(self):
        self.assertEqual(common.note_date(self.vault / "School/EET-145/Lectures/8-24-25.md"), dt.date(2025, 8, 24))
        self.assertEqual(common.note_date(self.vault / "School/PSY-201/Notes/1-29-26.md"), dt.date(2026, 1, 29))
        iso = self.vault / "Homelab/2024-09-12 rome.md"
        iso.write_text("x")
        self.assertEqual(common.note_date(iso), dt.date(2024, 9, 12))


@unittest.skipIf(common is None, "PyGObject not available")
class NoteFormat(unittest.TestCase):
    DATA = {"duration": 125.0, "speakers": [1, 2], "segments": [
        {"start": 0.0, "end": 4.0, "speaker": 1, "text": "Welcome to class."},
        {"start": 4.0, "end": 8.0, "speaker": 1, "text": "Today: Ohm's law."},
        {"start": 65.0, "end": 70.0, "speaker": 2, "text": "Is this on the exam?"}]}

    def test_build_split_parse_roundtrip(self):
        text = common.build_note(cls="EET-145", title="Circuits", date=dt.date(2026, 1, 29), data=self.DATA,
                                 names={"1": "Mr Smith"}, body="## Topics\n- Ohm", model="claude-haiku-5-5",
                                 source="a.mp3 (1.0 MB)")
        fm, head, tr = common.split_note(text)
        self.assertEqual(fm["course"], "EET-145")
        self.assertEqual(fm["type"], "lecture")
        self.assertEqual(fm["date"], "2026-01-29")
        self.assertEqual(fm["source"], "a.mp3 (1.0 MB)")
        self.assertIn("# Lecture Notes - 1/29/26: Circuits", head)
        self.assertIn("## Topics", head)
        self.assertEqual(common.transcript_speakers(text), ["Mr Smith", "Speaker 2"])
        segs, names = common.parse_transcript(tr)
        self.assertEqual([s["text"] for s in segs],
                         ["Welcome to class.", "Today: Ohm's law.", "Is this on the exam?"])
        self.assertEqual(names, {1: "Mr Smith", 2: "Speaker 2"})
        self.assertEqual(segs[2]["start"], 65.0)

    def test_transcript_only_note_marks_model_none(self):
        text = common.build_note(cls="X", title="", date=dt.date(2026, 1, 2), data=self.DATA, names={},
                                 body="", model="claude-haiku-5-5")
        self.assertEqual(common.split_note(text)[0]["model"], "none")
        self.assertIn("transcript only", text)


if __name__ == "__main__":
    unittest.main()
