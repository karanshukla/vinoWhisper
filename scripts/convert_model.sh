#!/usr/bin/env bash
# Converts openai/whisper-small.en to OpenVINO IR via optimum-intel.
# Swap MODEL_ID for base.en / a turbo variant if small.en's accuracy disappoints.
set -euo pipefail

MODEL_ID="${1:-openai/whisper-small.en}"
OUT_DIR="${2:-$HOME/.local/share/vinowhisper/models/whisper-small.en-ov}"

mkdir -p "$(dirname "$OUT_DIR")"

optimum-cli export openvino \
  --model "$MODEL_ID" \
  --task automatic-speech-recognition-with-past \
  "$OUT_DIR"

echo "Converted $MODEL_ID -> $OUT_DIR"
