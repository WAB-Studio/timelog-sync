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

from . import timelog

logger = logging.getLogger(__name__)

APP_NAME = "aw-timelog"
DEFAULT_URL = "https://timelog-delta.vercel.app"
# A failed upload waits less than a good one: the next try may find the network back.
RETRY_SECONDS = 300
# With no key yet, look for one often: the person is pasting it from the tray right now.
NO_KEY_SECONDS = 30


def config_path() -> str:
    return os.path.join(dirs.get_config_dir(APP_NAME), f"{APP_NAME}.toml")


def read_settings(path: Optional[str] = None) -> dict:
    """The file's settings over the defaults; a missing or broken file is the defaults."""
    settings = {"url": DEFAULT_URL, "key": "", "device": socket.gethostname(), "interval_minutes": 60, "days": 2, "claude_dir": "", "exclude": None, "browser_history": None}
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


def run_once(client, settings: dict) -> int:
    """Seconds until the next upload."""
    if not timelog.KEY_PATTERN.fullmatch(str(settings["key"])):
        logger.info("No TimeLog key yet: paste one from the tray, under «TimeLog…».")
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
        )
    except Exception as error:
        logger.warning(f"Upload failed, trying again in {RETRY_SECONDS // 60} min: {error}")
        return RETRY_SECONDS
    logger.info(f"Uploaded {result.get('written')} rows over {result.get('days')} days.")
    return max(5, int(settings["interval_minutes"])) * 60


def loop(client, read: Callable[[], dict] = read_settings, sleep: Callable[[float], None] = time.sleep) -> None:
    while True:
        sleep(run_once(client, read()))


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
    loop(client)


if __name__ == "__main__":
    main()
