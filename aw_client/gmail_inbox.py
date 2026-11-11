"""
Forwards the bank emails that reach a Gmail account to HomeFund, so `aw-timelog` does both jobs.

Each account is one JSON file in `gmail/` inside the config folder (`client-id`, `client-secret`,
`refresh-token`). Access is read-only. Only messages matching `gmail_query` leave the PC, one POST each.
Standard library only.
"""

import base64
import html
import http.server
import json
import logging
import os
import re
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from typing import Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

TOKEN_URL = "https://oauth2.googleapis.com/token"
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GMAIL_URL = "https://gmail.googleapis.com/gmail/v1/users/me"
SCOPE = "https://www.googleapis.com/auth/gmail.readonly"
DEFAULT_HOMEFUND_URL = "https://our-piggy-bank.vercel.app"
DEFAULT_QUERY = (
    "from:(notificacionesbancolombia.com OR nequi.com.co OR lulobank.com OR nu.com.co OR bancofalabella.com"
    " OR bancodebogota.com.co OR bancodebogota.net OR achcolombia.com.co OR davivienda.com OR bbva.com.co"
    " OR daviplata.com)"
)
HOMEFUND_KEY_PATTERN = re.compile(r"whk_[A-Za-z0-9_-]{16,}")
# HomeFund's Ajustes shows «<client_id> <client_secret>» on one line.
CLIENT_ID_SUFFIX = ".apps.googleusercontent.com"
CLIENT_SECRET_PREFIX = "GOCSPX-"
FOLDER = "gmail"
# The tray leaves this file; aw-timelog sees it within a tick and opens the browser.
CONNECT_REQUEST = "aw-timelog-gmail-connect-request"
# A message that arrives a little after its date is still found: ids already sent are skipped.
OVERLAP_SECONDS = 3600
SENT_KEPT = 1000
CONNECT_WAIT_SECONDS = 300
HTTP_TIMEOUT = 30


class Retry(Exception):
    """A 5xx or a network failure: the next round tries again."""


def _http(method: str, url: str, headers: Optional[Dict[str, str]] = None, body: Optional[bytes] = None) -> Tuple[int, bytes]:
    request = urllib.request.Request(url, data=body, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as error:
        return error.code, error.read()
    except (urllib.error.URLError, OSError) as error:
        raise Retry(str(error)) from error


def _json(status: int, raw: bytes, what: str) -> dict:
    if status >= 500:
        raise Retry(f"{what}: HTTP {status}")
    if status != 200:
        raise RuntimeError(f"{what}: HTTP {status}")
    return json.loads(raw.decode("utf-8"))


def parse_client(line: str) -> Optional[Tuple[str, str]]:
    """The id and secret of a pasted «<client_id> <client_secret>» line; None when it is not one."""
    parts = line.split()
    if len(parts) != 2 or not parts[0].endswith(CLIENT_ID_SUFFIX) or not parts[1].startswith(CLIENT_SECRET_PREFIX):
        return None
    return parts[0], parts[1]


def gmail_dir(config_dir: str) -> str:
    return os.path.join(config_dir, FOLDER)


def _read_json(path: str) -> Optional[dict]:
    try:
        with open(path, encoding="utf-8") as file:
            value = json.load(file)
        return value if isinstance(value, dict) else None
    except (OSError, ValueError):
        return None


def _write_json(path: str, value: dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    temporary = path + ".tmp"
    with open(temporary, "w", encoding="utf-8") as file:
        json.dump(value, file)
    os.replace(temporary, path)


def list_accounts(config_dir: str) -> List[str]:
    folder = gmail_dir(config_dir)
    try:
        return sorted(os.path.join(folder, name) for name in os.listdir(folder) if name.endswith(".json"))
    except OSError:
        return []


def access_token(account: dict) -> str:
    body = urllib.parse.urlencode(
        {
            "client_id": account["client-id"],
            "client_secret": account["client-secret"],
            "refresh_token": account["refresh-token"],
            "grant_type": "refresh_token",
        }
    ).encode()
    status, raw = _http("POST", TOKEN_URL, {"Content-Type": "application/x-www-form-urlencoded"}, body)
    return _json(status, raw, "Google token")["access_token"]


def _get(token: str, path: str, params: Dict[str, str]) -> dict:
    url = f"{GMAIL_URL}/{path}?{urllib.parse.urlencode(params)}"
    status, raw = _http("GET", url, {"Authorization": f"Bearer {token}"})
    return _json(status, raw, f"Gmail {path.split('/')[0]}")


def list_message_ids(token: str, query: str, after: int) -> List[str]:
    ids: List[str] = []
    page = ""
    while True:
        params = {"q": f"{query} after:{after}", "maxResults": "100"}
        if page:
            params["pageToken"] = page
        result = _get(token, "messages", params)
        ids += [message["id"] for message in result.get("messages", [])]
        page = result.get("nextPageToken", "")
        if not page:
            return ids


def _decode(data: str) -> str:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4)).decode("utf-8", errors="replace")


def _parts(payload: dict, found: Dict[str, List[str]]) -> None:
    kind = payload.get("mimeType", "")
    data = (payload.get("body") or {}).get("data")
    if data and kind in ("text/plain", "text/html"):
        found[kind].append(_decode(data))
    for part in payload.get("parts") or []:
        _parts(part, found)


def extract(message: dict) -> dict:
    """The subject and both bodies of a `format=full` message."""
    payload = message.get("payload") or {}
    subject = next((h.get("value", "") for h in payload.get("headers") or [] if h.get("name", "").lower() == "subject"), "")
    found: Dict[str, List[str]] = {"text/plain": [], "text/html": []}
    _parts(payload, found)
    return {"subject": subject, "text": "\n".join(found["text/plain"]), "html": "\n".join(found["text/html"])}


def forward(homefund_url: str, key: str, email: dict) -> str:
    """'sent' or 'skipped' (401/422: this message will never be accepted); Retry for a 5xx or no network."""
    status, raw = _http(
        "POST",
        homefund_url.rstrip("/") + "/api/webhooks/email",
        {"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        json.dumps(email).encode("utf-8"),
    )
    if status >= 500:
        raise Retry(f"HomeFund: HTTP {status}")
    if status in (401, 422):
        logger.error(f"HomeFund refused an email with HTTP {status}; skipping it.")
        return "skipped"
    if status >= 400:
        raise Retry(f"HomeFund: HTTP {status}")
    return "sent"


def poll_account(path: str, settings: dict, now: float) -> int:
    """Forwards the new matching emails of one account; returns how many were sent."""
    account = _read_json(path)
    if not account or not all(account.get(k) for k in ("client-id", "client-secret", "refresh-token")):
        logger.warning(f"Gmail account file {os.path.basename(path)} is incomplete; skipping it.")
        return 0
    state_path = path[: -len(".json")] + ".state"
    state = _read_json(state_path)
    if state is None:
        # First run of an account: from now on, never backfill.
        _write_json(state_path, {"after": int(now), "sent": []})
        return 0
    after = int(state.get("after", now))
    sent = [str(i) for i in state.get("sent", [])]
    token = access_token(account)
    clean = True
    count = 0
    for message_id in list_message_ids(token, settings["gmail_query"], after):
        if message_id in sent:
            continue
        email = extract(_get(token, f"messages/{message_id}", {"format": "full"}))
        try:
            outcome = forward(settings["homefund_url"], settings["homefund_key"], email)
        except Retry as error:
            logger.warning(f"HomeFund did not take a bank email, trying again next round: {error}")
            clean = False
            continue
        sent.append(message_id)
        count += outcome == "sent"
    # A message left over for the next round keeps `after` where it was.
    if clean:
        after = max(after, int(now) - OVERLAP_SECONDS)
    _write_json(state_path, {"after": after, "sent": sent[-SENT_KEPT:]})
    return count


def run_round(config_dir: str, settings: dict, now: Optional[float] = None) -> int:
    """One pass over every account. Never raises: Gmail must not touch the TimeLog upload."""
    now = time.time() if now is None else now
    if not HOMEFUND_KEY_PATTERN.fullmatch(str(settings.get("homefund_key", ""))):
        accounts = list_accounts(config_dir)
        if accounts:
            logger.info("No HomeFund key yet: paste one from the tray, under «HomeFund…».")
        return 0
    total = 0
    for path in list_accounts(config_dir):
        try:
            total += poll_account(path, settings, now)
        except Retry as error:
            logger.warning(f"Gmail {os.path.basename(path)}: {error}; trying again next round.")
        except Exception as error:
            logger.error(f"Gmail {os.path.basename(path)}: {type(error).__name__}: {error}")
    if total:
        logger.info(f"Forwarded {total} bank emails to HomeFund.")
    return total


def connect_account(config_dir: str, client_id: str, client_secret: str, open_browser: Callable[[str], object] = webbrowser.open, wait_seconds: float = CONNECT_WAIT_SECONDS) -> str:
    """Google's loopback flow; saves `gmail/<address>.json` and returns the address."""
    if not client_id or not client_secret:
        raise RuntimeError("No Gmail client: use «Gmail: conectar cuenta…» in the tray to paste it, or set gmail_client_id and gmail_client_secret in aw-timelog.toml.")
    expected = secrets.token_urlsafe(16)
    result: Dict[str, str] = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            if "code" not in query and "error" not in query:
                self.send_response(404)
                self.end_headers()
                return
            result.update({k: v[0] for k, v in query.items()})
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            message = "Listo, ya puedes cerrar esta pestaña." if "code" in query else "No se conectó la cuenta."
            self.wfile.write(f"<p>{html.escape(message)}</p>".encode("utf-8"))

        def log_message(self, *args):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    redirect = f"http://127.0.0.1:{server.server_port}"
    server.timeout = 1
    try:
        open_browser(
            AUTH_URL
            + "?"
            + urllib.parse.urlencode(
                {
                    "client_id": client_id,
                    "redirect_uri": redirect,
                    "response_type": "code",
                    "scope": SCOPE,
                    "access_type": "offline",
                    "prompt": "consent",
                    "state": expected,
                }
            )
        )
        deadline = time.time() + wait_seconds
        while not result and time.time() < deadline:
            server.handle_request()
    finally:
        server.server_close()
    if result.get("state") != expected or "code" not in result:
        raise RuntimeError("Google did not return a code for this request.")
    body = urllib.parse.urlencode(
        {"code": result["code"], "client_id": client_id, "client_secret": client_secret, "redirect_uri": redirect, "grant_type": "authorization_code"}
    ).encode()
    status, raw = _http("POST", TOKEN_URL, {"Content-Type": "application/x-www-form-urlencoded"}, body)
    tokens = _json(status, raw, "Google token")
    if not tokens.get("refresh_token"):
        raise RuntimeError("Google returned no refresh token.")
    address = _get(tokens["access_token"], "profile", {})["emailAddress"].strip().lower()
    safe = re.sub(r"[^a-z0-9@._+-]", "_", address)
    _write_json(os.path.join(gmail_dir(config_dir), f"{safe}.json"), {"client-id": client_id, "client-secret": client_secret, "refresh-token": tokens["refresh_token"]})
    return address


class Inbox:
    """Runs a round every `gmail_minutes` and opens a connection when the tray asks; `tick` is cheap."""

    def __init__(self, config_dir: str, clock: Callable[[], float] = time.time):
        self.config_dir = config_dir
        self.clock = clock
        self.last: Optional[float] = None
        self.request = os.path.join(config_dir, CONNECT_REQUEST)
        self.connecting = False
        self.thread: Optional[threading.Thread] = None

    def tick(self, settings: dict) -> None:
        try:
            self._tick(settings)
        except Exception as error:
            logger.error(f"Gmail: {type(error).__name__}: {error}")

    def _tick(self, settings: dict) -> None:
        if os.path.exists(self.request) and not self.connecting:
            try:
                os.remove(self.request)
            except OSError:
                pass
            self.connecting = True
            threading.Thread(target=self._connect, args=(dict(settings),), daemon=True).start()
        minutes = int(settings["gmail_minutes"])
        if minutes <= 0 or not settings["gmail_enabled"]:
            return
        if self.last is not None and self.clock() - self.last < minutes * 60:
            return
        if self.thread is not None and self.thread.is_alive():
            return
        self.last = self.clock()
        # Off the loop's thread: a slow Gmail never holds back the TimeLog upload.
        self.thread = threading.Thread(target=run_round, args=(self.config_dir, dict(settings), self.clock()), daemon=True)
        self.thread.start()

    def _connect(self, settings: dict) -> None:
        try:
            address = connect_account(self.config_dir, str(settings["gmail_client_id"]), str(settings["gmail_client_secret"]))
            logger.info(f"Gmail account connected: {address}")
            self.last = None
        except Exception as error:
            logger.error(f"Gmail connection failed: {type(error).__name__}: {error}")
        finally:
            self.connecting = False


def request_connect(folder: str) -> None:
    with open(os.path.join(folder, CONNECT_REQUEST), "w", encoding="utf-8"):
        pass
