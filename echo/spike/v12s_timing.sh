#!/bin/bash
cd ~/Development/vinoWhisper
S=~/.cache/claude-scratch/aec216-scripts; B=~/.cache/claude-scratch/aec216
R="uv run --with soundfile --with pyyaml --with einops python"
for t in 2 32; do
  $R $S/contention.py $B decode 47 2> $B/dec.err > $B/dec.out &
  until grep -q ready $B/dec.err 2>/dev/null; do sleep 1; done
  T=$t $R $S/v12s_check.py $B timing 2>/dev/null | grep "^{"
  wait; grep "^{" $B/dec.out
done
