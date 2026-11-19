# CLAUDE.md

Guidance for Claude Code (and any other coding agent) working in this repository. Read it fully before
changing anything: several behaviours look arbitrary but exist because of real failures, listed under
**Hard-won gotchas**.

## What this project is

**Study Notes** is a native GNOME (GTK4 + libadwaita) desktop app. It turns classes into notes:

1. **Record** from the microphone, or **import** existing audio/video files (single files, whole folders,
   drag-and-drop, "Open With").
2. **Transcribe locally** with OpenVINO GenAI Whisper, on the Intel **NPU** (falls back to GPU, then CPU).
3. **Detect speech and speakers** locally with sherpa-onnx (pyannote segmentation + wespeaker embeddings, or Silero
   VAD for a fast mode without speaker labels).
4. **Write the note with Claude** through the **Claude Agent SDK using the user's Claude Code login** (their
   subscription - there is deliberately **no API key** anywhere), giving Claude the user's Obsidian vault as
   context and their own lecture template/style.
5. **Save Markdown** into the user's Obsidian vault, in the vault's own layout and conventions.

Primary target: Arch-based Linux (developed on CachyOS), GNOME 51, libadwaita 1.10, Intel Core Ultra (Panther
Lake) with an NPU. Python 3.14. The author uses Obsidian, so the note format follows his vault conventions
(see **Vault conventions**).

## Commands

```bash
scripts/install.sh                      # venv + models + launcher + desktop entry + icon + D-Bus service
study-notes                             # run the app (launcher written by install.sh)
python3 studynotes/main.py              # same, straight from the tree (system Python: needs PyGObject)

python3 -m unittest discover -s tests -p 'test_common.py' -v        # UI-side logic (system Python)
.venv/bin/python -m unittest discover -s tests -p 'test_worker.py' -v   # worker logic (venv)
python3 -m py_compile studynotes/*.py   # cheap syntax check after edits

# Worker CLI (venv python), useful for debugging without the UI. Emits JSON lines on stdout.
.venv/bin/python studynotes/worker.py transcribe --wav in.wav --out out.json [--asr whisper-small.en-fp16-ov]
        [--device NPU|GPU|CPU] [--num-speakers N] [--speakers-mode detect|off]
.venv/bin/python studynotes/worker.py notes --transcript out.json --vault DIR --class COURSE --model claude-haiku-5-5
        --out note.md [--speakers '{"1":"Ms Lee"}'] [--budget 400000] [--extra OLDER_VAULT_DIR ...]

gapplication action io.github.ajxd2.StudyNotes record     # app actions work over D-Bus
```

There is no build step and no linter configuration. Match the surrounding style (see **Code style**).

## Architecture

Two processes, on purpose:

```
┌──────────────────────────── system Python (PyGObject) ───────────────────────────┐
│ main.py      App (Adw.Application): actions, accelerators, D-Bus activation, open  │
│ window.py    MainWindow: sidebar list, Markdown editor, note actions, RegenDialog   │
│ record.py    RecordDialog: GStreamer capture, waveform, pause, speaker naming       │
│ importer.py  ImportDialog: batch queue, ffmpeg convert, ETA, duplicate detection    │
│ prefs.py     Preferences (Adw.PreferencesDialog)                                    │
│ md.py        MarkdownStyler: live Markdown styling via Gtk.TextTags, link hit-test  │
│ common.py    config, vault layout helpers, note format, Worker/Job plumbing         │
└───────────────┬───────────────────────────────────────────────────────────────────┘
                │ spawns, JSON lines over stdout (stdin unused)         nice +10
┌───────────────▼────────────── project .venv (Python 3.14) ───────────────────────┐
│ worker.py    transcribe / notes commands: OpenVINO GenAI, sherpa-onnx,            │
│              claude-agent-sdk. No GTK. No import of common.py.                    │
└───────────────────────────────────────────────────────────────────────────────────┘
```

Why two interpreters: PyGObject/GTK come from the distro and must run on system Python; OpenVINO,
sherpa-onnx and the Agent SDK are pip wheels (cp314) that we do not want in system site-packages, and a crash
or 1.4 GB memory peak in the AI stack must not take the UI down. **`worker.py` must never import `gi` or
`common.py`; `common.py`/UI files must never import openvino/sherpa/anthropic.** `tests/` mirrors this split.

### File map

| File | Responsibility |
|---|---|
| `studynotes/main.py` | `App`. Actions: `record`, `import`, `new-class`, `preferences`, `shortcuts`, `about`, `open-vault`, `show-vault`, `quit`. `HANDLES_OPEN` (files -> import dialog). CSS. Guards that the venv exists. |
| `studynotes/window.py` | `MainWindow`: `Adw.NavigationSplitView` (collapses under 680sp). Sidebar grouped by course, search (cached text), debounced atomic autosave, picks up external edits (Obsidian) when the buffer is clean. Note actions: rename, rename speakers, regenerate (`RegenDialog`), copy, open in Obsidian, show in Files, trash with Undo (moves into the vault's `.trash`). Ctrl+click / Ctrl+Enter follows `[[wikilinks]]`. Drag-and-drop of audio opens the importer. |
| `studynotes/record.py` | `RecordDialog` state machine: record -> working -> speakers -> (notes) -> save. `Waveform` (Cairo bars from the GStreamer `level` element). Suspend/idle inhibit while recording and processing. `Job` does the work. |
| `studynotes/importer.py` | `ImportDialog`: add files/folders (recursive), ffprobe durations for a time estimate, per-file status rows, sequential queue, skips already-imported files (`source:` frontmatter key), stop-after-current, completion notification. Big batches (>10) default AI and speaker detection **off**. |
| `studynotes/prefs.py` | Vault folder, extra (read-only) context folders, model, context budget, Whisper model, device, speaker count, detect-speakers, keep-recordings. Shows Claude Code login state. |
| `studynotes/md.py` | `MarkdownStyler` over a plain `Gtk.TextView` (no GtkSourceView): regex pass after a 120 ms debounce applies tags for frontmatter, headings, bold/italic/code, quotes, bullets, wikilinks, speaker lines. Colours come from the theme + accent. `link_at(x, y)` for hit-testing. |
| `studynotes/common.py` | Config (`~/.config/study-notes/config.json`), vault layout (`classes_root`, `vault_classes`, `notes_subdir`, `vault_notes`, `course_of`), dates and filenames, note build/split/parse, `rename_speakers_text`, `find_note_by_link`, `Worker`, `convert_to_wav`, `Job`. |
| `studynotes/worker.py` | Everything that needs OpenVINO/sherpa/Agent SDK. See **Worker protocol**. |
| `data/` | Desktop entry, D-Bus service and icon templates (`@BIN@` is substituted by `install.sh`). |
| `scripts/install.sh` | Idempotent setup for one user (non-Flatpak). |
| `io.github.ajxd2.StudyNotes.yml` | Flatpak manifest (GNOME 51 runtime). Bundles Level Zero loader, Intel NPU driver + compiler, ffmpeg, pip deps, models. |
| `data/*.metainfo.xml` | AppStream metadata (validate with `appstreamcli validate --no-net`). |
| `tests/` | `unittest`; pure-logic tests only (no GTK display, no network, no models). |

## Data flow (one recording or import)

1. **Audio -> WAV.** Recording writes 16 kHz mono S16LE WAV directly (`pipewiresrc ! audioconvert ! level !
   audioresample ! caps ! wavenc ! filesink`; stop = send EOS so `wavenc` finalises the header). Imports run
   `ffmpeg -vn -ar 16000 -ac 1 -sample_fmt s16` into `~/.cache/study-notes/tmp` (never touching the original).
2. **`worker.py transcribe`** (`Job.transcribe`):
   a. Speech detection first: `diarize_audio` + `prune_speakers` (detect mode) or `vad_turns` (off mode).
   b. `split_points` cuts the audio into windows of at most 28 s at the quietest point in the last 10 s.
   c. Windows with under 0.3 s of detected speech are skipped (Whisper hallucinates on silence/noise).
   d. Each remaining window goes to `run_window`: a **fresh** `WhisperPipeline` per window; on NPU failure retry
      in halves, then CPU.
   e. Segments with no detected speech underneath are dropped; speakers assigned by overlap
      (`assign_speakers`, renumbered by first appearance); result JSON written (`segments`, `speakers`,
      `duration`, `device`, `skipped`).
3. **Speaker naming** (record flow only): the user names `Speaker N` labels; same name twice merges them.
   Imports keep `Speaker N` and can be renamed later (note menu -> Rename Speakers).
4. **`worker.py notes`** (`Job.notes`): gathers vault context (below), reads `Templates/Lecture Note.md`, builds
   one prompt, streams text deltas back as `text` events.
5. **`Job.save`**: `build_note` -> `School/<COURSE>/Lectures/M-D-YY[ - topic].md`; temp WAV/JSON deleted unless
   "Keep recordings" is on. If notes generation fails during an import, the transcript note is still saved.

## Worker protocol

`worker.py` prints one JSON object per line on stdout; `common.Worker` reads them on a thread and delivers
them to the GTK main loop with `GLib.idle_add`. stderr is captured (last 8 lines kept for error messages).

| `event` | Fields |
|---|---|
| `progress` | `stage` (`loading`/`transcribing`/`diarizing`/`notes`), `detail`, optional `percent` |
| `text` | `delta` - streamed note text |
| `result` | transcribe: `out`, `duration`, `device`, `speakers`, `skipped{windows,segments}`; notes: `body`, `stop_reason` |
| `error` | `message` - already human-readable |

Exactly one `result` or `error` ends a run. If the process dies without either, `Worker` synthesises an
`error`. `Worker.cancel()` terminates the process and suppresses further events. The worker runs at `nice +10`.

## Claude integration (read before touching `run_agent`)

- Uses `claude_agent_sdk.query()` which spawns the **Claude Code CLI** and so uses the user's existing Claude
  Code login. `Job.notes` passes `ANTHROPIC_API_KEY=""` (which `Worker` turns into *unset*) so a stray API key can
  never override the subscription. Do **not** add API-key handling or the `anthropic` package.
- It is a plain one-shot text generation, so everything else is disabled: `tools=[]`, `allowed_tools=[]`,
  `max_turns=1`, `setting_sources=[]` (no CLAUDE.md/memory/hooks from the user's machine), `strict_mcp_config=True`,
  `skills=[]`, `plugins=[]`, `permission_mode="dontAsk"`, and a throwaway `cwd`.
- **`extra_args={"no-session-persistence": None}`** is essential. Without it every run saves a session file under
  `~/.claude/projects/` containing the entire vault context and transcript.
- The big prompt (vault + transcript, up to ~400k+ chars) goes over **stdin** (the SDK always uses
  `--input-format stream-json`); only the short system prompt is on argv. Do not move vault text into the system
  prompt: a single argv entry is capped at ~128 KB.
- Models: `claude-haiku-5-5` (default) and `claude-sonnet-5-5`. Add models in `common.MODEL_CHOICES`.
- Auth/usage failures are mapped to actionable messages in `run_agent` ("run `claude` and use /login").
- The subscription route is for the user's own use. Anyone redistributing the app must review Anthropic's
  Agent SDK authentication terms; the README says so.

### Prompt design (`SYSTEM` in worker.py, `cmd_notes`)

- The notes must read like the **student's own notes** (terse bullets, bold key terms, `$...$`/`$$...$$` math,
  engineering notation) and follow their `Templates/Lecture Note.md` when present: `## Topics`, `## Notes`,
  `## Questions`, sections separated by `---`, homework/deadlines first under Questions.
- The model outputs **only the body starting at `## Topics`**; the app writes frontmatter, the
  `# Lecture Notes - M/D/YY[: topic]` heading and the transcript callout itself.
- Wikilinks only to notes that exist in the vault; fix speech-to-text mangling of technical terms using the
  vault; mark real uncertainty with `(?)`; never invent facts.
- Prompt parts, in order: `<lecture_template>`, `<vault>`, then the class/date/`<transcript>`.

## Vault conventions (the contract with Obsidian)

Layout detection (`common.classes_root`): if `<vault>/School` exists, courses are its subfolders, else the vault's
top-level folders. A course's notes go in its `Lectures/` (else `Notes/`) folder; `Lectures/` is created if
neither exists (`notes_subdir`). Folders named in `IGNORED_DIRS` (`Templates`, `Attachments`, `TMP`,
`node_modules`), dot-folders, and files named `CLAUDE.md`/`AGENTS.md` are never listed in the sidebar and never
sent to Claude as context. (`worker.py` duplicates these constants because it cannot import `common`; keep both in
sync.)

Filename: `M-D-YY.md` (e.g. `1-29-26.md`), with ` - topic` when one is given; collisions get ` 2`, ` 3`
(`unique_path`). Sidebar dates come from frontmatter `date:`, else the `M-D-YY` / `YYYY-MM-DD` filename, else mtime.

Frontmatter written by the app:

```yaml
---
course: EET-145
type: lecture
date: 2026-01-29
duration: 41:09
speakers: [Speaker 1, Speaker 2]
model: claude-haiku-5-5        # "none" for transcript-only notes
source: lecture.mp3 (16.6 MB)  # imports only; used for duplicate detection
tags: [study-notes]
---
```

Body: `# Lecture Notes - 1/29/26[: topic]`, `---`, the Claude body, then the transcript as a collapsed Obsidian
callout (`> [!note]- Transcript`, speaker lines `> **Name** [m:ss]`). `common.split_note` and `parse_transcript`
rebuild structure from that exact format - **if you change `build_note`, update both and the round-trip test**.
Regenerate Notes depends on this: it re-parses the transcript from the note, re-runs Claude, and swaps only the
body.

Context gathering (`worker.gather_vault`): same-course notes first, then the rest of the vault, then
read-only **extra context** folders (labelled `older/<folder>/...`), newest first within each tier, until the
character budget (Preferences: 0 / 100k / 400k / 1M chars) is used. Old transcript callouts are stripped
(`TRANSCRIPT_CALLOUT`). The note being regenerated is excluded. Notes are only ever **written** to the main vault.

## Configuration (`~/.config/study-notes/config.json`)

| Key | Default | Meaning |
|---|---|---|
| `vault` | `~/Documents/Study Notes` | Main vault (read + write) |
| `extra_context` | `[]` | Read-only folders added to Claude's context |
| `model` | `claude-haiku-5-5` | Notes model |
| `budget` | `400000` | Max characters of vault context |
| `asr` | `whisper-base.en-fp16-ov` | Folder name under the models dir |
| `device` | `NPU` | `NPU` / `GPU` / `CPU` (falls back NPU -> GPU -> CPU) |
| `detect_speakers` | `true` | Off = Silero VAD only (~4x faster, all lines `Speaker 1`) |
| `num_speakers` | `0` | 0 = auto |
| `keep_audio` | `false` | Otherwise temp WAV/JSON deleted after the note is saved |
| `win_w`, `win_h`, `last_note` | | Restored on launch |

Other paths: models `~/.local/share/study-notes/models` (override `STUDYNOTES_MODELS`), compile cache
`~/.cache/study-notes/ov`, temp `~/.cache/study-notes/tmp`, recordings `~/.local/share/study-notes/recordings`.

## Hard-won gotchas (each cost real debugging time)

**Whisper on the NPU**
- A `WhisperPipeline` **reused for a second `generate()` call degrades** and silently drops most of the text.
  Create a fresh pipeline per window (cheap with `CACHE_DIR`: ~0.3 s). Never cache/reuse the pipeline.
- The pipeline's own long-form handling also **drops text** (a 66 s clip returned 2 of 6 repeats on NPU, all 6 on
  CPU). Always chunk to <= 28 s yourself (`split_points`).
- Dense speech can raise **`KV-Cache is full`** from the NPU decoder, which used to abort a whole 41-minute
  import. `run_window` retries in halves (split at a quiet point), then on CPU. Never let one window kill a file.
- English-only models (`*.en`) reject the `language=` argument ("Cannot specify 'language' for not multilingual
  model"). Multilingual support (e.g. a Spanish class) is not implemented yet.
- `audio.tolist()` on a whole lecture is gigabytes of Python floats: only convert per window.

**Silence, noise and hallucination**
- Whisper invents text ("Thank you.") on silence/room noise. A **level threshold is the wrong fix**: measured room
  noise (~-37 dBFS) is louder than quiet real speech (~-48 dBFS). Gate on **speech detection** instead (windows
  with < 0.3 s of speech skipped; segments without speech under them dropped) and report counts in `skipped`.
- Don't test "noise" with a real microphone recording - it may contain someone talking (it did). Use synthetic
  pink noise (`ffmpeg -f lavfi -i anoisesrc=...`) and **delete** any mic captures after use (private audio).

**Speaker detection**
- pyannote + wespeaker on CPU is the slow step (~0.2x realtime, super-linear on long files); 4 threads measured
  fastest (6 is slower). Silero VAD (`detect_speakers=false`) does 41 min in ~7 s and makes a whole import ~4.5x
  faster.
- Clustering threshold **0.4**; 0.3 (tuned on a Chinese clip) split one English lecturer into 11 speakers. Then
  `prune_speakers` folds clusters under 5 % of speech *and* under 20 s into the nearest real speaker.
- The embedding model is English (voxceleb); other languages cluster worse.

**GTK / libadwaita / GNOME**
- Use `Gtk.ListBox.remove_all()` to clear lists. A `get_first_child()` loop can spin forever or hit non-row
  children (the list has a placeholder child); when iterating children, filter by your own attribute.
- `Adw.ClampScrollable` (not `Adw.Clamp`) is the right wrapper for a `TextView` inside a `ScrolledWindow`.
- Don't rebuild the sidebar on every focus change: it resets scroll and re-stats every note. `refresh_notes`
  compares a (path, mtime) signature first. `_refreshing` guards `row-selected`; load explicitly via `_open_row`.
- `GLib.set_prgname(APP_ID)` + matching `StartupWMClass` makes the dock match the window. `DBusActivatable=true` needs
  the `.service` file **and** a D-Bus config reload (`busctl --user call org.freedesktop.DBus /org/freedesktop/DBus
  org.freedesktop.DBus ReloadConfig`) after first install, or "name is not activatable".
- The suspend inhibit (`Gtk.Application.inhibit`) is held from record start until the speaker page / error / close,
  and during imports; closing the lid still suspends (documented in the UI).
- GNOME blocks programmatic screenshots, but you can **render a widget tree to PNG yourself**:
  `Gtk.WidgetPaintable.new(win)` -> `Gtk.Snapshot()` -> `paintable.snapshot(s, w, h)` -> `s.to_node()` ->
  `win.get_native().get_renderer().render_texture(node, None).save_to_png(path)`. Do it after a short
  `GLib.timeout_add` so the frame has drawn. This is how `docs/screenshots/` were made (from a throwaway demo vault -
  never the user's real vault, and nothing with personal data on screen).
- Tag/CSS colours read from `get_color()` are **stale inside `notify::dark`** (the new theme isn't resolved yet);
  recompute after a short delay (`MarkdownStyler.restyle_soon`). Check both light and dark when touching styling.

**Process / environment**
- **Tests that start the app collide with a running copy**: the app owns the D-Bus name, so a second instance
  just activates the first and never runs your `do_activate`. In tests set `main.APP_ID = "io.github.ajxd2.StudyNotesTest"`
  before constructing `App()`.
- Running Python from stdin with the venv interpreter prints a harmless `multiprocessing forkserver ...
  FileNotFoundError: <stdin>` traceback (Python 3.14 + a library using multiprocessing). Ignore it, or run a file.
- `pkill -f 'pattern'` matches your own shell command line if the pattern appears in it. Use `pkill -f '[p]attern'`.
- zsh aborts a whole command on an unmatched glob; use `find ... -delete` for cleanup.
- Large prompts: see **Claude integration** (stdin, not argv). Session persistence: always off.
- Obsidian opens a file by `obsidian://open?path=<urlencoded absolute path>` **only if** that file is inside a vault
  Obsidian already knows about.

## Flatpak

Build/install: `flatpak-builder --user --install --force-clean build-dir io.github.ajxd2.StudyNotes.yml`
(first build ~10 min: ffmpeg compile + ~1 GB of downloads; later builds reuse `.flatpak-builder/` stages).
Run: `flatpak run io.github.ajxd2.StudyNotes`; stop: `flatpak kill io.github.ajxd2.StudyNotes`.

How the sandbox changes the app (all handled in code, keep it working on **both** a normal install and Flatpak):
- `common.IN_FLATPAK` (`/.flatpak-info` exists). One Python for UI and worker (`worker_python()` returns the
  runtime's `sys.executable`; the venv is only used outside Flatpak). Deps are pip-installed to `/app`.
- Paths come from GLib XDG dirs (`common.CONFIG`, `DATA_DIR`, `CACHE_DIR`), which equal `~/.config` etc. on a normal
  install and `~/.var/app/<id>/...` in the sandbox. On first run in Flatpak the host config is copied over.
  `Worker` hands `STUDYNOTES_MODELS`, `STUDYNOTES_BUNDLED_MODELS`, `STUDYNOTES_CACHE` to `worker.py`, which cannot import GLib.
- Models: `worker.find_model` looks in the user models dir, then `/app/share/study-notes/models` (shipped:
  whisper-base.en + the sherpa models). Any other Whisper model is downloaded from Hugging Face on first use.
- Microphone uses `pulsesrc` in the sandbox (PulseAudio socket) and `pipewiresrc` on the host.
- Permissions are `--filesystem=home` (vaults and `~/.claude` can be anywhere), `--device=all` (`/dev/accel` NPU),
  `--socket=pulseaudio`, `--share=network`. The sandbox has a **private `/tmp`**: put test files under `$HOME`.
- The Intel **GPU** compute runtime is not bundled (OpenVINO lists only CPU and NPU in the sandbox).

Build gotchas:
- **`build-options: strip: false` and `no-debuginfo: true` are essential.** The Agent SDK bundles the Claude CLI as a
  self-contained executable (payload appended to the ELF). flatpak-builder's strip/debuglink step corrupts it, and it then
  runs as plain Bun (`--version` prints `1.4.4`; the SDK fails with `Script not found "stream-json"`). The correct
  CLI prints `2.1.x (Claude Code)`.
- The NPU stack must be bundled: Level Zero loader (built from source), and `libze_intel_npu` +
  `libopenvino_intel_npu_compiler*` extracted from Intel's official `linux-npu-driver` release debs (match the host
  kernel/firmware generation). The pip OpenVINO wheel does not contain them.
- `DBusActivatable=true` requires `/app/share/dbus-1/services/<id>.service` or the export fails.
- Each `build-commands` entry has its own `/tmp`; write intermediates in the build dir.
- pip and Hugging Face steps need `--share=network` at build time, so this is not Flathub-ready (that needs offline,
  checksummed sources, e.g. via flatpak-pip-generator).
- A host `~/.local/share/applications/<id>.desktop` or `dbus-1/services/<id>.service` **shadows** the Flatpak's export
  (the host entry wins). Remove them when switching to the Flatpak.
- To test the GUI in the sandbox, run a script with `flatpak run --command=python3 io.github.ajxd2.StudyNotes script.py`
  and set `main.APP_ID = "io.github.ajxd2.StudyNotes.Test"` (a sandbox may own `<app-id>.*` names). `pgrep -f studynotes/main.py`
  on the host also matches the sandboxed process.

## Measured performance (Intel Core Ultra 5 325, NPU, plugged in)

| Task | Time |
|---|---|
| Whisper base.en on NPU | ~26x realtime (86 s for 37 min of audio) |
| Speaker detection (pyannote + wespeaker, 4 threads) | ~0.14-0.22x realtime (4-8 min for a 41-min lecture) |
| Silero VAD | ~340x realtime (7 s for 41 min) |
| Whole 41-min lecture, speakers on | ~9 min (+ ~10 s Claude) |
| Whole 41-min lecture, speakers off | ~2 min |
| Peak memory (37-min file) | ~1.4 GB |
| 100+ fresh NPU pipelines in one run | no failures, no leak |

The importer's ETA uses 0.22x (speakers) / 0.05x (off) plus 25 s (notes) or 5 s per file. Recalibrate if the
pipeline changes.

## Testing guidelines

- Run both unit suites before committing. They cover vault layout, note round-trip, speaker renaming, link
  resolution, chunking, pruning, speaker assignment and context gathering. Add a test for any new pure function.
- GUI behaviour is verified by driving the real widgets from a throwaway script (see the gotcha about
  `APP_ID`): construct `App`, call dialog methods directly, poll with `GLib.timeout_add`, and assert on files.
  Always use a **scratch vault** (point `common.CONFIG` at a temp config) - never the user's real vault - and never
  delete or modify the audio you import.
- Real-speech test material: the MIT 6.002 *Circuits and Electronics* (2003) lectures on archive.org item
  `MIT6.002S07` (CC BY-NC-SA 3.0; ~22 MB `.ogv` each, `ffmpeg -vn` to extract). Synthetic clips (repeated/pitch-shifted
  speech) hide real failures such as the KV-cache error and the speaker over-split; test long real audio before
  claiming a pipeline change works. Do not commit audio or models (`.gitignore` blocks them).
- A change to `worker.py` that affects transcription must be re-verified on: a short clip, a 60+ s clip with
  repeated content (text-drop regression), speech surrounded by silence (hallucination gate), and a long lecture.

## Safety and privacy rules

- **Never modify or delete a user's original recordings.** Imports read them with ffmpeg; only temp copies are removed.
- **Never write to the user's real vault in tests or experiments.** Use a scratch copy. The only sanctioned
  writes are the app's own note creation.
- The user's vault content is sent to Anthropic when notes are generated. Keep context filtering (ignored
  dirs, agent files, transcript stripping) intact, and keep the Preferences text that says so. Extra context folders
  are read-only.
- No secrets in the repo. There is no API key handling by design. Don't log note/transcript contents.
- Audio of other people (classmates, teachers) is private: don't keep test captures; the app deletes working audio
  after saving unless "Keep recordings" is on.

## Code style and UX principles

- Python 3.14, `from __future__ import annotations`, 4 spaces, lines up to ~110 chars, type hints on signatures,
  f-strings. Comments explain **why** (especially anything tied to a gotcha above), not what.
- Keep modules in their lane (see **Architecture**). UI code stays thin; put logic in `common.py` (UI side) or
  `worker.py` (AI side) as pure, testable functions.
- UI follows the GNOME HIG with libadwaita widgets: `Adw.Dialog`/`AlertDialog`, `ToolbarView`, `StatusPage`
  empty states, `PreferencesGroup`/rows, toasts with Undo for destructive actions, sensible defaults.
- Product bar ("Apple mindset"): it should just work with zero setup beyond signing in; no dead ends (every error
  page says what to do or offers Try Again); destructive actions are undoable (trash moves into the vault's
  `.trash`); long work shows real progress and an honest time estimate; never lose the user's data (a failed notes
  step still saves the transcript; saves are atomic; originals untouched); don't surprise with cost (large batches
  default to transcript-only).
- Don't add dependencies lightly. The UI side has none beyond the distro; the worker's are pinned loosely in
  `requirements.txt`.

## Git and commits

- Commit messages: short imperative subject (<= 72 chars), optional body explaining why. **Do not add
  `Co-Authored-By` trailers, "Generated with" lines, or any mention of Claude/AI in commit messages or PR
  descriptions** - the owner wants the history clean. Author identity is the GitHub noreply address configured
  in the repo-local git config; do not change it or add a personal email.
- Keep commits focused. Don't commit `.venv/`, models, audio, caches or scratch files.

## Known gaps / roadmap

Highest value first: multilingual Whisper + per-class language (a Spanish class exists in the author's vault);
Whisper initial-prompt glossary built from the vault to fix technical terms and proper names; keep audio and make
transcript timestamps click-to-play in an in-app player; background processing instead of modal dialogs;
slides/PDF/whiteboard-photo attachments as extra context; course `Index.md` updates and links to the previous
lecture; persistent speaker voice profiles; a "what will be sent" preview with token estimate and per-folder exclude
list; `studynotes://` deep links; a log file and crash reporting; live transcription during class; Publishing to Flathub (offline sources, narrower filesystem access).
Diarization settings are tuned on a single real English lecture - revisit with more real multi-speaker audio.
