"""
Uploads the day's time to TimeLog, as seconds and nothing finer, classified on this computer:
per app, per website hostname and Claude Code's active time. Never a window title, a URL path,
a page title or an event.

The app query keeps the window events that overlap a not-AFK stretch and merges them by app,
the same way the web UI's "Top applications" does. The sites come from the browser watcher's
tab events over the same not-AFK stretches, reduced to the hostname here. Claude Code's time
comes from the timestamps in its session logs; their content is never read. A day is a calendar
day in Bogotá.
"""

import json
import os
import re
import socket
from datetime import date, datetime, time, timedelta, timezone
from typing import Dict, Iterable, List, Optional, Sequence, Tuple
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

import requests

TIME_ZONE = ZoneInfo("America/Bogota")
MAX_APPS = 500
MAX_SITES = 500
MAX_DAYS = 31
DAY_SECONDS = 86400
# Two messages further apart than this are two stretches of work; a message alone counts one minute.
CLAUDE_GAP_SECONDS = 300
CLAUDE_LONE_SECONDS = 60
# What TimeLog accepts as a site: a hostname, never anything that could carry a path or a query.
HOST_PATTERN = re.compile(r"[a-z0-9]([a-z0-9.-]{0,118}[a-z0-9])?")
# Windows, macOS and Linux names of the browsers the web watcher can sit in.
BROWSER_APPS = [
    "chrome.exe", "msedge.exe", "firefox.exe", "brave.exe", "opera.exe", "vivaldi.exe", "arc.exe", "chromium.exe",
    "Google Chrome", "Microsoft Edge", "Firefox", "Brave Browser", "Opera", "Vivaldi", "Arc", "Safari",
    "google-chrome", "chromium", "firefox", "brave-browser", "microsoft-edge", "opera", "vivaldi-stable",
]
# The shape TimeLog mints: a malformed key never reaches the network.
KEY_PATTERN = re.compile(r"tl_[A-Za-z0-9_-]{43}")


def build_query(hostname: Optional[str]) -> List[str]:
    host = f', "{hostname}"' if hostname else ""
    return [
        f'window = flood(query_bucket(find_bucket("aw-watcher-window_"{host})));',
        f'afk = flood(query_bucket(find_bucket("aw-watcher-afk_"{host})));',
        'not_afk = filter_keyvals(afk, "status", ["not-afk"]);',
        "window = filter_period_intersect(window, not_afk);",
        'RETURN = sort_by_duration(merge_events_by_keys(window, ["app"]));',
    ]


def build_web_query(hostname: Optional[str], bucket: str) -> List[str]:
    """One browser's tab events over the stretches the person was at the computer with a browser in front."""
    host = f', "{hostname}"' if hostname else ""
    browsers = ", ".join(json.dumps(name) for name in BROWSER_APPS)
    return [
        f'tabs = flood(query_bucket({json.dumps(bucket)}));',
        f'window = flood(query_bucket(find_bucket("aw-watcher-window_"{host})));',
        f'afk = flood(query_bucket(find_bucket("aw-watcher-afk_"{host})));',
        'not_afk = filter_keyvals(afk, "status", ["not-afk"]);',
        f'browser = filter_keyvals(window, "app", [{browsers}]);',
        "tabs = filter_period_intersect(tabs, not_afk);",
        "tabs = filter_period_intersect(tabs, browser);",
        # Only the url survives the merge: the page's title never reaches this process.
        'RETURN = sort_by_duration(merge_events_by_keys(tabs, ["url"]));',
    ]


def web_buckets(buckets: Dict[str, dict], hostname: Optional[str]) -> List[str]:
    """The browser watchers' bucket ids, optionally those of one computer."""
    return sorted(
        name
        for name, info in buckets.items()
        if name.startswith("aw-watcher-web") and (not hostname or info.get("hostname") in (None, hostname))
    )


def days_back(today: date, count: int) -> List[date]:
    """`count` days ending today, oldest first."""
    return [today - timedelta(days=offset) for offset in range(count - 1, -1, -1)]


def period_of(day: date) -> Tuple[datetime, datetime]:
    start = datetime.combine(day, time.min, tzinfo=TIME_ZONE)
    return start, start + timedelta(days=1)


def summarize(events: Iterable[dict]) -> List[Dict[str, object]]:
    """One row per app in whole seconds, longest first; under a second is dropped, the day capped."""
    seconds: Dict[str, int] = {}
    for event in events:
        # TimeLog refuses a control character in a name.
        app = "".join(c for c in str(event.get("data", {}).get("app") or "") if c.isprintable()).strip()[:120]
        if not app:
            continue
        seconds[app] = seconds.get(app, 0) + int(event.get("duration", 0))
    rows = sorted(
        ({"app": app, "seconds": value} for app, value in seconds.items() if value >= 1),
        key=lambda row: (-int(row["seconds"]), str(row["app"])),
    )[:MAX_APPS]
    # The server refuses a day past 24 h; overlapping watchers can add a little over it.
    total = sum(int(row["seconds"]) for row in rows)
    if total > DAY_SECONDS:
        scale = DAY_SECONDS / total
        rows = [{"app": row["app"], "seconds": int(int(row["seconds"]) * scale)} for row in rows]
        rows = [row for row in rows if int(row["seconds"]) >= 1]
    return rows


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


def summarize_sites(events: Iterable[dict]) -> List[Dict[str, object]]:
    """One row per hostname in whole seconds, longest first; under a second is dropped, the day capped."""
    seconds: Dict[str, int] = {}
    for event in events:
        host = host_of(event.get("data", {}).get("url"))
        if host is None:
            continue
        seconds[host] = seconds.get(host, 0) + int(event.get("duration", 0))
    rows = sorted(
        ({"host": host, "seconds": value} for host, value in seconds.items() if value >= 1),
        key=lambda row: (-int(row["seconds"]), str(row["host"])),
    )[:MAX_SITES]
    total = sum(int(row["seconds"]) for row in rows)
    if total > DAY_SECONDS:
        scale = DAY_SECONDS / total
        rows = [{"host": row["host"], "seconds": int(int(row["seconds"]) * scale)} for row in rows]
        rows = [row for row in rows if int(row["seconds"]) >= 1]
    return rows


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


def read_claude_sessions(folders: Sequence[str], since: datetime) -> Optional[List[List[datetime]]]:
    """Each session log's message timestamps, nothing else of the file. None when no folder exists, so the day is not touched."""
    existing = [folder for folder in folders if os.path.isdir(folder)]
    if not existing:
        return None
    sessions: List[List[datetime]] = []
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
                    sessions.append(stamps)
    return sessions


def payload(
    device: str,
    results: List[List[dict]],
    days: List[date],
    sites: Optional[List[List[dict]]] = None,
    claude: Optional[Dict[date, int]] = None,
) -> dict:
    """`sites` and `claude` are left out when their source is not there: the server then keeps what it has."""
    rows = []
    for index, (day, events) in enumerate(zip(days, results)):
        row: Dict[str, object] = {"day": day.isoformat(), "apps": summarize(events)}
        if sites is not None:
            row["sites"] = summarize_sites(sites[index])
        if claude is not None:
            row["claude_code_seconds"] = claude.get(day, 0)
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


def read_sites(client, hostname: Optional[str], periods: List[Tuple[datetime, datetime]]) -> Optional[List[List[dict]]]:
    """Every browser watcher's tab events per day, or None when no browser watcher exists."""
    buckets = web_buckets(client.get_buckets(), hostname)
    if not buckets:
        return None
    merged: List[List[dict]] = [[] for _ in periods]
    for bucket in buckets:
        for index, events in enumerate(client.query("\n".join(build_web_query(hostname, bucket)), periods)):
            merged[index].extend(events)
    return merged


def sync(
    client,
    url: str,
    key: str,
    count: int,
    hostname: Optional[str],
    device: Optional[str],
    dry_run: bool,
    claude_dirs: Optional[Sequence[str]] = None,
) -> dict:
    days = days_back(datetime.now(TIME_ZONE).date(), max(1, min(count, MAX_DAYS)))
    periods = [period_of(day) for day in days]
    try:
        results = client.query("\n".join(build_query(hostname)), periods)
        sites = read_sites(client, hostname, periods)
    except requests.exceptions.ConnectionError:
        raise RuntimeError(f"ActivityWatch is not answering at {client.server_address}: open it and try again.") from None
    sessions = read_claude_sessions(claude_dirs_from(claude_dirs), periods[0][0].astimezone(timezone.utc))
    claude = None if sessions is None else claude_code_seconds(sessions, days)
    body = payload(device or socket.gethostname(), results, days, sites, claude)
    return body if dry_run else upload(url, key, body)
