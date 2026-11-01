import json
from datetime import date, datetime, timedelta, timezone

from aw_client import timelog


def event(app, seconds, title="secret title"):
    return {"duration": seconds, "data": {"app": app, "title": title}}


def test_summarize_merges_by_app_longest_first_and_never_keeps_a_title():
    rows = timelog.summarize(
        [event("chrome.exe", 60.9), event("Code.exe", 3000), event("chrome.exe", 100), event("idle", 0.4), event("", 50)]
    )
    assert rows == [{"app": "Code.exe", "seconds": 3000}, {"app": "chrome.exe", "seconds": 160}]
    assert "title" not in str(rows)


def test_summarize_scales_a_day_past_24_hours_down_to_it():
    rows = timelog.summarize([event("a", 60000), event("b", 40000)])
    assert sum(row["seconds"] for row in rows) <= 86400


def test_days_back_ends_today_oldest_first():
    assert timelog.days_back(date(2026, 11, 1), 3) == [date(2026, 10, 30), date(2026, 10, 31), date(2026, 11, 1)]


def test_a_day_is_bogota_midnight_to_midnight():
    start, stop = timelog.period_of(date(2026, 10, 28))
    assert start.isoformat() == "2026-10-28T00:00:00-05:00"
    assert (stop - start).total_seconds() == 86400


def test_payload_pairs_each_day_with_its_period_result():
    body = timelog.payload("PC", [[event("Code.exe", 120)], []], [date(2026, 10, 27), date(2026, 10, 28)])
    assert body == {
        "device": "PC",
        "days": [{"day": "2026-10-27", "apps": [{"app": "Code.exe", "seconds": 120}]}, {"day": "2026-10-28", "apps": []}],
    }


def test_the_query_reads_only_apps_with_afk_filtered_out():
    query = "\n".join(timelog.build_query("pc-casa"))
    assert 'find_bucket("aw-watcher-window_", "pc-casa")' in query
    assert 'filter_keyvals(afk, "status", ["not-afk"])' in query
    assert 'merge_events_by_keys(window, ["app"])' in query
    assert "title" not in query


def test_sync_queries_one_period_per_day_and_uploads_nothing_on_a_dry_run(monkeypatch):
    asked = {}

    monkeypatch.setattr(timelog.browser_history, "load_history", lambda paths=None: (timelog.browser_history.History(), []))

    class Client:
        def query(self, query, periods):
            asked["periods"] = periods
            return [[event("Code.exe", 60)] for _ in periods]

    def no_upload(*args, **kwargs):
        raise AssertionError("a dry run uploads nothing")

    monkeypatch.setattr(timelog, "upload", no_upload)
    body = timelog.sync(Client(), "https://x", "tl_k", 2, None, "PC", dry_run=True)
    assert len(asked["periods"]) == 2
    assert [day["apps"] for day in body["days"]] == [[{"app": "Code.exe", "seconds": 60}]] * 2


def test_summarize_drops_control_characters_from_a_name():
    assert timelog.summarize([event("a\x00b.exe", 60)]) == [{"app": "ab.exe", "seconds": 60}]


def tab(url, seconds, title="secret page"):
    return {"duration": seconds, "data": {"url": url, "title": title}}


def test_host_of_keeps_the_hostname_alone():
    assert timelog.host_of("https://www.GitHub.com/WAB-Studio/shelf?tab=x#y") == "github.com"
    assert timelog.host_of("https://claude.ai/chat/abc") == "claude.ai"
    assert timelog.host_of("http://user:pw@docs.example.org:8080/a") == "docs.example.org"
    assert timelog.host_of("http://localhost:3000/") == "localhost"
    assert timelog.host_of("https://youtube.com.") == "youtube.com"


def test_host_of_drops_what_is_not_a_site():
    for url in ["", None, "about:blank", "chrome://extensions", "file:///C:/x.txt", "https://", "https://[::1]/", "not a url", "https://a b.com/"]:
        assert timelog.host_of(url) is None, url


def test_summarize_sites_adds_by_host_longest_first_and_never_keeps_a_path_or_title():
    rows = timelog.summarize_sites(
        [
            tab("https://www.github.com/a/b", 100),
            tab("https://github.com/c?x=1", 50),
            tab("https://claude.ai/chat/1", 900),
            tab("about:blank", 400),
            tab("https://tiny.dev/", 0.4),
        ]
    )
    assert rows == [{"host": "claude.ai", "seconds": 900}, {"host": "github.com", "seconds": 150}]
    assert "chat" not in str(rows) and "secret" not in str(rows)


def test_summarize_sites_scales_a_day_past_24_hours_down_to_it():
    rows = timelog.summarize_sites([tab("https://a.com/", 60000), tab("https://b.com/", 40000)])
    assert sum(row["seconds"] for row in rows) <= 86400


def test_the_browser_query_reads_focused_browser_windows_event_by_event_over_not_afk_time():
    query = "\n".join(timelog.build_browser_query("pc-casa"))
    assert 'find_bucket("aw-watcher-window_", "pc-casa")' in query
    assert 'filter_keyvals(afk, "status", ["not-afk"])' in query
    assert 'filter_keyvals(window, "app", ["chrome.exe"' in query
    assert "merge_events" not in query and "aw-watcher-web" not in query


def test_the_titles_query_leaves_browsers_to_the_browser_query():
    assert 'exclude_keyvals(window, "app", ["chrome.exe"' in "\n".join(timelog.build_titles_query(None))


def at(clock, day=30):
    """Bogotá wall clock (UTC-5) on 2026-10-`day`, as the UTC timestamp Claude Code writes."""
    hour, minute, second = (int(part) for part in clock.split(":"))
    return datetime(2026, 10, day, hour, minute, second, tzinfo=timezone.utc) + timedelta(hours=5)


DAY = date(2026, 10, 30)


def test_messages_five_minutes_apart_or_less_join_up_and_a_gap_breaks_them():
    stamps = [at("09:00:00"), at("09:03:00"), at("09:08:00"), at("09:20:00"), at("09:24:00")]
    assert timelog.claude_code_seconds([stamps], [DAY]) == {DAY: (8 + 4) * 60}


def test_a_message_alone_counts_one_minute():
    assert timelog.claude_code_seconds([[at("10:00:00")]], [DAY]) == {DAY: 60}
    assert timelog.claude_code_seconds([[at("10:00:00"), at("10:30:00"), at("11:30:00")]], [DAY]) == {DAY: 180}


def test_two_sessions_at_once_count_once():
    one = [at("09:00:00"), at("09:05:00"), at("09:10:00")]
    two = [at("09:02:00"), at("09:07:00"), at("09:10:00")]
    assert timelog.claude_code_seconds([one, two], [DAY]) == {DAY: 10 * 60}


def test_a_stretch_across_midnight_is_cut_at_the_bogota_day():
    stamps = [at("23:58:00"), at("00:02:00", 31)]
    both = timelog.claude_code_seconds([stamps], [DAY, date(2026, 10, 31)])
    assert both == {DAY: 120, date(2026, 10, 31): 120}


def test_a_day_with_no_messages_is_zero():
    assert timelog.claude_code_seconds([], [DAY]) == {DAY: 0}


def test_timestamps_parse_with_z_milliseconds_and_no_zone():
    assert timelog.parse_stamp("2026-10-30T14:00:00.123Z") == datetime(2026, 10, 30, 14, 0, 0, 123000, tzinfo=timezone.utc)
    assert timelog.parse_stamp("2026-10-30T14:00:00.5+00:00").microsecond == 500000
    assert timelog.parse_stamp("2026-10-30T14:00:00").tzinfo is not None
    assert timelog.parse_stamp("nope") is None and timelog.parse_stamp(None) is None


def test_reading_session_logs_takes_timestamps_alone(tmp_path):
    project = tmp_path / "-home-me-app"
    project.mkdir()
    secret = "do not leave this PC"
    lines = [
        {"type": "user", "timestamp": "2026-10-30T14:00:00.000Z", "message": {"content": secret}},
        {"type": "summary", "summary": secret},
        {"type": "assistant", "timestamp": "2026-10-30T14:02:00.000Z", "message": {"content": secret}},
    ]
    (project / "s1.jsonl").write_text("\n".join(json.dumps(line) for line in lines) + "\nnot json \"timestamp\"\n", encoding="utf-8")
    (project / "notes.txt").write_text("2026-10-30T15:00:00Z", encoding="utf-8")
    sessions = timelog.read_claude_sessions([str(tmp_path)], datetime(2026, 10, 1, tzinfo=timezone.utc))
    assert sessions == [
        timelog.ClaudeSession("-home-me-app", "s1", [datetime(2026, 10, 30, 14, 0, tzinfo=timezone.utc), datetime(2026, 10, 30, 14, 2, tzinfo=timezone.utc)])
    ]
    assert timelog.claude_code_seconds([one.stamps for one in sessions], [DAY]) == {DAY: 120}
    assert secret not in str(timelog.claude_session_rows(sessions, [DAY]))


def test_a_log_older_than_the_window_is_skipped(tmp_path):
    (tmp_path / "old.jsonl").write_text(json.dumps({"timestamp": "2026-10-30T14:00:00Z"}) + "\n", encoding="utf-8")
    assert timelog.read_claude_sessions([str(tmp_path)], datetime(2100, 1, 1, tzinfo=timezone.utc)) == []


def test_no_claude_folder_is_none_so_the_server_keeps_its_days(tmp_path):
    assert timelog.read_claude_sessions([str(tmp_path / "missing")], datetime(2026, 10, 1, tzinfo=timezone.utc)) is None


def test_claude_folders_come_from_a_list_or_a_semicolon_string_else_the_default():
    assert timelog.claude_dirs_from("C:\\a;  \\\\wsl$\\Ubuntu\\x ;") == ["C:\\a", "\\\\wsl$\\Ubuntu\\x"]
    assert timelog.claude_dirs_from(["/a", "/b"]) == ["/a", "/b"]
    assert timelog.claude_dirs_from("") == timelog.default_claude_dirs()
    assert timelog.claude_dirs_from(()) == timelog.default_claude_dirs()


def test_payload_carries_sites_and_claude_seconds_only_when_their_source_exists():
    days = [date(2026, 10, 29), DAY]
    old = timelog.payload("PC", [[], []], days)
    assert all(set(day) == {"day", "apps"} for day in old["days"])
    body = timelog.payload(
        "PC", [[], []], days, sites=[[tab("https://claude.ai/x", 90, "Chat")], []], claude={DAY: 600}
    )
    assert body["days"][0] == {
        "day": "2026-10-29",
        "apps": [],
        "sites": [{"host": "claude.ai", "seconds": 90}],
        "pages": [{"host": "claude.ai", "url": "https://claude.ai/x", "title": "Chat", "seconds": 90}],
        "claude_code_seconds": 0,
    }
    assert body["days"][1]["sites"] == [] and body["days"][1]["claude_code_seconds"] == 600


def test_sync_sends_no_sites_without_a_browser_history_and_reads_claude_from_the_folder(monkeypatch, tmp_path):
    monkeypatch.setattr(timelog.browser_history, "load_history", lambda paths=None: (timelog.browser_history.History(), []))

    class Client:
        def query(self, query, periods):
            return [[event("Code.exe", 60)] for _ in periods]

    now = datetime.now(timezone.utc).isoformat()
    (tmp_path / "s.jsonl").write_text(json.dumps({"timestamp": now}) + "\n", encoding="utf-8")
    body = timelog.sync(Client(), "https://x", "tl_k", 1, None, "PC", True, [str(tmp_path)])
    (day,) = body["days"]
    assert "sites" not in day
    assert 0 <= day["claude_code_seconds"] <= 60


def window(app, title, seconds):
    return {"duration": seconds, "data": {"app": app, "title": title}}


def test_page_of_keeps_the_path_and_drops_credentials_query_and_fragment():
    assert timelog.page_of("https://user:pw@www.GitHub.com:8443/WAB-Studio/shelf/pull/1?tab=files#diff") == (
        "github.com",
        "https://github.com:8443/WAB-Studio/shelf/pull/1",
    )
    assert timelog.page_of("https://claude.ai/") == ("claude.ai", "https://claude.ai/")
    assert timelog.page_of("about:blank") is None
    assert timelog.page_of("https://x.com/" + "a" * 600) is None


def test_pages_add_by_address_and_title_and_never_keep_a_query():
    rows = timelog.summarize_pages(
        [tab("https://a.com/x?token=SECRET", 30, "Page X"), tab("https://a.com/x#frag", 20, "Page X"), tab("https://a.com/y", 5, "Y")]
    )
    assert rows == [
        {"host": "a.com", "url": "https://a.com/x", "title": "Page X", "seconds": 50},
        {"host": "a.com", "url": "https://a.com/y", "title": "Y", "seconds": 5},
    ]
    assert "SECRET" not in str(rows)


def test_the_default_exclusion_list_drops_banks_and_password_managers_everywhere():
    skip = timelog.exclusions_from(None)
    for host in ["bancolombia.com", "www.bancolombia.com", "sucursal.davivienda.com", "bbva.com.co", "nequi.com.co", "daviplata.com", "vault.bitwarden.com", "my.1password.com"]:
        assert timelog.is_excluded(host, skip), host
    assert not timelog.is_excluded("github.com", skip)
    assert not timelog.is_excluded("notbancolombia.com", skip)
    events = [tab("https://www.bancolombia.com/personas", 600, "Mis cuentas"), tab("https://github.com/a", 60, "a")]
    assert timelog.summarize_sites(events, skip) == [{"host": "github.com", "seconds": 60}]
    assert [row["host"] for row in timelog.summarize_pages(events, skip)] == ["github.com"]
    apps = timelog.summarize([event("KeePassXC.exe", 300), event("Code.exe", 60), event("1Password.exe", 50)], skip)
    assert apps == [{"app": "Code.exe", "seconds": 60}]
    assert timelog.summarize_windows([window("Bitwarden.exe", "Vault", 100)], skip) == []


def test_the_exclusion_setting_replaces_the_default_and_an_empty_one_is_none():
    assert timelog.exclusions_from(None) == timelog.DEFAULT_EXCLUDE
    assert timelog.exclusions_from("Mybank.com; Foo.exe ;") == ["mybank.com", "foo.exe"]
    assert timelog.exclusions_from([]) == []
    assert timelog.is_excluded("foo.exe", ["foo"]) and timelog.is_excluded("Foo", ["foo.exe"])


def test_window_titles_per_app_skip_browsers_private_windows_and_empty_titles():
    rows = timelog.summarize_windows(
        [
            window("Code.exe", "shelf - VS Code", 100),
            window("Code.exe", "shelf - VS Code", 50),
            window("chrome.exe", "Bank - Google Chrome", 500),
            window("Code.exe", "", 40),
            window("WindowsTerminal.exe", "Incognito sketch", 70),
            window("Slack.exe", "general | Slack", 30),
        ]
    )
    assert rows == [
        {"app": "Code.exe", "title": "shelf - VS Code", "seconds": 150},
        {"app": "Slack.exe", "title": "general | Slack", "seconds": 30},
    ]
    kept = timelog.summarize_windows([window("chrome.exe", "Bank - Google Chrome", 500)], keep_browsers=True)
    assert kept == [{"app": "chrome.exe", "title": "Bank - Google Chrome", "seconds": 500}]


def test_a_private_window_title_or_page_is_dropped():
    assert timelog.summarize_windows([window("Edge.exe", "x - InPrivate", 10), window("Code.exe", "Private Browsing notes", 10)]) == []
    assert timelog.summarize_pages([tab("https://a.com/", 10, "a - Navegación privada")]) == []


def test_claude_sessions_per_day_carry_project_session_first_last_and_seconds(tmp_path):
    project = tmp_path / "-home-me-projects-shelf"
    (project / "s1" / "subagents").mkdir(parents=True)
    lines = lambda *times: "\n".join(json.dumps({"timestamp": t}) for t in times) + "\n"
    (project / "s1.jsonl").write_text(lines("2026-10-30T14:00:00Z", "2026-10-30T14:03:00Z"), encoding="utf-8")
    (project / "s1" / "subagents" / "agent-1.jsonl").write_text(lines("2026-10-30T14:04:00Z"), encoding="utf-8")
    (project / "s2.jsonl").write_text(lines("2026-10-30T20:00:00Z"), encoding="utf-8")
    sessions = timelog.read_claude_sessions([str(tmp_path)], datetime(2026, 10, 1, tzinfo=timezone.utc))
    rows = timelog.claude_session_rows(sessions, [DAY])[DAY]
    assert rows == [
        {"session": "s1", "project": "-home-me-projects-shelf", "first": "2026-10-30T14:00:00Z", "last": "2026-10-30T14:04:00Z", "seconds": 240 + 0},
        {"session": "s2", "project": "-home-me-projects-shelf", "first": "2026-10-30T20:00:00Z", "last": "2026-10-30T20:00:00Z", "seconds": 60},
    ]


def test_payload_names_windows_and_sessions_when_given_and_applies_the_exclusion():
    body = timelog.payload(
        "PC",
        [[event("Code.exe", 60), event("KeePass.exe", 90)]],
        [DAY],
        titles=[[window("Code.exe", "a.py", 60)]],
        sessions={DAY: [{"session": "s", "project": "p", "first": "x", "last": "y", "seconds": 5}]},
        exclude=timelog.DEFAULT_EXCLUDE,
    )
    (day,) = body["days"]
    assert day["apps"] == [{"app": "Code.exe", "seconds": 60}]
    assert day["windows"] == [{"app": "Code.exe", "title": "a.py", "seconds": 60}]
    assert day["claude_sessions"][0]["session"] == "s"
