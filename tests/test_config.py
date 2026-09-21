from vinowhisper import config


def test_the_token_cap_clears_the_densest_measured_speech():
    """Measured 2026-09-21: LibriVox time-stretched to 1.75x peaked at 8.5
    tokens per second of audio, while a repetition loop ("very, very, ...")
    ran to the model's 448-token limit and stalled captions for 5.7s. The cap
    must sit above real speech with room to spare and well below the limit.
    """
    for duration_s in (1.5, 8.0, 12.0, config.MAX_WINDOW_S):
        cap = config.max_new_tokens(duration_s)
        assert cap >= 8.5 * duration_s * 1.3
    assert config.max_new_tokens(config.WINDOW_S) < 448 / 2
