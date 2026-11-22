"""numpy port of LocalVQE's DAF front end (ggml/daf_frontend.cpp, Apache-2.0):
gated GCC-PHAT prealignment + PBFDKF (M=128, N=128, k=2) + the 2,742-param
controller. Streaming: process(mic_hop, ref_hop) -> e, any multiple of 128."""
import numpy as np

M, N, NFFT, NB, K_ITER = 128, 128, 256, 129, 2
A2 = 0.999 ** 2
P_INIT = 1e3
G_WIN, G_HOP, G_NFFT, G_MAXLAG = 16384, 8000, 32768, 16384
G_GATE = 10 ** (-26.0 / 20)
G_CONF_THR = 8.0
LOCK_AFTER = 56000
F32 = np.float32


def load_weights(path):
    import gguf

    r = gguf.GGUFReader(str(path))
    return {t.name: np.asarray(t.data, F32).reshape(-1) for t in r.tensors}


def _sig(x):
    return 1.0 / (1.0 + np.exp(-x))


class _GRU:
    def __init__(self, w, prefix, nin, nh):
        self.wih = w[prefix + ".weight_ih"].reshape(3 * nh, nin)
        self.whh = w[prefix + ".weight_hh"].reshape(3 * nh, nh)
        self.bih = w[prefix + ".bias_ih"]
        self.bhh = w[prefix + ".bias_hh"]
        self.nh = nh

    def __call__(self, x, h):  # x (B, nin), h (B, nh) -> new h
        gi = x @ self.wih.T + self.bih
        gh = h @ self.whh.T + self.bhh
        nh = self.nh
        r = _sig(gi[:, :nh] + gh[:, :nh])
        z = _sig(gi[:, nh:2 * nh] + gh[:, nh:2 * nh])
        n = np.tanh(gi[:, 2 * nh:] + r * gh[:, 2 * nh:])
        return ((1 - z) * n + z * h).astype(F32)


def _cos_ln(f, w, b):  # LayerNorm over the first 6 features, rest raw. f (B, 10)
    a = f[:, :6]
    mu = a.mean(1, keepdims=True)
    var = ((a - mu) ** 2).mean(1, keepdims=True)
    return np.concatenate([(a - mu) / np.sqrt(var + 1e-5) * w + b, f[:, 6:]], 1)


def _lg(x):
    return np.log10(x + 1e-10)


class DAF:
    def __init__(self, weights, prealign=True):
        w = weights
        self.g_ln = (w["daf.glob.norm.ln.weight"], w["daf.glob.norm.ln.bias"])
        self.b_ln = (w["daf.bins.norm.ln.weight"], w["daf.bins.norm.ln.bias"])
        self.p_ln = (w["daf.part.ln.weight"], w["daf.part.ln.bias"])
        self.g_gru = _GRU(w, "daf.glob.gru", 10, 8)
        self.b_gru = _GRU(w, "daf.bins.gru", 18, 16)
        self.p_gru = _GRU(w, "daf.part.gru", 10, 8)
        self.head_w, self.head_b = w["daf.head.weight"], w["daf.head.bias"][0]
        self.p_head_w, self.p_head_b = w["daf.part.head.weight"], w["daf.part.head.bias"][0]
        self.prealign = prealign
        self.reset()

    def reset(self):
        self.H = np.zeros((N, NB), np.complex64)
        self.X = np.zeros((N, NB), np.complex64)  # newest at p=0
        self.P = np.full((N, NB), P_INIT, F32)
        self.x_old = np.zeros(M, F32)
        self.p_idx = 0
        self.hg = np.zeros((1, 8), F32)
        self.hb = np.zeros((NB, 16), F32)
        self.hp = np.zeros((N, 8), F32)
        self.gcc_S = np.zeros(G_NFFT // 2 + 1, np.complex128)
        self.mic_ring = np.zeros(G_WIN, F32)
        self.ref_ring = np.zeros(G_WIN, F32)
        self.n_seen = 0
        self.gcc_maxrms = 1e-12
        self.gcc_conf = 0.0
        self.cur_shift = 0
        self.locked = False
        self.dline = np.zeros(G_MAXLAG + M, F32)
        self.dpos = 0

    # ── GCC-PHAT ──
    def _gcc(self):
        if self.locked:
            return
        rms = float(np.sqrt(np.mean(self.ref_ring.astype(np.float64) ** 2)))
        self.gcc_maxrms = max(self.gcc_maxrms, rms)
        if rms > self.gcc_maxrms * G_GATE:
            X = np.fft.rfft(self.ref_ring, G_NFFT)
            D = np.fft.rfft(self.mic_ring, G_NFFT)
            S = D * np.conj(X)
            self.gcc_S += S / (np.abs(S) + 1e-9)
        c = np.fft.irfft(self.gcc_S, G_NFFT)[:G_MAXLAG]
        best = int(np.argmax(c))
        conf = float(c[best] / (np.abs(c).mean() + 1e-12))
        self.gcc_conf = conf
        self.cur_shift = (max(0, best - M) // M * M) if conf > G_CONF_THR else 0
        if conf > G_CONF_THR and self.n_seen >= LOCK_AFTER:
            self.locked = True

    # ── one M-sample block ──
    def _block(self, d, x):
        Xn = np.fft.rfft(np.concatenate([self.x_old, x])).astype(np.complex64)
        self.x_old = x.copy()
        self.X[1:] = self.X[:-1]
        self.X[0] = Xn
        H, X, P = self.H, self.X, self.P

        Y = (H * X).sum(0)
        e = (d - np.fft.irfft(Y, NFFT)[M:]).astype(F32)
        z = np.zeros(M, F32)
        E = np.fft.rfft(np.concatenate([z, e])).astype(np.complex64)
        D = np.fft.rfft(np.concatenate([z, d])).astype(np.complex64)
        X2 = (X.real ** 2 + X.imag ** 2).sum(0)
        meanP = P.mean(0)

        Xa, Ea, Ya, Da = np.abs(Xn), np.abs(E), np.abs(Y), np.abs(D)
        ce = 1e-9
        dot = lambda a, b: a.real * b.real + a.imag * b.imag  # noqa: E731
        fb = np.stack([_lg(Xa * Xa), _lg(X2), _lg(Da * Da), _lg(Ea * Ea), _lg(Ya * Ya), _lg(meanP),
                       dot(E, D) / (Ea * Da + ce), dot(D, Y) / (Da * Ya + ce),
                       dot(E, Y) / (Ea * Ya + ce), dot(Xn, D) / (Xa * Da + ce)], 1).astype(F32)

        self.hg = self.g_gru(_cos_ln(fb.mean(0, keepdims=True), *self.g_ln), self.hg)
        ctx = np.repeat(self.hg, NB, 0)
        self.hb = self.b_gru(np.concatenate([_cos_ln(fb, *self.b_ln), ctx], 1), self.hb)
        gbin = 2 * _sig(self.hb @ self.head_w + self.head_b)

        H2 = H.real ** 2 + H.imag ** 2
        f2 = np.stack([_lg(H2.mean(1)), _lg(P.mean(1))], 1)
        mu2 = f2.mean(1, keepdims=True)
        var2 = ((f2 - mu2) ** 2).mean(1, keepdims=True)
        f2n = (f2 - mu2) / np.sqrt(var2 + 1e-5) * self.p_ln[0] + self.p_ln[1]
        self.hp = self.p_gru(np.concatenate([f2n, np.repeat(self.hg, N, 0)], 1).astype(F32), self.hp)
        spart = 2 * _sig(self.hp @ self.p_head_w + self.p_head_b)

        Rt = (E.real ** 2 + E.imag ** 2) / N
        Pe = 0.5 * P * X2 + Rt
        mu = P / (Pe + 1e-10) * gbin[None, :] * spart[:, None]
        s = mu * X2
        mu = np.where(s > 2.0, mu * (2.0 / np.maximum(s, 1e-30)), mu)
        fac = np.maximum(1 - 0.5 * mu * X2, 0)
        self.P = np.maximum(A2 * fac * P + (1 - A2) * H2, 1e-12).astype(F32)

        Ei = E
        for it in range(K_ITER):
            if it > 0:
                Y2 = (self.H * X).sum(0)
                e2 = (d - np.fft.irfft(Y2, NFFT)[M:]).astype(F32)
                Ei = np.fft.rfft(np.concatenate([z, e2])).astype(np.complex64)
            self.H = (self.H + mu * Ei[None, :] * np.conj(X)).astype(np.complex64)

        h = np.fft.irfft(self.H[self.p_idx], NFFT)
        h[M:] = 0
        self.H[self.p_idx] = np.fft.rfft(h).astype(np.complex64)
        self.p_idx = (self.p_idx + 1) % N
        return e

    def process(self, mic, ref):
        mic = np.asarray(mic, F32)
        ref = np.asarray(ref, F32)
        out = np.empty_like(mic)
        DL = len(self.dline)
        for o in range(0, len(mic), M):
            m, r = mic[o:o + M], ref[o:o + M]
            self.mic_ring[:-M] = self.mic_ring[M:]
            self.mic_ring[-M:] = m
            self.ref_ring[:-M] = self.ref_ring[M:]
            self.ref_ring[-M:] = r
            idx = (self.dpos + np.arange(M)) % DL
            self.dline[idx] = r
            self.n_seen += M
            if self.prealign and self.n_seen >= G_WIN and (self.n_seen - G_WIN) % G_HOP < M:
                self._gcc()
            src = (self.dpos + np.arange(M) - self.cur_shift) % DL
            valid = (self.n_seen - M + np.arange(M)) >= self.cur_shift
            xs = np.where(valid, self.dline[src], 0).astype(F32)
            self.dpos = (self.dpos + M) % DL
            out[o:o + M] = self._block(m, xs)
        return out
