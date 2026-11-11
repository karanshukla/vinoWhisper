"""Our own Whisper decode loop (#178): features, tokenizer, prompt and greedy rules
checked against transformers' output, and the request bookkeeping against a fake
openvino. The fixtures came from openai/whisper-small.en@e8727524f962 under
transformers 5.22.2; the tokenizer one holds only the merges its cases use."""

import json
import sys
from pathlib import Path
from types import ModuleType

import numpy as np
import pytest

from vinowhisper import whisper_step

FIXTURES = Path(__file__).parent / "fixtures"
EOS, SOT, PREV, NO_TS = 50256, 50257, 50360, 50362


def test_features_match_the_reference_extractor():
    ref = np.load(FIXTURES / "whisper_features.npz")
    n = np.arange(3 * 16000) / 16000
    audio = 0.2 * np.sin(2 * np.pi * 220 * n) * np.sin(2 * np.pi * 0.5 * n)
    audio = (audio + 0.05 * np.sin(2 * np.pi * 1800 * n)).astype(np.float32)
    mel = whisper_step.log_mel(audio, ref["filters"])
    assert mel.shape == (80, whisper_step.MEL_FRAMES)
    np.testing.assert_allclose(mel[:, :320], ref["head"], atol=1e-4)
    # The zero padding past the audio is one floor value per bin.
    np.testing.assert_allclose(mel[:, 320:].mean(axis=1), ref["tail"], atol=1e-4)


def test_audio_past_30_seconds_is_cut_not_squeezed():
    filters = np.load(FIXTURES / "whisper_features.npz")["filters"]
    long = np.ones(35 * 16000, dtype=np.float32) * 0.1
    assert whisper_step.log_mel(long, filters).shape == (80, 3000)


@pytest.fixture(scope="module")
def bpe():
    return whisper_step.Bpe(FIXTURES / "whisper_tokenizer_mini.json")


@pytest.mark.parametrize(
    "case", json.loads((FIXTURES / "whisper_bpe_cases.json").read_text())["cases"]
)
def test_the_tokenizer_matches_transformers(bpe, case):
    assert bpe.encode(case["text"]) == case["ids"]
    assert bpe.decode(case["ids"]) == case["decoded"]


def test_decoding_skips_special_tokens(bpe):
    ids = bpe.encode(" Plasma")
    assert bpe.decode([SOT, NO_TS, *ids, EOS]) == " Plasma"


def generation(tmp_path, **extra):
    raw = {
        "eos_token_id": EOS,
        "decoder_start_token_id": SOT,
        "prev_sot_token_id": PREV,
        "no_timestamps_token_id": NO_TS,
        "suppress_tokens": [1, 2],
        "begin_suppress_tokens": [220, EOS],
        **extra,
    }
    (tmp_path / "generation_config.json").write_text(json.dumps(raw))
    return whisper_step.Generation(tmp_path)


def test_the_prompt_is_laid_out_as_whisper_pipeline_lays_hotwords(tmp_path, bpe):
    """context_tokens.cpp at 2026.4.1.0: <|startofprev|>, the encoded " " + hotwords with
    special tokens dropped, then the start of transcript and <|notimestamps|>."""
    gen = generation(tmp_path)
    words = whisper_step.hotword_ids(bpe, gen, "Karan, OpenVINO, Wildcat Lake, Plasma")
    assert words == bpe.encode(" Karan, OpenVINO, Wildcat Lake, Plasma")
    assert gen.prompt(words) == [PREV, *words, SOT, NO_TS]
    assert gen.prompt([]) == [SOT, NO_TS]


def test_the_multilingual_prompt_names_language_and_task(tmp_path):
    gen = generation(
        tmp_path,
        is_multilingual=True,
        lang_to_id={"<|en|>": 50259, "<|fr|>": 50265},
        task_to_id={"transcribe": 50360 + 99, "translate": 50358},
    )
    assert gen.prompt([7], language="fr", task="translate") == [PREV, 7, SOT, 50265, 50358, NO_TS]
    with pytest.raises(ValueError):
        gen.prompt([])


class Scripted:
    """A decoder that answers with fixed logits per position and records its calls."""

    def __init__(self, best_by_position, vocab=NO_TS + 10):
        self.best = best_by_position
        self.vocab = vocab
        self.calls = []

    def __call__(self, token, position):
        self.calls.append((token, position))
        row = np.zeros(self.vocab, dtype=np.float32)
        for rank, token_id in enumerate(self.best.get(position, [EOS])):
            row[token_id] = 10.0 - rank
        return row


def test_greedy_feeds_the_prompt_then_answers_until_eos(tmp_path):
    gen = generation(tmp_path)
    prompt = [SOT, NO_TS]
    decoder = Scripted({1: [500], 2: [600], 3: [EOS]})
    assert whisper_step.greedy(decoder, prompt, gen, 10) == [500, 600]
    assert decoder.calls == [(SOT, 0), (NO_TS, 1), (500, 2), (600, 3)]


def test_a_blank_or_eos_cannot_open_the_answer_but_eos_can_end_it(tmp_path):
    gen = generation(tmp_path)
    decoder = Scripted({1: [220, EOS, 500], 2: [EOS]})
    assert whisper_step.greedy(decoder, [SOT, NO_TS], gen, 10) == [500]


def test_suppressed_and_timestamp_tokens_are_never_chosen(tmp_path):
    gen = generation(tmp_path)
    decoder = Scripted({1: [NO_TS + 3, 2, 700], 2: [1, NO_TS, EOS]})
    assert whisper_step.greedy(decoder, [SOT, NO_TS], gen, 10) == [700]


def test_the_answer_stops_at_its_token_limit(tmp_path):
    gen = generation(tmp_path)
    decoder = Scripted({p: [500 + p] for p in range(1, 40)})
    assert whisper_step.greedy(decoder, [SOT, NO_TS], gen, 3) == [501, 502, 503]


@pytest.fixture
def export(tmp_path, monkeypatch):
    """An export directory and a fake openvino whose decoder echoes its inputs."""
    layers, heads, slots, dim = 2, 1, 8, 2
    (tmp_path / "step_export.json").write_text(
        json.dumps({"layers": layers, "heads": heads, "head_dim": dim, "cache_slots": slots})
    )
    generation(tmp_path)
    (tmp_path / "tokenizer.json").write_text((FIXTURES / "whisper_tokenizer_mini.json").read_text())
    filters = np.load(FIXTURES / "whisper_features.npz")["filters"].tolist()
    (tmp_path / "preprocessor_config.json").write_text(json.dumps({"mel_filters": filters}))
    kv = [f"{'kv'[j % 2]}{j // 2}" for j in range(2 * layers)]
    seen = {"encoder": 0, "masks": [], "eos_at": 3}

    class Tensor:
        def __init__(self, array):
            self.data = array

    class Decoder:
        def __init__(self):
            self.tensors = {
                "ids": Tensor(np.zeros((1, 1), np.int64)),
                "pos": Tensor(np.zeros(1, np.int64)),
                "mask": Tensor(np.zeros((1, 1, 1, slots + 1), np.float32)),
                "logits": Tensor(np.zeros((1, NO_TS + 10), np.float32)),
            }
            for n in kv:
                self.tensors[f"past_{n}"] = Tensor(np.zeros((1, heads, slots, dim), np.float32))
                self.tensors[f"cross_{n}"] = Tensor(np.zeros((1, heads, 4, dim), np.float32))
                self.tensors[f"new_{n}"] = Tensor(np.zeros((1, heads, 1, dim), np.float32))

        def get_tensor(self, name):
            return self.tensors[name]

        def infer(self):
            position = int(self.tensors["pos"].data[0])
            seen["masks"].append(self.tensors["mask"].data.copy())
            for n in kv:
                self.tensors[f"new_{n}"].data[...] = position + 1
            logits = self.tensors["logits"].data
            logits[...] = 0
            logits[0, EOS if position >= seen["eos_at"] else 500 + position] = 1.0

    class Encoder:
        def infer(self, inputs):
            assert inputs["mel"].shape == (1, 80, 3000)
            seen["encoder"] += 1
            return {f"cross_{n}": np.full((1, heads, 4, dim), 7.0, np.float32) for n in kv}

    class Compiled:
        def __init__(self, path):
            self.path = path

        def create_infer_request(self):
            return Encoder() if self.path.endswith("encoder.xml") else Decoder()

    class Core:
        def compile_model(self, path, device):
            seen["device"] = device
            return Compiled(path)

    openvino = ModuleType("openvino")
    openvino.Core = Core
    monkeypatch.setitem(sys.modules, "openvino", openvino)
    return tmp_path, seen, slots


def test_the_loop_writes_each_step_into_the_cache_and_opens_the_mask(export):
    path, seen, slots = export
    step = whisper_step.StepWhisper(path, "NPU")
    tokens = step.decode(np.zeros(16000, np.float32), [SOT, NO_TS], 10)
    assert tokens == [501, 502]
    assert seen["device"] == "NPU" and seen["encoder"] == 1
    past = step._past[0]
    # Position p holds what the decoder returned at p (p + 1 here); the rest is empty.
    assert [float(past[0, 0, p, 0]) for p in range(slots)] == [1, 2, 3, 4, 0, 0, 0, 0]
    # Each step sees the slots already filled and its own one, never a later one.
    first, third = seen["masks"][0][0, 0, 0], seen["masks"][2][0, 0, 0]
    assert (first[:slots] < 0).all() and first[slots] == 0
    assert (third[:2] == 0).all() and (third[2:slots] < 0).all()
    assert (step._cross[0][1] == 7.0).all()


def test_a_second_decode_starts_from_an_empty_cache(export):
    path, seen, slots = export
    step = whisper_step.StepWhisper(path, "NPU")
    step.decode(np.zeros(16000, np.float32), [SOT, NO_TS], 10)
    step.decode(np.zeros(16000, np.float32), [SOT, NO_TS], 1)
    assert float(step._past[0][0, 0, 3, 0]) == 0
    assert seen["encoder"] == 2


def test_a_prompt_that_fills_the_cache_is_refused(export):
    path, _, slots = export
    step = whisper_step.StepWhisper(path, "NPU")
    with pytest.raises(ValueError, match="no room"):
        step.decode(np.zeros(16000, np.float32), [SOT] * slots, 5)


def test_an_answer_never_runs_past_the_cache(export):
    path, seen, slots = export
    seen["eos_at"] = 99
    step = whisper_step.StepWhisper(path, "NPU")
    prompt = [SOT] * (slots - 4) + [NO_TS]
    tokens = step.decode(np.zeros(16000, np.float32), prompt, 50)
    # Every answer token but the last is written into a slot; none past the end.
    assert len(tokens) == slots - len(prompt) - 1


def test_a_directory_is_an_export_only_with_every_file():
    assert not whisper_step.exported(FIXTURES)
