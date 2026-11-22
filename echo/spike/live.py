"""Live double talk: four 28 s takes. Beep = start reading, two beeps = stop.
take order: A quiet, A with playback, B quiet, B with playback.
usage: live.py BENCH_DIR
"""
import json, os, subprocess, sys, threading, time
from pathlib import Path
import numpy as np, soundfile as sf
sys.path.insert(0, os.path.expanduser("~/Development/vinoWhisper"))
sys.path.insert(0, os.path.dirname(__file__))
from vinowhisper import capture  # noqa
from record import reader  # noqa

B = Path(sys.argv[1]); OUT = B / "live"; OUT.mkdir(exist_ok=True); FS = 16000; TAKE = 28.0

def tone(n):
    t = np.arange(int(0.15 * FS)) / FS
    b = 0.3 * np.sin(2 * np.pi * 880 * t) * np.hanning(len(t))
    gap = np.zeros(int(0.12 * FS))
    return np.concatenate([np.concatenate([b, gap]) for _ in range(n)]).astype(np.float32)

def far_end(seed):
    refs = sorted((B / "clips").glob("fe_*/ref.wav"))
    parts = []
    for p in refs[seed::2]:
        x, _ = sf.read(p, dtype="float32"); nz = np.flatnonzero(np.abs(x) > 1e-4)
        parts += [x[nz[0]:nz[-1]], np.zeros(int(0.3 * FS), np.float32)]
    return np.concatenate(parts)[: int(TAKE * FS)]

def wav(path, x):
    sf.write(path, x, FS, subtype="FLOAT"); return path

beep1, beep2 = wav(OUT / "beep1.wav", tone(1)), wav(OUT / "beep2.wav", tone(2))
silence = wav(OUT / "silence.wav", np.zeros(int(TAKE * FS), np.float32))
takes = [("A_quiet", silence), ("A_echo", wav(OUT / "far0.wav", far_end(0))),
         ("B_quiet", silence), ("B_echo", wav(OUT / "far1.wav", far_end(1)))]
for name, play in takes:
    if (OUT / f"{name}_mic.npy").exists():
        continue
    subprocess.run(["pw-play", str(beep1)], check=True)
    procs, bufs, firsts = {}, {}, {}
    for lane, argv in (("mic", capture.record_argv("input", None)), ("mon", capture.record_argv("output", None))):
        p = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        procs[lane], bufs[lane], firsts[lane] = p, [], [None]
        threading.Thread(target=reader, args=(p, bufs[lane], firsts[lane]), daemon=True).start()
    time.sleep(0.3)
    print(f"{name}: go", flush=True)
    subprocess.run(["pw-play", str(play)], check=True)
    for p in procs.values():
        p.terminate()
    for p in procs.values():
        p.wait()
    time.sleep(0.1)
    for lane in procs:
        raw = b"".join(bufs[lane]); np.save(OUT / f"{name}_{lane}.npy", np.frombuffer(raw[: len(raw) // 4 * 4], "<f4"))
    (OUT / f"{name}_skew.txt").write_text(str(firsts["mic"][0] - firsts["mon"][0]))
    subprocess.run(["pw-play", str(beep2)], check=True)
    print(f"{name}: done", flush=True)
    time.sleep(4.0)
print("all done")
