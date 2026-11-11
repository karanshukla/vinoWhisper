"""The personal vocabulary (issue #161): the file, and how it reaches Whisper's decoder."""

import sys
from types import ModuleType, SimpleNamespace

import numpy as np
import pytest

from vinowhisper import config, devices, failures, vocabulary, whisper_step
from vinowhisper.transcriber import WhisperTranscriber


def test_one_term_per_line_with_comments_blanks_and_repeats_dropped():
    config.VOCABULARY_FILE.write_text(
        "# names\nKubernetes\n\n  vino   Whisper  \nkubernetes\nSaoirse\n", encoding="utf-8"
    )
    assert vocabulary.read() == (["Kubernetes", "vino Whisper", "Saoirse"], None)


def test_a_missing_file_is_no_terms_and_no_problem():
    assert vocabulary.read() == ([], None)


def test_an_unreadable_file_is_named_not_raised():
    config.VOCABULARY_FILE.write_bytes(b"\xff\xfe\xff")
    terms, problem = vocabulary.read()
    assert terms == []
    assert problem


def test_a_line_that_spells_a_special_token_or_runs_long_is_skipped():
    config.VOCABULARY_FILE.write_text(
        "<|endoftext|>\n" + "x" * (vocabulary.MAX_TERM_CHARS + 1) + "\nfine\n"
    )
    assert vocabulary.read()[0] == ["fine"]


def test_past_the_limit_the_first_terms_are_kept_and_the_rest_reported():
    config.VOCABULARY_FILE.write_text(
        "\n".join(f"term{n}" for n in range(vocabulary.MAX_TERMS + 5))
    )
    terms, problem = vocabulary.read()
    assert terms == [f"term{n}" for n in range(vocabulary.MAX_TERMS)]
    assert "only the first" in problem


def test_the_prompt_is_a_comma_list():
    assert vocabulary.prompt(["Kubernetes", "Saoirse"]) == "Kubernetes, Saoirse"


class _Tokenizer:
    def encode(self, text, add_special_tokens):
        assert text.startswith(" ") and not add_special_tokens
        # One token per word: enough to test the budget without a real tokenizer.
        return SimpleNamespace(input_ids=np.zeros((1, len(text.split())), dtype=np.int64))


@pytest.fixture
def pipeline(monkeypatch, tmp_path):
    calls = []

    class Pipeline:
        def __init__(self, model_dir, device, **kwargs):
            self.device = device

        def get_tokenizer(self):
            return _Tokenizer()

        def generate(self, samples, streamer, **kwargs):
            calls.append(kwargs)
            streamer("ok")

    genai = ModuleType("openvino_genai")
    genai.WhisperPipeline = Pipeline
    monkeypatch.setitem(sys.modules, "openvino_genai", genai)
    monkeypatch.setattr(config, "FAILED_DEVICES_FILE", tmp_path / "failed-devices.json")
    monkeypatch.setattr(config, "model_dir", lambda kind, multilingual: tmp_path)
    monkeypatch.setattr(WhisperTranscriber, "_check_export", lambda self, kind, model_dir: None)
    return calls


def _transcriber(monkeypatch, device, **settings):
    monkeypatch.setattr(devices, "available", lambda: [devices.Device(device, device)])
    transcriber = WhisperTranscriber(**settings)
    transcriber.select_device()
    transcriber.load()
    return transcriber


def _decode(transcriber, seconds=2.0, biased=True):
    samples = np.zeros(int(seconds * config.SAMPLE_RATE_HZ), dtype=np.float32)
    return "".join(transcriber.transcribe_stream(samples, biased))


def test_a_dictation_decode_carries_the_vocabulary_as_hotwords(pipeline, monkeypatch):
    config.VOCABULARY_FILE.write_text("Kubernetes\nSaoirse\n")
    _decode(_transcriber(monkeypatch, "CPU"))
    assert pipeline[-1]["hotwords"] == "Kubernetes, Saoirse"


def test_a_decode_that_did_not_ask_gets_no_prompt(pipeline, monkeypatch):
    config.VOCABULARY_FILE.write_text("Kubernetes\n")
    _decode(_transcriber(monkeypatch, "CPU"), biased=False)
    assert "hotwords" not in pipeline[-1]


def test_no_file_means_no_prompt(pipeline, monkeypatch):
    _decode(_transcriber(monkeypatch, "CPU"))
    assert "hotwords" not in pipeline[-1]


def test_characterization_the_npu_never_gets_the_prompt(pipeline, monkeypatch):
    """characterization: the NPU static pipeline raises "'hotwords' parameter is not
    supported on NPU device" (and the same for initial_prompt) on any generate() that
    sets one. Passing the vocabulary there would turn every dictation into an error, so
    WhisperPipeline never gets it on the NPU, with or without our own export (#178,
    which takes the prompt instead). Flip this only when OpenVINO GenAI drops the assert
    in pipeline_static.cpp, and after a decode on a real NPU."""
    config.VOCABULARY_FILE.write_text("Kubernetes\n")
    _decode(_transcriber(monkeypatch, "NPU"))
    assert "hotwords" not in pipeline[-1]


def test_an_edit_to_the_file_is_picked_up_without_a_restart(pipeline, monkeypatch):
    transcriber = _transcriber(monkeypatch, "CPU")
    config.VOCABULARY_FILE.write_text("Kubernetes\n")
    _decode(transcriber)
    config.VOCABULARY_FILE.write_text("Kubernetes\nSaoirse Ronan\n")
    _decode(transcriber)
    assert pipeline[-1]["hotwords"] == "Kubernetes, Saoirse Ronan"
    config.VOCABULARY_FILE.unlink()
    _decode(transcriber)
    assert "hotwords" not in pipeline[-1]


def test_terms_past_the_token_budget_are_left_out(pipeline, monkeypatch, capsys):
    monkeypatch.setattr(config, "VOCABULARY_MAX_TOKENS", 3)
    config.VOCABULARY_FILE.write_text("one\ntwo\nthree\nfour\nfive\n")
    _decode(_transcriber(monkeypatch, "CPU"))
    assert pipeline[-1]["hotwords"] == "one, two, three"
    assert "first 3 of 5" in capsys.readouterr().err


def test_the_prompt_comes_out_of_the_answers_token_budget(pipeline, monkeypatch):
    """The prompt and the answer share the decoder's 448 positions: measured 2026-11-10,
    a prompt past them returns a few characters of garbage and no error."""
    config.VOCABULARY_FILE.write_text("\n".join(f"w{n}" for n in range(100)))
    transcriber = _transcriber(monkeypatch, "CPU")
    _decode(transcriber, seconds=29.0, biased=False)
    plain = pipeline[-1]["max_new_tokens"]
    _decode(transcriber, seconds=29.0)
    biased = pipeline[-1]["max_new_tokens"]
    assert plain == config.max_new_tokens(29.0)
    assert biased == config.WHISPER_MAX_TOKENS - 100 - config.MAX_TOKENS_MARGIN < plain


def test_only_whisper_off_the_npu_takes_the_prompt():
    assert vocabulary.unprompted("CPU", config.CANARY_MODEL)
    assert vocabulary.unprompted("NPU", config.WHISPER_MODEL)
    assert vocabulary.unprompted("GPU", config.WHISPER_MODEL) is None


def test_our_own_export_lets_the_npu_take_the_prompt():
    assert vocabulary.unprompted("NPU", config.WHISPER_MODEL, step_export=True) is None
    assert vocabulary.unprompted("NPU", config.CANARY_MODEL, step_export=True)


def test_auto_language_has_no_prompt_on_the_npu_only():
    """Our loop writes the language token into the prompt itself, and auto has none.
    WhisperPipeline on a CPU or GPU detects the language under its own hotwords."""
    reason = vocabulary.unprompted("NPU", config.WHISPER_MODEL, True, config.AUTO_LANGUAGE)
    assert reason and "auto" in reason and "Settings" in reason
    assert vocabulary.unprompted("NPU", config.WHISPER_MODEL, True, "fr") is None
    assert vocabulary.unprompted("GPU", config.WHISPER_MODEL, False, config.AUTO_LANGUAGE) is None


@pytest.fixture
def step(monkeypatch):
    """Our own loop (#178), faked: what it was asked, and whether it loaded."""
    state = {"calls": [], "fails": False, "built": 0, "says": "Kubernetes is up."}
    for directory in (config.STEP_MODEL_DIR, config.MULTILINGUAL_STEP_MODEL_DIR):
        directory.mkdir(parents=True, exist_ok=True)
        for name in ("encoder.xml", "decoder.xml", "step_export.json", "tokenizer.json"):
            (directory / name).write_text("{}")

    class Step:
        def __init__(self, model_dir, device):
            if state["fails"]:
                raise RuntimeError(state["fails"])
            state["built"] += 1
            state["device"] = device
            state["dir"] = model_dir

        def transcribe(self, samples, hotwords, max_new_tokens, **prompt):
            state["calls"].append(
                {"hotwords": hotwords, "max_new_tokens": max_new_tokens, **prompt}
            )
            return state["says"]

    monkeypatch.setattr(whisper_step, "StepWhisper", Step)
    return state


def test_with_our_export_the_npu_dictates_through_the_prompted_loop(pipeline, step, monkeypatch):
    config.VOCABULARY_FILE.write_text("Kubernetes\nSaoirse\n")
    transcriber = _transcriber(monkeypatch, "NPU")
    assert _decode(transcriber) == "Kubernetes is up."
    assert step["calls"][-1]["hotwords"] == "Kubernetes, Saoirse"
    assert step["calls"][-1]["language"] is None
    assert step["device"] == "NPU"
    assert pipeline == []
    _decode(transcriber)
    assert step["built"] == 1


def test_captions_and_unbiased_decodes_stay_on_the_pipeline(pipeline, step, monkeypatch):
    config.VOCABULARY_FILE.write_text("Kubernetes\n")
    _decode(_transcriber(monkeypatch, "NPU"), biased=False)
    assert step["calls"] == []
    assert "hotwords" not in pipeline[-1]


def test_the_loop_is_built_at_load_when_a_vocabulary_will_use_it(pipeline, step, monkeypatch):
    """Its two models take 24.8 s from an empty driver cache (docs/models.md): paid at
    the key press's warm-up load, not under the lock inside the first dictation."""
    config.VOCABULARY_FILE.write_text("Kubernetes\n")
    transcriber = _transcriber(monkeypatch, "NPU")
    assert step["built"] == 1
    _decode(transcriber)
    assert step["built"] == 1


@pytest.mark.parametrize("vocabulary_text", [None, "# no terms yet\n"])
def test_without_terms_the_loop_waits_for_a_dictation_that_needs_it(
    pipeline, step, monkeypatch, vocabulary_text
):
    if vocabulary_text is not None:
        config.VOCABULARY_FILE.write_text(vocabulary_text)
    transcriber = _transcriber(monkeypatch, "NPU")
    assert step["built"] == 0
    config.VOCABULARY_FILE.write_text("Kubernetes\n")
    assert _decode(transcriber) == "Kubernetes is up."
    assert step["built"] == 1


def test_the_loop_is_not_built_off_the_npu(pipeline, step, monkeypatch):
    config.VOCABULARY_FILE.write_text("Kubernetes\n")
    _decode(_transcriber(monkeypatch, "CPU"))
    assert step["built"] == 0 and step["calls"] == []


def test_a_named_language_reaches_the_multilingual_loop(pipeline, step, monkeypatch):
    config.VOCABULARY_FILE.write_text("Kubernetes\n")
    transcriber = _transcriber(monkeypatch, "NPU", language="fr")
    assert _decode(transcriber) == "Kubernetes is up."
    assert step["dir"] == config.MULTILINGUAL_STEP_MODEL_DIR
    assert (step["calls"][-1]["language"], step["calls"][-1]["task"]) == ("fr", "transcribe")
    assert pipeline == []


def test_auto_language_on_the_npu_dictates_without_the_prompt(pipeline, step, monkeypatch):
    """The loop's prompt needs a language token and auto has none, so the pipeline decodes
    and nothing is compiled for a prompt that will never be used."""
    config.VOCABULARY_FILE.write_text("Kubernetes\n")
    transcriber = _transcriber(monkeypatch, "NPU", language=config.AUTO_LANGUAGE)
    assert _decode(transcriber) == "ok"
    assert step["built"] == 0 and step["calls"] == []
    assert "hotwords" not in pipeline[-1]


def test_a_reload_gives_a_loop_that_failed_another_try(pipeline, step, monkeypatch, capsys):
    step["fails"] = "compile failed: ZE_RESULT_ERROR_UNSUPPORTED_FEATURE"
    config.VOCABULARY_FILE.write_text("Kubernetes\n")
    transcriber = _transcriber(monkeypatch, "NPU")
    assert _decode(transcriber) == "ok"
    step["fails"] = False
    assert _decode(transcriber) == "ok", "still off: one failure is not retried per dictation"
    transcriber._unload()
    transcriber.load()
    assert _decode(transcriber) == "Kubernetes is up."


def test_a_reset_while_the_loop_loads_is_a_reset_not_a_broken_export(
    pipeline, step, monkeypatch, capsys
):
    """ZE_RESULT_ERROR_DEVICE_LOST is another program's fault resetting the NPU for
    everyone. Swallowing it as "did not load" would turn the prompt off for good and
    leave a dead pipeline in place; raised, transcribe_stream reloads and retries."""
    transcriber = _transcriber(monkeypatch, "NPU")
    config.VOCABULARY_FILE.write_text("Kubernetes\n")
    step["fails"] = "compile_model: ZE_RESULT_ERROR_DEVICE_LOST"
    builds = []
    real = WhisperTranscriber._build

    def build(self, selection, model_dir):
        builds.append(selection.kind)
        step["fails"] = False
        real(self, selection, model_dir)

    monkeypatch.setattr(WhisperTranscriber, "_build", build)
    assert _decode(transcriber) == "Kubernetes is up."
    assert builds == ["NPU"]
    err = capsys.readouterr().err
    assert "was reset, reloading the model" in err
    assert "vocabulary prompt is off" not in err


def test_a_reset_at_load_leaves_the_loop_for_the_first_dictation(pipeline, step, monkeypatch):
    config.VOCABULARY_FILE.write_text("Kubernetes\n")
    step["fails"] = "compile_model: ZE_RESULT_ERROR_DEVICE_LOST"
    transcriber = _transcriber(monkeypatch, "NPU")
    assert not transcriber._step_failed
    step["fails"] = False
    assert _decode(transcriber) == "Kubernetes is up."
    assert not failures.load()


def test_a_loop_that_does_not_load_leaves_dictation_working(pipeline, step, monkeypatch, capsys):
    step["fails"] = "compile failed: ZE_RESULT_ERROR_UNSUPPORTED_FEATURE"
    config.VOCABULARY_FILE.write_text("Kubernetes\n")
    transcriber = _transcriber(monkeypatch, "NPU")
    assert _decode(transcriber) == "ok"
    assert "hotwords" not in pipeline[-1]
    assert "vocabulary prompt is off on NPU" in capsys.readouterr().err
    # Tried once, not on every dictation.
    _decode(transcriber)
    assert capsys.readouterr().err == ""


def test_the_prompted_loop_still_gets_the_noise_check(pipeline, step, monkeypatch):
    """#175: a decode that is only listed terms is re-run without the prompt."""
    step["says"] = "Kubernetes."
    config.VOCABULARY_FILE.write_text("Kubernetes\n")
    assert _decode(_transcriber(monkeypatch, "NPU")) == "ok"
    assert len(step["calls"]) == 1 and len(pipeline) == 1


TERMS = ["Cresswell", "Estafania", "Neverbend", "Southey", "Poyser", "Kubernetes", "Wayland"]


@pytest.mark.parametrize(
    ("heard", "typed"),
    [
        ("added Creswell, avoiding", "added Cresswell, avoiding"),
        ("Donna Estefania, who", "Donna Estafania, who"),
        ("misses Neverbent.", "misses Neverbend."),
        ("inserting Sothey's reply", "inserting Southey's reply"),
        ("said Poiser", "said Poyser"),
    ],
)
def test_a_near_miss_spelling_becomes_the_listed_term(heard, typed):
    """These are what Canary and Whisper wrote for LibriSpeech names, 2026-11-10."""
    assert vocabulary.correct(heard, TERMS) == typed


@pytest.mark.parametrize(
    "text",
    [
        "the wayward poster",  # a different first letter, or too far
        "Kubernetes and Wayland",  # already right
        "south of the poise",  # short words are left alone
        "Cooper Netties",  # two words for one term: the decoder prompt's job
    ],
)
def test_words_that_are_not_a_near_miss_are_left_alone(text):
    assert vocabulary.correct(text, TERMS) == text


def test_characterization_short_and_multi_word_terms_are_never_fuzzy_matched():
    """characterization: a term under five letters has real words one letter away
    ("Vino"/"vine"), and a multi-word term would need words joined before comparing.
    Both are exact-only here; loosening either trades missed names for changed words,
    with no way for the user to see what was changed."""
    assert vocabulary.correct("a vine and a fine Saoirse Ronin", ["Vino", "Saoirse Ronan"]) == (
        "a vine and a fine Saoirse Ronin"
    )


def test_characterization_a_real_word_one_letter_from_a_term_is_rewritten():
    """characterization: there is no dictionary to say "clause" is a word, so it is
    corrected to a listed "Claude". Known and documented (docs/tray.md); telling the
    two apart needs a word list this project does not ship."""
    assert vocabulary.correct("the last clause", ["Claude"]) == "the last Claude"


def _echo_pipeline(monkeypatch, tmp_path, prompted, plain):
    calls = []

    class Pipeline:
        def __init__(self, model_dir, device, **kwargs):
            pass

        def get_tokenizer(self):
            return _Tokenizer()

        def generate(self, samples, streamer, **kwargs):
            calls.append(kwargs)
            streamer(prompted if "hotwords" in kwargs else plain)

    genai = ModuleType("openvino_genai")
    genai.WhisperPipeline = Pipeline
    monkeypatch.setitem(sys.modules, "openvino_genai", genai)
    monkeypatch.setattr(config, "FAILED_DEVICES_FILE", tmp_path / "failed-devices.json")
    monkeypatch.setattr(config, "model_dir", lambda kind, multilingual: tmp_path)
    monkeypatch.setattr(WhisperTranscriber, "_check_export", lambda self, kind, model_dir: None)
    config.VOCABULARY_FILE.write_text("Prynne\nKubernetes\n")
    return calls


def test_a_decode_that_is_only_listed_terms_over_noise_falls_back_to_the_plain_one(
    monkeypatch, tmp_path
):
    """Measured 2026-11-10: on noise the prompted decoder wrote "Prynne," where the
    plain one wrote "you" (7 of 45 clips, none with this check)."""
    calls = _echo_pipeline(monkeypatch, tmp_path, " Prynne,", " you")
    assert _decode(_transcriber(monkeypatch, "CPU")) == " you"
    assert len(calls) == 2


def test_a_listed_term_spoken_alone_is_kept(monkeypatch, tmp_path):
    calls = _echo_pipeline(monkeypatch, tmp_path, " Kubernetes.", " Cooper Netties.")
    assert _decode(_transcriber(monkeypatch, "CPU")) == " Kubernetes."
    assert len(calls) == 2


def test_ordinary_speech_is_decoded_once(monkeypatch, tmp_path):
    calls = _echo_pipeline(monkeypatch, tmp_path, " I deployed Kubernetes today.", " unused")
    assert _decode(_transcriber(monkeypatch, "CPU")) == " I deployed Kubernetes today."
    assert len(calls) == 1


def test_a_prompted_decode_that_loops_over_noise_falls_back_to_the_plain_one(
    pipeline, step, monkeypatch
):
    """Measured 2026-11-11 on the NPU: barking alone, prompted, came back as one listed
    name and then "Cebu" 48 times. Not only listed terms, so the #175 check missed it."""
    step["says"] = " Xochitl, " + ", ".join(["Cebu"] * 48)
    config.VOCABULARY_FILE.write_text("Xochitl\n")
    assert _decode(_transcriber(monkeypatch, "NPU")) == "ok"
    assert len(pipeline) == 1 and "hotwords" not in pipeline[0]
