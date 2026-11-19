#!/usr/bin/env python3
"""Study Notes worker: runs inside the project venv.

Talks to the GTK front-end with JSON lines on stdout:
  {"event": "progress", "stage": "...", "detail": "..."}
  {"event": "result", ...}
  {"event": "text", "delta": "..."}     (streamed note text)
  {"event": "error", "message": "..."}

Usage:
  worker.py transcribe --wav FILE --out FILE [--asr MODEL] [--device NPU]
  worker.py notes --transcript FILE --vault DIR --class NAME --model ID
                  --out FILE [--speakers JSON] [--budget CHARS]
Claude is reached through the Claude Agent SDK using the user's Claude Code login.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
import wave
from pathlib import Path

MODELS = Path(os.environ.get("STUDYNOTES_MODELS", "~/.local/share/study-notes/models")).expanduser()
BUNDLED = Path(os.environ.get("STUDYNOTES_BUNDLED_MODELS", "/app/share/study-notes/models"))  # Flatpak
CACHE = Path(os.environ.get("STUDYNOTES_CACHE", "~/.cache/study-notes")).expanduser() / "ov"


def find_model(rel: str) -> Path:
    """First existing copy of a model: the user's models dir, then the one shipped inside the Flatpak."""
    for root in (MODELS, BUNDLED):
        if (root / rel).exists():
            return root / rel
    return MODELS / rel


SEG_MODEL = find_model("sherpa-onnx-pyannote-segmentation-3-0/model.int8.onnx")
EMB_MODEL = find_model("wespeaker_en_voxceleb_resnet34.onnx")
VAD_MODEL = find_model("silero_vad.onnx")


def emit(**kw) -> None:
    sys.stdout.write(json.dumps(kw) + "\n")
    sys.stdout.flush()


def read_wav(path: str):
    import numpy as np

    with wave.open(path) as w:
        if w.getframerate() != 16000 or w.getnchannels() != 1 or w.getsampwidth() != 2:
            raise RuntimeError("expected 16 kHz mono 16-bit WAV")
        raw = w.readframes(w.getnframes())
    return np.frombuffer(raw, np.int16).astype(np.float32) / 32768.0


# --------------------------------------------------------------------------
# Transcription (OpenVINO GenAI Whisper, NPU first)
# --------------------------------------------------------------------------
MIN_WINDOW_SPEECH = 0.3   # s of detected speech needed for a window to be transcribed at all
MIN_SEG_OVERLAP = 0.25    # s (or 25% of the segment) of detected speech needed to keep a segment
CHUNK_MAX = 28.0   # seconds; Whisper's window is 30 s
CHUNK_MIN = 18.0


def split_points(audio, sr: int = 16000):
    """Cut audio into <=28 s windows, preferring the quietest 50 ms in the last 10 s.

    Whisper on the NPU silently drops text in its own long-form mode, so we
    never hand it more than one window.
    """
    import numpy as np

    n = len(audio)
    hop = sr // 20  # 50 ms
    frames = n // hop
    rms = np.sqrt(np.mean(audio[:frames * hop].reshape(frames, hop) ** 2, axis=1)) if frames else np.zeros(0)
    out, start = [], 0.0
    total = n / sr
    while start < total:
        end = min(start + CHUNK_MAX, total)
        if total - start > CHUNK_MAX:
            lo, hi = int((start + CHUNK_MIN) * 20), int(end * 20)
            end = (lo + int(np.argmin(rms[lo:hi]))) / 20 if hi > lo else end
        out.append((start, end))
        start = end
    return out


def quiet_split(chunk, sr: int = 16000) -> int:
    """Sample index of the quietest 50 ms in the middle third of a window."""
    import numpy as np

    n = len(chunk)
    hop = sr // 20
    lo, hi = n // 3 // hop, max(n * 2 // 3 // hop, n // 3 // hop + 1)
    frames = chunk[:(n // hop) * hop].reshape(-1, hop)
    rms = np.sqrt(np.mean(frames ** 2, axis=1))
    return (lo + int(np.argmin(rms[lo:hi]))) * hop


def run_window(make, dev: str, chunk, offset: float, dur: float, depth: int = 0):
    """Transcribe one <=28 s window -> [(start, end, text)].

    The NPU's decoder has a fixed-size KV cache and raises "KV-Cache is full" on dense speech or a
    decoding loop. Never let that kill a whole lecture: retry on each half (splitting at a quiet
    point), and for pieces that still fail fall back to the CPU, which has no such limit.
    A fresh pipeline per call because a reused NPU pipeline degrades after its first call.
    """
    def collect(pipe):
        r = pipe.generate(chunk.tolist(), return_timestamps=True)
        out = []
        for c in r.chunks or []:
            text = c.text.strip()
            if text:
                out.append((min(offset + float(c.start_ts), dur), min(offset + float(c.end_ts), dur), text))
        return out

    try:
        return collect(make(dev))
    except Exception as e:  # noqa: BLE001
        why = str(e).replace("\n", " ")[:90]
        if dev != "CPU" and depth < 2 and len(chunk) > 16000 * 6:
            emit(event="progress", stage="transcribing", detail=f"Retrying a dense section in halves ({why})")
            cut = quiet_split(chunk)
            left = run_window(make, dev, chunk[:cut], offset, dur, depth + 1)
            right = run_window(make, dev, chunk[cut:], offset + cut / 16000, dur, depth + 1)
            return left + right
        if dev != "CPU":
            emit(event="progress", stage="transcribing", detail=f"Using the CPU for one section ({why})")
            return run_window(make, "CPU", chunk, offset, dur, depth)
        raise


def speech_overlap(turns, a: float, b: float) -> float:
    return sum(max(0.0, min(b, y) - max(a, x)) for x, y, _ in turns)


def transcribe_audio(audio, asr: str, device: str, turns=None):
    import openvino_genai as ov_genai

    dur = len(audio) / 16000

    model_dir = find_model(asr)
    if not model_dir.is_dir():
        # not bundled and not downloaded yet: fetch the OpenVINO IR from Hugging Face once
        try:
            from huggingface_hub import snapshot_download

            emit(event="progress", stage="loading", detail="Downloading the speech model (one time)…")
            MODELS.mkdir(parents=True, exist_ok=True)
            snapshot_download(f"OpenVINO/{asr}", local_dir=MODELS / asr)
            model_dir = MODELS / asr
        except Exception as e:  # noqa: BLE001
            raise RuntimeError(f"Speech model '{asr}' is missing and could not be downloaded: {e}")
    CACHE.mkdir(parents=True, exist_ok=True)
    def make(dev):
        return ov_genai.WhisperPipeline(str(model_dir), dev, CACHE_DIR=str(CACHE))

    pipe = None
    used = device
    for dev in dict.fromkeys([device, "GPU", "CPU"]):  # fall back NPU -> GPU -> CPU
        try:
            emit(event="progress", stage="loading", detail=f"Loading Whisper on {dev}")
            pipe = make(dev)
            used = dev
            break
        except Exception as e:  # noqa: BLE001
            emit(event="progress", stage="loading", detail=f"{dev} unavailable ({str(e)[:80]})")
    if pipe is None:
        raise RuntimeError("No usable OpenVINO device for Whisper")
    del pipe  # it only proved the device works; each window gets its own fresh pipeline

    segs = []
    skipped_windows = dropped_segments = 0
    bounds = split_points(audio)
    for i, (a0, a1) in enumerate(bounds):
        pct = int(100 * i / len(bounds))
        emit(event="progress", stage="transcribing", detail=f"Transcribing on {used} ({pct}%)", percent=pct)
        chunk = audio[int(a0 * 16000):int(a1 * 16000)]
        if len(chunk) < 8000:  # < 0.5 s: nothing useful
            continue
        # Whisper invents text ("Thank you.") for silence and room noise, so only transcribe windows
        # where the speech detector (pyannote) heard speech. Level alone is useless: a room can be
        # louder than quiet speech.
        if turns is not None and speech_overlap(turns, a0, a1) < MIN_WINDOW_SPEECH:
            skipped_windows += 1
            continue
        for st, en, text in run_window(make, used, chunk, a0, dur):
            if turns is not None and speech_overlap(turns, st, en) < min(MIN_SEG_OVERLAP, 0.25 * max(en - st, 0.1)) \
                    and speech_overlap(turns, st, en) < MIN_SEG_OVERLAP:
                dropped_segments += 1  # text with no speech under it = hallucination
                continue
            segs.append({"start": st, "end": en, "text": text})
    return segs, used, {"windows": skipped_windows, "segments": dropped_segments}


# --------------------------------------------------------------------------
# Diarization (sherpa-onnx, offline, CPU; 4 threads measured fastest - 6 is slower)
# --------------------------------------------------------------------------
def diarize_audio(audio, threads: int = 4, num_speakers: int = -1, threshold: float = 0.4):
    import sherpa_onnx as so

    cfg = so.OfflineSpeakerDiarizationConfig(
        segmentation=so.OfflineSpeakerSegmentationModelConfig(
            pyannote=so.OfflineSpeakerSegmentationPyannoteModelConfig(model=str(SEG_MODEL)),
            num_threads=threads,
        ),
        embedding=so.SpeakerEmbeddingExtractorConfig(model=str(EMB_MODEL), num_threads=threads),
        clustering=so.FastClusteringConfig(num_clusters=num_speakers, threshold=threshold),
        min_duration_on=0.3,
        min_duration_off=0.5,
    )
    if not cfg.validate():
        raise RuntimeError("Invalid diarization config (are the models downloaded?)")
    sd = so.OfflineSpeakerDiarization(cfg)
    last = [-1]

    def progress(done, total):  # called per audio chunk; report ~every 5 %
        pct = int(100 * done / max(total, 1))
        if pct >= last[0] + 5:
            last[0] = pct
            emit(event="progress", stage="diarizing", detail=f"Finding speech and speakers ({pct}%)", percent=pct)
        return 0

    res = sd.process(audio, progress).sort_by_start_time()
    return [(s.start, s.end, int(s.speaker)) for s in res]


def vad_turns(audio):
    """Speech regions only (Silero VAD, ~300x realtime) as (start, end, 0) tuples. No speaker labels."""
    import sherpa_onnx as so

    cfg = so.VadModelConfig()
    cfg.silero_vad.model = str(VAD_MODEL)
    cfg.silero_vad.threshold = 0.5
    cfg.silero_vad.min_silence_duration = 0.4
    cfg.silero_vad.min_speech_duration = 0.25
    cfg.sample_rate = 16000
    cfg.num_threads = 1
    vad = so.VoiceActivityDetector(cfg, buffer_size_in_seconds=60)
    win = cfg.silero_vad.window_size
    out = []

    def drain():
        while not vad.empty():
            seg = vad.front
            out.append((seg.start / 16000, (seg.start + len(seg.samples)) / 16000, 0))
            vad.pop()

    for i in range(0, len(audio) - win + 1, win):
        vad.accept_waveform(audio[i:i + win])
        drain()
    vad.flush()
    drain()
    return out


def prune_speakers(turns, min_share: float = 0.05, min_seconds: float = 20.0):
    """Fold tiny clusters (a cough, one stray word) into the nearest real speaker.

    Clustering on real lectures over-splits one teacher into many labels; a "speaker" with under 5 % of
    the speech (and under 20 s) is almost never a real participant.
    """
    tot: dict[int, float] = {}
    for a, b, k in turns:
        tot[k] = tot.get(k, 0.0) + (b - a)
    total = sum(tot.values()) or 1.0
    real = {k for k, v in tot.items() if v / total >= min_share or v >= min_seconds}
    if not real or len(real) == len(tot):
        return turns
    anchors = [(a, b, k) for a, b, k in turns if k in real]
    out = []
    for a, b, k in turns:
        if k in real:
            out.append((a, b, k))
            continue
        mid = (a + b) / 2
        near = min(anchors, key=lambda t: 0 if t[0] <= mid <= t[1] else min(abs(mid - t[0]), abs(mid - t[1])))
        out.append((a, b, near[2]))
    return out


def assign_speakers(segs, turns):
    """Give each Whisper segment the speaker whose turns overlap it most."""
    for s in segs:
        overlap: dict[int, float] = {}
        for a, b, spk in turns:
            o = min(s["end"], b) - max(s["start"], a)
            if o > 0:
                overlap[spk] = overlap.get(spk, 0.0) + o
        s["speaker"] = max(overlap, key=overlap.get) if overlap else -1
    # renumber by first appearance so "Speaker 1" is whoever talks first
    order: dict[int, int] = {}
    for s in segs:
        if s["speaker"] not in order and s["speaker"] != -1:
            order[s["speaker"]] = len(order) + 1
    prev = 1
    for s in segs:
        s["speaker"] = order.get(s["speaker"], prev)
        prev = s["speaker"]
    return segs


def cmd_transcribe(a) -> None:
    audio = read_wav(a.wav)
    dur = len(audio) / 16000
    turns = None
    labelled = a.speakers_mode != "off"
    try:
        if labelled:
            emit(event="progress", stage="diarizing", detail="Finding speech and speakers")
            turns = prune_speakers(diarize_audio(audio, num_speakers=a.num_speakers))
        else:
            emit(event="progress", stage="diarizing", detail="Finding speech")
            turns = vad_turns(audio)
    except Exception as e:  # noqa: BLE001 - still transcribe, just without gating / speaker labels
        emit(event="progress", stage="diarizing", detail=f"Speech detection failed: {e}")
        labelled = False
    segs, dev, skipped = transcribe_audio(audio, a.asr, a.device, turns)
    if turns is not None and labelled:
        segs = assign_speakers(segs, turns)
    else:
        for s in segs:
            s["speaker"] = 1
    out = {"duration": dur, "device": dev, "segments": segs, "skipped": skipped,
           "speakers": sorted({s["speaker"] for s in segs})}
    Path(a.out).write_text(json.dumps(out, indent=1))
    emit(event="result", out=a.out, duration=dur, device=dev, speakers=out["speakers"], skipped=skipped)


# --------------------------------------------------------------------------
# Claude notes
# --------------------------------------------------------------------------
TRANSCRIPT_CALLOUT = re.compile(r"\n> \[!note\]- Transcript.*", re.S)


def fmt_time(t: float) -> str:
    t = int(t)
    return f"{t // 3600:d}:{t % 3600 // 60:02d}:{t % 60:02d}" if t >= 3600 else f"{t // 60:d}:{t % 60:02d}"


def format_transcript(data, names: dict[str, str]) -> str:
    lines, last = [], None
    for s in data["segments"]:
        who = names.get(str(s["speaker"])) or f"Speaker {s['speaker']}"
        if who != last:
            lines.append(f"\n**{who}** [{fmt_time(s['start'])}]")
            last = who
        lines.append(s["text"])
    return "\n".join(lines).strip()


IGNORED_DIRS = {"Templates", "Attachments", "TMP", "node_modules"}
AGENT_FILES = {"CLAUDE.md", "AGENTS.md"}  # instructions for coding agents, not course material


def gather_vault(vault: Path, cls: str, budget: int, skip: Path | None, extras: list[str] | None = None):
    """Return (context_text, n_notes_included, n_notes_total).

    Priority: the same course first (newest first), then the rest of the vault, then the extra
    read-only folders (e.g. an older vault). Old transcripts are stripped - the notes are the
    useful part. Templates and agent-instruction files are left out.
    """
    roots = [(vault, "")] + [(Path(e).expanduser(), f"older/{Path(e).name}/") for e in (extras or [])]
    files: list[tuple[int, float, Path, str]] = []
    for root, label in roots:
        if not root.is_dir():
            continue
        for p in root.rglob("*.md"):
            parts = p.relative_to(root).parts
            if (any(x.startswith(".") or x in IGNORED_DIRS for x in parts[:-1]) or parts[-1].startswith(".")
                    or p.name in AGENT_FILES or p == skip):
                continue
            tier = (0 if cls in parts else 1) + (2 if label else 0)  # course > vault > older vaults
            try:
                files.append((tier, -p.stat().st_mtime, p, label + "/".join(parts)))
            except OSError:
                continue
    files.sort(key=lambda t: (t[0], t[1]))
    chunks, used, n = [], 0, 0
    for _t, _m, p, rel in files:
        try:
            txt = TRANSCRIPT_CALLOUT.sub("", p.read_text(errors="ignore")).strip()
        except OSError:
            continue
        piece = f'<note path="{rel}">\n{txt}\n</note>'
        if used + len(piece) > budget:
            continue
        chunks.append(piece)
        used += len(piece)
        n += 1
    return "\n\n".join(chunks), n, len(files)


def find_template(vault: Path, extras: list[str]) -> str:
    for root in [vault] + [Path(e).expanduser() for e in extras]:
        p = root / "Templates" / "Lecture Note.md"
        if p.is_file():
            return p.read_text(errors="ignore").strip()
    return ""


SYSTEM = """You turn a class transcript into the student's lecture notes, in Obsidian Markdown, written \
the way THIS student writes. You are given their existing notes (the vault) - study how they write: \
terse bullet points, **bold** key terms, short definitions, nesting for sub-points, `$$ ... $$` for math, \
engineering notation, no filler sentences. Match that voice and density; do not write essays.

Structure: write the body under the student's lecture template, which is shown if available. If there \
is no template use these sections. Separate sections with a line containing only `---`.
## Topics    - the main topics covered, as a short bullet list (or a few ### headings if they use them)
## Notes     - the substance, organised under ### headings by topic; define terms precisely
## Questions - things that were unclear, homework / assignments / due dates the teacher mentioned, \
                and review questions. Put homework and deadlines first.

Use the vault as context: connect to earlier material and use the course's established terminology \
and spelling (the transcript is speech-to-text and will mangle technical terms - fix them using the \
vault and your own knowledge, and mark real uncertainty with (?)). Link to existing notes with \
[[wikilinks]] (file name without extension) ONLY when that note exists in the vault; never invent links. \
Do not repeat at length what the student already has.

Be faithful: do not add facts that were not said in class. Speakers are labelled; attribute questions \
and announcements when it matters.

Output ONLY the body: start at `## Topics`. No frontmatter, no `# title` heading, no preamble."""


def cmd_notes(a) -> None:
    data = json.loads(Path(a.transcript).read_text())
    names = json.loads(a.speakers) if a.speakers else {}
    transcript = format_transcript(data, names)
    vault = Path(a.vault).expanduser()
    out = Path(a.out)

    ctx, n_in, n_all = ("", 0, 0)
    if a.budget > 0 and vault.is_dir():
        ctx, n_in, n_all = gather_vault(vault, a.cls, a.budget, out, a.extra)
    template = find_template(vault, a.extra)
    emit(event="progress", stage="notes",
         detail=f"Asking Claude ({n_in}/{n_all} vault notes as context)")

    today = dt.date.today().isoformat()
    parts = []
    if template:
        parts.append(f"<lecture_template>\n{template}\n</lecture_template>")
    if ctx:
        parts.append(f"<vault>\n{ctx}\n</vault>")
    parts.append(f"Class: {a.cls}\nDate: {today}\n\n<transcript>\n{transcript}\n</transcript>\n\n"
                 "Write the study note.")
    prompt = "\n\n".join(parts)  # goes over stdin, so size is not limited by argv

    body = run_agent(prompt, a.model)
    emit(event="result", body=body, stop_reason="end_turn")


def run_agent(prompt: str, model: str) -> str:
    """Ask Claude through the Claude Agent SDK, i.e. the user's own Claude Code login
    (subscription) - no API key. All tools, settings, hooks, MCP and memory are off so
    this is a plain one-shot text generation."""
    import tempfile

    import anyio
    from claude_agent_sdk import (AssistantMessage, ClaudeAgentOptions, CLINotFoundError,
                                  ResultMessage, StreamEvent, TextBlock, query)

    chunks: list[str] = []
    final_text: list[str] = []
    err: list[str] = []

    async def go(cwd: str):
        opts = ClaudeAgentOptions(
            system_prompt=SYSTEM, model=model, tools=[], allowed_tools=[], max_turns=1,
            setting_sources=[], strict_mcp_config=True, skills=[], plugins=[],
            permission_mode="dontAsk", cwd=cwd, include_partial_messages=True,
            extra_args={"no-session-persistence": None},  # do not copy the vault into ~/.claude
            
            stderr=lambda line: err.append(line))
        async for msg in query(prompt=prompt, options=opts):
            if isinstance(msg, StreamEvent):
                ev = msg.event or {}
                d = ev.get("delta") or {}
                if ev.get("type") == "content_block_delta" and d.get("type") == "text_delta":
                    chunks.append(d["text"])
                    emit(event="text", delta=d["text"])
            elif isinstance(msg, AssistantMessage):
                final_text.append("".join(b.text for b in msg.content if isinstance(b, TextBlock)))
            elif isinstance(msg, ResultMessage):
                if msg.is_error:
                    raise RuntimeError(str(getattr(msg, "result", "") or "Claude returned an error"))
                u = getattr(msg, "usage", None) or {}
                emit(event="progress", stage="notes",
                     detail=f"done - in={u.get('input_tokens', '?')} out={u.get('output_tokens', '?')}")

    try:
        with tempfile.TemporaryDirectory(prefix="study-notes-") as cwd:
            anyio.run(go, cwd)
    except CLINotFoundError:
        raise RuntimeError("Claude Code was not found - install it and sign in with `claude`")
    except RuntimeError:
        raise
    except Exception as e:  # noqa: BLE001
        tail = " ".join(err[-2:])
        msg = f"{e} {tail}".strip()
        if any(k in msg.lower() for k in ("login", "authenticat", "credential", "401", "oauth")):
            msg = "Not signed in to Claude Code - run `claude` in a terminal and use /login"
        raise RuntimeError(msg)
    text = "".join(chunks) or "".join(final_text)
    if not text.strip():
        raise RuntimeError("Claude returned no text. " + " ".join(err[-2:]))
    return text.strip()


def main() -> None:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    t = sub.add_parser("transcribe")
    t.add_argument("--wav", required=True)
    t.add_argument("--out", required=True)
    t.add_argument("--asr", default="whisper-base.en-fp16-ov")
    t.add_argument("--device", default="NPU")
    t.add_argument("--num-speakers", type=int, default=-1)
    t.add_argument("--speakers-mode", choices=["detect", "off"], default="detect")
    n = sub.add_parser("notes")
    n.add_argument("--transcript", required=True)
    n.add_argument("--vault", required=True)
    n.add_argument("--class", dest="cls", required=True)
    n.add_argument("--model", default="claude-haiku-5-5")
    n.add_argument("--out", required=True)
    n.add_argument("--speakers", default="")
    n.add_argument("--budget", type=int, default=400_000)
    n.add_argument("--extra", action="append", default=[])
    a = ap.parse_args()
    try:
        {"transcribe": cmd_transcribe, "notes": cmd_notes}[a.cmd](a)
    except Exception as e:  # noqa: BLE001
        emit(event="error", message=str(e))
        sys.exit(1)


if __name__ == "__main__":
    main()
