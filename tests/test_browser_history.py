import os
import sqlite3
from datetime import datetime, timezone

from aw_client import browser_history as bh
from aw_client import timelog

NOW = datetime(2026, 10, 30, 15, 0, 0, tzinfo=timezone.utc).timestamp()


def chromium_db(path, rows):
    """rows: (url, title, unix seconds)."""
    connection = sqlite3.connect(path)
    connection.execute("create table urls (id integer primary key, url text, title text, visit_count int, last_visit_time integer)")
    for url, title, when in rows:
        connection.execute("insert into urls (url, title, last_visit_time) values (?, ?, ?)", (url, title, int((when + bh.CHROMIUM_EPOCH_OFFSET) * 1_000_000)))
    connection.commit()
    connection.close()


def firefox_db(path, rows):
    connection = sqlite3.connect(path)
    connection.execute("create table moz_places (id integer primary key, url text, title text, last_visit_date integer)")
    for url, title, when in rows:
        connection.execute("insert into moz_places (url, title, last_visit_date) values (?, ?, ?)", (url, title, int(when * 1_000_000)))
    connection.commit()
    connection.close()


def test_the_browser_suffix_is_stripped_per_browser():
    cases = {
        "Inbox (3) - Google Chrome": "Inbox (3)",
        "Docs — Mozilla Firefox": "Docs",
        "Docs - Mozilla Firefox": "Docs",
        "News - Microsoft​ Edge": "News",
        "News - Personal - Microsoft​ Edge": "News",
        "Home - Brave": "Home",
        "Home - Opera": "Home",
        "Home - Vivaldi": "Home",
        "Mail - Chromium": "Mail",
    }
    for window_title, page in cases.items():
        assert page in bh.page_titles(window_title), window_title
    assert bh.page_titles("a - b - Google Chrome") == ["a - b"]
    assert "A - Personal" in bh.page_titles("A - Personal - Microsoft Edge") and "A" in bh.page_titles("A - Personal - Microsoft Edge")
    assert "Page" in bh.page_titles("Page and 4 more pages - Work - Microsoft​ Edge")
    assert bh.page_titles("main.py - Visual Studio Code") == []


def test_the_url_is_the_exact_title_nearest_the_visit(tmp_path):
    path = str(tmp_path / "History")
    chromium_db(path, [("https://a.com/old", "Inbox", NOW - 86400), ("https://a.com/new", "Inbox", NOW - 60), ("https://b.com/", "Other", NOW)])
    history, problems = bh.load_history([path])
    assert problems == []
    assert history.find("Inbox", NOW) == "https://a.com/new"
    assert history.find("Inbox", NOW - 80000) == "https://a.com/old"
    assert history.find("inbox", NOW) == "https://a.com/new"
    assert history.find("Inbox 2", NOW) is None


def test_firefox_places_are_read_too(tmp_path):
    profile = tmp_path / "Profiles" / "abc.default"
    profile.mkdir(parents=True)
    firefox_db(str(profile / "places.sqlite"), [("https://mozilla.org/en/", "Mozilla", NOW)])
    history, _ = bh.load_history([str(tmp_path / "Profiles")])
    assert history.find("Mozilla", NOW) == "https://mozilla.org/en/"


def test_a_data_folder_gives_every_profile_and_no_config_finds_the_usual_places(tmp_path):
    for name in ["Default", "Profile 1"]:
        (tmp_path / "Google" / "Chrome" / "User Data" / name).mkdir(parents=True)
        chromium_db(str(tmp_path / "Google" / "Chrome" / "User Data" / name / "History"), [(f"https://{name[-1]}.com/", name, NOW)])
    found = bh.history_files(None, {"LOCALAPPDATA": str(tmp_path)})
    assert [os.path.basename(os.path.dirname(f)) for f in found if str(tmp_path) in f] == ["Default", "Profile 1"]
    assert len(bh.history_files([str(tmp_path / "Google" / "Chrome" / "User Data")])) == 2


def test_a_locked_file_is_read_from_a_copy_and_the_original_is_untouched(tmp_path):
    path = str(tmp_path / "History")
    chromium_db(path, [("https://a.com/", "Locked", NOW)])
    connection = sqlite3.connect(path)
    connection.execute("pragma journal_mode=wal")
    connection.execute("insert into urls (url, title, last_visit_time) values ('https://wal.com/', 'In the wal', ?)", (int((NOW + bh.CHROMIUM_EPOCH_OFFSET) * 1_000_000),))
    connection.commit()
    # The browser still holds the file open and a write lock.
    connection.execute("begin exclusive")
    before = {name: (tmp_path / name).read_bytes() for name in os.listdir(tmp_path)}
    assert "History-wal" in before
    history, problems = bh.load_history([path])
    untouched = {name: (tmp_path / name).read_bytes() for name in before} == before
    connection.rollback()
    connection.close()
    assert problems == [] and untouched
    assert history.find("Locked", NOW) == "https://a.com/" and history.find("In the wal", NOW) == "https://wal.com/"


def test_an_unreadable_file_is_a_problem_not_a_crash(tmp_path):
    bad = tmp_path / "History"
    bad.write_bytes(b"not a database")
    history, problems = bh.load_history([str(bad)])
    assert not history and len(problems) == 1


def browser_event(title, seconds, app="chrome.exe"):
    return {"timestamp": "2026-10-30T15:00:00+00:00", "duration": seconds, "data": {"app": app, "title": title}}


def test_browser_time_becomes_pages_and_what_nothing_places_is_not_sent():
    history = bh.History([bh.Visit("https://github.com/WAB-Studio/shelf?tab=x", "shelf", NOW), bh.Visit("https://www.bancolombia.com/personas", "Mis cuentas", NOW)])
    events = [
        browser_event("shelf - Google Chrome", 120),
        browser_event("Mis cuentas - Google Chrome", 300),
        browser_event("Unknown thing - Google Chrome", 40),
        browser_event("Secret - Google Chrome (Incognito)", 500),
        browser_event("Secreto - Google Chrome (Modo incógnito)", 500),
    ]
    (pages,) = timelog.resolve_browser([events], history)
    assert timelog.summarize_pages(pages, timelog.DEFAULT_EXCLUDE) == [
        {"host": "github.com", "url": "https://github.com/WAB-Studio/shelf", "title": "shelf", "seconds": 120}
    ]
    assert timelog.summarize_sites(pages, timelog.DEFAULT_EXCLUDE) == [{"host": "github.com", "seconds": 120}]
    assert "Unknown" not in str(pages)


def test_sync_reads_the_browser_history_and_sends_pages_without_any_web_bucket(monkeypatch, tmp_path):
    path = str(tmp_path / "History")
    chromium_db(path, [("https://claude.ai/chat/1?x=1", "Chat", NOW)])
    queries = []

    class Client:
        def query(self, query, periods):
            queries.append(query)
            if 'filter_keyvals(window, "app"' in query:
                return [[browser_event("Chat - Google Chrome", 100), browser_event("Nowhere - Google Chrome", 30)] for _ in periods]
            return [[{"duration": 60, "data": {"app": "Code.exe", "title": "a.py"}}] for _ in periods]

    body = timelog.sync(Client(), "https://x", "tl_k", 1, None, "PC", True, [str(tmp_path / "none")], None, [path])
    (day,) = body["days"]
    assert day["sites"] == [{"host": "claude.ai", "seconds": 100}]
    assert day["pages"][0]["url"] == "https://claude.ai/chat/1"
    assert all(row["app"] != "chrome.exe" for row in day["windows"])
    assert not any("aw-watcher-web" in q for q in queries)


def test_opera_and_arc_are_searched_in_their_usual_places(tmp_path):
    opera = tmp_path / "roaming" / "Opera Software" / "Opera Stable"
    arc = tmp_path / "local" / "Packages" / "TheBrowserCompany.Arc_abc" / "LocalCache" / "Local" / "Arc" / "User Data" / "Default"
    for folder in (opera, arc):
        folder.mkdir(parents=True)
        chromium_db(str(folder / "History"), [("https://a.com/", "A", NOW)])
    found = bh.history_files(None, {"APPDATA": str(tmp_path / "roaming"), "LOCALAPPDATA": str(tmp_path / "local")})
    assert str(opera / "History") in found and str(arc / "History") in found
