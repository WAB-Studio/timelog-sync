"""
Records the page in front by reading the browser's address bar, so the site and page of a moment are
exact rather than guessed from history. Windows only (UI Automation); elsewhere nothing runs.

Every few seconds the poller reads the focused window. When it is a browser's, public and not on the
exclusion list, the page (host, address without query and fragment, title) and the seconds are kept in a
small SQLite file in the user's data folder. Nothing private or excluded ever reaches that file. The daily
upload intersects those samples with the not-AFK browser time, so only time the person was at the computer
counts, and falls back to the browser's history where there is no sample.
"""

import logging
import os
import re
import sqlite3
import threading
import time
from typing import Callable, List, NamedTuple, Optional, Sequence

from . import timelog

logger = logging.getLogger(__name__)

# A sample longer than this many polls is a gap (the computer slept), not time on a page.
MAX_GAP_POLLS = 3
KEEP_DAYS = 40
IP_ADDRESS = re.compile(r"\d{1,3}(\.\d{1,3}){3}(:\d+)?(/.*)?")


class Reading(NamedTuple):
    """What the focused window shows: its app, its title and, for a browser, what the address bar says."""

    app: str
    title: str
    address: str
    focused_bar: bool = False
    private: bool = False


class Sample(NamedTuple):
    start: float
    end: float
    host: str
    url: str
    title: str


def normalize_address(text: object) -> Optional[str]:
    """What an address bar shows as an http(s) address: a missing scheme is added (https, http for localhost and IPs).
    None for search text, internal pages («chrome://», «about:») and anything that is not a site."""
    value = str(text or "").strip().replace("​", "")
    if not value or " " in value or "\n" in value:
        return None
    if "://" not in value:
        if value.startswith(("about:", "chrome:", "edge:", "file:", "view-source:", "data:", "javascript:")):
            return None
        head = value.split("/")[0].split(":")[0].lower()
        local = head == "localhost" or head.endswith(".localhost") or bool(IP_ADDRESS.fullmatch(value))
        if "." not in head and head != "localhost":
            return None
        value = ("http://" if local else "https://") + value
    return value if timelog.host_of(value) else None


def sample_of(reading: Reading, exclude: Sequence[str]) -> Optional[Sequence[str]]:
    """(host, address, page title) of a reading worth keeping, or None: not a browser, a private window, the bar
    being typed in, an address that is no page, or a host on the exclusion list."""
    if not timelog.is_browser(reading.app) or reading.focused_bar or reading.private:
        return None
    if timelog.PRIVATE_TITLE.search(reading.title):
        return None
    address = normalize_address(reading.address)
    if address is None:
        return None
    try:
        page = timelog.page_of(address)
    except ValueError:
        return None
    if page is None or timelog.is_excluded(page[0], exclude):
        return None
    from .browser_history import page_titles

    titles = page_titles(reading.title)
    title = timelog.printable(titles[0] if titles else reading.title, timelog.MAX_TITLE)
    return page[0], page[1], title


def data_path() -> str:
    from aw_core import dirs

    return os.path.join(dirs.get_data_dir("aw-timelog"), "addresses.sqlite")


class AddressStore:
    """Samples in SQLite: start and end (Unix seconds), host, address and title. Pruned past `KEEP_DAYS`."""

    def __init__(self, path: str):
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self.path = path
        with self._open() as connection:
            connection.execute(
                "create table if not exists samples (start real not null, end real not null, host text not null, url text not null, title text not null)"
            )
            connection.execute("create index if not exists samples_start on samples (start)")

    def _open(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path, timeout=10)

    def add(self, sample: Sample) -> None:
        with self._open() as connection:
            connection.execute("insert into samples values (?, ?, ?, ?, ?)", sample)
            connection.execute("delete from samples where end < ?", (sample.end - KEEP_DAYS * 86400,))

    def between(self, start: float, stop: float) -> List[Sample]:
        """Samples that overlap [start, stop), oldest first."""
        with self._open() as connection:
            rows = connection.execute(
                "select start, end, host, url, title from samples where end > ? and start < ? order by start", (start, stop)
            ).fetchall()
        return [Sample(*row) for row in rows]


class Poller:
    """One reading every `poll_seconds`; each kept reading is a sample reaching back to the previous poll."""

    def __init__(
        self,
        reader: Callable[[], Optional[Reading]],
        store: AddressStore,
        exclude: Callable[[], Sequence[str]],
        poll_seconds: float = 5,
        clock: Callable[[], float] = time.time,
    ):
        self.reader, self.store, self.exclude, self.poll = reader, store, exclude, max(1.0, float(poll_seconds))
        self.clock = clock
        self._last: Optional[float] = None

    def tick(self) -> Optional[Sample]:
        now = self.clock()
        before, self._last = self._last, now
        try:
            reading = self.reader()
        except Exception as error:
            logger.debug(f"Address bar not read: {error}")
            return None
        if reading is None:
            return None
        kept = sample_of(reading, self.exclude())
        if kept is None:
            return None
        start = now - self.poll if before is None or now - before > self.poll * MAX_GAP_POLLS else before
        sample = Sample(start, now, *kept)
        self.store.add(sample)
        return sample

    def run(self, stop: threading.Event) -> None:
        while not stop.wait(self.poll):
            self.tick()


def windows_reader() -> Optional[Callable[[], Optional[Reading]]]:
    """A reader of the focused window's address bar through UI Automation; None off Windows or without `comtypes`."""
    if os.name != "nt":
        return None
    try:
        import ctypes
        from ctypes import wintypes

        import comtypes.client

        comtypes.client.GetModule("UIAutomationCore.dll")
        from comtypes.gen import UIAutomationClient as UIA
    except Exception as error:
        logger.warning(f"Address bar reading is off: {error}")
        return None
    user32, kernel32 = ctypes.windll.user32, ctypes.windll.kernel32  # type: ignore[attr-defined]
    automation = comtypes.client.CreateObject("{ff48dba4-60ef-4201-aa87-54103eef594e}", interface=UIA.IUIAutomation)
    button_condition = automation.CreatePropertyCondition(UIA.UIA_ControlTypePropertyId, UIA.UIA_ButtonControlTypeId)
    edits = automation.CreatePropertyCondition(UIA.UIA_ControlTypePropertyId, UIA.UIA_EditControlTypeId)

    def app_of(window: int) -> str:
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(window, ctypes.byref(pid))
        process = kernel32.OpenProcess(0x1000, False, pid.value)
        if not process:
            return ""
        try:
            buffer, size = ctypes.create_unicode_buffer(520), wintypes.DWORD(520)
            kernel32.QueryFullProcessImageNameW(process, 0, buffer, ctypes.byref(size))
            return os.path.basename(buffer.value)
        finally:
            kernel32.CloseHandle(process)

    def read() -> Optional[Reading]:
        window = user32.GetForegroundWindow()
        if not window:
            return None
        app = app_of(window)
        if not timelog.is_browser(app):
            return None
        title = ctypes.create_unicode_buffer(512)
        user32.GetWindowTextW(window, title, 512)
        root = automation.ElementFromHandle(window)
        # Chromium's profile button reads «Incognito» / «InPrivate» and Firefox marks its private windows by name:
        # a button of the window naming one makes the whole window private.
        buttons = root.FindAll(UIA.TreeScope_Descendants, button_condition)
        private = any(timelog.PRIVATE_TITLE.search(buttons.GetElement(i).CurrentName or "") for i in range(min(buttons.Length, 60)))
        if private:
            return Reading(app, title.value, "", private=True)
        found = root.FindAll(UIA.TreeScope_Descendants, edits)
        # The address bar comes first in the tree; page fields come after.
        for index in range(min(found.Length, 12)):
            element = found.GetElement(index)
            pattern = element.GetCurrentPattern(UIA.UIA_ValuePatternId).QueryInterface(UIA.IUIAutomationValuePattern)
            value = pattern.CurrentValue
            if normalize_address(value):
                return Reading(app, title.value, value, bool(element.CurrentHasKeyboardFocus))
        return Reading(app, title.value, "")

    return read


def start(read_settings: Callable[[], dict], reader: Optional[Callable[[], Optional[Reading]]] = None) -> Optional[threading.Event]:
    """Starts the poller in a daemon thread beside the upload loop; the returned event stops it. None when nothing can run
    (not Windows, no UI Automation, or `poll_seconds` = 0)."""
    settings = read_settings()
    seconds = float(settings.get("poll_seconds", 5) or 0)
    reader = reader or windows_reader()
    if seconds <= 0 or reader is None:
        return None
    poller = Poller(reader, AddressStore(data_path()), lambda: timelog.exclusions_from(read_settings().get("exclude")), seconds)
    stop = threading.Event()
    threading.Thread(target=poller.run, args=(stop,), name="address-bar", daemon=True).start()
    return stop
