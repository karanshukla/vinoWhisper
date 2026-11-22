"""The text safety net (#216 step 8): a `you` line that repeats what `them` just said is the echo."""

from vinowhisper.echo_text import EchoDedup


def _w(text):
    return text.split()


class _Clock:
    now = 0.0

    def __call__(self):
        return self.now


def test_a_you_line_that_repeats_a_recent_them_line_is_dropped():
    dedup = EchoDedup(_Clock())
    dedup.heard_them(_w("So the quarterly numbers are up four percent"))

    assert dedup.echoed(_w("the quarterly numbers are up four percent"))
    assert dedup.echoed(_w("Quarterly numbers, are up four percent."))


def test_your_own_words_are_kept():
    dedup = EchoDedup(_Clock())
    dedup.heard_them(_w("So the quarterly numbers are up four percent"))

    assert not dedup.echoed(_w("Sounds good, I will send the deck tonight"))


def test_two_words_are_never_dropped():
    dedup = EchoDedup(_Clock())
    dedup.heard_them(_w("yes okay sure"))

    assert not dedup.echoed(_w("yes okay"))


def test_what_them_said_long_ago_no_longer_counts():
    clock = _Clock()
    dedup = EchoDedup(clock)
    dedup.heard_them(_w("the quarterly numbers are up four percent"))
    clock.now = 60.0

    assert not dedup.echoed(_w("the quarterly numbers are up four percent"))


def test_nothing_is_dropped_before_them_has_said_anything():
    assert not EchoDedup(_Clock()).echoed(_w("the quarterly numbers are up"))
