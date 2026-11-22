#!/bin/bash
cd ~/Development/vinoWhisper
S=~/.cache/claude-scratch/aec216-scripts/contention.py; B=~/.cache/claude-scratch/aec216
R="uv run --with soundfile python"
for t in 1 2; do
echo "== threads=$t hops alone"; LVQE_THREADS=$t $R $S $B hops 45 2>/dev/null | grep "^{"
echo "== threads=$t together"
$R $S $B decode 47 2> $B/dec.err > $B/dec.out &
until grep -q ready $B/dec.err 2>/dev/null; do sleep 1; done
LVQE_THREADS=$t $R $S $B hops 45 2>/dev/null | grep "^{"
wait; grep "^{" $B/dec.out
done
