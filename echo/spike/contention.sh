#!/bin/bash
# alone, alone, then together (decoder warmed before hops start)
cd ~/Development/vinoWhisper
S=~/.cache/claude-scratch/aec216-scripts/contention.py; B=~/.cache/claude-scratch/aec216
R="uv run --with soundfile python"
echo "== hops alone";   $R $S $B hops 60 2>/dev/null | grep "^{"
echo "== decode alone: skipped (measured)"
echo "== together"
$R $S $B decode 62 2> $B/dec.err > $B/dec.out &
until grep -q ready $B/dec.err 2>/dev/null; do sleep 1; done
$R $S $B hops 60 2>/dev/null | grep "^{"
wait; tail -1 $B/dec.out
