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


# --- Image-based systems ---------------------------------------------------
#
# Fedora Atomic (Silverblue, Kinoite, Bazzite) and openSUSE Aeon keep /usr
# read-only, so `dnf install` or `zypper install` fails outright there.


def _image_root(tmp_path, *markers):
    for marker in markers:
        path = tmp_path / marker
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("")
    return tmp_path


def test_fedora_atomic_is_detected_from_the_ostree_marker(fake_os_release, tmp_path):
    path = fake_os_release(
        ID="fedora", VARIANT_ID="kinoite", PRETTY_NAME="Fedora Linux 45 (Kinoite)"
    )
    info = distro.detect(path, root=_image_root(tmp_path / "root", "run/ostree-booted"))
    assert (info.family, info.image) == ("fedora", distro.IMAGE_OSTREE)
    assert "image-based" in str(info)


def test_a_plain_install_is_not_image_based(fake_os_release, tmp_path):
    path = fake_os_release(ID="fedora", PRETTY_NAME="Fedora Linux 45")
    info = distro.detect(path, root=tmp_path)
    assert not info.image_based
    assert (
        distro.install_command(distro.AUDIO_PIPEWIRE, info) == "sudo dnf install -y pipewire-utils"
    )


def test_fedora_atomic_layers_instead_of_dnf():
    info = distro.Distro(id="fedora", name="Kinoite", version="45", family="fedora", image="ostree")
    remedy = distro.remediation(distro.GPU_RUNTIME, info)
    assert remedy.commands == ("sudo rpm-ostree install --idempotent intel-compute-runtime",)
    assert any("reboot" in note for note in remedy.notes)
    assert distro.install_needs_reboot(info)


def test_aeon_is_detected_only_when_usr_is_read_only(monkeypatch, fake_os_release, tmp_path):
    path = fake_os_release(
        ID="opensuse-microos", ID_LIKE="suse opensuse", PRETTY_NAME="openSUSE Aeon"
    )
    root = _image_root(tmp_path / "root", "usr/sbin/transactional-update")
    monkeypatch.setattr(distro, "_read_only", lambda path: False)
    assert not distro.detect(path, root=root).image_based
    monkeypatch.setattr(distro, "_read_only", lambda path: True)
    info = distro.detect(path, root=root)
    assert (info.family, info.image) == ("suse", distro.IMAGE_TRANSACTIONAL)
    command = distro.install_command(distro.AUDIO_PIPEWIRE, info)
    assert command == "sudo transactional-update --non-interactive pkg install pipewire-tools"


def test_an_image_without_a_known_layering_tool_gets_notes_not_a_command():
    info = distro.Distro(
        id="endless", name="Endless OS", version="", family="debian", image="ostree"
    )
    remedy = distro.remediation(distro.AUDIO_PIPEWIRE, info)
    assert remedy.commands == ()
    assert any("read-only" in note and "pipewire-bin" in note for note in remedy.notes)
    assert not distro.install_needs_reboot(info)


def test_the_npu_compiler_steps_avoid_read_only_usr():
    info = distro.Distro(
        id="fedora", name="Silverblue", version="45", family="fedora", image="ostree"
    )
    image_steps = "\n".join(distro.remediation(distro.NPU_COMPILER, info).notes)
    assert "/usr/local/lib64" in image_steps and "ld.so.conf.d" in image_steps
    assert " /usr/lib64/" not in image_steps
    plain = distro.Distro(id="fedora", name="Fedora", version="45", family="fedora")
    assert "/usr/lib64/" in "\n".join(distro.remediation(distro.NPU_COMPILER, plain).notes)


def test_universal_blue_images_are_fedora(fake_os_release):
    for distro_id in ("bazzite", "aurora", "bluefin"):
        path = fake_os_release(ID=distro_id, PRETTY_NAME=distro_id)
        assert distro.detect(path).family == "fedora", distro_id
