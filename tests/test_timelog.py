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

    class Client:
        def get_buckets(self):
            return {}

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


def test_the_web_query_returns_the_url_and_never_the_title():
    query = "\n".join(timelog.build_web_query("pc-casa", "aw-watcher-web-chrome"))
    assert 'query_bucket("aw-watcher-web-chrome")' in query
    assert 'filter_keyvals(afk, "status", ["not-afk"])' in query
    assert 'merge_events_by_keys(tabs, ["url"])' in query
    assert "title" not in query


def test_web_buckets_are_the_browser_watchers_of_this_computer():
    buckets = {
        "aw-watcher-web-chrome": {"hostname": "pc"},
        "aw-watcher-web-firefox": {"hostname": "other"},
        "aw-watcher-window_pc": {"hostname": "pc"},
    }
    assert timelog.web_buckets(buckets, "pc") == ["aw-watcher-web-chrome"]
    assert timelog.web_buckets(buckets, None) == ["aw-watcher-web-chrome", "aw-watcher-web-firefox"]
    assert timelog.web_buckets({"aw-watcher-window_pc": {}}, None) == []


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
    assert sessions == [[datetime(2026, 10, 30, 14, 0, tzinfo=timezone.utc), datetime(2026, 10, 30, 14, 2, tzinfo=timezone.utc)]]
    assert timelog.claude_code_seconds(sessions, [DAY]) == {DAY: 120}


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
        "PC", [[], []], days, sites=[[tab("https://claude.ai/x", 90)], []], claude={DAY: 600}
    )
    assert body["days"][0] == {
        "day": "2026-10-29",
        "apps": [],
        "sites": [{"host": "claude.ai", "seconds": 90}],
        "claude_code_seconds": 0,
    }
    assert body["days"][1]["sites"] == [] and body["days"][1]["claude_code_seconds"] == 600


def test_sync_sends_no_sites_without_a_browser_watcher_and_reads_claude_from_the_folder(monkeypatch, tmp_path):
    class Client:
        def get_buckets(self):
            return {"aw-watcher-window_pc": {"hostname": "pc"}}

        def query(self, query, periods):
            return [[event("Code.exe", 60)] for _ in periods]

    now = datetime.now(timezone.utc).isoformat()
    (tmp_path / "s.jsonl").write_text(json.dumps({"timestamp": now}) + "\n", encoding="utf-8")
    body = timelog.sync(Client(), "https://x", "tl_k", 1, None, "PC", True, [str(tmp_path)])
    (day,) = body["days"]
    assert "sites" not in day
    assert 0 <= day["claude_code_seconds"] <= 60
