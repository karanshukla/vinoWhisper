"""Streaming step for LocalVQE v1.2, built from the PyTorch reference's own layers.
Step(T): (mic, ref) of T*256 new samples + state -> T*256 output samples (one hop
late, as GGML) + new state. Export to OpenVINO with static shapes."""
import math
import os
import sys
from pathlib import Path

import torch
import torch.nn.functional as Fn
from einops import rearrange

HOP = 256


def load_model(checkout, checkpoint):
    import yaml

    src = Path(checkout) / "pytorch"
    sys.path.insert(0, str(src))
    from localvqe.model import LocalVQE

    cfg = yaml.safe_load(open(src / "configs/default.yaml"))
    state = torch.load(Path(checkpoint), map_location="cpu", weights_only=True)
    mcfg = dict(cfg["model"])
    for k, v in (state.get("model_config") or {}).items():
        if k in ("mic_channels", "far_channels", "bottleneck_hidden", "kernel_size"):
            mcfg[k] = v
    m = LocalVQE(**mcfg, n_freqs=cfg["audio"]["n_freqs"])
    m.load_state_dict(state.get("model_state_dict", state.get("model")))
    m.align.fold_temperature()
    m.eval()
    return m


def _fpad(pad_module):  # ZeroPad2d(l, r, top, bottom) -> freq-only padding
    l, r, top, _ = pad_module.padding
    return (l, r), top


class Step(torch.nn.Module):
    def __init__(self, m):
        super().__init__()
        self.m = m
        # every time-kernel conv, in call order: (pad module, conv)
        self.convs = []
        for name in ("mic_enc1", "mic_enc2", "far_enc1", "far_enc2", "mic_enc3", "mic_enc4", "mic_enc5"):
            b = getattr(m, name)
            self.convs += [(b.pad, b.conv), (b.resblock.pad, b.resblock.conv)]
        for name in ("dec5", "dec4", "dec3", "dec2", "dec1"):
            b = getattr(m, name)
            self.convs += [(b.resblock.pad, b.resblock.conv), (b.deconv.pad, b.deconv.conv)]

    # ---- state layout ----
    def init_state(self):
        m = self.m
        st = [torch.zeros(1, HOP), torch.zeros(1, HOP), torch.zeros(1, HOP)]  # mic prev, ref prev, OLA tail
        shapes = self._conv_shapes()
        st += [torch.zeros(s) for s in shapes]
        a = m.align
        F2 = self._f2
        st += [torch.zeros(1, a.hidden_channels, a.dmax - 1, F2),  # K history
               torch.zeros(1, a.in_channels, a.dmax - 1, F2),      # x_ref history
               torch.zeros(1, a.hidden_channels, 4, a.dmax),       # V history
               torch.zeros(1, 2, 2, m.n_freqs),                    # CCM input history
               torch.zeros(1, m.bottleneck.hidden_size), torch.zeros(1, m.bottleneck.hidden_size)]
        return st

    def _conv_shapes(self):
        # run a dummy whole-clip pass with hooks to learn each conv's padded input shape
        shapes = []
        hooks = []
        for pad, conv in self.convs:
            def hook(mod, inp, out, conv=conv):
                x = inp[0]
                shapes.append((conv, (1, x.shape[1], conv.kernel_size[0] - 1, x.shape[-1])))
            hooks.append(conv.register_forward_hook(hook))
        cap = {}
        def f2_hook(mod, i, o):
            cap["f2"] = o.shape[-1]

        h2 = self.m.align.pconv_ref.register_forward_hook(f2_hook)
        with torch.no_grad():
            self.m(torch.zeros(1, 4096), torch.zeros(1, 4096))
        for h in hooks + [h2]:
            h.remove()
        self._f2 = cap["f2"]
        order = {id(c): s for c, s in shapes}
        return [order[id(conv)] for _, conv in self.convs]

    # ---- helpers ----
    def _conv(self, i, x, caches, new):
        pad, conv = self.convs[i]
        (l, r), _ = _fpad(pad)
        x = Fn.pad(x, (l, r, 0, 0))
        full = torch.cat([caches[i], x], 2)
        new[i] = full[:, :, -(conv.kernel_size[0] - 1):]
        return conv(full)

    def _enc(self, blk, i, x, caches, new):
        y = blk.act(self._conv(i, blk.norm(x), caches, new))
        rb = blk.resblock
        return rb.act(self._conv(i + 1, rb.norm(y), caches, new)) + y

    def _dec(self, blk, i, x, x_en, caches, new):
        y = x + blk.skip_conv(blk.skip_norm(x_en))
        rb = blk.resblock
        y = rb.act(self._conv(i, rb.norm(y), caches, new)) + y
        y = self._conv(i + 1, blk.deconv.norm(y), caches, new)
        y = rearrange(y, "b (r c) t f -> b c t (r f)", r=2)
        return y if blk.is_last else blk.act(y)

    def forward(self, mic, ref, *state):
        m = self.m
        T = mic.shape[1] // HOP
        mic_prev, ref_prev, ola = state[0], state[1], state[2]
        nconv = len(self.convs)
        caches = list(state[3:3 + nconv])
        k_hist, r_hist, v_hist, ccm_hist, h_re, h_im = state[3 + nconv:]
        new = [None] * nconv

        # DCT analysis: frame t = [prev hop | hop t]
        def frames(prev, x):
            full = torch.cat([prev, x], 1)  # (1, (T+1)*HOP)
            out = Fn.conv1d(full.unsqueeze(1), m.encoder.conv.weight, stride=HOP)  # (1, 512, T)
            return out.reshape(1, m.n_freqs, 2, T).permute(0, 1, 3, 2), full[:, -HOP:]

        mic_enc, mic_prev2 = frames(mic_prev, mic)
        ref_enc, ref_prev2 = frames(ref_prev, ref)
        mic_fe, ref_fe = m.fe_mic(mic_enc), m.fe_ref(ref_enc)

        e1 = self._enc(m.mic_enc1, 0, mic_fe, caches, new)
        e2 = self._enc(m.mic_enc2, 2, e1, caches, new)
        f1 = self._enc(m.far_enc1, 4, ref_fe, caches, new)
        f2 = self._enc(m.far_enc2, 6, f1, caches, new)

        a = m.align
        D = a.dmax
        Q = a.pconv_mic(e2)
        K = a.pconv_ref(f2)
        Kall = torch.cat([k_hist, K], 2)  # (1, H, D-1+T, F)
        Ku = Kall.unfold(2, D, 1)  # (1, H, T, F, D)
        V = torch.sum(Q.unsqueeze(-1) * Ku, dim=3) / math.sqrt(Ku.shape[3])  # (1, H, T, D)
        Vall = torch.cat([v_hist, V], 2)
        Vs = a.conv[1](Fn.pad(Vall, (1, 1, 0, 0)))  # (1, 1, T, D)
        A = torch.softmax(Vs, dim=-1)
        Rall = torch.cat([r_hist, f2], 2)
        Ru = Rall.unfold(2, D, 1)  # (1, C, T, F, D)
        aligned = torch.sum(Ru * A.unsqueeze(3), dim=-1)  # (1, C, T, F)

        x = torch.cat([e2, aligned], 1)
        e3 = self._enc(m.mic_enc3, 8, x, caches, new)
        e4 = self._enc(m.mic_enc4, 10, e3, caches, new)
        e5 = self._enc(m.mic_enc5, 12, e4, caches, new)

        bn = m.bottleneck
        C5 = e5.shape[1]
        u = rearrange(e5, "b c t f -> b t (c f)")
        v = bn.input_proj(u)
        sp = Fn.softplus(bn.A_log_rate).clamp(min=0.01)
        r = torch.exp(-sp)
        ar, ai = r * torch.cos(bn.A_theta), r * torch.sin(bn.A_theta)
        ys = []
        for t in range(T):
            vt = v[:, t]
            h_re, h_im = ar * h_re - ai * h_im + bn.B_real * vt, ar * h_im + ai * h_re + bn.B_imag * vt
            ys.append(bn.C_real * h_re - bn.C_imag * h_im)
        y = torch.stack(ys, 1)
        b5 = rearrange(bn.output_proj(y) + bn.D * u, "b t (c f) -> b c t f", c=C5)

        d5 = self._dec(m.dec5, 14, b5, e5, caches, new)[..., : e4.shape[-1]]
        d4 = self._dec(m.dec4, 16, d5, e4, caches, new)[..., : e3.shape[-1]]
        d3 = self._dec(m.dec3, 18, d4, e3, caches, new)[..., : e2.shape[-1]]
        d2 = self._dec(m.dec2, 20, d3, e2, caches, new)[..., : e1.shape[-1]]
        d1 = self._dec(m.dec1, 22, d2, e1, caches, new)[..., : mic_fe.shape[-1]]

        # CCM, causal 3x3 over (time, freq)
        mm = rearrange(d1, "b (r c) t f -> b r c t f", r=3)
        Hr = torch.sum(m.mask.v_real[None, :, None, None, None] * mm, 1)
        Hi = torch.sum(m.mask.v_imag[None, :, None, None, None] * mm, 1)
        xp = mic_enc.permute(0, 3, 2, 1)  # (1, 2, T, F)
        X3 = Fn.pad(torch.cat([ccm_hist, xp], 2), (1, 1, 0, 0))  # (1, 2, T+2, F+2)
        Fq = xp.shape[-1]
        er = torch.zeros(1, T, Fq)
        ei = torch.zeros(1, T, Fq)
        for mi in range(3):
            for ni in range(3):
                k = mi * 3 + ni
                xs = X3[:, :, mi: mi + T, ni: ni + Fq]
                er = er + Hr[:, k] * xs[:, 0] - Hi[:, k] * xs[:, 1]
                ei = ei + Hr[:, k] * xs[:, 1] + Hi[:, k] * xs[:, 0]
        enh = torch.stack([er, ei], 3).transpose(1, 2)  # (1, F, T, 2)

        # DCT synthesis + overlap-add (one hop late; steady-state overlap count 2)
        z = enh.permute(0, 1, 3, 2).reshape(1, m.n_freqs * 2, T)
        fr = m.decoder.linear(z.transpose(1, 2))  # (1, T, 512)
        outs = []
        tail = ola
        for t in range(T):
            outs.append((tail + fr[:, t, :HOP]) / 2)
            tail = fr[:, t, HOP:]
        out = torch.cat(outs, 1)

        return (out, mic_prev2, ref_prev2, tail, *new,
                Kall[:, :, -(D - 1):], Rall[:, :, -(D - 1):], Vall[:, :, -4:],
                torch.cat([ccm_hist, xp], 2)[:, :, -2:], h_re, h_im)

