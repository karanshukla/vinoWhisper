"""Play each clip's far end out of the laptop speakers; capture the mic and the
sink monitor with vinoWhisper's own record_argv. Then build real-echo clips:
mic = recorded mic (real speaker, room, mic, room noise) + the clip's near-end
voice scaled to the clip's signal-to-echo ratio; ref = recorded sink monitor.

usage: record.py BENCH_DIR
"""
import json, os, subprocess, sys, threading, time
from pathlib import Path
import numpy as np
import soundfile as sf

sys.path.insert(0, os.path.expanduser("~/Development/vinoWhisper"))
from vinowhisper import capture  # noqa: E402

B = Path(sys.argv[1]); SRC = B / "clips"; RAW = B / "real_raw"; OUTC = B / "clips_real"
FS = 16000; LEAD = 1.0; TAIL = 1.5

def reader(proc, sink, first):
    while True:
        b = proc.stdout.read(4096)
        if not b:
            return
        if first[0] is None:
            first[0] = time.monotonic()
        sink.append(b)

def take(ref_path):
    procs, bufs, firsts = {}, {}, {}
    for lane, argv in (("mic", capture.record_argv("input", None)), ("mon", capture.record_argv("output", None))):
        p = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        procs[lane], bufs[lane], firsts[lane] = p, [], [None]
        threading.Thread(target=reader, args=(p, bufs[lane], firsts[lane]), daemon=True).start()
    time.sleep(LEAD)
    subprocess.run(["pw-play", str(ref_path)], check=True)
    time.sleep(TAIL)
    for p in procs.values():
        p.terminate()
    for p in procs.values():
        p.wait()
    time.sleep(0.1)
    a = {k: np.frombuffer(b"".join(v)[: len(b"".join(v)) // 4 * 4], "<f4").copy() for k, v in bufs.items()}
    skew = firsts["mic"][0] - firsts["mon"][0]  # >0: mic's first sample arrived later
    return a["mic"], a["mon"], skew

def onset(mon, ref):
    n = 1 << (len(mon) + len(ref)).bit_length()
    cc = np.fft.irfft(np.fft.rfft(mon, n) * np.conj(np.fft.rfft(ref, n)), n)[: len(mon)]
    return int(np.argmax(cc))

def rms(x):
    return float(np.sqrt(np.mean(np.square(x, dtype=np.float64)) + 1e-20))

def main():
    RAW.mkdir(exist_ok=True); OUTC.mkdir(exist_ok=True)
    metas = json.load(open(SRC / "index.json")); out_meta = []; log = []
    for i, m in enumerate(metas):
        d = SRC / m["name"]
        raw = RAW / m["name"]
        if not (raw / "mic.npy").exists():
            print(f"[{i+1}/{len(metas)}] playing {m['name']} ({m['seconds']}s)", flush=True)
            mic, mon, skew = take(d / "ref.wav")
            raw.mkdir(exist_ok=True)
            np.save(raw / "mic.npy", mic); np.save(raw / "mon.npy", mon); (raw / "skew.txt").write_text(str(skew))
        mic, mon = np.load(raw / "mic.npy"), np.load(raw / "mon.npy"); skew = float((raw / "skew.txt").read_text())
        # put both lanes on one clock by first-arrival: drop the head of whichever started earlier
        k = int(round(skew * FS))
        if k > 0:
            mon = mon[k:]
        elif k < 0:
            mic = mic[-k:]
        ref, _ = sf.read(d / "ref.wav", dtype="float32")
        clean, _ = sf.read(d / "clean.wav", dtype="float32")
        L = len(ref)
        start = onset(mon, ref) if np.any(ref) else int(LEAD * FS)
        mic_c = mic[start : start + L]; mon_c = mon[start : start + L]
        mic_c = np.pad(mic_c, (0, L - len(mic_c))); mon_c = np.pad(mon_c, (0, L - len(mon_c)))
        # real acoustic delay ref->mic, for the record
        delay_ms = None
        if np.any(ref):
            n = 1 << (2 * L).bit_length()
            cc = np.fft.irfft(np.fft.rfft(mic_c, n) * np.conj(np.fft.rfft(mon_c, n)), n)
            lag = int(np.argmax(np.abs(np.concatenate([cc[-FS // 2 :], cc[: FS // 2]])))) - FS // 2
            delay_ms = lag / FS * 1000
        echo_rms = rms(mic_c)
        if m["kind"] == "dt":
            near_rms = rms(clean)
            g = near_rms * 10 ** (-m["ser_db"] / 20) / echo_rms  # echo scaled to the clip's SER
            mic_out = mic_c * g + clean
            ref_out = mon_c * g  # the ref's absolute level doesn't matter to the AEC; keep it with the mic
            clean_out = clean
        elif m["kind"] == "fe":
            g = 0.05 * 10 ** (-m["ser_db"] / 20) / echo_rms
            mic_out = mic_c * g; ref_out = mon_c * g; clean_out = np.zeros(L, np.float32)
        else:  # ne: nothing played, real room noise at the dt gain is unknown; keep the clip's own
            g = 1.0
            mic_out, _ = sf.read(d / "mic.wav", dtype="float32"); ref_out = ref; clean_out = clean
        peak = float(np.max(np.abs(mic_out))) or 1.0
        if peak > 0.99:
            s = 0.99 / peak; mic_out *= s; ref_out *= s; clean_out = clean_out * s
        od = OUTC / m["name"]; od.mkdir(exist_ok=True)
        for nm, x in (("mic", mic_out), ("ref", ref_out), ("clean", clean_out)):
            sf.write(od / f"{nm}.wav", np.asarray(x, np.float32), FS, subtype="FLOAT")
        rec = dict(m, echo_dbfs=20 * np.log10(echo_rms), gain=g, delay_ms=delay_ms, skew_ms=skew * 1000,
                   mon_dbfs=20 * np.log10(rms(mon_c) + 1e-12))
        out_meta.append(m); log.append(rec)
        print(f"  {m['name']:22s} echo {rec['echo_dbfs']:6.1f} dBFS  delay {delay_ms}  skew {skew*1000:.0f} ms  gain {g:.2f}", flush=True)
    json.dump(out_meta, open(OUTC / "index.json", "w"), indent=1)
    json.dump(log, open(B / "real_log.json", "w"), indent=1, default=float)

if __name__ == "__main__":
    main()
