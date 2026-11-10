"""
`aw-timelog` keeps the ActivityWatch + TimeLog bundle current on its own.

The installer CI writes `timelog-version.txt` at the bundle's root. Every few hours, or when the tray asks,
the module reads the newest `timelog-v*` release, downloads its setup, checks the SHA-256 GitHub publishes for
it, and starts it silent and detached: Inno Setup closes ActivityWatch, replaces the files and starts it again.
A pip install has no version file and never updates itself.
"""

import hashlib
import logging
import os
import re
import subprocess
import sys
import tempfile
import time
from typing import Callable, Optional, Tuple

import requests

logger = logging.getLogger(__name__)

RELEASES_URL = "https://api.github.com/repos/WAB-Studio/activitywatch/releases/latest"
VERSION_FILE = "timelog-version.txt"
REQUEST_FILE = "aw-timelog-update-request"
CHECK_SECONDS = 6 * 3600
TAG = re.compile(r"^timelog-v(\d+)\.(\d+)\.(\d+)$")
NUMBER = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")

Version = Tuple[int, int, int]


def bundle_root() -> Optional[str]:
    """`<root>/aw-client/aw-timelog.exe` in the bundle; `None` outside a frozen build."""
    if not getattr(sys, "frozen", False):
        return None
    return os.path.dirname(os.path.dirname(os.path.abspath(sys.executable)))


def installed_version(root: Optional[str]) -> Optional[Version]:
    """A branch build writes «dev», which never updates: it is someone's test."""
    if not root:
        return None
    try:
        with open(os.path.join(root, VERSION_FILE), encoding="utf-8") as file:
            match = NUMBER.match(file.read().strip())
    except OSError:
        return None
    return tuple(int(part) for part in match.groups()) if match else None  # type: ignore[return-value]


def latest_release(get: Callable = requests.get) -> Optional[dict]:
    """The newest release's version, setup address and digest, or `None` when it is not one of ours."""
    response = get(RELEASES_URL, headers={"Accept": "application/vnd.github+json"}, timeout=30)
    response.raise_for_status()
    release = response.json()
    match = TAG.match(str(release.get("tag_name", "")))
    if not match:
        return None
    for asset in release.get("assets", []):
        digest = str(asset.get("digest") or "")
        if asset.get("name", "").endswith("-setup.exe") and digest.startswith("sha256:"):
            return {
                "version": tuple(int(part) for part in match.groups()),
                "url": asset["browser_download_url"],
                "sha256": digest[len("sha256:"):].lower(),
            }
    return None


def download(url: str, sha256: str, folder: str, get: Callable = requests.get) -> str:
    """The setup on disk, refused unless its bytes hash to what the release says."""
    path = os.path.join(folder, "activitywatch-timelog-setup.exe")
    digest = hashlib.sha256()
    with get(url, stream=True, timeout=120) as response:
        response.raise_for_status()
        with open(path, "wb") as file:
            for chunk in response.iter_content(chunk_size=1 << 16):
                digest.update(chunk)
                file.write(chunk)
    if digest.hexdigest() != sha256:
        os.remove(path)
        raise ValueError("the downloaded setup does not match the release's SHA-256")
    return path


def launch(setup: str, popen: Callable = subprocess.Popen) -> None:
    """Detached, so the setup outlives this process when it closes ActivityWatch."""
    log = os.path.join(os.path.dirname(setup), "setup.log")
    flags = 0x00000008 | 0x00000200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    popen(
        [setup, "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/CLOSEAPPLICATIONS", "/FORCECLOSEAPPLICATIONS", f"/LOG={log}"],
        creationflags=flags,
        close_fds=True,
    )


def update_if_newer(root: Optional[str], get: Callable = requests.get, popen: Callable = subprocess.Popen) -> str:
    """What happened, in a word the log and the tests read."""
    current = installed_version(root)
    if current is None:
        return "unmanaged"
    release = latest_release(get)
    if release is None:
        return "no-release"
    if release["version"] <= current:
        return "current"
    folder = tempfile.mkdtemp(prefix="aw-timelog-update-")
    setup = download(release["url"], release["sha256"], folder, get)
    logger.info(f"Updating TimeLog {'.'.join(map(str, current))} -> {'.'.join(map(str, release['version']))}")
    launch(setup, popen)
    return "updating"


class Checker:
    """Checks on start, then every `CHECK_SECONDS`, and at once when the tray leaves its request file."""

    def __init__(self, config_dir: str, root: Optional[str], enabled: Callable[[], bool], clock: Callable[[], float] = time.time):
        self.request = os.path.join(config_dir, REQUEST_FILE)
        self.root = root
        self.enabled = enabled
        self.clock = clock
        self.last: Optional[float] = None

    def requested(self) -> bool:
        return os.path.exists(self.request)

    def due(self) -> bool:
        return self.requested() or self.last is None or self.clock() - self.last >= CHECK_SECONDS

    def tick(self, get: Callable = requests.get, popen: Callable = subprocess.Popen) -> Optional[str]:
        if not self.due():
            return None
        asked = self.requested()
        try:
            os.remove(self.request)
        except OSError:
            pass
        self.last = self.clock()
        if not asked and not self.enabled():
            return "disabled"
        try:
            outcome = update_if_newer(self.root, get, popen)
        except Exception as error:
            logger.warning(f"Could not check for a TimeLog update: {error}")
            return "failed"
        if outcome != "updating":
            logger.info(f"TimeLog update check: {outcome}")
        return outcome
