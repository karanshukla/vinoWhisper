#!/usr/bin/env bash
# Converts openai/whisper-small.en to OpenVINO IR via optimum-intel.
#
# The standalone version of the wizard's export; keep the flags in step with
# vinowhisper/wizard.py:export_argv(). Why two variants: docs/hardware.md
#
# Usage:
#   ./scripts/convert_model.sh                      # npu variant (default)
#   ./scripts/convert_model.sh --variant stateful   # cpu/gpu variant
#   ./scripts/convert_model.sh --variant both
#   ./scripts/convert_model.sh --model openai/whisper-base.en --out /tmp/x
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
    echo "optimum-cli not found. It is the optional export extra: run 'uv sync --extra export' first." >&2
    exit 1
fi

INTEGRITY_FAILED=0

# Fails only when the pinned toolchain produced different bytes (docs/install.md)
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

# Checked after the loop so one bad variant does not hide the other
if [[ "$INTEGRITY_FAILED" -ne 0 ]]; then
    echo "==> the export completed, but its digests did not verify (see above)" >&2
    exit 1
fi
