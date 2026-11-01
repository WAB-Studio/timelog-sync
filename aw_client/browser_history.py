"""
Resolves a browser window's title to the page's address by reading the browser's own history on this
computer, so no browser extension is needed.

Chromium-family browsers keep `History` (SQLite, `urls`), Firefox `places.sqlite` (`moz_places`). A browser
holds its file locked while it runs, so the file (and its `-wal`) is copied to a temporary folder and the copy
is opened read-only; the original is never opened for writing. Only the address, title and last visit time of
each page are read.
"""

import glob
import os
import re
import shutil
import sqlite3
import tempfile
import unicodedata
from typing import Dict, Iterable, List, NamedTuple, Optional, Sequence, Tuple, Union

# Chromium counts microseconds from 1601-01-01 UTC.
CHROMIUM_EPOCH_OFFSET = 11644473600
BROWSER_NAMES = r"Google Chrome|Mozilla Firefox|Microsoft\W*Edge|Brave|Opera|Vivaldi|Chromium|Firefox|Arc"
DASH = r"\s[-–—]\s"
# «Title - Google Chrome», «Title — Mozilla Firefox».
SUFFIX = re.compile(rf"{DASH}(?:{BROWSER_NAMES})$", re.I)
# Edge adds the profile: «Title - Personal - Microsoft Edge».
PROFILE_SUFFIX = re.compile(rf"{DASH}[^-–—]+{DASH}Microsoft\W*Edge$", re.I)
# A page counts for a window when it was visited this long before the window came to the front, or while it was there.
VISIT_LEAD_SECONDS = 1800
MORE_PAGES = re.compile(r"\s+and \d+ more pages?$", re.I)


class Visit(NamedTuple):
    url: str
    title: str
    when: float  # Unix seconds


def normalize(text: object) -> str:
    """A title as compared: invisible format characters (Edge writes a zero-width space) gone, spaces collapsed, case folded."""
    kept = "".join(c for c in str(text or "") if unicodedata.category(c) != "Cf")
    return " ".join(kept.split()).casefold()


def page_titles(window_title: object) -> List[str]:
    """The page titles a browser window title can stand for, most likely first. Empty when it names no browser."""
    title = "".join(c for c in str(window_title or "") if unicodedata.category(c) != "Cf").strip()
    found: List[str] = []
    for pattern in (SUFFIX, PROFILE_SUFFIX):
        match = pattern.search(title)
        if match:
            stripped = title[: match.start()].strip()
            for candidate in (stripped, MORE_PAGES.sub("", stripped)):
                if candidate and candidate not in found:
                    found.append(candidate)
    return found


def _chromium_roots(env: Dict[str, str]) -> List[str]:
    local, roaming, home = env.get("LOCALAPPDATA", ""), env.get("APPDATA", ""), os.path.expanduser("~")
    roots = []
    if roaming:
        # Opera keeps its profile folder itself as the data folder.
        roots += [os.path.join(roaming, "Opera Software", name) for name in ["Opera Stable", "Opera GX Stable"]]
    if local:
        roots += [
            os.path.join(local, "Google", "Chrome", "User Data"),
            os.path.join(local, "Microsoft", "Edge", "User Data"),
            os.path.join(local, "BraveSoftware", "Brave-Browser", "User Data"),
            os.path.join(local, "Vivaldi", "User Data"),
            os.path.join(local, "Chromium", "User Data"),
            # Arc for Windows is a Store app: its data sits under a package folder.
            os.path.join(local, "Packages", "TheBrowserCompany.Arc_*", "LocalCache", "Local", "Arc", "User Data"),
        ]
    roots += [
        os.path.join(home, ".config", name)
        for name in ["google-chrome", "chromium", "microsoft-edge", "BraveSoftware/Brave-Browser", "vivaldi"]
    ]
    roots += [
        os.path.join(home, "Library", "Application Support", name)
        for name in ["Google/Chrome", "Microsoft Edge", "BraveSoftware/Brave-Browser", "Vivaldi", "Chromium"]
    ]
    return roots


def _firefox_roots(env: Dict[str, str]) -> List[str]:
    home = os.path.expanduser("~")
    roots = [os.path.join(home, ".mozilla", "firefox"), os.path.join(home, "Library", "Application Support", "Firefox", "Profiles")]
    if env.get("APPDATA"):
        roots.append(os.path.join(env["APPDATA"], "Mozilla", "Firefox", "Profiles"))
    return roots


def _in_folder(folder: str) -> List[str]:
    """Every history file in a browser's data folder: `History` in it and in each profile folder, `places.sqlite` in each profile."""
    if not os.path.isdir(folder):
        return []
    patterns = ["History", "*/History", "places.sqlite", "*/places.sqlite"]
    return sorted({path for pattern in patterns for path in glob.glob(os.path.join(glob.escape(folder), pattern)) if os.path.isfile(path)})


def history_files(configured: Union[str, Sequence[str], None] = None, env: Optional[Dict[str, str]] = None) -> List[str]:
    """The history files to read: the configured paths (a file, or a browser's data folder; `%VAR%`, `$VAR` and `~` expand),
    else every one found in the usual places of this computer."""
    env = dict(os.environ) if env is None else env
    items = configured.split(";") if isinstance(configured, str) else (configured or [])
    entries = [str(item).strip() for item in items if str(item).strip()]
    if entries:
        folders = [os.path.expanduser(os.path.expandvars(entry)) for entry in entries]
    else:
        folders = _chromium_roots(env) + _firefox_roots(env)
    files: List[str] = []
    for pattern in folders:
        for path in (glob.glob(pattern) if glob.has_magic(pattern) else [pattern]):
            found = [path] if os.path.isfile(path) else _in_folder(path)
            files += [item for item in found if item not in files]
    return files


def _copy(path: str, folder: str, index: int) -> str:
    """A copy of the file and its `-wal` in `folder`; the original is only read."""
    target = os.path.join(folder, f"{index}-{os.path.basename(path)}")
    shutil.copyfile(path, target)
    for suffix in ("-wal", "-shm"):
        if os.path.isfile(path + suffix):
            shutil.copyfile(path + suffix, target + suffix)
    return target


def _chromium_time(value: int) -> float:
    return value / 1_000_000 - CHROMIUM_EPOCH_OFFSET


def _read(copy: str, since: float = 0) -> List[Visit]:
    """Every visit since `since` (Unix seconds): the real visit times where the browser keeps them, else the last visit of each page."""
    connection = sqlite3.connect(f"file:{copy}?mode=ro", uri=True)
    try:
        tables = {row[0] for row in connection.execute("select name from sqlite_master where type = 'table'")}
        if "urls" in tables:
            floor = int((since + CHROMIUM_EPOCH_OFFSET) * 1_000_000)
            if "visits" in tables:
                rows = connection.execute(
                    "select u.url, u.title, v.visit_time from visits v join urls u on u.id = v.url where u.title <> '' and v.visit_time >= ?", (floor,)
                )
            else:
                rows = connection.execute("select url, title, last_visit_time from urls where title <> '' and last_visit_time >= ?", (floor,))
            return [Visit(url, title, _chromium_time(when)) for url, title, when in rows]
        if "moz_places" in tables:
            floor = int(since * 1_000_000)
            if "moz_historyvisits" in tables:
                rows = connection.execute(
                    "select p.url, p.title, v.visit_date from moz_historyvisits v join moz_places p on p.id = v.place_id "
                    "where p.title is not null and p.title <> '' and v.visit_date >= ?",
                    (floor,),
                )
            else:
                rows = connection.execute(
                    "select url, title, last_visit_date from moz_places where title is not null and title <> '' and last_visit_date >= ?", (floor,)
                )
            return [Visit(url, title, when / 1_000_000) for url, title, when in rows]
        return []
    finally:
        connection.close()


class History:
    """Pages from the browsers' histories, found again by title and time."""

    def __init__(self, visits: Iterable[Visit] = ()):
        by_title: Dict[str, List[Tuple[float, str]]] = {}
        for visit in visits:
            by_title.setdefault(normalize(visit.title), []).append((visit.when, visit.url))
        self._by_title = {title: sorted(items) for title, items in by_title.items()}

    def __bool__(self) -> bool:
        return bool(self._by_title)

    def find(self, title: str, start: float, end: Optional[float] = None) -> Optional[str]:
        """The address whose title is exactly `title` and whose visit falls between `VISIT_LEAD_SECONDS` before `start` and `end`,
        the nearest to `start`; None when no visit is that close (Unix seconds)."""
        items = self._by_title.get(normalize(title))
        if not items:
            return None
        end = start if end is None else max(start, end)
        near = [item for item in items if start - VISIT_LEAD_SECONDS <= item[0] <= end]
        return min(near, key=lambda item: abs(item[0] - start))[1] if near else None


def load_history(configured: Union[str, Sequence[str], None] = None, env: Optional[Dict[str, str]] = None, since: float = 0) -> Tuple[History, List[str]]:
    """(history, problems). A file that cannot be copied or read is skipped and named in the problems."""
    visits: List[Visit] = []
    problems: List[str] = []
    with tempfile.TemporaryDirectory(prefix="aw-timelog-") as folder:
        for index, path in enumerate(history_files(configured, env)):
            try:
                visits += _read(_copy(path, folder, index), since)
            except (OSError, sqlite3.Error) as error:
                problems.append(f"{path}: {error}")
    return History(visits), problems
