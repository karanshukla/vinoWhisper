#!/bin/bash
cd ~/Development/vinoWhisper
S=~/.cache/claude-scratch/aec216-scripts; B=~/.cache/claude-scratch/aec216
R="uv run --with soundfile --with gguf python"
echo "== alone"; OMP_NUM_THREADS=1 $R $S/combined.py $B 45 2>/dev/null | grep "^{"
echo "== next to NPU Whisper"
$R $S/contention.py $B decode 47 2> $B/dec.err > $B/dec.out &
until grep -q ready $B/dec.err 2>/dev/null; do sleep 1; done
OMP_NUM_THREADS=1 $R $S/combined.py $B 45 2>/dev/null | grep "^{"
wait; grep "^{" $B/dec.out
