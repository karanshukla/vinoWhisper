"""os-release parsing and the package-name table.

The point of these is that a wrong answer here is invisible until someone on
another distro runs the tool and gets told to install a package that doesn't
exist. The mapping is data, so it can at least be checked for shape.
"""

from vinowhisper import distro


def test_fedora_is_recognised(fake_os_release):
    path = fake_os_release(ID="fedora", VERSION_ID="42", PRETTY_NAME="Fedora Linux 42 (KDE)")
    info = distro.detect(path)
    assert (info.id, info.family, info.version) == ("fedora", "fedora", "42")
    assert info.known
    assert "Fedora" in str(info)


def test_id_like_is_the_fallback_for_derivatives(fake_os_release):
    path = fake_os_release(ID="some-remix", ID_LIKE="ubuntu debian", PRETTY_NAME="Some Remix")
    assert distro.detect(path).family == "debian"


def test_unknown_distro_degrades_instead_of_raising(fake_os_release):
    path = fake_os_release(ID="plan9", PRETTY_NAME="Not Linux Really")
    info = distro.detect(path)
    assert not info.known
    assert info.family == "unknown"
    remedy = distro.remediation(distro.AUDIO_PIPEWIRE, info)
    assert remedy.commands == ()
    assert any("Unrecognised" in note for note in remedy.notes)


def test_missing_os_release_never_raises(tmp_path):
    info = distro.detect(tmp_path / "nope")
    assert info.family == "unknown"
    assert info.name == "unknown Linux"


def test_quoted_and_commented_values_parse(fake_os_release):
    path = fake_os_release(ID="arch", PRETTY_NAME="Arch Linux")
    assert distro.detect(path).family == "arch"


def test_each_family_can_install_a_capture_tool():
    """Every family we claim to support must answer the audio question."""
    for family in ("fedora", "debian", "arch", "suse", "void", "gentoo", "alpine"):
        info = distro.Distro(id=family, name=family, version="", family=family)
        command = distro.install_command(distro.AUDIO_PIPEWIRE, info)
        assert command and command.startswith("sudo "), family


def test_nixos_gets_configuration_advice_not_an_imperative_command():
    """Imperative installs do not persist there, so a command would be wrong."""
    info = distro.Distro(id="nixos", name="NixOS", version="", family="nixos")
    assert distro.install_command(distro.AUDIO_PIPEWIRE, info) is None
    remedy = distro.remediation(distro.AUDIO_PIPEWIRE, info)
    assert any("services.pipewire" in note for note in remedy.notes)


def test_npu_remediation_always_carries_the_upstream_url():
    """No distro reliably packages the NPU userspace, so this is the constant."""
    for family in ("fedora", "debian", "arch", "suse", "unknown"):
        info = distro.Distro(id=family, name=family, version="", family=family)
        remedy = distro.remediation(distro.NPU_DRIVER, info)
        assert remedy.url == distro.NPU_RELEASES_URL
        assert remedy.actionable
        assert remedy.lines()


def test_debian_has_no_npu_package_and_says_so():
    info = distro.Distro(id="ubuntu", name="Ubuntu", version="24.04", family="debian")
    remedy = distro.remediation(distro.NPU_DRIVER, info)
    assert remedy.commands == ()
    assert any("intel-level-zero-npu" in note for note in remedy.notes)
