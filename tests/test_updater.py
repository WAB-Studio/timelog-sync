import hashlib
import os

import pytest

from aw_client import timelog_module, updater

SETUP = b"MZ fake setup bytes"
SHA = hashlib.sha256(SETUP).hexdigest()


class Response:
    def __init__(self, payload=None, body=b""):
        self.payload = payload
        self.body = body

    def raise_for_status(self):
        pass

    def json(self):
        return self.payload

    def iter_content(self, chunk_size):
        yield self.body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def release(tag="timelog-v0.3.0", digest=f"sha256:{SHA}", name="activitywatch-v0.3.0-windows-x86_64-setup.exe"):
    return {"tag_name": tag, "assets": [{"name": name, "browser_download_url": "https://example.invalid/setup.exe", "digest": digest}]}


def fake_get(payload, body=SETUP):
    def get(url, **kwargs):
        return Response(body=body) if kwargs.get("stream") else Response(payload=payload)

    return get


def bundle(tmp_path, version="0.2.0"):
    (tmp_path / updater.VERSION_FILE).write_text(version + "\n", encoding="utf-8")
    return str(tmp_path)


def test_only_a_numbered_version_file_is_a_managed_install(tmp_path):
    assert updater.installed_version(None) is None
    assert updater.installed_version(str(tmp_path)) is None, "no file: a pip install"
    assert updater.installed_version(bundle(tmp_path, "dev")) is None, "a branch build never updates"
    assert updater.installed_version(bundle(tmp_path, "0.2.0")) == (0, 2, 0)


def test_a_release_is_read_only_with_our_tag_and_a_sha256(tmp_path):
    get = lambda payload: (lambda url, **kwargs: Response(payload=payload))
    assert updater.latest_release(get(release()))["version"] == (0, 3, 0)
    assert updater.latest_release(get(release(tag="v0.13.0"))) is None, "upstream's tags are not ours"
    assert updater.latest_release(get(release(digest=""))) is None, "no digest, no download"
    assert updater.latest_release(get(release(name="activitywatch.zip"))) is None


def test_a_newer_release_is_downloaded_checked_and_started_silent_and_detached(tmp_path):
    started = []
    outcome = updater.update_if_newer(bundle(tmp_path), fake_get(release()), lambda args, **kwargs: started.append((args, kwargs)))
    assert outcome == "updating"
    (args, kwargs), = started
    assert "/VERYSILENT" in args and "/CLOSEAPPLICATIONS" in args
    assert kwargs["creationflags"] & 0x00000008, "detached: it outlives aw-timelog"
    with open(args[0], "rb") as file:
        assert file.read() == SETUP


def test_the_same_or_an_older_release_starts_nothing(tmp_path):
    never = lambda *args, **kwargs: pytest.fail("started a setup")
    assert updater.update_if_newer(bundle(tmp_path, "0.3.0"), fake_get(release()), never) == "current"
    assert updater.update_if_newer(bundle(tmp_path, "0.4.0"), fake_get(release()), never) == "current"
    assert updater.update_if_newer(None, fake_get(release()), never) == "unmanaged"


def test_a_setup_whose_bytes_do_not_match_is_deleted_and_never_started(tmp_path):
    never = lambda *args, **kwargs: pytest.fail("started a tampered setup")
    with pytest.raises(ValueError):
        updater.update_if_newer(bundle(tmp_path), fake_get(release(), body=b"tampered"), never)


def test_the_checker_runs_on_start_then_waits_six_hours_unless_the_tray_asks(tmp_path):
    now = [1000.0]
    calls = []
    checker = updater.Checker(str(tmp_path), None, lambda: True, clock=lambda: now[0])
    get = lambda url, **kwargs: calls.append(url) or Response(payload=release())
    assert checker.tick(get) == "unmanaged"
    now[0] += 60
    assert checker.tick(get) is None, "not due yet"
    (tmp_path / updater.REQUEST_FILE).write_text("", encoding="utf-8")
    assert checker.tick(get) == "unmanaged", "the tray's request is seen at once"
    assert not os.path.exists(tmp_path / updater.REQUEST_FILE), "and consumed"
    now[0] += updater.CHECK_SECONDS
    assert checker.tick(get) == "unmanaged"


def test_turned_off_it_checks_only_when_the_tray_asks(tmp_path):
    checker = updater.Checker(str(tmp_path), bundle(tmp_path), lambda: False)
    assert checker.tick(fake_get(release()), lambda *a, **k: pytest.fail("started")) == "disabled"
    (tmp_path / updater.REQUEST_FILE).write_text("", encoding="utf-8")
    started = []
    assert checker.tick(fake_get(release()), lambda args, **kwargs: started.append(args)) == "updating"
    assert started


def test_a_failed_check_is_logged_and_the_upload_loop_goes_on(tmp_path):
    def broken(url, **kwargs):
        raise ConnectionError("offline")

    checker = updater.Checker(str(tmp_path), bundle(tmp_path), lambda: True)
    assert checker.tick(broken) == "failed"


def test_the_wait_sleeps_in_ticks_and_checks_between_them(tmp_path):
    ticks = []

    class Probe:
        def tick(self):
            ticks.append(1)

    sleeps = []
    timelog_module.wait(70, sleeps.append, Probe())
    assert sleeps == [30, 30, 10]
    assert len(ticks) == 4
