#!/usr/bin/env bash
# Converts openai/whisper-small.en to OpenVINO IR via optimum-intel.
#
# small.en is a deliberate choice, not just the default: benchmarked against
# base.en and tiny.en on this NPU (2026-08-03), same test clip. base.en (2.6x
# faster) and tiny.en (3.8x faster) both introduced real transcription errors
# (garbled words, dropped/mis-heard terms, tiny.en hallucinated a repeat at
# the end). small.en was the only one with zero observed accuracy loss.
#
# --disable-stateful is required for NPU: WhisperPipeline's NPU static
# pipeline needs the separate KV-cache "decoder_with_past" submodel that the
# default stateful export doesn't produce (self_attn_nodes assertion
# otherwise — see openvinotoolkit/openvino.genai#1728).
set -euo pipefail

MODEL_ID="${1:-openai/whisper-small.en}"
OUT_DIR="${2:-$HOME/.local/share/vinowhisper/models/whisper-small.en-ov}"

mkdir -p "$(dirname "$OUT_DIR")"

optimum-cli export openvino \
  --model "$MODEL_ID" \
  --task automatic-speech-recognition-with-past \
  --disable-stateful \
  "$OUT_DIR"

echo "Converted $MODEL_ID -> $OUT_DIR"
