"""
`aw-timelog`: an ActivityWatch module that uploads to TimeLog every hour while ActivityWatch runs.

aw-qt starts it like a watcher. Its settings live in `aw-timelog.toml` in ActivityWatch's config
folder; the tray writes the key there, and the file is read again before every upload, so a key
pasted in the tray takes effect without a restart.
"""

import argparse
import logging
import os
import socket
import time
from typing import Callable, List, Optional

import tomlkit
from aw_core import dirs
from aw_core.log import setup_logging

import aw_client

from . import address_bar, gmail_inbox, timelog, updater

logger = logging.getLogger(__name__)

APP_NAME = "aw-timelog"
DEFAULT_URL = "https://timelog-delta.vercel.app"
# A failed upload waits less than a good one: the next try may find the network back.
RETRY_SECONDS = 300
# With no key yet, look for one often: the person is pasting it from the tray right now.
NO_KEY_SECONDS = 30


# The no-key notice is logged once per run, not every 30 seconds.
_warned_no_key: List[bool] = []


def config_path() -> str:
    return os.path.join(dirs.get_config_dir(APP_NAME), f"{APP_NAME}.toml")


def read_settings(path: Optional[str] = None) -> dict:
    """The file's settings over the defaults; a missing or broken file is the defaults."""
    settings = {"url": DEFAULT_URL, "key": "", "device": socket.gethostname(), "interval_minutes": 60, "days": 2, "claude_dir": "", "exclude": None, "browser_history": None, "poll_seconds": 5, "auto_update": True, "timelog_enabled": True, "gmail_enabled": True, "gmail_minutes": 10, "gmail_query": gmail_inbox.DEFAULT_QUERY, "gmail_client_id": "", "gmail_client_secret": "", "homefund_url": gmail_inbox.DEFAULT_HOMEFUND_URL, "homefund_key": ""}
    try:
        with open(path or config_path(), encoding="utf-8") as file:
            stored = tomlkit.parse(file.read()).get(APP_NAME, {})
    except (OSError, tomlkit.exceptions.TOMLKitError):
        return settings
    for name, value in stored.items():
        if name in settings and value not in ("", None):
            settings[name] = value.unwrap() if hasattr(value, "unwrap") else value
    return settings


def write_key(key: str, device: Optional[str] = None, path: Optional[str] = None) -> None:
    """Keeps every other setting in the file; the tray calls this."""
    path = path or config_path()
    try:
        with open(path, encoding="utf-8") as file:
            document = tomlkit.parse(file.read())
    except (OSError, tomlkit.exceptions.TOMLKitError):
        document = tomlkit.document()
    section = document.setdefault(APP_NAME, tomlkit.table())
    section["key"] = key.strip()
    if device:
        section["device"] = device.strip()
    with open(path, "w", encoding="utf-8") as file:
        file.write(tomlkit.dumps(document))


def write_exclude(entries: List[str], path: Optional[str] = None) -> None:
    """Keeps every other setting in the file; the tray's exclusion list calls this."""
    path = path or config_path()
    try:
        with open(path, encoding="utf-8") as file:
            document = tomlkit.parse(file.read())
    except (OSError, tomlkit.exceptions.TOMLKitError):
        document = tomlkit.document()
    section = document.setdefault(APP_NAME, tomlkit.table())
    section["exclude"] = [entry.strip().lower() for entry in entries if entry.strip()]
    with open(path, "w", encoding="utf-8") as file:
        file.write(tomlkit.dumps(document))


def write_homefund_key(key: str, path: Optional[str] = None) -> bool:
    """False for anything HomeFund would not have minted; the file is left as it was. The tray calls this."""
    key = key.strip()
    if not gmail_inbox.HOMEFUND_KEY_PATTERN.fullmatch(key):
        return False
    path = path or config_path()
    try:
        with open(path, encoding="utf-8") as file:
            document = tomlkit.parse(file.read())
    except (OSError, tomlkit.exceptions.TOMLKitError):
        document = tomlkit.document()
    document.setdefault(APP_NAME, tomlkit.table())["homefund_key"] = key
    with open(path, "w", encoding="utf-8") as file:
        file.write(tomlkit.dumps(document))
    return True


def write_gmail_client(line: str, path: Optional[str] = None) -> bool:
    """False for anything but «<client_id> <client_secret>»; the file is left as it was. The tray calls this."""
    client = gmail_inbox.parse_client(line)
    if client is None:
        return False
    path = path or config_path()
    try:
        with open(path, encoding="utf-8") as file:
            document = tomlkit.parse(file.read())
    except (OSError, tomlkit.exceptions.TOMLKitError):
        document = tomlkit.document()
    section = document.setdefault(APP_NAME, tomlkit.table())
    section["gmail_client_id"], section["gmail_client_secret"] = client
    with open(path, "w", encoding="utf-8") as file:
        file.write(tomlkit.dumps(document))
    return True


def write_enabled(name: str, enabled: bool, path: Optional[str] = None) -> None:
    """`timelog_enabled` or `gmail_enabled`, keeping every other setting; the tray's checkable entries call this."""
    if name not in ("timelog_enabled", "gmail_enabled"):
        raise ValueError(name)
    path = path or config_path()
    try:
        with open(path, encoding="utf-8") as file:
            document = tomlkit.parse(file.read())
    except (OSError, tomlkit.exceptions.TOMLKitError):
        document = tomlkit.document()
    document.setdefault(APP_NAME, tomlkit.table())[name] = bool(enabled)
    with open(path, "w", encoding="utf-8") as file:
        file.write(tomlkit.dumps(document))


def run_once(client, settings: dict) -> int:
    """Seconds until the next upload; with TimeLog off, until the tray may have turned it on."""
    if not settings["timelog_enabled"]:
        return NO_KEY_SECONDS
    if not timelog.KEY_PATTERN.fullmatch(str(settings["key"])):
        if not _warned_no_key:
            _warned_no_key.append(True)
            logger.info("No TimeLog key yet: paste one from the tray, under «TimeLog…», or turn TimeLog off there.")
        return NO_KEY_SECONDS
    try:
        result = timelog.sync(
            client,
            settings["url"],
            settings["key"],
            int(settings["days"]),
            None,
            settings["device"],
            False,
            timelog.claude_dirs_from(settings["claude_dir"]),
            timelog.exclusions_from(settings["exclude"]),
            settings["browser_history"],
            address_bar.data_path(),
        )
    except Exception as error:
        logger.warning(f"Upload failed, trying again in {RETRY_SECONDS // 60} min: {error}")
        return RETRY_SECONDS
    logger.info(f"Uploaded {result.get('written')} rows over {result.get('days')} days.")
    return max(5, int(settings["interval_minutes"])) * 60


# How long a request from the tray waits at most to be seen.
TICK_SECONDS = 30


def wait(seconds: float, sleep: Callable[[float], None], checker: Optional[updater.Checker], inbox: Optional[gmail_inbox.Inbox] = None, read: Callable[[], dict] = read_settings) -> None:
    """Sleeps in short steps when updates or Gmail are on, so the tray's requests are seen within a tick."""
    if checker is None and inbox is None:
        sleep(seconds)
        return

    def tick() -> None:
        if checker is not None:
            checker.tick()
        if inbox is not None:
            inbox.tick(read())

    tick()
    left = seconds
    while left > 0:
        step = min(TICK_SECONDS, left)
        sleep(step)
        left -= step
        tick()


def loop(client, read: Callable[[], dict] = read_settings, sleep: Callable[[float], None] = time.sleep, checker: Optional[updater.Checker] = None, inbox: Optional[gmail_inbox.Inbox] = None) -> None:
    while True:
        wait(run_once(client, read()), sleep, checker, inbox, read)


def main() -> None:
    parser = argparse.ArgumentParser(APP_NAME, description="Uploads each day's time per app, site and Claude Code to TimeLog")
    parser.add_argument("--testing", action="store_true")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    setup_logging(APP_NAME, testing=args.testing, verbose=args.verbose, log_stderr=True, log_file=True)
    client = aw_client.ActivityWatchClient(APP_NAME, testing=args.testing)
    try:
        client.wait_for_start(timeout=60)
    except Exception:
        # A server still down only delays the first upload: `run_once` retries on its own.
        logger.warning("ActivityWatch's server did not answer in 60 s; uploading anyway on schedule.")
    # Reads the address bar beside the upload loop; a no-op off Windows.
    address_bar.start(read_settings)
    checker = updater.Checker(dirs.get_config_dir(APP_NAME), updater.bundle_root(), lambda: bool(read_settings()["auto_update"]))
    loop(client, checker=checker, inbox=gmail_inbox.Inbox(dirs.get_config_dir(APP_NAME)))


if __name__ == "__main__":
    main()
