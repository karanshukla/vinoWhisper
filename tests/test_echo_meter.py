"""The headphones hint (#216 step 9): only when their voice is audibly in the mic while you are not
speaking, for long enough, and not again for a while."""

import numpy as np

from tests import pcm
from vinowhisper.echo_meter import REPEAT_AFTER_S, EchoMeter

SECONDS = 8.0


def _loud_echo(meter, mic_level):
    ref = pcm.noise(SECONDS, amplitude=0.2, seed=3)
    mic = pcm.noise(SECONDS, amplitude=mic_level, seed=4)
    gate = np.zeros_like(mic)
    meter.push(mic, ref, gate)


def test_their_voice_in_the_mic_while_you_are_silent_earns_the_hint_once():
    meter = EchoMeter()
    _loud_echo(meter, mic_level=0.1)

    assert meter.hint(now_s=10.0) is not None
    assert meter.hint(now_s=11.0) is None
    assert meter.hint(now_s=10.0 + REPEAT_AFTER_S + 1) is not None


def test_a_quiet_mic_while_they_talk_is_a_clean_setup_and_says_nothing():
    meter = EchoMeter()
    _loud_echo(meter, mic_level=0.001)

    assert meter.hint(now_s=10.0) is None


def test_you_talking_over_them_is_not_echo():
    meter = EchoMeter()
    ref = pcm.noise(SECONDS, amplitude=0.2, seed=3)
    mic = pcm.noise(SECONDS, amplitude=0.1, seed=4)
    meter.push(mic, ref, gate=mic)

    assert meter.hint(now_s=10.0) is None


def test_a_few_seconds_of_far_end_is_not_enough_to_judge():
    meter = EchoMeter()
    ref = pcm.noise(2.0, amplitude=0.2, seed=3)
    mic = pcm.noise(2.0, amplitude=0.1, seed=4)
    meter.push(mic, ref, np.zeros_like(mic))

    assert meter.hint(now_s=10.0) is None
