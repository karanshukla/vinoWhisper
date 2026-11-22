"""A 250 s two-lane 'call': continuous mic + sink-monitor capture while a far-end
track plays. Beeps at 25/80/140/195 s: read passage C (over far end), D (far end
paused), E (over far end), F (far end paused).  usage: session.py BENCH_DIR"""
import json, os, subprocess, sys, threading, time
from pathlib import Path
import numpy as np, soundfile as sf
sys.path.insert(0, os.path.expanduser("~/Development/vinoWhisper")); sys.path.insert(0, os.path.dirname(__file__))
from vinowhisper import capture  # noqa
from record import reader  # noqa

B = Path(sys.argv[1]); OUT = B / "session"; OUT.mkdir(exist_ok=True); FS = 16000; TOTAL = 250
READS = [(25, "C", True), (80, "D", False), (140, "E", True), (195, "F", False)]  # (start s, passage, far end keeps talking)
READ_S = 25

def tone(n):
    t = np.arange(int(0.15 * FS)) / FS
    b = 0.3 * np.sin(2 * np.pi * 880 * t) * np.hanning(len(t))
    gap = np.zeros(int(0.12 * FS))
    return np.concatenate([np.concatenate([b, gap]) for _ in range(n)]).astype(np.float32)

def far_track():
    utts = []
    for p in sorted((B / "clips").glob("*/ref.wav")):
        x, _ = sf.read(p, dtype="float32"); nz = np.flatnonzero(np.abs(x) > 1e-4)
        if len(nz):
            utts.append(x[nz[0]:nz[-1]])
    track = np.zeros(TOTAL * FS, np.float32); pos = 0; k = 0
    quiet = [(int(s * FS), int((s + READ_S) * FS)) for s, _, talk in READS if not talk]
    while pos < len(track) - FS:
        if any(a <= pos < b for a, b in quiet):
            pos = next(b for a, b in quiet if a <= pos < b); continue
        u = utts[k % len(utts)]; k += 1
        end = min(pos + len(u), len(track))
        for a, b in quiet:  # stop before a quiet turn
            if pos < a < end:
                end = a
        track[pos:end] = u[: end - pos]; pos = end + int(0.4 * FS)
    beep = tone(1)
    for s, _, _ in READS:
        i = int((s - 0.6) * FS); track[i:i + len(beep)] += beep
    return track

if not (OUT / "far.wav").exists():
    sf.write(OUT / "far.wav", far_track(), FS, subtype="FLOAT")
procs, bufs, firsts = {}, {}, {}
for lane, argv in (("mic", capture.record_argv("input", None)), ("mon", capture.record_argv("output", None))):
    p = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    procs[lane], bufs[lane], firsts[lane] = p, [], [None]
    threading.Thread(target=reader, args=(p, bufs[lane], firsts[lane]), daemon=True).start()
time.sleep(0.5)
t_play = time.monotonic()
print("playing", flush=True)
subprocess.run(["pw-play", str(OUT / "far.wav")], check=True)
time.sleep(1.0)
for p in procs.values(): p.terminate()
for p in procs.values(): p.wait()
time.sleep(0.1)
for lane in procs:
    raw = b"".join(bufs[lane]); np.save(OUT / f"{lane}.npy", np.frombuffer(raw[: len(raw) // 4 * 4], "<f4"))
json.dump(dict(skew=firsts["mic"][0] - firsts["mon"][0], mon_first=firsts["mon"][0], play=t_play, reads=READS, read_s=READ_S),
          open(OUT / "meta.json", "w"))
print("done", flush=True)
