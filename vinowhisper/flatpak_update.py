import contextlib
import os
import re
import shutil
import subprocess  # nosec B404 - fixed argv, never a shell
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

from . import config, launch, overlay, sandbox

REPO = "karanshukla/vinoWhisper"
# The release asset, beside BUNDLE.sha256; flatpak.yml uploads both.
BUNDLE = "vinowhisper-x86_64.flatpak"
MAX_SUMS_BYTES = 4096
SYSTEM_DIR = Path("/var/lib/flatpak")
DOWNLOAD_TIMEOUT_S = 120.0
TAG = re.compile(r"v\d+(\.\d+)*")
TRAY = "vinowhisper-tray"


class UpdateError(Exception):
    pass


def release_tag(tag: str) -> bool:
    # It ends up in a URL.
    return len(tag) <= 32 and TAG.fullmatch(tag) is not None


def same_version(tag: str, version: str) -> bool:
    def parts(text: str) -> list[int] | None:
        if not re.fullmatch(r"\d+(\.\d+)*", text):
            return None
        numbers = [int(part) for part in text.split(".")]
        while len(numbers) > 1 and numbers[-1] == 0:
            numbers.pop()
        return numbers

    wanted, have = parts(tag.removeprefix("v")), parts(version)
    return wanted is not None and wanted == have


def installation(app_path: str, app: str, home: Path) -> str:
    # `<dir>/app/<id>/...`: the update goes where this build is, not beside it.
    at = app_path.find(f"/app/{app}/")
    if at < 0:
        raise UpdateError(f"Can't tell which Flatpak installation {app_path} is in")
    directory = Path(app_path[:at])
    if directory == SYSTEM_DIR:
        return "--system"
    homes = {home}
    with contextlib.suppress(OSError):
        homes.add(home.resolve())
    if any(directory.is_relative_to(candidate) for candidate in homes):
        return "--user"
    raise UpdateError(
        f"vinoWhisper is in the Flatpak installation {directory}, which the Update button "
        f"can't update: install the release's {BUNDLE} with flatpak install"
    )


def expected_sum(sums: str, name: str) -> str | None:
    # Exactly one line must name the file: `sha256sum -c` would pass whatever it listed.
    hashes = [
        match.group(1)
        for line in sums.splitlines()
        if (match := re.fullmatch(r"([0-9a-f]{64}) [ *](.+)", line)) and match.group(2) == name
    ]
    return hashes[0] if len(hashes) == 1 else None


def fetch_sums(url: str, get: Callable[..., Any] | None = None) -> str:
    if get is None:
        import requests

        get = requests.get
    name = url.rsplit("/", 1)[-1]
    try:
        with get(
            url, stream=True, timeout=(config.CONNECT_TIMEOUT_S, DOWNLOAD_TIMEOUT_S)
        ) as response:
            response.raise_for_status()
            body = next(response.iter_content(chunk_size=MAX_SUMS_BYTES), b"")
    except OSError as exc:  # requests' exceptions are OSErrors too
        raise UpdateError(f"Download failed: {name}: {exc}") from exc
    return body.decode("utf-8", errors="replace")


def host_flatpak(*args: str) -> subprocess.CompletedProcess:
    # The host has no sandbox paths to start in.
    return subprocess.run(  # nosec B603 B607 - fixed argv
        sandbox.host_argv(["flatpak", *args]),
        cwd="/",
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        check=False,
    )


def install(scope: str, bundle: Path, tag: str) -> None:
    # A newer build of the installed branch replaces it; a sandbox still running keeps
    # the old one until it exits.
    try:
        done = host_flatpak("install", scope, "-y", "--bundle", str(bundle))
    except OSError as exc:
        raise UpdateError(f"Couldn't run flatpak-spawn: {exc}") from exc
    sys.stderr.write(done.stderr)
    if done.returncode == 0:
        return
    lines = [line.strip() for line in done.stderr.splitlines() if line.strip()]
    why = lines[-1].removeprefix("error:").strip() if lines else f"exit status {done.returncode}"
    raise UpdateError(f"flatpak couldn't install {tag}: {why}")


def installed_version(scope: str, app: str) -> str | None:
    try:
        done = host_flatpak("run", scope, f"--command={TRAY}", app, "--version")
    except OSError:
        return None
    text = done.stdout.strip()
    if done.returncode != 0 or not text.startswith(f"{TRAY} "):
        return None
    return text.removeprefix(f"{TRAY} ")


def restart() -> None:
    # As install.sh does: the server and the tray's dictation child hold the old build.
    if not launch.stop():
        print("the server runs the old build until `vinowhisper-server --stop`", file=sys.stderr)
    try:
        done = subprocess.run(  # nosec B603 B607 - fixed argv
            [TRAY, "restart"], stdin=subprocess.DEVNULL, check=False
        )
    except OSError as exc:
        print(f"the tray runs the old build until it is restarted: {exc}", file=sys.stderr)
        return
    if done.returncode != 0:
        print("the tray runs the old build until it is restarted", file=sys.stderr)


def update(tag: str, get: Callable[..., Any] | None = None) -> None:
    app = sandbox.flatpak_id()
    if app is None:
        raise UpdateError(
            "Not running in the Flatpak: a release install updates with vinowhisper-upgrade"
        )
    if not release_tag(tag):
        raise UpdateError(f"Not a release tag: {tag[:40]!r}")
    branch = sandbox.info_value("Instance", "branch") or ""
    if branch != sandbox.RELEASE_BRANCH:
        raise UpdateError(
            f"This is a {branch or 'local'} build, not a release: install the release's "
            f"{BUNDLE} with flatpak install"
        )
    app_path = sandbox.info_value("Instance", "app-path")
    if app_path is None:
        raise UpdateError("/.flatpak-info doesn't say where the app is installed")
    home = Path.home()
    scope = installation(app_path, app, home)

    # A mirror, or a test's fake release. The checksum is checked the same.
    base = os.environ.get("VINOWHISPER_RELEASE_BASE", "").rstrip("/")
    base = base or f"https://github.com/{REPO}/releases/download/{tag}"
    # The app's cache under ~/.var/app, not /tmp: the host's flatpak reads it.
    scratch = home / ".var/app" / app / "cache/vinowhisper-update"
    shutil.rmtree(scratch, ignore_errors=True)
    try:
        scratch.mkdir(parents=True)
        bundle = scratch / BUNDLE
        print(f"downloading {tag} from {base}", file=sys.stderr)
        want = expected_sum(fetch_sums(f"{base}/{BUNDLE}.sha256", get), BUNDLE)
        if want is None:
            raise UpdateError(
                f"Checksum verification failed for {BUNDLE}: its .sha256 does not name it. "
                "Nothing was installed."
            )
        try:
            # The download the tray and window binaries get: streamed, hashed, and
            # kept only if the hash is the one expected.
            overlay.download_verified(
                f"{base}/{BUNDLE}",
                want,
                bundle,
                f"Checksum verification failed for {BUNDLE}",
                f"Try again, or install the release's {BUNDLE} with flatpak install.",
                get,
            )
        except overlay.OverlayError as exc:
            raise UpdateError(str(exc)) from exc
        print("checksum verified", file=sys.stderr)
        install(scope, bundle, tag)
    except OSError as exc:
        raise UpdateError(f"Couldn't write {scratch}: {exc}") from exc
    finally:
        shutil.rmtree(scratch, ignore_errors=True)

    # Flatpak installs an older build over a newer one without a word, so the build now
    # installed is asked what it is.
    version = installed_version(scope, app)
    if version is None:
        raise UpdateError(f"{tag} is installed but doesn't run; nothing was restarted")
    if not same_version(tag, version):
        raise UpdateError(
            f"{tag}'s bundle is vinoWhisper {version}, and that is installed now; "
            "nothing was restarted"
        )
    restart()
    print(f"updated to {tag}; reopen the settings window to use it there too")


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        prog="vinowhisper-flatpak-update",
        description="The Flatpak's Update button: download a release's bundle and its "
        ".sha256, refuse the bundle unless they match, have the host's flatpak install it "
        "where this build is installed, and restart the server and the tray on it. A "
        "release install updates with vinowhisper-upgrade instead.",
    )
    parser.add_argument(
        "tag", nargs="?", help="The release, e.g. v1.8.0. Default: $VINO_UPDATE_TAG."
    )
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)
    try:
        update(args.tag or os.environ.get("VINO_UPDATE_TAG", ""))
    except UpdateError as exc:
        # The last line of stderr is what the button shows.
        print(f"✗ {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
