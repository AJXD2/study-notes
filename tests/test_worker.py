"""Tests for the pure logic in studynotes/worker.py. Needs numpy (the project venv):
    .venv/bin/python -m unittest discover -s tests -v
"""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "studynotes"))
try:
    import numpy as np
    import worker
except ImportError:
    np = worker = None

SR = 16000


@unittest.skipIf(worker is None, "numpy not available (use the project venv)")
class Chunking(unittest.TestCase):
    def tone(self, seconds, amp=0.3):
        t = np.arange(int(seconds * SR)) / SR
        return (amp * np.sin(2 * np.pi * 220 * t)).astype("float32")

    def test_windows_cover_audio_and_stay_under_limit(self):
        audio = self.tone(100)
        bounds = worker.split_points(audio)
        self.assertAlmostEqual(bounds[0][0], 0.0)
        self.assertAlmostEqual(bounds[-1][1], 100.0, places=1)
        for (a, b), (c, _d) in zip(bounds, bounds[1:]):
            self.assertAlmostEqual(b, c)  # contiguous, nothing dropped
        self.assertTrue(all(b - a <= worker.CHUNK_MAX + 1e-6 for a, b in bounds))

    def test_cut_prefers_the_quiet_gap(self):
        audio = self.tone(60)
        audio[int(24.0 * SR):int(24.3 * SR)] = 0.0  # a pause at 24 s
        first_end = worker.split_points(audio)[0][1]
        self.assertAlmostEqual(first_end, 24.15, delta=0.3)

    def test_quiet_split_finds_middle_gap(self):
        audio = self.tone(20)
        audio[int(9.5 * SR):int(9.8 * SR)] = 0.0
        self.assertAlmostEqual(worker.quiet_split(audio) / SR, 9.65, delta=0.3)


@unittest.skipIf(worker is None, "numpy not available (use the project venv)")
class Speakers(unittest.TestCase):
    def test_prune_folds_tiny_speakers_into_neighbours(self):
        turns = [(0, 100, 0), (100, 103, 1), (103, 200, 0)]
        out = worker.prune_speakers(turns)
        self.assertEqual({k for _, _, k in out}, {0})

    def test_prune_keeps_real_second_speaker(self):
        turns = [(0, 100, 0), (100, 160, 1), (160, 260, 0)]
        self.assertEqual({k for _, _, k in worker.prune_speakers(turns)}, {0, 1})

    def test_assign_speakers_by_overlap_and_renumbers_by_first_appearance(self):
        segs = [{"start": 0, "end": 5, "text": "a"}, {"start": 5, "end": 10, "text": "b"},
                {"start": 10, "end": 12, "text": "c"}]
        turns = [(0, 5, 7), (5, 10, 3), (10, 12, 7)]
        out = worker.assign_speakers(segs, turns)
        self.assertEqual([s["speaker"] for s in out], [1, 2, 1])

    def test_assign_speakers_without_overlap_keeps_previous(self):
        segs = [{"start": 0, "end": 5, "text": "a"}, {"start": 50, "end": 55, "text": "b"}]
        out = worker.assign_speakers(segs, [(0, 5, 0)])
        self.assertEqual([s["speaker"] for s in out], [1, 1])

    def test_speech_overlap(self):
        self.assertAlmostEqual(worker.speech_overlap([(0, 2, 0), (5, 9, 0)], 1, 6), 2.0)


@unittest.skipIf(worker is None, "numpy not available (use the project venv)")
class Context(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.vault, self.old = root / "Master", root / "Old"
        for d in ("School/EET-145/Lectures", "School/PSY-201/Notes", "Templates", "Attachments", "TMP"):
            (self.vault / d).mkdir(parents=True)
        (self.old / "School/PHY/Notes").mkdir(parents=True)
        (self.vault / "School/EET-145/Lectures/a.md").write_text(
            "# EET note\n> [!note]- Transcript\n> **Speaker 1** [0:00]\n> secret transcript words\n")
        (self.vault / "School/PSY-201/Notes/b.md").write_text("# PSY note\n")
        (self.vault / "Templates/Lecture Note.md").write_text("# template\n")
        (self.vault / "Attachments/c.md").write_text("# attachment\n")
        (self.vault / "TMP/d.md").write_text("# tmp\n")
        (self.vault / "CLAUDE.md").write_text("# agent instructions\n")
        (self.old / "School/PHY/Notes/e.md").write_text("# old PHY note\n")
        (self.old / "AGENTS.md").write_text("# old agent file\n")

    def test_gather_orders_course_first_then_vault_then_older_and_filters(self):
        ctx, n, total = worker.gather_vault(self.vault, "EET-145", 10_000, None, [str(self.old)])
        self.assertEqual((n, total), (3, 3))
        paths = [line.split('"')[1] for line in ctx.splitlines() if line.startswith("<note ")]
        self.assertEqual(paths, ["School/EET-145/Lectures/a.md", "School/PSY-201/Notes/b.md",
                                 "older/Old/School/PHY/Notes/e.md"])
        for banned in ("template", "attachment", "agent", "tmp", "secret transcript words"):
            self.assertNotIn(banned, ctx.lower())

    def test_budget_and_skip(self):
        ctx, n, _ = worker.gather_vault(self.vault, "EET-145", 60, None)
        self.assertLessEqual(len(ctx), 60)
        ctx, n, total = worker.gather_vault(self.vault, "EET-145", 10_000, self.vault / "School/PSY-201/Notes/b.md")
        self.assertNotIn("PSY note", ctx)

    def test_find_template(self):
        self.assertEqual(worker.find_template(self.vault, []), "# template")
        self.assertEqual(worker.find_template(self.old, []), "")

    def test_format_transcript_groups_by_speaker_and_applies_names(self):
        data = {"segments": [{"start": 0, "speaker": 1, "text": "hi"}, {"start": 3, "speaker": 1, "text": "there"},
                             {"start": 70, "speaker": 2, "text": "question?"}]}
        out = worker.format_transcript(data, {"1": "Teacher"})
        self.assertEqual(out, "**Teacher** [0:00]\nhi\nthere\n\n**Speaker 2** [1:10]\nquestion?")


if __name__ == "__main__":
    unittest.main()
