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
    private: bool = False


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
    """Samples in SQLite: start and end (Unix seconds), host, address, title, and whether the window was private
    (a private sample carries no address). Samples older than `KEEP_DAYS` are pruned when the store opens."""

    def __init__(self, path: str):
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self.path = path
        self._connection = sqlite3.connect(path, timeout=10, check_same_thread=False)
        with self._connection:
            self._connection.execute(
                "create table if not exists samples (start real not null, end real not null, host text not null, url text not null, title text not null, private integer not null default 0)"
            )
            self._connection.execute("create index if not exists samples_end on samples (end)")
            self._connection.execute("delete from samples where end < ?", (time.time() - KEEP_DAYS * 86400,))

    def add(self, sample: Sample) -> None:
        with self._connection:
            self._connection.execute("insert into samples values (?, ?, ?, ?, ?, ?)", tuple(sample))

    def between(self, start: float, stop: float) -> List[Sample]:
        """Samples that overlap [start, stop), oldest first."""
        rows = self._connection.execute(
            "select start, end, host, url, title, private from samples where end > ? and start < ? order by start", (start, stop)
        ).fetchall()
        return [Sample(*row[:5], bool(row[5])) for row in rows]


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
        self._failed = False

    def tick(self) -> Optional[Sample]:
        now = self.clock()
        before, self._last = self._last, now
        try:
            reading = self.reader()
        except Exception as error:
            if not self._failed:
                self._failed = True
                logger.warning(f"Address bar not read (this is logged once): {error}")
            return None
        if reading is None:
            return None
        start = now - self.poll if before is None or now - before > self.poll * MAX_GAP_POLLS else before
        # A private window leaves a marker with no address, so the history fallback skips that stretch too.
        if timelog.is_browser(reading.app) and (reading.private or timelog.PRIVATE_TITLE.search(reading.title)):
            sample = Sample(start, now, "", "", "", True)
            self.store.add(sample)
            return sample
        kept = sample_of(reading, self.exclude())
        if kept is None:
            return None
        sample = Sample(start, now, *kept)
        self.store.add(sample)
        return sample

    def run(self, stop: threading.Event) -> None:
        while not stop.wait(self.poll):
            self.tick()


def windows_reader() -> Optional[Callable[[], Optional[Reading]]]:
    """A reader of the focused window's address bar through UI Automation. Call it on the thread that will read:
    COM objects belong to the thread that made them. None off Windows or without `comtypes`."""
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

    def of_type(kind: int):
        return automation.CreatePropertyCondition(UIA.UIA_ControlTypePropertyId, kind)

    toolbars, buttons, edits = of_type(UIA.UIA_ToolBarControlTypeId), of_type(UIA.UIA_ButtonControlTypeId), of_type(UIA.UIA_EditControlTypeId)

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
        # Only the browser's toolbars are searched (Chromium's ToolbarView, Firefox's nav-bar), never the page.
        bars = root.FindAll(UIA.TreeScope_Descendants, toolbars)
        address, focused = "", False
        for bar_index in range(min(bars.Length, 6)):
            bar = bars.GetElement(bar_index)
            # Chromium's profile button reads «Incognito» / «InPrivate»; Firefox names its private windows.
            names = bar.FindAll(UIA.TreeScope_Descendants, buttons)
            if any(timelog.PRIVATE_TITLE.search(names.GetElement(i).CurrentName or "") for i in range(min(names.Length, 40))):
                return Reading(app, title.value, "", private=True)
            if address:
                continue
            found = bar.FindAll(UIA.TreeScope_Descendants, edits)
            for index in range(min(found.Length, 4)):
                element = found.GetElement(index)
                try:
                    value = element.GetCurrentPattern(UIA.UIA_ValuePatternId).QueryInterface(UIA.IUIAutomationValuePattern).CurrentValue
                except Exception:
                    continue
                if normalize_address(value):
                    address, focused = value, bool(element.CurrentHasKeyboardFocus)
                    break
        return Reading(app, title.value, address, focused)

    return read


def start(read_settings: Callable[[], dict], reader: Optional[Callable[[], Optional[Reading]]] = None) -> Optional[threading.Event]:
    """Starts the poller in a daemon thread beside the upload loop; the returned event stops it. None when nothing can run
    (not Windows, or `poll_seconds` = 0). It never raises: a failure is a warning and the upload loop goes on."""
    try:
        seconds = float(read_settings().get("poll_seconds", 5) or 0)
        if seconds <= 0 or (reader is None and os.name != "nt"):
            return None
        poller_store = AddressStore(data_path())
        stop = threading.Event()

        def work() -> None:
            initialized = False
            try:
                if reader is None:
                    import comtypes

                    comtypes.CoInitializeEx()
                    initialized = True
                use = reader or windows_reader()
                if use is None:
                    return
                Poller(use, poller_store, lambda: timelog.exclusions_from(read_settings().get("exclude")), seconds).run(stop)
            except Exception as error:
                logger.warning(f"Address bar reading is off: {error}")
            finally:
                if initialized:
                    import comtypes

                    comtypes.CoUninitialize()

        threading.Thread(target=work, name="address-bar", daemon=True).start()
        return stop
    except Exception as error:
        logger.warning(f"Address bar reading is off: {error}")
        return None
