#!/usr/bin/env bash
# Sets up Study Notes for the current user: venv, models, launcher, desktop entry, icon, D-Bus service.
# System packages (Arch names) must already be installed - see README.md.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BIN="$HOME/.local/bin/study-notes"
MODELS="${STUDYNOTES_MODELS:-$HOME/.local/share/study-notes/models}"
APP_ID="io.github.ajxd2.StudyNotes"

echo "==> Python environment"
python3 -m venv "$ROOT/.venv"
"$ROOT/.venv/bin/python" -m pip install --quiet --upgrade pip
"$ROOT/.venv/bin/python" -m pip install --quiet -r "$ROOT/requirements.txt"

echo "==> Models -> $MODELS"
mkdir -p "$MODELS"
"$ROOT/.venv/bin/python" - "$MODELS" <<'PY'
import sys
from pathlib import Path
from huggingface_hub import snapshot_download
models = Path(sys.argv[1])
for repo in ("OpenVINO/whisper-base.en-fp16-ov", "OpenVINO/whisper-small.en-fp16-ov"):
    snapshot_download(repo, local_dir=models / repo.split("/")[1])
PY
GH=https://github.com/k2-fsa/sherpa-onnx/releases/download
[ -f "$MODELS/sherpa-onnx-pyannote-segmentation-3-0/model.int8.onnx" ] || {
  curl -fsSL "$GH/speaker-segmentation-models/sherpa-onnx-pyannote-segmentation-3-0.tar.bz2" | tar xj -C "$MODELS"; }
[ -f "$MODELS/wespeaker_en_voxceleb_resnet34.onnx" ] || \
  curl -fsSL -o "$MODELS/wespeaker_en_voxceleb_resnet34.onnx" "$GH/speaker-recongition-models/wespeaker_en_voxceleb_resnet34.onnx"
[ -f "$MODELS/silero_vad.onnx" ] || \
  curl -fsSL -o "$MODELS/silero_vad.onnx" "$GH/asr-models/silero_vad.onnx"

echo "==> Desktop integration"
mkdir -p "$HOME/.local/bin" "$HOME/.local/share/applications" "$HOME/.local/share/dbus-1/services" \
         "$HOME/.local/share/icons/hicolor/scalable/apps"
printf '#!/bin/sh\nexec /usr/bin/python3 "%s/studynotes/main.py" "$@"\n' "$ROOT" > "$BIN"
chmod +x "$BIN"
sed "s#@BIN@#$BIN#g" "$ROOT/data/$APP_ID.desktop" > "$HOME/.local/share/applications/$APP_ID.desktop"
sed "s#@BIN@#$BIN#g" "$ROOT/data/$APP_ID.service" > "$HOME/.local/share/dbus-1/services/$APP_ID.service"
cp "$ROOT/data/$APP_ID.svg" "$HOME/.local/share/icons/hicolor/scalable/apps/"
update-desktop-database "$HOME/.local/share/applications" 2>/dev/null || true
gtk-update-icon-cache -q "$HOME/.local/share/icons/hicolor" 2>/dev/null || true
busctl --user call org.freedesktop.DBus /org/freedesktop/DBus org.freedesktop.DBus ReloadConfig >/dev/null 2>&1 || true

echo "Done. Launch 'Study Notes' from the app grid, or run: study-notes"
echo "Then sign in once for notes: run 'claude' and use /login."
