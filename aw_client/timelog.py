"""
Uploads the day's time to TimeLog as seconds per bucket, classified on this computer, for the
person's own eyes: per app and per window title, per website hostname and per page (URL without
its query and fragment, and its title), and Claude Code's active time per day and per session.
Never an event, a URL's query, a message of Claude Code, or anything on the exclusion list.

Left out before anything is built: private windows (a title naming one is dropped), and every host
or app on the exclusion list (banks and password managers by default).

The app query keeps the window events that overlap a not-AFK stretch and merges them by app,
the same way the web UI's "Top applications" does. The sites and pages come from the focused
browser window over the same not-AFK stretches: its title, without the browser's name, is looked up
in the browser's own history on this computer (`browser_history.py`), no extension. A title the
history does not know stays a window row. Claude Code's time comes from the
timestamps in its session logs; their content is never read. A day is a calendar day in Bogotá.
"""

import json
import logging
import os
import re
import socket
from datetime import date, datetime, time, timedelta, timezone
from typing import Dict, Iterable, List, NamedTuple, Optional, Sequence, Tuple
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

import requests

from . import browser_history

logger = logging.getLogger(__name__)

TIME_ZONE = ZoneInfo("America/Bogota")
MAX_APPS = 500
MAX_SITES = 500
MAX_WINDOWS = 200
MAX_PAGES = 300
MAX_SESSIONS = 100
MAX_TITLE = 300
MAX_URL = 500
MAX_DAYS = 31
DAY_SECONDS = 86400
# Two messages further apart than this are two stretches of work; a message alone counts one minute.
CLAUDE_GAP_SECONDS = 300
CLAUDE_LONE_SECONDS = 60
# What TimeLog accepts as a site: a hostname, never anything that could carry a path or a query.
HOST_PATTERN = re.compile(r"[a-z0-9]([a-z0-9.-]{0,118}[a-z0-9])?")
# Windows, macOS and Linux names of the browsers whose history is read.
BROWSER_APPS = [
    "chrome.exe", "msedge.exe", "firefox.exe", "brave.exe", "opera.exe", "vivaldi.exe", "arc.exe", "chromium.exe",
    "Google Chrome", "Microsoft Edge", "Firefox", "Brave Browser", "Opera", "Vivaldi", "Arc", "Safari",
    "google-chrome", "chromium", "firefox", "brave-browser", "microsoft-edge", "opera", "vivaldi-stable",
]
# The shape TimeLog mints: a malformed key never reaches the network.
KEY_PATTERN = re.compile(r"tl_[A-Za-z0-9_-]{43}")


# Hosts (and their subdomains) and apps never sent: Colombian banks and password managers. A person's own
# list in `aw-timelog.toml` replaces it.
DEFAULT_EXCLUDE = [
    "bancolombia.com", "davivienda.com", "bbva.com.co", "nequi.com.co", "daviplata.com",
    "1password.com", "bitwarden.com", "lastpass.com", "dashlane.com", "keepersecurity.com", "nordpass.com",
    "pass.proton.me", "keepass", "keepassxc", "1password", "bitwarden", "lastpass", "dashlane", "nordpass",
]
# A window title that names a private window.
PRIVATE_TITLE = re.compile(r"inc[oó]gnito|inprivate|private browsing|private window|navegaci[oó]n privada|ventana privada|modo privado|modo inc[oó]gnito", re.I)


def exclusions_from(value: object) -> List[str]:
    """A setting as the exclusion list: a list, or a string split by «;»; unset is the default, an empty list is none."""
    if value is None or value == "":
        return list(DEFAULT_EXCLUDE)
    items = value if isinstance(value, (list, tuple)) else str(value).split(";")
    return [str(item).strip().lower() for item in items if str(item).strip()]


def _bare(app: str) -> str:
    app = app.lower()
    return app[:-4] if app.endswith(".exe") else app


def is_excluded(name: str, exclude: Sequence[str]) -> bool:
    """A hostname matches an entry as itself or a subdomain; an app matches by process name, `.exe` or not."""
    name = name.lower()
    bare = _bare(name)
    return any(name == entry or name.endswith("." + entry) or bare == _bare(entry) for entry in exclude if entry)


def is_browser(app: str) -> bool:
    return _bare(app) in {_bare(name) for name in BROWSER_APPS}


def printable(text: object, limit: int) -> str:
    """TimeLog refuses a control character in a name."""
    return "".join(c for c in str(text or "") if c.isprintable()).strip()[:limit]


def _browser_list() -> str:
    return ", ".join(json.dumps(name) for name in BROWSER_APPS)


def build_titles_query(hostname: Optional[str]) -> List[str]:
    """Window titles of everything but browsers, whose pages `build_browser_query` reads."""
    host = f', "{hostname}"' if hostname else ""
    return [
        f'window = flood(query_bucket(find_bucket("aw-watcher-window_"{host})));',
        f'afk = flood(query_bucket(find_bucket("aw-watcher-afk_"{host})));',
        'not_afk = filter_keyvals(afk, "status", ["not-afk"]);',
        "window = filter_period_intersect(window, not_afk);",
        f'window = exclude_keyvals(window, "app", [{_browser_list()}]);',
        'RETURN = sort_by_duration(merge_events_by_keys(window, ["app", "title"]));',
    ]


def build_query(hostname: Optional[str]) -> List[str]:
    host = f', "{hostname}"' if hostname else ""
    return [
        f'window = flood(query_bucket(find_bucket("aw-watcher-window_"{host})));',
        f'afk = flood(query_bucket(find_bucket("aw-watcher-afk_"{host})));',
        'not_afk = filter_keyvals(afk, "status", ["not-afk"]);',
        "window = filter_period_intersect(window, not_afk);",
        'RETURN = sort_by_duration(merge_events_by_keys(window, ["app"]));',
    ]


def build_browser_query(hostname: Optional[str]) -> List[str]:
    """The focused browser windows over not-AFK time, event by event: each keeps its start, which dates the history lookup."""
    host = f', "{hostname}"' if hostname else ""
    return [
        f'window = flood(query_bucket(find_bucket("aw-watcher-window_"{host})));',
        f'afk = flood(query_bucket(find_bucket("aw-watcher-afk_"{host})));',
        'not_afk = filter_keyvals(afk, "status", ["not-afk"]);',
        "window = filter_period_intersect(window, not_afk);",
        f'RETURN = filter_keyvals(window, "app", [{_browser_list()}]);',
    ]


def days_back(today: date, count: int) -> List[date]:
    """`count` days ending today, oldest first."""
    return [today - timedelta(days=offset) for offset in range(count - 1, -1, -1)]


def period_of(day: date) -> Tuple[datetime, datetime]:
    start = datetime.combine(day, time.min, tzinfo=TIME_ZONE)
    return start, start + timedelta(days=1)


def summarize(events: Iterable[dict], exclude: Sequence[str] = ()) -> List[Dict[str, object]]:
    """One row per app in whole seconds, longest first; under a second is dropped, the day capped."""
    seconds: Dict[str, int] = {}
    for event in events:
        app = printable(event.get("data", {}).get("app"), 120)
        if not app or is_excluded(app, exclude):
            continue
        seconds[app] = seconds.get(app, 0) + int(event.get("duration", 0))
    return _capped([{"app": app, "seconds": value} for app, value in seconds.items() if value >= 1], MAX_APPS)


def host_of(url: object) -> Optional[str]:
    """The hostname of a web address, lower-cased and without «www.»; None for anything that is not a site."""
    try:
        parts = urlsplit(str(url or "").strip())
        host = parts.hostname
    except ValueError:
        return None
    if parts.scheme not in ("http", "https") or not host:
        return None
    host = host.lower().rstrip(".")
    if host.startswith("www."):
        host = host[4:]
    return host if HOST_PATTERN.fullmatch(host) else None


def _capped(rows: List[Dict[str, object]], limit: int) -> List[Dict[str, object]]:
    """The longest `limit` rows; a day past 24 h (overlapping watchers) scaled down to it, rows under a second dropped."""
    rows = sorted(rows, key=lambda row: (-int(row["seconds"]), str(row.get("app") or row.get("host") or ""), str(row.get("title") or row.get("url") or "")))[:limit]
    total = sum(int(row["seconds"]) for row in rows)
    if total > DAY_SECONDS:
        scale = DAY_SECONDS / total
        rows = [{**row, "seconds": int(int(row["seconds"]) * scale)} for row in rows]
    return [row for row in rows if int(row["seconds"]) >= 1]


def summarize_sites(events: Iterable[dict], exclude: Sequence[str] = ()) -> List[Dict[str, object]]:
    """One row per hostname in whole seconds, longest first; an excluded host is never counted."""
    seconds: Dict[str, int] = {}
    for event in events:
        host = host_of(event.get("data", {}).get("url"))
        if host is None or is_excluded(host, exclude):
            continue
        seconds[host] = seconds.get(host, 0) + int(event.get("duration", 0))
    return _capped([{"host": host, "seconds": value} for host, value in seconds.items() if value >= 1], MAX_SITES)


def page_of(url: object) -> Optional[Tuple[str, str]]:
    """(hostname, address) of a web page: its URL without credentials, query and fragment. None for anything that is not a page."""
    host = host_of(url)
    if host is None:
        return None
    parts = urlsplit(str(url).strip())
    port = f":{parts.port}" if parts.port else ""
    address = f"{parts.scheme}://{host}{port}{parts.path}"
    return (host, address) if len(address) <= MAX_URL and all(c.isprintable() for c in address) else None


def summarize_pages(events: Iterable[dict], exclude: Sequence[str] = ()) -> List[Dict[str, object]]:
    """One row per page (address and title), longest first. Excluded hosts and titles naming a private window are never counted."""
    seconds: Dict[Tuple[str, str, str], int] = {}
    for event in events:
        data = event.get("data", {})
        try:
            page = page_of(data.get("url"))
        except ValueError:
            continue
        title = printable(data.get("title"), MAX_TITLE)
        if page is None or is_excluded(page[0], exclude) or PRIVATE_TITLE.search(title):
            continue
        key = (page[0], page[1], title)
        seconds[key] = seconds.get(key, 0) + int(event.get("duration", 0))
    return _capped(
        [{"host": host, "url": url, "title": title, "seconds": value} for (host, url, title), value in seconds.items() if value >= 1],
        MAX_PAGES,
    )


def summarize_windows(events: Iterable[dict], exclude: Sequence[str] = ()) -> List[Dict[str, object]]:
    """One row per (app, window title), longest first. A browser's titles are never a row (its pages are kept above,
    its time stays under the app); neither are excluded apps and titles naming a private window."""
    seconds: Dict[Tuple[str, str], int] = {}
    for event in events:
        data = event.get("data", {})
        app = printable(data.get("app"), 120)
        title = printable(data.get("title"), MAX_TITLE)
        if not app or not title or is_browser(app) or is_excluded(app, exclude) or PRIVATE_TITLE.search(title):
            continue
        seconds[(app, title)] = seconds.get((app, title), 0) + int(event.get("duration", 0))
    return _capped([{"app": app, "title": title, "seconds": value} for (app, title), value in seconds.items() if value >= 1], MAX_WINDOWS)


def parse_stamp(value: object) -> Optional[datetime]:
    """An ISO timestamp as an aware datetime; a naive one is UTC, as Claude Code writes them."""
    text = str(value or "").strip()
    if not text:
        return None
    text = text[:-1] + "+00:00" if text.endswith("Z") else text
    # Python 3.8 reads only 3 or 6 fractional digits.
    text = re.sub(r"\.(\d+)", lambda m: "." + m.group(1)[:6].ljust(6, "0"), text, count=1)
    try:
        stamp = datetime.fromisoformat(text)
    except ValueError:
        return None
    return stamp if stamp.tzinfo else stamp.replace(tzinfo=timezone.utc)


def active_intervals(stamps: Sequence[datetime]) -> List[Tuple[datetime, datetime]]:
    """The stretches of one session's work: consecutive messages at most 5 minutes apart join up, a message alone is a minute."""
    ordered = sorted(stamps)
    intervals: List[Tuple[datetime, datetime]] = []
    for index, stamp in enumerate(ordered):
        following = ordered[index + 1] if index + 1 < len(ordered) else None
        previous = ordered[index - 1] if index > 0 else None
        if following is not None and (following - stamp).total_seconds() <= CLAUDE_GAP_SECONDS:
            intervals.append((stamp, following))
        elif previous is None or (stamp - previous).total_seconds() > CLAUDE_GAP_SECONDS:
            intervals.append((stamp, stamp + timedelta(seconds=CLAUDE_LONE_SECONDS)))
    return intervals


def claude_code_seconds(sessions: Iterable[Sequence[datetime]], days: List[date]) -> Dict[date, int]:
    """Seconds Claude Code was working per day: every session's stretches in one union (two sessions at once count once), clipped to the Bogotá day."""
    merged: List[List[datetime]] = []
    for start, end in sorted(i for stamps in sessions for i in active_intervals(stamps)):
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    result: Dict[date, int] = {}
    for day in days:
        begin, stop = period_of(day)
        total = sum(
            (min(end, stop) - max(start, begin)).total_seconds() for start, end in merged if start < stop and end > begin
        )
        result[day] = min(int(total), DAY_SECONDS)
    return result


def default_claude_dirs() -> List[str]:
    """`~/.claude/projects`: %USERPROFILE%\\.claude\\projects on Windows."""
    return [os.path.join(os.path.expanduser("~"), ".claude", "projects")]


def claude_dirs_from(value: object) -> List[str]:
    """A setting as folders: a list, or one string with several folders split by «;»; empty is the default."""
    items = value if isinstance(value, (list, tuple)) else str(value or "").split(";")
    folders = [str(item).strip() for item in items if str(item).strip()]
    return folders or default_claude_dirs()


class ClaudeSession(NamedTuple):
    """One Claude Code session: the project folder it ran in (its name as Claude Code wrote it), its id and its message times."""

    project: str
    session: str
    stamps: List[datetime]


def _session_of(folder: str, path: str) -> Tuple[str, str]:
    """(project, session id) of a log: the first folder under `folder`, and the file's name, or the session folder a subagent's log sits in."""
    parts = os.path.relpath(path, folder).split(os.sep)
    project = parts[0] if len(parts) > 1 else os.path.basename(folder.rstrip("/\\")) or "claude"
    if "subagents" in parts:
        return project, parts[parts.index("subagents") - 1]
    return project, os.path.splitext(parts[-1])[0]


def read_claude_sessions(folders: Sequence[str], since: datetime) -> Optional[List[ClaudeSession]]:
    """Each session's message timestamps, nothing else of its logs. None when no folder exists, so the day is not touched."""
    existing = [folder for folder in folders if os.path.isdir(folder)]
    if not existing:
        return None
    sessions: Dict[Tuple[str, str], List[datetime]] = {}
    for folder in existing:
        for root, _, files in os.walk(folder):
            for name in files:
                if not name.endswith(".jsonl"):
                    continue
                path = os.path.join(root, name)
                try:
                    # A log not written since `since` has no message in the window.
                    if datetime.fromtimestamp(os.path.getmtime(path), tz=timezone.utc) < since:
                        continue
                    stamps = []
                    with open(path, encoding="utf-8", errors="replace") as file:
                        for line in file:
                            if '"timestamp"' not in line:
                                continue
                            try:
                                stamp = parse_stamp(json.loads(line).get("timestamp"))
                            except (ValueError, AttributeError):
                                continue
                            if stamp is not None:
                                stamps.append(stamp)
                except OSError:
                    continue
                if stamps:
                    sessions.setdefault(_session_of(folder, path), []).extend(stamps)
    return [ClaudeSession(project, session, sorted(stamps)) for (project, session), stamps in sessions.items()]


def claude_session_rows(sessions: Iterable[ClaudeSession], days: List[date]) -> Dict[date, List[Dict[str, object]]]:
    """Per day, each session that worked in it: id, project, first and last message of that day (UTC, ISO) and its active seconds."""
    result: Dict[date, List[Dict[str, object]]] = {day: [] for day in days}
    for one in sessions:
        intervals = active_intervals(one.stamps)
        for day in days:
            begin, stop = period_of(day)
            seconds = int(sum((min(end, stop) - max(start, begin)).total_seconds() for start, end in intervals if start < stop and end > begin))
            inside = [stamp for stamp in one.stamps if begin <= stamp < stop]
            if seconds < 1 or not inside:
                continue
            result[day].append(
                {
                    "session": printable(one.session, 80),
                    "project": printable(one.project, 200),
                    "first": inside[0].astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "last": inside[-1].astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "seconds": min(seconds, DAY_SECONDS),
                }
            )

    for day in days:
        result[day] = sorted(result[day], key=lambda row: (-int(row["seconds"]), str(row["session"])))[:MAX_SESSIONS]
    return result


def payload(
    device: str,
    results: List[List[dict]],
    days: List[date],
    sites: Optional[List[List[dict]]] = None,
    claude: Optional[Dict[date, int]] = None,
    titles: Optional[List[List[dict]]] = None,
    sessions: Optional[Dict[date, List[Dict[str, object]]]] = None,
    exclude: Sequence[str] = (),
) -> dict:
    """Each part a source gives is named in its day; one left out (no browser history, no Claude folder) leaves what the server has."""
    rows = []
    for index, (day, events) in enumerate(zip(days, results)):
        row: Dict[str, object] = {"day": day.isoformat(), "apps": summarize(events, exclude)}
        if titles is not None:
            row["windows"] = summarize_windows(titles[index], exclude)
        if sites is not None:
            row["sites"] = summarize_sites(sites[index], exclude)
            row["pages"] = summarize_pages(sites[index], exclude)
        if claude is not None:
            row["claude_code_seconds"] = claude.get(day, 0)
        if sessions is not None:
            row["claude_sessions"] = sessions.get(day, [])
        rows.append(row)
    return {"device": device[:80], "days": rows}


def upload(url: str, key: str, body: dict, timeout: float = 30) -> dict:
    response = requests.post(
        f"{url.rstrip('/')}/api/ingest",
        json=body,
        headers={"Authorization": f"Bearer {key}"},
        timeout=timeout,
    )
    if response.status_code == 401:
        raise RuntimeError("TimeLog refused the key: create one in «Llaves».")
    response.raise_for_status()
    return response.json()


def _page_event(seconds: float, url: str, title: str) -> dict:
    return {"duration": seconds, "data": {"url": url, "title": title}}


def resolve_browser(
    days_events: List[List[dict]], history: "browser_history.History", store: Optional[object] = None
) -> List[List[dict]]:
    """Per day, the focused browser windows as page events. A stretch the address-bar poller sampled takes its address
    from the samples (only the seconds they overlap the window); the rest takes it from the browser's history by title
    and time. A title neither knows is not sent: its time stays under the browser's app row. Private windows are dropped."""
    days: List[List[dict]] = []
    for events in days_events:
        found: List[dict] = []
        for event in events:
            data = event.get("data", {})
            title = str(data.get("title") or "")
            if not title.strip() or PRIVATE_TITLE.search(title):
                continue
            start = browser_history.event_time(event)
            duration = float(event.get("duration", 0))
            left = duration
            if store is not None and start:
                for sample in store.between(start, start + duration):  # type: ignore[attr-defined]
                    overlap = min(sample.end, start + duration) - max(sample.start, start)
                    if overlap > 0:
                        found.append(_page_event(overlap, sample.url, sample.title))
                        left -= overlap
            if left < 1:
                continue
            candidates = browser_history.page_titles(title)
            for candidate in candidates:
                url = history.find(candidate, start)
                if url:
                    found.append(_page_event(left, url, candidate))
                    break
        days.append(found)
    return days


def sync(
    client,
    url: str,
    key: str,
    count: int,
    hostname: Optional[str],
    device: Optional[str],
    dry_run: bool,
    claude_dirs: Optional[Sequence[str]] = None,
    exclude: Optional[Sequence[str]] = None,
    browser_history_paths: Optional[Sequence[str]] = None,
    addresses_path: Optional[str] = None,
) -> dict:
    days = days_back(datetime.now(TIME_ZONE).date(), max(1, min(count, MAX_DAYS)))
    periods = [period_of(day) for day in days]
    skip = list(DEFAULT_EXCLUDE) if exclude is None else [str(item).strip().lower() for item in exclude]
    try:
        results = client.query("\n".join(build_query(hostname)), periods)
        titles = client.query("\n".join(build_titles_query(hostname)), periods)
        browsers = client.query("\n".join(build_browser_query(hostname)), periods)
    except requests.exceptions.ConnectionError:
        raise RuntimeError(f"ActivityWatch is not answering at {client.server_address}: open it and try again.") from None
    history, problems = browser_history.load_history(browser_history_paths)
    for problem in problems:
        logger.warning(f"Browser history not read: {problem}")
    from .address_bar import AddressStore

    store = AddressStore(addresses_path) if addresses_path and os.path.isfile(addresses_path) else None
    pages = resolve_browser(browsers, history, store)
    # With neither a history nor samples on this computer the sites are left to the server.
    sites = pages if (history or store is not None) else None
    read = read_claude_sessions(claude_dirs_from(claude_dirs), periods[0][0].astimezone(timezone.utc))
    claude = None if read is None else claude_code_seconds([one.stamps for one in read], days)
    sessions = None if read is None else claude_session_rows(read, days)
    body = payload(device or socket.gethostname(), results, days, sites, claude, titles, sessions, skip)
    return body if dry_run else upload(url, key, body)
