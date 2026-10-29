"""
Uploads the day's time per app to TimeLog: app names and seconds only, never a window title.

The query keeps the window events that overlap a not-AFK stretch and merges them by app,
the same way the web UI's "Top applications" does. A day is a calendar day in Bogotá.
"""

import socket
from datetime import date, datetime, time, timedelta
from typing import Dict, Iterable, List, Optional, Tuple
from zoneinfo import ZoneInfo

import requests

TIME_ZONE = ZoneInfo("America/Bogota")
MAX_APPS = 500
MAX_DAYS = 31
DAY_SECONDS = 86400


def build_query(hostname: Optional[str]) -> List[str]:
    host = f', "{hostname}"' if hostname else ""
    return [
        f'window = flood(query_bucket(find_bucket("aw-watcher-window_"{host})));',
        f'afk = flood(query_bucket(find_bucket("aw-watcher-afk_"{host})));',
        'not_afk = filter_keyvals(afk, "status", ["not-afk"]);',
        "window = filter_period_intersect(window, not_afk);",
        'RETURN = sort_by_duration(merge_events_by_keys(window, ["app"]));',
    ]


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


def payload(device: str, results: List[List[dict]], days: List[date]) -> dict:
    return {
        "device": device[:80],
        "days": [{"day": day.isoformat(), "apps": summarize(events)} for day, events in zip(days, results)],
    }


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


def sync(client, url: str, key: str, count: int, hostname: Optional[str], device: Optional[str], dry_run: bool) -> dict:
    days = days_back(datetime.now(TIME_ZONE).date(), max(1, min(count, MAX_DAYS)))
    try:
        results = client.query("\n".join(build_query(hostname)), [period_of(day) for day in days])
    except requests.exceptions.ConnectionError:
        raise RuntimeError(f"ActivityWatch is not answering at {client.server_address}: open it and try again.") from None
    body = payload(device or socket.gethostname(), results, days)
    return body if dry_run else upload(url, key, body)
