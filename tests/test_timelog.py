from datetime import date

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
        def query(self, query, periods):
            asked["periods"] = periods
            return [[event("Code.exe", 60)] for _ in periods]

    def no_upload(*args, **kwargs):
        raise AssertionError("a dry run uploads nothing")

    monkeypatch.setattr(timelog, "upload", no_upload)
    body = timelog.sync(Client(), "https://x", "tl_k", 2, None, "PC", dry_run=True)
    assert len(asked["periods"]) == 2
    assert [day["apps"] for day in body["days"]] == [[{"app": "Code.exe", "seconds": 60}]] * 2
