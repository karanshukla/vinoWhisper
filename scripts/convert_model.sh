#!/usr/bin/env bash
# Converts openai/whisper-small.en to OpenVINO IR via optimum-intel.
#
# `vinowhisper-setup` runs the equivalent of this for the device it detects;
# this script is the standalone version, for re-exporting without the wizard.
# The flags below and vinowhisper/wizard.py:export_argv() must stay in step.
#
# small.en is a deliberate choice, not just the default: benchmarked against
# base.en and tiny.en on this NPU (2026-08-03), same test clip. base.en (2.6x
# faster) and tiny.en (3.8x faster) both introduced real transcription errors
# (garbled words, dropped/mis-heard terms, tiny.en hallucinated a repeat at
# the end). small.en was the only one with zero observed accuracy loss.
#
# Two variants, because the export is device-specific:
#
#   npu       --disable-stateful, which produces the separate KV-cache
#             "decoder_with_past" submodel WhisperPipeline's NPU static
#             pipeline requires (self_attn_nodes assertion otherwise — see
#             openvinotoolkit/openvino.genai#1728).
#   stateful  the ordinary export, for CPU and GPU. The npu export cannot run
#             on CPU at all: it fails on a beam_idx port error.
#
# Usage:
#   ./scripts/convert_model.sh                      # npu variant (default)
#   ./scripts/convert_model.sh --variant stateful   # cpu/gpu variant
#   ./scripts/convert_model.sh --variant both
#   ./scripts/convert_model.sh --model openai/whisper-base.en --out /tmp/x
#
# Each export is checked against the digests pinned in
# vinowhisper/model_digests.json before you are told it is done. That check
# warns and continues on anything unpinned; see vinowhisper/integrity.py.
set -euo pipefail

MODEL_ID="openai/whisper-small.en"
VARIANT="npu"
OUT_DIR=""
DATA_HOME="${XDG_DATA_HOME:-$HOME/.local/share}"

usage() {
    cat <<'EOF'
Export openai/whisper-small.en to OpenVINO IR.

  --variant npu|stateful|both   npu (default) uses --disable-stateful, which the
                                NPU static pipeline requires; stateful is the
                                CPU/GPU export. They are not interchangeable.
  --model <hf-id>               model to export (default openai/whisper-small.en)
  --out <dir>                   output directory (default: the XDG data dir)
EOF
    exit "${1:-0}"
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --variant) VARIANT="${2:?--variant needs npu|stateful|both}"; shift 2 ;;
        --model)   MODEL_ID="${2:?--model needs a Hugging Face model id}"; shift 2 ;;
        --out)     OUT_DIR="${2:?--out needs a directory}"; shift 2 ;;
        -h|--help) usage 0 ;;
        *) echo "unknown argument: $1" >&2; usage 2 ;;
    esac
done

case "$VARIANT" in
    npu|stateful|both) ;;
    *) echo "--variant must be npu, stateful or both (got '$VARIANT')" >&2; exit 2 ;;
esac

if [[ -n "$OUT_DIR" && "$VARIANT" == "both" ]]; then
    echo "--out cannot be combined with --variant both" >&2
    exit 2
fi

if ! command -v optimum-cli >/dev/null 2>&1; then
    echo "optimum-cli not found. Run 'uv sync' first, or 'uv run $0 $*'." >&2
    exit 1
fi

INTEGRITY_FAILED=0

# Verify what came down against vinowhisper/model_digests.json. Warn-and-
# continue on an export nobody has pinned or a toolchain that has moved on;
# non-zero only when the pinned toolchain produced different bytes, which is
# the case worth stopping for. See vinowhisper/integrity.py.
verify_one() {
    local variant="$1" out="$2"
    if ! python3 -c "import vinowhisper" >/dev/null 2>&1; then
        echo "==> skipping digest check: vinowhisper not importable by $(command -v python3)"
        echo "    (re-run this as 'uv run $0', or check by hand with"
        echo "     python -m vinowhisper.integrity --variant $variant --dir $out)"
        return 0
    fi
    echo "==> verifying digests ($variant)"
    python3 -m vinowhisper.integrity --variant "$variant" --dir "$out" || INTEGRITY_FAILED=1
}

export_one() {
    local variant="$1" out="$2"
    local args=(--model "$MODEL_ID" --task automatic-speech-recognition-with-past)
    [[ "$variant" == "npu" ]] && args+=(--disable-stateful)

    mkdir -p "$(dirname "$out")"
    echo "==> exporting $MODEL_ID ($variant) to $out"
    optimum-cli export openvino "${args[@]}" "$out"
    echo "==> done: $out"
    verify_one "$variant" "$out"
}

for variant in npu stateful; do
    [[ "$VARIANT" == "both" || "$VARIANT" == "$variant" ]] || continue
    if [[ "$variant" == "npu" ]]; then
        export_one npu "${OUT_DIR:-$DATA_HOME/vinowhisper/models/whisper-small.en-ov}"
    else
        export_one stateful "${OUT_DIR:-$DATA_HOME/vinowhisper/models/whisper-small.en-ov-stateful}"
    fi
done

# Deliberately not `set -e`-aborting mid-loop: with --variant both, a digest
# problem on the first export should not hide whether the second one worked.
if [[ "$INTEGRITY_FAILED" -ne 0 ]]; then
    echo "==> the export completed, but its digests did not verify (see above)" >&2
    exit 1
fi
