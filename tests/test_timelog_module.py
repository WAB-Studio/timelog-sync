from aw_client import timelog, timelog_module

KEY = "tl_" + "a" * 43


def test_settings_default_to_timelog_and_no_key_when_the_file_is_missing(tmp_path):
    settings = timelog_module.read_settings(str(tmp_path / "missing.toml"))
    assert settings["url"] == timelog_module.DEFAULT_URL
    assert settings["key"] == ""
    assert settings["interval_minutes"] == 60


def test_a_broken_file_reads_as_the_defaults(tmp_path):
    path = tmp_path / "aw-timelog.toml"
    path.write_text("[aw-timelog\nkey = ", encoding="utf-8")
    assert timelog_module.read_settings(str(path))["key"] == ""


def test_write_key_keeps_the_other_settings(tmp_path):
    path = tmp_path / "aw-timelog.toml"
    path.write_text('[aw-timelog]\ninterval_minutes = 15\n', encoding="utf-8")
    timelog_module.write_key(f"  {KEY} ", device="PC de la casa", path=str(path))
    settings = timelog_module.read_settings(str(path))
    assert settings["key"] == KEY
    assert settings["device"] == "PC de la casa"
    assert settings["interval_minutes"] == 15


def test_with_no_key_it_sends_nothing_and_looks_again_soon(monkeypatch):
    monkeypatch.setattr(timelog, "sync", lambda *args: (_ for _ in ()).throw(AssertionError("sent")))
    settings = timelog_module.read_settings("/nonexistent")
    assert timelog_module.run_once(None, settings) == timelog_module.NO_KEY_SECONDS


def test_an_upload_waits_the_interval_and_a_failure_retries_sooner(monkeypatch):
    settings = {**timelog_module.read_settings("/nonexistent"), "key": KEY, "interval_minutes": 60}
    monkeypatch.setattr(timelog, "sync", lambda *args: {"written": 3, "days": 2})
    assert timelog_module.run_once(None, settings) == 3600

    def fail(*args):
        raise RuntimeError("ActivityWatch is not answering")

    monkeypatch.setattr(timelog, "sync", fail)
    assert timelog_module.run_once(None, settings) == timelog_module.RETRY_SECONDS


def test_the_loop_reads_the_file_again_before_every_upload(monkeypatch):
    reads = []
    waits = []

    def read():
        reads.append(1)
        if len(reads) > 2:
            raise SystemExit
        return {**timelog_module.read_settings("/nonexistent"), "key": KEY if len(reads) == 2 else ""}

    monkeypatch.setattr(timelog, "sync", lambda *args: {"written": 1, "days": 1})
    try:
        timelog_module.loop(None, read=read, sleep=waits.append)
    except SystemExit:
        pass
    assert waits == [timelog_module.NO_KEY_SECONDS, 3600]


def test_write_exclude_keeps_the_other_settings_and_is_read_back(tmp_path):
    path = tmp_path / "aw-timelog.toml"
    path.write_text('[aw-timelog]\ninterval_minutes = 15\n', encoding="utf-8")
    timelog_module.write_exclude(["MyBank.com", " Foo.exe ", ""], path=str(path))
    settings = timelog_module.read_settings(str(path))
    assert settings["interval_minutes"] == 15
    assert timelog.exclusions_from(settings["exclude"]) == ["mybank.com", "foo.exe"]
    assert timelog.exclusions_from(timelog_module.read_settings("/nonexistent")["exclude"]) == timelog.DEFAULT_EXCLUDE
