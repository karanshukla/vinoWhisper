import sys, numpy as np, soundfile as sf
B = sys.argv[1]; sys.path.insert(0, sys.argv[3])
import daf
from v12_stream_ov import Runner
c = B + "/clips_real/dt_d30_ser-5_nl_0/"
mic, _ = sf.read(c + "mic.wav", dtype="float32"); ref, _ = sf.read(c + "ref.wav", dtype="float32")
n = 5 * 16000 // 512 * 512; mic, ref = mic[:n], ref[:n]
f = daf.DAF(dict(np.load(B + "/models/localvqe-v1.4-aec-2.7K-f32.npz")))
e = np.concatenate([f.process(mic[i:i+512], ref[i:i+512]) for i in range(0, n, 512)])
r = Runner(B + "/ov_v12s/v12s_T2.xml")
g = np.concatenate([r(mic[i:i+512], ref[i:i+512]) for i in range(0, n, 512)])
np.save(sys.argv[2], np.stack([e, g]))
print("2.7K rms %.6e  v1.2 rms %.6e" % (np.sqrt(np.mean(e**2)), np.sqrt(np.mean(g**2))))
