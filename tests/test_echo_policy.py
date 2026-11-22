"""When echo cancellation runs by itself (#216 step 3): speakers get it, personal outputs don't,
and a filter-chain sink is judged by the device it plays into."""

from vinowhisper import echo_policy as policy
from vinowhisper.echo_policy import AUTO, OFF, ON, Output


def _sink(name, port=None, port_type="", **props):
    ports = [{"name": port, "type": port_type}] if port else []
    return {"name": name, "active_port": port, "ports": ports, "properties": props}


def _node(node_id, **props):
    return {"id": node_id, "type": "PipeWire:Interface:Node", "info": {"props": props}}


def _link(source, target):
    info = {"output-node-id": source, "input-node-id": target}
    return {"id": 900 + source, "type": "PipeWire:Interface:Link", "info": info}


ALSA = "alsa_output.pci-0000_00_1f.3.analog-stereo"


def _chain_into(real_sink):
    dump = [
        _node(10, **{"node.name": "effect_input.speaker_eq_v2", "node.link-group": "eq"}),
        _node(
            11,
            **{
                "node.name": "effect_output.speaker_eq_v2",
                "media.class": "Stream/Output/Audio",
                "node.link-group": "eq",
            },
        ),
        _node(12, **{"node.name": real_sink["name"]}),
        _link(11, 12),
    ]
    return dump, [_sink("effect_input.speaker_eq_v2"), real_sink]


def test_built_in_speakers_and_hdmi_keep_echo_cancellation_on():
    speakers = _sink(ALSA, "analog-output-speaker", "Speaker", **{"device.api": "alsa"})
    hdmi = _sink("alsa_output.hdmi-stereo", "hdmi-output-0", "HDMI", **{"device.api": "alsa"})
    for sink in (speakers, hdmi):
        output = policy.resolve(sink["name"], [], [sink])
        assert policy.decide(AUTO, output) == policy.Decision(True, "speakers")


def test_bluetooth_usb_and_the_headphone_jack_turn_it_off_and_say_why():
    cases = {
        "Bluetooth": _sink(
            "bluez_output.AA", "headset-output", "Headset", **{"device.api": "bluez5"}
        ),
        "USB audio": _sink(
            "alsa_output.usb-Dock", "analog-output", "Line", **{"device.bus": "usb"}
        ),
        "headphones": _sink(
            ALSA, "analog-output-headphones", "Headphones", **{"device.api": "alsa"}
        ),
    }
    for reason, sink in cases.items():
        decision = policy.decide(AUTO, policy.resolve(sink["name"], [], [sink]))
        assert decision == policy.Decision(False, reason)


def test_bluetooth_speakers_count_as_bluetooth():
    sink = _sink("bluez_output.BB", "speaker-output", "Speaker", **{"device.api": "bluez5"})
    assert policy.decide(AUTO, policy.resolve(sink["name"], [], [sink])).active is False


def test_a_filter_chain_sink_is_judged_by_the_device_it_plays_into():
    speakers = _sink(ALSA, "analog-output-speaker", "Speaker", **{"device.api": "alsa"})
    dump, sinks = _chain_into(speakers)
    output = policy.resolve("effect_input.speaker_eq_v2", dump, sinks)
    assert output.sink == ALSA and output.via == ("effect_input.speaker_eq_v2",)
    assert policy.decide(AUTO, output).active is True

    buds = _sink("bluez_output.CC", "headset-output", "Headset", **{"device.api": "bluez5"})
    dump, sinks = _chain_into(buds)
    assert policy.decide(AUTO, policy.resolve("effect_input.speaker_eq_v2", dump, sinks)) == (
        policy.Decision(False, "Bluetooth")
    )


def test_a_chain_that_goes_nowhere_is_not_followed_forever():
    dump = [
        _node(10, **{"node.name": "effect_input.loop", "node.link-group": "g"}),
        _node(
            11,
            **{
                "node.name": "effect_output.loop",
                "media.class": "Stream/Output/Audio",
                "node.link-group": "g",
            },
        ),
        _link(11, 10),
    ]
    output = policy.resolve("effect_input.loop", dump, [_sink("effect_input.loop")])
    assert output.sink == "effect_input.loop"


def test_on_and_off_ignore_the_output():
    buds = Output(sink="bluez_output.AA", api="bluez5")
    assert policy.decide(ON, buds).active is True
    assert policy.decide(OFF, None).active is False
    assert policy.decide(OFF, Output(sink=ALSA)).reason == "off in settings"


def test_an_output_nothing_could_describe_is_assumed_to_be_speakers():
    assert policy.decide(AUTO, None).active is True


def test_the_settings_line_says_what_auto_chose():
    assert policy.decide(AUTO, Output(api="bluez5")).label(AUTO) == "off: Bluetooth"
    assert policy.decide(AUTO, Output(sink=ALSA)).label(AUTO) == "on: speakers"
    assert policy.decide(OFF, None).label(OFF) == "off in settings"


def test_a_change_of_sink_or_port_is_a_different_output():
    jack = Output(sink=ALSA, port="analog-output-speaker")
    assert jack.key != Output(sink=ALSA, port="analog-output-headphones").key
    assert jack.key == Output(sink=ALSA, port="analog-output-speaker", via=("x",)).key
