"""Digest pinning for the model export.

No OpenVINO here and none needed: the module is stdlib hashing plus a regex
over the rt_info block every OpenVINO IR carries, so a handful of files in
tmp_path is a faithful stand-in for a 1.5GB export.

The two behaviours worth defending are that an unpinned export is not an
error, and that a toolchain bump is reported differently from the same
toolchain producing different bytes. Both are characterization tests below.
"""

import json
import tomllib
from pathlib import Path

import pytest

from vinowhisper import config, integrity

# A stripped IR tail. Real exports put rt_info at the end of the graph, after
# several hundred KB of layers, which is why the reader seeks rather than
# reads the whole file.
_RT_INFO = """\
	<rt_info>
		<info name="OpenVINO Runtime" value="{runtime}" />
		<Runtime_version value="{runtime}" />
		<conversion_parameters>
			<framework value="pytorch" />
		</conversion_parameters>
		<optimum>
			<optimum_intel_version value="{intel}" />
			<optimum_version value="{optimum}" />
			<pytorch_version value="2.13.0" />
			<transformers_version value="5.0.0" />
		</optimum>
	</rt_info>
</net>
"""


def make_export(
    directory: Path,
    weights: bytes = b"weights",
    runtime: str = "2026.2.1-21919",
    intel: str = "2.0.0",
    optimum: str = "2.2.0",
) -> Path:
    """An export directory shaped like optimum's: flat, .xml/.bin, plus JSON."""
    directory.mkdir(parents=True, exist_ok=True)
    graph = '<?xml version="1.0"?>\n<net name="Model0" version="11">\n' + "\t<layers/>\n" * 200
    for stem in ("openvino_encoder_model", "openvino_decoder_with_past_model"):
        (directory / f"{stem}.xml").write_text(
            graph + _RT_INFO.format(runtime=runtime, intel=intel, optimum=optimum),
            encoding="utf-8",
        )
        (directory / f"{stem}.bin").write_bytes(weights)
    (directory / "generation_config.json").write_text('{"max_length": 448}', encoding="utf-8")
    return directory


def pin_for(directory: Path, variant: str = "npu", model_id: str = config.MODEL_ID, **extra):
    """A pin table holding exactly this export. `pins={}` keeps the shipped
    file out of it, so these tests never depend on the real digests."""
    pin = integrity.record(directory, variant, model_id, recorded="2026-09-04", pins={})
    if extra:
        pin = integrity.Pin(**{**pin.as_json(), **extra})
    return {f"{model_id}/{variant}": pin}


# --- reading the export ---------------------------------------------------


def test_every_file_in_the_export_is_digested(tmp_path):
    """Not just the weights: generation_config decides how decoding behaves."""
    digests = integrity.digest_export(make_export(tmp_path / "npu"))
    assert set(digests) == {
        "openvino_encoder_model.xml",
        "openvino_encoder_model.bin",
        "openvino_decoder_with_past_model.xml",
        "openvino_decoder_with_past_model.bin",
        "generation_config.json",
    }
    assert all(len(value) == 64 for value in digests.values())


def test_dotfiles_are_skipped(tmp_path):
    directory = make_export(tmp_path / "npu")
    (directory / ".DS_Store").write_bytes(b"junk")
    assert ".DS_Store" not in integrity.digest_export(directory)


def test_the_toolchain_is_read_out_of_the_export_itself(tmp_path):
    """rt_info is how a drifting export can name the versions that made it."""
    directory = make_export(tmp_path / "npu", runtime="2026.3.1-22476", intel="2.1.0")
    found = integrity.read_toolchain(directory)
    assert found["OpenVINO Runtime"] == "2026.3.1-22476"
    assert found["optimum_intel_version"] == "2.1.0"
    assert found["transformers_version"] == "5.0.0"


def test_an_export_with_no_readable_rt_info_reports_nothing(tmp_path):
    directory = tmp_path / "npu"
    directory.mkdir()
    (directory / "openvino_encoder_model.xml").write_text("<net/>", encoding="utf-8")
    assert integrity.read_toolchain(directory) == {}


# --- verification ---------------------------------------------------------


def test_an_untouched_export_verifies(tmp_path):
    directory = make_export(tmp_path / "npu")
    result = integrity.verify(directory, "npu", pins=pin_for(directory))
    assert result.status == integrity.VERIFIED
    assert not result.severe
    assert not result.differing


def test_characterization_an_unpinned_export_warns_and_continues(tmp_path):
    """characterization: no pin is not a failure, on purpose.

    This project ships one pinned export. Failing hard on an unrecognised
    (model, variant) would make `--model openai/whisper-base.en` unusable the
    first time anyone tried it, and would make the stateful export, which has
    never been produced on this hardware, look broken rather than unpinned.
    If this ever goes red because UNPINNED became severe, the question is
    whether every unpinned export was meant to stop working.
    """
    directory = make_export(tmp_path / "npu")
    result = integrity.verify(directory, "npu", pins={})
    assert result.status == integrity.UNPINNED
    assert not result.severe
    assert integrity.UNPINNED not in integrity.SEVERE


def test_an_entry_with_no_files_reads_as_unpinned(tmp_path):
    """How the stateful export ships: present in the file, empty, honest."""
    directory = make_export(tmp_path / "stateful")
    pins = {
        f"{config.MODEL_ID}/stateful": integrity.Pin(
            model_id=config.MODEL_ID, variant="stateful", note="never produced"
        )
    }
    assert integrity.verify(directory, "stateful", pins=pins).status == integrity.UNPINNED


def test_changed_weights_on_the_same_toolchain_are_a_mismatch(tmp_path):
    directory = make_export(tmp_path / "npu")
    pins = pin_for(directory)
    make_export(directory, weights=b"different weights")

    result = integrity.verify(directory, "npu", pins=pins)
    assert result.status == integrity.MISMATCH
    assert result.severe
    assert "openvino_encoder_model.bin" in result.differing
    assert "SAME toolchain" in result.summary()


def test_characterization_a_toolchain_bump_is_drift_not_a_mismatch(tmp_path):
    """characterization: different bytes AND different versions is not an alarm.

    Measured 2026-09-04 on the real export: re-exporting whisper-small.en
    under OpenVINO 2026.3.1 / optimum-intel 2.1.0 changed all six .xml graphs
    and both decoder .bin files against the 2026.2.1 / 2.0.0 export, while
    openvino_encoder_model.bin came out byte-identical. So a pin outlives at
    most one toolchain, and if a version bump raised the same alarm as tampered
    weights, the alarm would be trained away by the second optimum release.
    Collapsing DRIFT into MISMATCH is what breaks if this is "fixed".
    """
    directory = make_export(tmp_path / "npu")
    pins = pin_for(directory)
    make_export(directory, weights=b"rebuilt", runtime="2026.4.0-1", intel="2.2.0")

    result = integrity.verify(directory, "npu", pins=pins)
    assert result.status == integrity.DRIFT
    assert not result.severe
    assert "2026.2.1 -> 2026.4.0" in result.summary()


def test_an_unknown_local_toolchain_cannot_excuse_changed_bytes(tmp_path):
    """No rt_info means no comparison, so it does not get the benefit of doubt."""
    directory = make_export(tmp_path / "npu")
    pins = pin_for(directory)
    make_export(directory, weights=b"rebuilt")
    for path in directory.glob("*.xml"):
        path.write_text("<net/>", encoding="utf-8")

    assert integrity.verify(directory, "npu", pins=pins).status == integrity.MISMATCH


def test_a_missing_pinned_file_is_a_partial_export(tmp_path):
    directory = make_export(tmp_path / "npu")
    pins = pin_for(directory)
    (directory / "openvino_decoder_with_past_model.bin").unlink()

    result = integrity.verify(directory, "npu", pins=pins)
    assert result.status == integrity.INCOMPLETE
    assert result.severe
    assert "openvino_decoder_with_past_model.bin" in result.missing


def test_a_file_the_pin_does_not_know_about_is_reported_not_failed(tmp_path):
    directory = make_export(tmp_path / "npu")
    pins = pin_for(directory)
    (directory / "notes.txt").write_text("mine", encoding="utf-8")

    result = integrity.verify(directory, "npu", pins=pins)
    assert result.status == integrity.VERIFIED
    assert result.unpinned_files == ("notes.txt",)


def test_the_graph_and_the_weights_are_named_separately(tmp_path):
    """An .xml-only difference and a changed .bin mean different things."""
    directory = make_export(tmp_path / "npu")
    pins = pin_for(directory)
    for path in directory.glob("*.xml"):
        path.write_text(path.read_text(encoding="utf-8") + "<!-- -->", encoding="utf-8")

    result = integrity.verify(directory, "npu", pins=pins)
    assert "graph only" in result.summary()


# --- toolchains measured to produce a broken export ------------------------

_BAD = [{"match": {"optimum_intel_version": "2.1.0"}, "reason": "cache_position"}]


def test_a_known_bad_export_toolchain_is_severe(tmp_path):
    """The case this exists for: bytes are fine, the toolchain is not.

    Measured 2026-09-04: optimum-intel 2.1.0 / optimum 2.3.0 / transformers
    5.5.4 exports a graph the NPU static pipeline compiles and then cannot
    run (`Port for tensor name cache_position was not found`). Reporting that
    as ordinary drift would tell someone their broken model is fine.
    """
    directory = make_export(tmp_path / "npu")
    pins = pin_for(directory, known_bad=_BAD)
    make_export(directory, weights=b"rebuilt", intel="2.1.0")

    result = integrity.verify(directory, "npu", pins=pins)
    assert result.status == integrity.KNOWN_BAD
    assert result.severe
    assert "cache_position" in result.summary()


def test_a_known_bad_toolchain_still_fires_when_nothing_is_pinned(tmp_path):
    """An entry can carry the list without carrying digests."""
    directory = make_export(tmp_path / "stateful", intel="2.1.0")
    pins = {
        f"{config.MODEL_ID}/stateful": integrity.Pin(
            model_id=config.MODEL_ID, variant="stateful", known_bad=_BAD
        )
    }
    assert integrity.verify(directory, "stateful", pins=pins).status == integrity.KNOWN_BAD


def test_a_partial_toolchain_overlap_does_not_fire(tmp_path):
    """Every key in `match` has to agree, or a shared minor version alarms."""
    directory = make_export(tmp_path / "npu")
    bad = [{"match": {"optimum_intel_version": "2.1.0", "transformers_version": "5.5.4"}}]
    pins = pin_for(directory, known_bad=bad)
    make_export(directory, weights=b"rebuilt", intel="2.1.0")

    assert integrity.verify(directory, "npu", pins=pins).status == integrity.DRIFT


def test_the_pinned_toolchain_itself_is_never_known_bad(tmp_path):
    """A verifying export cannot be reported as broken, whatever the list says."""
    directory = make_export(tmp_path / "npu", intel="2.1.0")
    pins = pin_for(directory, known_bad=_BAD)
    assert integrity.verify(directory, "npu", pins=pins).status == integrity.VERIFIED


def test_regenerating_a_pin_keeps_the_known_bad_list(tmp_path):
    """It is hand-written from measurement; re-hashing must not drop it."""
    directory = make_export(tmp_path / "npu")
    pins = pin_for(directory, known_bad=_BAD)
    fresh = integrity.record(directory, "npu", config.MODEL_ID, "2026-10-01", pins=pins)
    assert fresh.known_bad == _BAD


def test_the_shipped_pin_names_the_toolchain_that_breaks_the_npu_export():
    """Regression guard on the finding, not just on the mechanism."""
    pin = integrity.load_pins()[f"{config.MODEL_ID}/npu"]
    reason = pin.bad_toolchain(
        {
            "optimum_intel_version": "2.1.0",
            "optimum_version": "2.3.0",
            "transformers_version": "5.5.4",
        }
    )
    assert "cache_position" in reason


# --- every failure names its fix ------------------------------------------


@pytest.mark.parametrize(
    "status",
    [
        integrity.MISMATCH,
        integrity.DRIFT,
        integrity.INCOMPLETE,
        integrity.UNPINNED,
        integrity.KNOWN_BAD,
    ],
)
def test_every_non_verified_status_prints_a_command(tmp_path, status):
    result = integrity.Verification(
        status=status,
        model_id=config.MODEL_ID,
        variant="npu",
        directory=tmp_path,
        differing=("openvino_encoder_model.bin",),
        missing=("openvino_encoder_model.bin",) if status == integrity.INCOMPLETE else (),
    )
    text = "\n".join(result.lines())
    assert (
        "update_digests.py" in text
        or "convert_model.sh" in text
        or "setup" in text
        or "pinned export was produced by" in text
    )


# --- the pin file itself --------------------------------------------------


def test_the_shipped_pin_file_parses_and_covers_both_variants():
    pins = integrity.load_pins()
    assert f"{config.MODEL_ID}/npu" in pins
    assert f"{config.MODEL_ID}/stateful" in pins


def test_the_npu_export_is_actually_pinned():
    """The whole point. An empty npu entry would silently verify nothing."""
    pin = integrity.load_pins()[f"{config.MODEL_ID}/npu"]
    assert pin.pinned
    assert pin.recorded, "a pin with no date cannot be judged stale"
    assert pin.toolchain.get("OpenVINO Runtime"), "a pin with no toolchain cannot detect drift"
    assert "openvino_encoder_model.bin" in pin.files


def test_the_stateful_export_is_unpinned_and_says_why():
    """It has never been produced on this hardware, so there is nothing to pin."""
    pin = integrity.load_pins()[f"{config.MODEL_ID}/stateful"]
    assert not pin.pinned
    assert pin.note


def test_a_missing_pin_file_degrades_to_unpinned(tmp_path):
    """A wheel built without the data file must not break every export."""
    assert integrity.load_pins(tmp_path / "absent.json") == {}


def test_a_corrupt_pin_file_degrades_to_unpinned(tmp_path):
    path = tmp_path / "model_digests.json"
    path.write_text("{ not json", encoding="utf-8")
    assert integrity.load_pins(path) == {}


def test_the_pin_file_is_declared_as_package_data():
    """load_pins() swallows a missing file, so a packaging slip is silent."""
    config_text = Path("pyproject.toml").read_bytes()
    package_data = tomllib.loads(config_text.decode())["tool"]["setuptools"]["package-data"]
    assert "model_digests.json" in package_data["vinowhisper"]
    assert integrity.PINS_PATH.is_file()


def test_writing_a_pin_leaves_the_other_entries_alone(tmp_path):
    path = tmp_path / "model_digests.json"
    directory = make_export(tmp_path / "npu")
    integrity.write_pin(integrity.record(directory, "npu", "a/model", "2026-09-04"), path)
    integrity.write_pin(integrity.record(directory, "stateful", "b/model", "2026-09-04"), path)

    written = json.loads(path.read_text(encoding="utf-8"))
    assert set(written["exports"]) == {"a/model/npu", "b/model/stateful"}
    assert written["schema"] == integrity.SCHEMA


# --- the toolchain is read from every graph, not one -----------------------


def test_the_toolchain_merges_what_each_graph_knows(tmp_path):
    """optimum stamps the model graphs; openvino-tokenizers stamps the pair."""
    directory = make_export(tmp_path / "npu")
    (directory / "openvino_tokenizer.xml").write_text(
        '<net name="t">\n\t<rt_info>\n'
        '\t\t<info name="OpenVINO Runtime" value="2026.2.1-21919" />\n'
        '\t\t<tokenizers_version value="0.22.2" />\n'
        "\t</rt_info>\n</net>\n",
        encoding="utf-8",
    )
    found = integrity.read_toolchain(directory)
    assert found["optimum_intel_version"] == "2.0.0"
    assert found["tokenizers_version"] == "0.22.2"


def test_characterization_graphs_disagreeing_makes_the_toolchain_unknown(tmp_path):
    """characterization: one relabelled graph must not buy a softer verdict.

    `drift` warns and `mismatch` fails, and the difference between them is
    decided by version strings sitting in the same directory as the weights.
    Reading a single .xml meant editing a single .xml relabelled the whole
    export. Disagreement now reads as unknown, and an unknown toolchain
    cannot excuse changed bytes, so the verdict falls back to `mismatch`.
    Verified on the real 1.5GB export 2026-09-04, by rewriting one graph's
    rt_info and leaving the other four alone.
    """
    directory = make_export(tmp_path / "npu")
    pins = pin_for(directory)
    make_export(directory, weights=b"tampered")
    graph = directory / "openvino_encoder_model.xml"
    graph.write_text(
        graph.read_text(encoding="utf-8").replace('value="2.0.0"', 'value="9.9.9"'),
        encoding="utf-8",
    )

    assert integrity.read_toolchain(directory) == {}
    assert integrity.verify(directory, "npu", pins=pins).status == integrity.MISMATCH
