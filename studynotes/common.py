"""Shared helpers: config, paths, worker subprocess, note read/write."""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import subprocess
import sys
import threading
from pathlib import Path

from gi.repository import GLib

APP_ID = "io.github.ajxd2.StudyNotes"
ROOT = Path(__file__).resolve().parent.parent
IN_FLATPAK = Path("/.flatpak-info").exists()
# Inside Flatpak the app and the worker share the runtime's Python (deps live in /app); on a normal
# install the worker runs in the project's virtualenv.
VENV_PY = ROOT / ".venv" / "bin" / "python"
WORKER = ROOT / "studynotes" / "worker.py"
# XDG dirs (identical to ~/.config etc. on a normal install; ~/.var/app/<id>/... under Flatpak)
CONFIG = Path(GLib.get_user_config_dir()) / "study-notes" / "config.json"
DATA_DIR = Path(GLib.get_user_data_dir()) / "study-notes"
CACHE_DIR = Path(GLib.get_user_cache_dir()) / "study-notes"
REC_DIR = DATA_DIR / "recordings"
TMP_DIR = CACHE_DIR / "tmp"
MODELS_DIR = Path(os.environ.get("STUDYNOTES_MODELS") or DATA_DIR / "models")
BUNDLED_MODELS = Path("/app/share/study-notes/models")  # shipped inside the Flatpak


def worker_python() -> Path | None:
    if VENV_PY.exists():
        return VENV_PY
    return Path(sys.executable) if IN_FLATPAK else None

MODEL_CHOICES = [("claude-haiku-5-5", "Claude Haiku 5.5", "Fast, light on your usage"),
                 ("claude-sonnet-5-5", "Claude Sonnet 5.5", "Better notes, uses more of your plan")]
ASR_CHOICES = [("whisper-base.en-fp16-ov", "Whisper Base (English)", "Fastest"),
               ("whisper-small.en-fp16-ov", "Whisper Small (English)", "More accurate")]
DEVICES = ["NPU", "GPU", "CPU"]
BUDGETS = [(0, "Off"), (100_000, "Small (about 25k tokens)"),
           (400_000, "Medium (about 100k tokens)"), (1_000_000, "Large (about 250k tokens)")]
AUDIO_EXT = {".wav", ".mp3", ".m4a", ".aac", ".ogg", ".oga", ".opus", ".flac", ".wma", ".amr",
             ".mp4", ".m4v", ".mov", ".mkv", ".webm", ".3gp", ".caf", ".aiff", ".aif"}

IGNORED_DIRS = {"Templates", "Attachments", "TMP", "node_modules"}
AGENT_FILES = {"CLAUDE.md", "AGENTS.md"}

DEFAULTS = {
    "vault": str(Path("~/Documents/Study Notes").expanduser()),
    "extra_context": [],  # read-only folders (e.g. an older vault) added to Claude's context
    "model": "claude-haiku-5-5",
    "asr": "whisper-base.en-fp16-ov",
    "device": "NPU",
    "budget": 400_000,
    "num_speakers": 0,  # 0 = detect automatically
    "detect_speakers": True,  # off = fast speech detection only (no "Speaker 1/2" labels)
    "keep_audio": False,
    "win_w": 1100, "win_h": 720,
    "last_note": "",
}


# ---- config ---------------------------------------------------------------
def load_config() -> dict:
    cfg = dict(DEFAULTS)
    if IN_FLATPAK and not CONFIG.exists():  # first run in the sandbox: adopt the host app's settings
        legacy = Path.home() / ".config" / "study-notes" / "config.json"
        try:
            CONFIG.parent.mkdir(parents=True, exist_ok=True)
            CONFIG.write_text(legacy.read_text())
        except OSError:
            pass
    try:
        cfg.update(json.loads(CONFIG.read_text()))
    except (OSError, ValueError):
        pass
    return cfg


def save_config(cfg: dict) -> None:
    CONFIG.parent.mkdir(parents=True, exist_ok=True)
    CONFIG.write_text(json.dumps(cfg, indent=1))


def update_config(**kw) -> dict:
    cfg = load_config()
    cfg.update(kw)
    save_config(cfg)
    return cfg


def signed_in() -> bool:
    return (Path.home() / ".claude" / ".credentials.json").exists()


# ---- small helpers --------------------------------------------------------
def fmt_dur(sec: float) -> str:
    sec = int(sec)
    return (f"{sec // 3600}:{sec % 3600 // 60:02d}:{sec % 60:02d}" if sec >= 3600
            else f"{sec // 60}:{sec % 60:02d}")


def fmt_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return ""


def safe_name(s: str) -> str:
    s = "".join(c for c in s if c not in '\\/:*?"<>|#^[]').strip().strip(".")
    return s or "Untitled"


def vault_dir() -> Path:
    return Path(load_config()["vault"])


def classes_root(vault: Path | None = None) -> Path:
    """Where the course folders live: <vault>/School if it exists (your layout), else the vault itself."""
    vault = vault or vault_dir()
    return vault / "School" if (vault / "School").is_dir() else vault


def vault_classes(vault: Path | None = None) -> list[str]:
    root = classes_root(vault)
    if not root.is_dir():
        return []
    return sorted(p.name for p in root.iterdir()
                  if p.is_dir() and not p.name.startswith(".") and p.name not in IGNORED_DIRS)


def notes_subdir(course_dir: Path) -> Path:
    """Where a course's lecture notes go: its Lectures/ (or Notes/) folder, creating Lectures/ if neither exists."""
    for n in ("Lectures", "Notes"):
        if (course_dir / n).is_dir():
            return course_dir / n
    return course_dir / "Lectures"


def vault_notes(vault: Path | None = None) -> list[Path]:
    vault = vault or vault_dir()
    if not vault.is_dir():
        return []
    out = []
    for p in vault.rglob("*.md"):
        parts = p.relative_to(vault).parts
        if any(x.startswith(".") or x in IGNORED_DIRS for x in parts[:-1]) or parts[-1].startswith("."):
            continue
        if p.name in AGENT_FILES:
            continue
        out.append(p)
    return out


def course_of(path: Path, vault: Path | None = None) -> str:
    """Sidebar group: the course folder under School/, or the top-level folder, or 'Notes'."""
    vault = vault or vault_dir()
    try:
        rel = path.relative_to(classes_root(vault))
        if classes_root(vault) != vault and len(rel.parts) > 1:
            return rel.parts[0]
    except ValueError:
        pass
    rel = path.relative_to(vault)
    return rel.parts[0] if len(rel.parts) > 1 else "Notes"


_DATE_PREFIX = re.compile(r"^(\d{4})-(\d{2})-(\d{2})")


_FM_DATE = re.compile(r"^date:\s*[\"']?(\d{4})-(\d{2})-(\d{2})", re.M)
_MDY = re.compile(r"^(\d{1,2})-(\d{1,2})-(\d{2,4})")


def note_date(p: Path) -> dt.date:
    """Frontmatter date, else M-D-YY / YYYY-MM-DD in the file name, else the file's modified date."""
    try:
        with p.open(errors="ignore") as f:
            m = _FM_DATE.search(f.read(500))
        if m:
            return dt.date(int(m[1]), int(m[2]), int(m[3]))
    except (OSError, ValueError):
        pass
    m = _MDY.match(p.stem)
    if m:
        try:
            y = int(m[3])
            return dt.date(2000 + y if y < 100 else y, int(m[1]), int(m[2]))
        except ValueError:
            pass
    m = _DATE_PREFIX.match(p.stem)
    if m:
        try:
            return dt.date(int(m[1]), int(m[2]), int(m[3]))
        except ValueError:
            pass
    return dt.date.fromtimestamp(p.stat().st_mtime)


def note_filename(d: dt.date, topic: str = "") -> str:
    """Your convention: M-D-YY (e.g. 1-29-26), plus an optional topic."""
    base = f"{d.month}-{d.day}-{d.year % 100:02d}"
    return f"{base} - {topic}" if topic else base


def human_date(d: dt.date) -> str:
    today = dt.date.today()
    if d == today:
        return "Today"
    if d == today - dt.timedelta(days=1):
        return "Yesterday"
    if 0 < (today - d).days < 7:
        return d.strftime("%A")
    return d.strftime("%b %-d") if d.year == today.year else d.strftime("%b %-d, %Y")


def unique_path(d: Path, stem: str) -> Path:
    p = d / f"{stem}.md"
    n = 2
    while p.exists():
        p = d / f"{stem} {n}.md"
        n += 1
    return p


# ---- frontmatter / note structure ----------------------------------------
TRANSCRIPT_MARK = "\n> [!note]- Transcript"
_FM = re.compile(r"\A---\n(.*?)\n---\n", re.S)


def split_note(text: str):
    """-> (frontmatter dict, head_without_fm, transcript_block)."""
    fm: dict[str, str] = {}
    m = _FM.match(text)
    rest = text
    if m:
        for line in m[1].splitlines():
            if ":" in line:
                k, v = line.split(":", 1)
                fm[k.strip()] = v.strip()
        rest = text[m.end():]
    i = rest.find(TRANSCRIPT_MARK)
    head, tr = (rest[:i], rest[i:]) if i >= 0 else (rest, "")
    return fm, head, tr


def parse_transcript(block: str):
    """Rebuild transcript data from the callout. -> (segments, names{id:name})."""
    segs, names, ids = [], {}, {}
    cur = 0
    t = 0.0
    for line in block.splitlines():
        if not line.startswith(">"):
            continue
        line = line[1:].strip()
        m = re.match(r"\*\*(.+?)\*\*\s*\[(?:(\d+):)?(\d+):(\d+)\]", line)
        if m:
            nm = m[1]
            cur = ids.setdefault(nm, len(ids) + 1)
            names[cur] = nm
            t = int(m[2] or 0) * 3600 + int(m[3]) * 60 + int(m[4])
        elif line and not line.startswith("[!note]"):
            segs.append({"start": float(t), "end": float(t), "speaker": cur or 1, "text": line})
    return segs, names


def transcript_speakers(text: str) -> list[str]:
    _, _, tr = split_note(text)
    out: list[str] = []
    for m in re.finditer(r"^> \*\*(.+?)\*\* \[", tr, re.M):
        if m[1] not in out:
            out.append(m[1])
    return out


def build_note(*, cls: str, title: str, date: dt.date, data: dict, names: dict[str, str],
               body: str, model: str, source: str = "") -> str:
    spk = [names.get(str(s)) or f"Speaker {s}" for s in data["speakers"]]
    tr, last = [], None
    for s in data["segments"]:
        who = names.get(str(s["speaker"])) or f"Speaker {s['speaker']}"
        if who != last:
            tr.append(f"> **{who}** [{fmt_dur(s['start'])}]")
            last = who
        tr.append(f"> {s['text']}")
    meta = ["---", f"course: {cls}", "type: lecture", f"date: {date.isoformat()}",
            f"duration: {fmt_dur(data['duration'])}", f"speakers: [{', '.join(dict.fromkeys(spk))}]",
            f"model: {model if body else 'none'}"]
    if source:
        meta.append(f"source: {source}")
    meta += ["tags: [study-notes]", "---", ""]
    heading = f"Lecture Notes - {date.month}/{date.day}/{date.year % 100:02d}" + (f": {title}" if title else "")
    return ("\n".join(meta) + f"# {heading}\n\n---\n\n{body or '_No AI notes - transcript only._'}\n"
            + TRANSCRIPT_MARK + "\n" + "\n".join(tr) + "\n")


def already_imported(source_key: str) -> bool:
    if not source_key:
        return False
    for p in vault_notes():
        try:
            with p.open(errors="ignore") as f:
                head = f.read(600)
        except OSError:
            continue
        if f"source: {source_key}" in head:
            return True
    return False


# ---- worker subprocess ----------------------------------------------------
class Worker:
    """Run worker.py; deliver its JSON-line events on the GLib main loop."""

    def __init__(self, args: list[str], on_event, env_extra: dict | None = None):
        env = dict(os.environ)
        env.setdefault("STUDYNOTES_MODELS", str(MODELS_DIR))
        env.setdefault("STUDYNOTES_BUNDLED_MODELS", str(BUNDLED_MODELS))
        env.setdefault("STUDYNOTES_CACHE", str(CACHE_DIR))
        for k, v in (env_extra or {}).items():
            env.pop(k, None) if v == "" else env.__setitem__(k, v)
        py = worker_python()
        if py is None:
            raise RuntimeError("The AI environment is missing. Run scripts/install.sh")
        self.proc = subprocess.Popen(
            [str(py), str(WORKER), *args], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, env=env, preexec_fn=lambda: os.nice(10))  # keep UI responsive, fans calm
        self.on_event = on_event
        self.cancelled = False
        self.stderr_tail: list[str] = []
        threading.Thread(target=self._read, daemon=True).start()
        threading.Thread(target=self._read_err, daemon=True).start()

    def _read_err(self):
        for line in self.proc.stderr:
            self.stderr_tail = (self.stderr_tail + [line.rstrip()])[-8:]

    def _read(self):
        terminal = False
        for line in self.proc.stdout:
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            terminal |= ev.get("event") in ("result", "error")
            if not self.cancelled:
                GLib.idle_add(self.on_event, ev)
        self.proc.wait()
        if not terminal and not self.cancelled:
            msg = "The background worker stopped unexpectedly. " + " ".join(self.stderr_tail[-2:])
            GLib.idle_add(self.on_event, {"event": "error", "message": msg.strip()})

    def cancel(self):
        self.cancelled = True
        if self.proc.poll() is None:
            self.proc.terminate()


def convert_to_wav(src: Path, dst: Path, on_done) -> None:
    """ffmpeg any audio/video -> 16 kHz mono WAV, off the main thread."""
    def run():
        dst.parent.mkdir(parents=True, exist_ok=True)
        try:
            r = subprocess.run(
                ["ffmpeg", "-y", "-loglevel", "error", "-i", str(src), "-vn", "-ar", "16000", "-ac", "1",
                 "-sample_fmt", "s16", str(dst)], capture_output=True, text=True, timeout=3600)
            err = "" if r.returncode == 0 else (r.stderr.strip().splitlines() or ["ffmpeg failed"])[-1]
        except FileNotFoundError:
            err = "ffmpeg is not installed"
        except subprocess.TimeoutExpired:
            err = "Conversion timed out"
        GLib.idle_add(on_done, err)
    threading.Thread(target=run, daemon=True).start()


class Job:
    """One audio file -> note. Stages are separate so the UI can pause between them."""

    def __init__(self, wav: Path, cls: str, *, title: str = "", date: dt.date | None = None,
                 source: str = ""):
        self.cfg = load_config()
        self.detect_speakers = bool(self.cfg.get("detect_speakers", True))
        self.wav = wav
        self.json = wav.with_suffix(".json")
        self.cls = cls
        self.title = title
        self.date = date or dt.date.today()
        self.source = source
        self.data: dict | None = None
        self.worker: Worker | None = None
        self.path: Path | None = None

    def transcribe(self, on_event):
        c = self.cfg
        self.worker = Worker(["transcribe", "--wav", str(self.wav), "--out", str(self.json),
                              "--asr", c["asr"], "--device", c["device"],
                              "--num-speakers", str(c["num_speakers"] or -1),
                              "--speakers-mode", "detect" if self.detect_speakers else "off"], on_event)

    def load(self):
        self.data = json.loads(self.json.read_text())
        return self.data

    def notes(self, names: dict, on_event):
        c = self.cfg
        self.worker = Worker(["notes", "--transcript", str(self.json), "--vault", c["vault"],
                              "--class", self.cls, "--model", c["model"], "--out", str(self._target()),
                              "--speakers", json.dumps(names), "--budget", str(c["budget"]),
                              *[x for e in c.get("extra_context", []) for x in ("--extra", e)]],
                             on_event, {"ANTHROPIC_API_KEY": ""})  # force the subscription login

    def _dir(self) -> Path:
        return notes_subdir(classes_root() / self.cls)

    def _target(self) -> Path:
        if not self.path:
            self.path = unique_path(self._dir(), note_filename(self.date, self.title))
        return self.path

    def save(self, names: dict, body: str) -> Path:
        path = self._target()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(build_note(
            cls=self.cls, title=self.title, date=self.date, data=self.data, names=names, body=body,
            model=self.cfg["model"], source=self.source))
        return path

    def cleanup(self, keep_audio: bool):
        for p in ([self.json] if keep_audio else [self.json, self.wav]):
            try:
                p.unlink()
            except OSError:
                pass

    def cancel(self):
        if self.worker:
            self.worker.cancel()


def rename_speakers_text(text: str, mapping: dict[str, str]) -> str:
    """Rename speakers everywhere in a note. Two-phase so swaps (A<->B) and merges work."""
    marks: dict[str, str] = {}
    for i, (old, new) in enumerate(mapping.items()):
        new = new.strip()
        if new and new != old:
            tok = f"\u0000{i}\u0000"
            marks[tok] = new
            text = re.sub(rf"(?<!\w){re.escape(old)}(?!\w)", tok, text)
    for tok, new in marks.items():
        text = text.replace(tok, new)
    return text


def find_note_by_link(name: str, notes: list[Path] | None = None) -> Path | None:
    """Obsidian-style: exact file name, else ignoring a leading date, else a unique contains-match."""
    want = name.strip().lower()
    notes = notes if notes is not None else vault_notes()
    strip = lambda s: re.sub(r"^\d{4}-\d{2}-\d{2}[ _-]+", "", s.lower())
    for p in notes:
        if p.stem.lower() == want:
            return p
    dated = [p for p in notes if strip(p.stem) == want]
    if dated:
        return max(dated, key=lambda p: p.stem)  # the most recent one
    hits = [p for p in notes if want in p.stem.lower()]
    return hits[0] if len(hits) == 1 else None
