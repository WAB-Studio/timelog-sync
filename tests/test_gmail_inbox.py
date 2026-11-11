import base64
import json
import os
import threading

import pytest

from aw_client import gmail_inbox, timelog_module

HOMEFUND_KEY = "whk_" + "a" * 32
NOW = 1_800_000_000


def b64(text):
    return base64.urlsafe_b64encode(text.encode()).decode().rstrip("=")


def message(subject="Compra", text="Compra por $10.000", html="<p>Compra</p>"):
    return {
        "payload": {
            "mimeType": "multipart/alternative",
            "headers": [{"name": "Subject", "value": subject}],
            "parts": [
                {"mimeType": "text/plain", "body": {"data": b64(text)}},
                {"mimeType": "text/html", "body": {"data": b64(html)}},
            ],
        }
    }


class Fake:
    """Gmail, Google's token endpoint and HomeFund, in one stub of `_http`."""

    def __init__(self, ids=(), homefund=(200,)):
        self.ids = list(ids)
        self.homefund = list(homefund)
        self.calls = []
        self.posted = []

    def __call__(self, method, url, headers=None, body=None):
        self.calls.append((method, url, headers))
        if url == gmail_inbox.TOKEN_URL:
            return 200, json.dumps({"access_token": "at"}).encode()
        if url.endswith("/api/webhooks/email"):
            assert headers["Authorization"] == f"Bearer {HOMEFUND_KEY}"
            self.posted.append(json.loads(body))
            status = self.homefund.pop(0) if len(self.homefund) > 1 else self.homefund[0]
            return status, b"{}"
        if "/messages?" in url:
            return 200, json.dumps({"messages": [{"id": i} for i in self.ids]}).encode()
        if "/messages/" in url:
            assert "format=full" in url
            return 200, json.dumps(message(subject=url.rsplit("/", 1)[1].split("?")[0])).encode()
        raise AssertionError(url)

    def queries(self):
        return [u for _, u, _ in self.calls if "/messages?" in u]


@pytest.fixture
def setup(tmp_path):
    folder = tmp_path / "gmail"
    folder.mkdir()
    account = folder / "me@gmail.com.json"
    account.write_text(json.dumps({"client-id": "c", "client-secret": "s", "refresh-token": "r"}), encoding="utf-8")
    settings = {**timelog_module.read_settings("/nonexistent"), "homefund_key": HOMEFUND_KEY}
    return tmp_path, str(account), settings


def test_settings_default_to_every_ten_minutes_and_the_banks_query():
    settings = timelog_module.read_settings("/nonexistent")
    assert settings["gmail_minutes"] == 10
    assert settings["homefund_url"] == "https://our-piggy-bank.vercel.app"
    assert "nequi.com.co" in settings["gmail_query"]


def test_extract_reads_subject_and_both_bodies():
    assert gmail_inbox.extract(message()) == {"subject": "Compra", "text": "Compra por $10.000", "html": "<p>Compra</p>"}


def test_the_first_run_starts_from_now_and_sends_nothing(setup, monkeypatch):
    tmp, account, settings = setup
    fake = Fake(ids=["old"])
    monkeypatch.setattr(gmail_inbox, "_http", fake)
    assert gmail_inbox.run_round(str(tmp), settings, NOW) == 0
    assert fake.calls == []
    assert json.load(open(account[:-5] + ".state"))["after"] == NOW


def test_each_email_is_sent_once_across_rounds(setup, monkeypatch):
    tmp, account, settings = setup
    gmail_inbox.run_round(str(tmp), settings, NOW)
    fake = Fake(ids=["m1", "m2"])
    monkeypatch.setattr(gmail_inbox, "_http", fake)
    assert gmail_inbox.run_round(str(tmp), settings, NOW + 600) == 2
    assert gmail_inbox.run_round(str(tmp), settings, NOW + 1200) == 0
    assert [p["subject"] for p in fake.posted] == ["m1", "m2"]
    assert f"after:{NOW}" in fake.queries()[0].replace("%3A", ":")


def test_a_5xx_is_retried_next_round_and_not_lost(setup, monkeypatch):
    tmp, account, settings = setup
    gmail_inbox.run_round(str(tmp), settings, NOW)
    monkeypatch.setattr(gmail_inbox, "_http", Fake(ids=["m1"], homefund=(503,)))
    assert gmail_inbox.run_round(str(tmp), settings, NOW + 7200) == 0
    state = json.load(open(account[:-5] + ".state"))
    assert state["sent"] == [] and state["after"] == NOW
    fake = Fake(ids=["m1"], homefund=(200,))
    monkeypatch.setattr(gmail_inbox, "_http", fake)
    assert gmail_inbox.run_round(str(tmp), settings, NOW + 7800) == 1


def test_a_network_error_is_retried_next_round(setup, monkeypatch):
    tmp, account, settings = setup
    gmail_inbox.run_round(str(tmp), settings, NOW)

    def down(*args, **kwargs):
        raise gmail_inbox.Retry("offline")

    monkeypatch.setattr(gmail_inbox, "_http", down)
    assert gmail_inbox.run_round(str(tmp), settings, NOW + 600) == 0
    assert json.load(open(account[:-5] + ".state"))["after"] == NOW


@pytest.mark.parametrize("status", [401, 422])
def test_a_refused_email_is_skipped_and_never_retried(setup, monkeypatch, status):
    tmp, account, settings = setup
    gmail_inbox.run_round(str(tmp), settings, NOW)
    fake = Fake(ids=["m1"], homefund=(status,))
    monkeypatch.setattr(gmail_inbox, "_http", fake)
    assert gmail_inbox.run_round(str(tmp), settings, NOW + 600) == 0
    assert gmail_inbox.run_round(str(tmp), settings, NOW + 1200) == 0
    assert len(fake.posted) == 1


def test_without_a_homefund_key_nothing_leaves(setup, monkeypatch):
    tmp, account, settings = setup
    monkeypatch.setattr(gmail_inbox, "_http", lambda *a, **k: (_ for _ in ()).throw(AssertionError("network")))
    assert gmail_inbox.run_round(str(tmp), {**settings, "homefund_key": ""}, NOW) == 0


def test_a_broken_account_never_raises(setup, monkeypatch):
    tmp, account, settings = setup
    gmail_inbox.run_round(str(tmp), settings, NOW)
    monkeypatch.setattr(gmail_inbox, "_http", lambda *a, **k: (400, b"{}"))
    assert gmail_inbox.run_round(str(tmp), settings, NOW + 600) == 0


def test_the_inbox_runs_on_its_cadence_and_zero_turns_it_off(setup, monkeypatch):
    tmp, account, settings = setup
    rounds = []
    monkeypatch.setattr(gmail_inbox, "run_round", lambda *args: rounds.append(1))
    clock = [0.0]
    inbox = gmail_inbox.Inbox(str(tmp), clock=lambda: clock[0])
    inbox.tick({**settings, "gmail_minutes": 0})
    assert rounds == []
    inbox.tick(settings)
    inbox.thread.join()
    clock[0] = 300
    inbox.tick(settings)
    clock[0] = 600
    inbox.tick(settings)
    inbox.thread.join()
    assert len(rounds) == 2


def test_the_tray_request_starts_a_connection_once(setup, monkeypatch):
    tmp, account, settings = setup
    calls = []
    monkeypatch.setattr(gmail_inbox, "connect_account", lambda *args: calls.append(args) or "me@gmail.com")
    gmail_inbox.request_connect(str(tmp))
    inbox = gmail_inbox.Inbox(str(tmp))
    inbox.tick({**settings, "gmail_minutes": 0, "gmail_client_id": "cid", "gmail_client_secret": "sec"})
    for _ in range(100):
        if calls:
            break
        __import__("time").sleep(0.02)
    assert calls == [(str(tmp), "cid", "sec")]
    assert not os.path.exists(inbox.request)


def test_connect_account_saves_the_token_named_after_the_address(tmp_path, monkeypatch):
    import urllib.parse
    import urllib.request

    seen = {}

    def browser(url):
        query = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        seen.update({k: v[0] for k, v in query.items()})
        # The server only answers once connect_account is serving, after the browser call returns.
        address = f"{query['redirect_uri'][0]}/?code=abc&state={query['state'][0]}"
        threading.Thread(target=lambda: urllib.request.urlopen(address, timeout=10).read(), daemon=True).start()

    def fake(method, url, headers=None, body=None):
        if url == gmail_inbox.TOKEN_URL:
            return 200, json.dumps({"access_token": "at", "refresh_token": "rt"}).encode()
        assert url.endswith("/profile?")
        return 200, json.dumps({"emailAddress": "Me@Gmail.com"}).encode()

    monkeypatch.setattr(gmail_inbox, "_http", fake)
    assert gmail_inbox.connect_account(str(tmp_path), "cid", "sec", open_browser=browser, wait_seconds=10) == "me@gmail.com"
    assert seen["scope"] == gmail_inbox.SCOPE and seen["access_type"] == "offline" and seen["prompt"] == "consent"
    saved = json.load(open(tmp_path / "gmail" / "me@gmail.com.json"))
    assert saved == {"client-id": "cid", "client-secret": "sec", "refresh-token": "rt"}


def test_connect_account_needs_the_client_in_the_settings(tmp_path):
    with pytest.raises(RuntimeError):
        gmail_inbox.connect_account(str(tmp_path), "", "")


def test_write_homefund_key_keeps_the_other_settings_and_refuses_a_wrong_key(tmp_path):
    path = tmp_path / "aw-timelog.toml"
    path.write_text("[aw-timelog]\ninterval_minutes = 15\n", encoding="utf-8")
    assert not timelog_module.write_homefund_key("tl_" + "a" * 43, str(path))
    assert "homefund_key" not in path.read_text(encoding="utf-8")
    assert timelog_module.write_homefund_key(f" {HOMEFUND_KEY} ", str(path))
    settings = timelog_module.read_settings(str(path))
    assert settings["homefund_key"] == HOMEFUND_KEY and settings["interval_minutes"] == 15
