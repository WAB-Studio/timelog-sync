from datetime import datetime, timezone

from aw_client import address_bar as ab
from aw_client import timelog
from aw_client.browser_history import History, Visit

T0 = datetime(2026, 10, 30, 15, 0, 0, tzinfo=timezone.utc).timestamp()


def reading(address, title="Page - Google Chrome", app="chrome.exe", **kw):
    return ab.Reading(app, title, address, **kw)


def test_addresses_typed_without_a_scheme_are_normalized():
    assert ab.normalize_address("github.com/a/b") == "https://github.com/a/b"
    assert ab.normalize_address("localhost:3000/x") == "http://localhost:3000/x"
    assert ab.normalize_address("192.168.1.2:8080") == "http://192.168.1.2:8080"
    assert ab.normalize_address("https://claude.ai/chat") == "https://claude.ai/chat"
    for text in ["", "how to cook rice", "chrome://settings", "about:blank", "word", None]:
        assert ab.normalize_address(text) is None, text


def test_a_sample_keeps_the_address_without_query_and_fragment_and_the_title_without_the_browser():
    assert ab.sample_of(reading("github.com/a?token=SECRET#x", "Repo - Google Chrome"), []) == ("github.com", "https://github.com/a", "Repo")


def test_private_windows_and_the_bar_being_typed_in_are_skipped():
    assert ab.sample_of(reading("a.com", "x - Google Chrome (Incognito)"), []) is None
    assert ab.sample_of(reading("a.com", "x - Microsoft Edge InPrivate"), []) is None
    assert ab.sample_of(reading("a.com", "x - Mozilla Firefox (Private Browsing)", app="firefox.exe"), []) is None
    assert ab.sample_of(reading("a.com", private=True), []) is None
    assert ab.sample_of(reading("a.com", focused_bar=True), []) is None
    assert ab.sample_of(reading("a.com", app="Code.exe"), []) is None


def test_an_excluded_host_is_never_written_to_disk(tmp_path):
    store = ab.AddressStore(str(tmp_path / "a.sqlite"))
    clock = iter([T0, T0 + 5, T0 + 10])
    poller = ab.Poller(lambda: reading("www.bancolombia.com/personas"), store, lambda: timelog.DEFAULT_EXCLUDE, 5, lambda: next(clock))
    assert poller.tick() is None and poller.tick() is None
    assert store.between(0, T0 * 2) == []
    assert "bancolombia" not in open(tmp_path / "a.sqlite", "rb").read().decode("latin-1")


def test_the_store_cuts_at_day_boundaries(tmp_path):
    store = ab.AddressStore(str(tmp_path / "a.sqlite"))
    midnight = T0
    store.add(ab.Sample(midnight - 30, midnight - 10, "a.com", "https://a.com/", "A"))
    store.add(ab.Sample(midnight - 5, midnight + 5, "b.com", "https://b.com/", "B"))
    store.add(ab.Sample(midnight + 100, midnight + 110, "c.com", "https://c.com/", "C"))
    assert [s.host for s in store.between(midnight, midnight + 86400)] == ["b.com", "c.com"]
    assert [s.host for s in store.between(midnight - 86400, midnight)] == ["a.com", "b.com"]


def test_a_long_gap_is_not_time_on_a_page(tmp_path):
    store = ab.AddressStore(str(tmp_path / "a.sqlite"))
    clock = iter([T0, T0 + 3600])
    poller = ab.Poller(lambda: reading("a.com"), store, lambda: [], 5, lambda: next(clock))
    poller.tick()
    sample = poller.tick()
    assert sample.end - sample.start == 5


def browser_event(title, start, seconds):
    return {"timestamp": datetime.fromtimestamp(start, timezone.utc).isoformat(), "duration": seconds, "data": {"app": "chrome.exe", "title": title}}


def test_only_the_samples_inside_not_afk_browser_time_count_and_the_rest_falls_back_to_history(tmp_path):
    store = ab.AddressStore(str(tmp_path / "a.sqlite"))
    # Sampled 0-60 s; the person was at the computer only 20-50 s (the event), then 50-80 s more with no sample.
    store.add(ab.Sample(T0, T0 + 60, "a.com", "https://a.com/x", "X"))
    history = History([Visit("https://h.com/y", "X", T0)])
    (pages,) = timelog.resolve_browser([[browser_event("X - Google Chrome", T0 + 20, 60)]], history, store)
    rows = timelog.summarize_pages(pages)
    assert {(r["url"], r["seconds"]) for r in rows} == {("https://a.com/x", 40), ("https://h.com/y", 20)}


def test_with_no_samples_the_history_decides_and_with_neither_nothing_is_sent():
    history = History([Visit("https://h.com/y", "X", T0)])
    (pages,) = timelog.resolve_browser([[browser_event("X - Google Chrome", T0, 30), browser_event("Z - Google Chrome", T0, 30)]], history, None)
    assert [r["url"] for r in timelog.summarize_pages(pages)] == ["https://h.com/y"]


def test_the_poller_does_not_start_when_switched_off_or_without_a_reader():
    assert ab.start(lambda: {"poll_seconds": 0}, reader=lambda: None) is None
    assert ab.start(lambda: {"poll_seconds": 5}, reader=None) is None or ab.windows_reader() is not None
