"""No authentication secret reaches a log line (Pass 2E-B).

The reproduction: the dashboard authenticates its WebSocket with
`/ws/signals?key=<API key>`, and uvicorn wrote that request line verbatim
into backend.log on every connection. uvicorn's access and error loggers have
their own handlers and do not propagate, so the root-logger filters never saw
them. `redact.install_log_redaction` now redacts every handler's finished
line, and app.main installs it before the first request.

Every secret here is synthetic; the live key is never read.
"""
import io
import logging
import logging.config
import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
sys.path.insert(0, str(BACKEND))

from app import redact  # noqa: E402
from app.redact import install_log_redaction, scrub  # noqa: E402
from app.redact import redact as redact_text

SECRET = "TEST_SECRET_DO_NOT_USE_7f3a"
DB_SECRET = "TEST_DB_SECRET_DO_NOT_USE_19c2"
REDIS_SECRET = "TEST_REDIS_SECRET_DO_NOT_USE_5e0b"
ENCODED = "TEST%5FSECRET%5FDO%5FNOT%5FUSE%5F7f3a"          # SECRET, percent-encoded
NAMES = ("key", "api_key", "token", "access_token", "auth_token", "password", "secret")
ALL = (SECRET, DB_SECRET, REDIS_SECRET, ENCODED)


def _absent(text: str) -> None:
    for secret in ALL:
        assert secret.lower() not in text.lower(), "a synthetic secret reached the log"
    assert "7f3a" not in text, "a fragment of the synthetic secret reached the log"


# --- The patterns ------------------------------------------------------------

@pytest.mark.parametrize("name", NAMES)
@pytest.mark.parametrize("form", [
    "/ws/signals?{n}={s}", "/ws/signals?a=1&{n}={s}&b=2", "/x?{n}={e}",
    "/x?{N}={s}", "{n}: {s}", '{{"{n}": "{s}"}}', "{n}='{s}'",
    "/next?to=%2Fws%3F{n}%3D{e}", "/x?{n}%3D{s}",
])
def test_every_secret_parameter_is_redacted_in_every_form(name, form):
    line = form.format(n=name, N=name.upper(), s=SECRET, e=ENCODED)
    assert "7f3a" not in redact_text(line), redact_text(line)


def test_a_percent_encoded_parameter_name_is_still_a_parameter_name():
    # Starlette decodes `k%65y` to `key` and would authenticate with it.
    for line in (f"/ws?k%65y={SECRET}", f"/ws?%6B%65%79={SECRET}",
                 f"/x?api%5Fkey={SECRET}", f"/x?x%2Dapi%2Dkey={SECRET}"):
        assert "7f3a" not in redact_text(line), line


def test_prefixed_names_headers_and_bare_jwts_are_redacted():
    jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJURVNUIn0.c2lnbmF0dXJlLTdmM2E"
    for line in (f"feed_token={SECRET}", f"x-feed-token: {SECRET}",
                 f"kite_access_token={SECRET}", f"Authorization: Bearer {SECRET}",
                 f"X-API-Key: {SECRET}", f"session {jwt} opened"):
        out = redact_text(line)
        assert "7f3a" not in out and "c2lnbmF0dXJl" not in out, out


def test_ordinary_lines_are_left_alone():
    for line in ("Angel feed starting for token 99926000", "price 22620.3 key levels",
                 "dropped 3 duplicate keys", "bypass=true", "monkey=1",
                 "GET /data/readiness?symbol=NIFTY&timeframe=5m HTTP/1.1"):
        assert redact_text(line) == line


def test_a_remembered_secret_is_removed_wherever_it_appears():
    redact.remember_secrets(SECRET)
    for line in (f"/ws/{SECRET}/x", f"value {SECRET}.", f"q={ENCODED}",
                 f"('x-api-key', '{SECRET}')"):
        assert "7f3a" not in scrub(line), line


def test_the_settings_secret_fields_are_collected():
    from app.config import Settings

    s = Settings(_env_file=None, api_key="A" * 12, angel_api_key="B" * 12,
                 angel_password="C" * 12, angel_totp_secret="D" * 12,
                 kite_access_token="E" * 12, angel_nifty_token="99926000",
                 database_url=f"postgresql+psycopg://u:{DB_SECRET}@h:1/d",
                 redis_url=f"redis://:{REDIS_SECRET}@h:1/0")
    found = set(redact.settings_secrets(s))
    assert {"A" * 12, "B" * 12, "C" * 12, "D" * 12, "E" * 12,
            DB_SECRET, REDIS_SECRET} <= found
    assert "99926000" not in found                 # an instrument id, not a secret


# --- Codex review (2E-B): each reproduced leak -------------------------------

@pytest.mark.parametrize("line", [
    # Schemes whose credential follows the scheme word.
    f"Authorization: Negotiate YIIGhgYJKoZIhvcS{SECRET}",
    f"authorization:Bearer {SECRET} trailing",
    f"Proxy-Authorization: Basic {SECRET}",
    f"Authorization: AWS4-HMAC-SHA256 Credential=AKIA/x, Signature={SECRET}",
    # Digest: the credential is spread over several fields.
    f'Authorization: Digest username="bob", realm="r", nonce="{SECRET}", uri="/", '
    f'response="{SECRET}", cnonce="{SECRET}"',
    f"{{'authorization': 'Digest username=\"bob\", response=\"{SECRET}\"', 'host': 'h'}}",
    f'{{"Authorization": "Digest username=\\"bob\\", response=\\"{SECRET}\\""}}',
    # Header lists as ASGI and Starlette hold them: byte and str tuples, dicts.
    f"[(b'host', b'h'), (b'authorization', b'Bearer {SECRET}')]",
    f"[(b'x-api-key', b'{SECRET}')]",
    f"('authorization', 'Negotiate {SECRET}')",
    f"{{b'x-api-key': b'{SECRET}'}}",
    f"Headers({{'authorization': 'Bearer {SECRET}', 'accept': '*/*'}})",
    # A query value with ; or , inside it — Starlette reads it as one value.
    f"/ws/signals?key=abc;{SECRET}",
    f"/ws/signals?key=abc;def{SECRET}&x=1",
    f'127.0.0.1:5 - "GET /health?api_key=a,b;{SECRET} HTTP/1.1" 200',
])
def test_every_codex_reproduction_is_redacted(line):
    out = redact_text(line)
    assert "7f3a" not in out, out
    assert "***" in out


def test_the_redacted_forms_keep_the_line_readable():
    assert (redact_text("[(b'host', b'h'), (b'authorization', b'Bearer x1')]")
            == "[(b'host', b'h'), (b'authorization', ***)]")
    assert (redact_text("{'authorization': 'Negotiate x1', 'host': 'h'}")
            == "{'authorization': ***, 'host': 'h'}")
    assert (redact_text('127.0.0.1:5 - "GET /ws/signals?key=a;b&x=1 HTTP/1.1" 200')
            == '127.0.0.1:5 - "GET /ws/signals?key=***&x=1 HTTP/1.1" 200')


def test_a_short_configured_credential_is_masked_as_a_whole_token():
    """Codex: a configured key under 8 characters escaped known-value
    masking. 4–7 characters: masked wherever it stands alone, never out of
    the middle of a longer word or number."""
    assert redact.remember_secrets("Zq7w", "4821") == 0
    for line in ("/ws/Zq7w/x", "key-less mention Zq7w.", "path /x/zq7w", "pin 4821",
                 "q=%5Aq7w"):
        assert "q7w" not in scrub(line).lower() and "4821" not in scrub(line), line
    for line in ("Zq7wX", "xZq7w", "strike 48210", "22620 x48213", "order 14821"):
        assert scrub(line) == line, line


def test_a_credential_too_short_to_mask_alone_is_reported(caplog):
    assert redact.remember_secrets("ab") == 1                     # 1–3 characters
    assert scrub("?key=ab&x=1") == "?key=***&x=1"                # still after a name
    assert scrub("the word ab stays") == "the word ab stays"
    with caplog.at_level(logging.WARNING, logger="app.redact"):
        install_log_redaction(secrets=("xy",))
    assert "shorter than 4 characters" in caplog.text
    assert "xy" not in caplog.text                                # the value is never logged


# Every character that may appear in a query value — reserved, sub-delims,
# the "unsafe" ones clients send raw anyway, and an encoded `)`.
PUNCTUATION = list(")]}([{'!*$,;:@/?=+~|^`<>\\") + ["%29", '"']


def _uvicorn_sinks():
    """uvicorn's own formatters, as its default logging config builds them:
    AccessFormatter for HTTP request lines, DefaultFormatter for the
    WebSocket lines uvicorn writes through `uvicorn.error`."""
    from uvicorn.logging import AccessFormatter, DefaultFormatter

    sinks = {}
    for name, formatter in (
            ("qd.test.q-access", AccessFormatter(
                '%(levelprefix)s %(client_addr)s - "%(request_line)s" %(status_code)s',
                use_colors=False)),
            ("qd.test.q-error", DefaultFormatter("%(levelprefix)s %(message)s",
                                                 use_colors=False))):
        sinks[name] = io.StringIO()
        handler = logging.StreamHandler(sinks[name])
        handler.setFormatter(formatter)
        logger = logging.getLogger(name)
        logger.handlers[:] = [handler]
        logger.propagate = False
        logger.setLevel(logging.INFO)
    return sinks


@pytest.mark.parametrize("mark", PUNCTUATION)
@pytest.mark.parametrize("name", NAMES)
def test_a_query_secret_is_masked_through_its_own_punctuation(boundary, name, mark):
    """Codex: `?token=prefix)SUFFIX&x=1` logged as `token=***)SUFFIX`. The
    secret is not a remembered value, so only the query parser can catch it;
    it runs through uvicorn's real formatters, HTTP and WebSocket."""
    suffix = "UNREMEMBERED_SUFFIX_DO_NOT_USE_7f3a"
    value = f"prefix{mark}{suffix}"
    sinks = _uvicorn_sinks()
    logging.getLogger("qd.test.q-access").info(
        '%s - "%s %s HTTP/%s" %d', "127.0.0.1:5", "GET",
        f"/ws/signals?{name}={value}&x=1", "1.1", 101)
    logging.getLogger("qd.test.q-error").info(
        '%s - "WebSocket %s" [accepted]', "127.0.0.1:5", f"/ws/signals?{name}={value}")
    http, ws = sinks["qd.test.q-access"].getvalue(), sinks["qd.test.q-error"].getvalue()
    for out in (http, ws):
        assert "7f3a" not in out and "prefix" not in out, out
    assert f'"GET /ws/signals?{name}=***&x=1 HTTP/1.1" 101' in http, http
    assert f'"WebSocket /ws/signals?{name}=***" [accepted]' in ws, ws


def test_uvicorn_formats_the_new_shapes_without_breaking(boundary):
    from uvicorn.logging import AccessFormatter

    sink = io.StringIO()
    logger = logging.getLogger("qd.test.access-shapes")
    handler = logging.StreamHandler(sink)
    handler.setFormatter(AccessFormatter('%(client_addr)s - "%(request_line)s" %(status_code)s',
                                         use_colors=False))
    logger.handlers[:] = [handler]
    logger.propagate = False
    logger.setLevel(logging.INFO)
    logger.info('%s - "%s %s HTTP/%s" %d', "127.0.0.1:5", "GET",
                f"/ws/signals?key=abc;{SECRET}&x=1", "1.1", 101)
    logger.info("headers %s", [(b"authorization", f"Negotiate {SECRET}".encode())])
    out = sink.getvalue()
    _absent(out)
    assert 'GET /ws/signals?key=***&x=1 HTTP/1.1" 101' in out, out


# --- The boundary: every handler, however configured ------------------------

@pytest.fixture
def boundary():
    install_log_redaction(secrets=(SECRET,))
    yield


def _isolated_logger(name: str) -> tuple[logging.Logger, io.StringIO]:
    """A logger configured the way uvicorn's are: its own handler, no
    propagation — invisible to anything attached to the root logger."""
    sink = io.StringIO()
    logger = logging.getLogger(name)
    logger.handlers[:] = [logging.StreamHandler(sink)]
    logger.propagate = False
    logger.setLevel(logging.INFO)
    return logger, sink


def test_a_non_propagating_logger_added_later_is_redacted(boundary):
    logger, sink = _isolated_logger("qd.test.later")
    logger.info('%s - "WebSocket %s" [accepted]', "127.0.0.1:5", f"/ws/signals?key={SECRET}")
    logger.info("GET /x?key=%s", SECRET)                     # split across msg and args
    logger.info("literal /ws/signals?key=" + SECRET)
    _absent(sink.getvalue())
    assert "key=***" in sink.getvalue()


def test_the_finished_line_is_redacted_even_when_no_argument_is(boundary):
    """Only the handler boundary sees these: the secret is not a remembered
    value, and it is split across the format string and its argument, or
    inside a non-string argument (an exception) that only formatting turns
    into text."""
    unknown = "TEST_UNREMEMBERED_DO_NOT_USE_7f3a"
    logger, sink = _isolated_logger("qd.test.split")
    logger.info("GET /ws/signals?key=%s HTTP/1.1", unknown)
    logger.warning("upstream failed: %s", RuntimeError(f"GET /v1?api_key={unknown}"))
    logger.warning("headers: %s", {"X-API-Key": unknown})
    out = sink.getvalue()
    _absent(out)
    assert out.count("***") == 3, out


def test_uvicorns_own_logging_config_is_redacted(boundary):
    """uvicorn's LOGGING_CONFIG and its AccessFormatter — which unpacks the
    record's argument tuple, so that tuple must keep its shape."""
    from uvicorn.config import LOGGING_CONFIG

    logging.config.dictConfig(LOGGING_CONFIG)
    sinks = {}
    for name in ("uvicorn.access", "uvicorn.error"):
        sinks[name] = io.StringIO()
        for handler in logging.getLogger(name).handlers or logging.getLogger("uvicorn").handlers:
            handler.setStream(sinks[name])
    logging.getLogger("uvicorn.access").info(
        '%s - "%s %s HTTP/%s" %d', "127.0.0.1:5", "GET", f"/health?key={SECRET}", "1.1", 200)
    logging.getLogger("uvicorn.error").info(
        '%s - "WebSocket %s" [accepted]', "127.0.0.1:5", f"/ws/signals?key={ENCODED}")
    out = "".join(s.getvalue() for s in sinks.values())
    _absent(out)
    assert "GET /health?key=*** HTTP/1.1\" 200" in out, out


def test_an_exception_log_quoting_a_url_is_redacted(boundary):
    logger, sink = _isolated_logger("qd.test.exc")
    try:
        raise RuntimeError(f"GET https://upstream/api?access_token={SECRET} failed")
    except RuntimeError:
        logger.exception("Exception in ASGI application")
    _absent(sink.getvalue())
    assert "Traceback" in sink.getvalue()


def test_a_record_that_fails_to_format_is_reported_without_its_content(boundary, capsys):
    logger, _ = _isolated_logger("qd.test.broken")
    logger.info("%s %s /ws?key=%s", SECRET)                # too few arguments
    err = capsys.readouterr().err
    _absent(err)
    assert "Logging error" in err


def test_an_uncaught_thread_exception_is_redacted(boundary, capsys):
    def boom():
        raise RuntimeError(f"websocket to wss://feed?feed_token={SECRET} dropped")

    t = threading.Thread(target=boom)
    t.start()
    t.join()
    err = capsys.readouterr().err
    _absent(err)
    assert "dropped" in err


# --- The real server: app.main under uvicorn, as start.sh runs it -----------

def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def served(tmp_path_factory):
    """app.main (via tests/redaction_harness.py) under uvicorn with its
    default logging, stdout and stderr captured separately — backend.log is
    both. No lifespan: no database, lease or feed; the logging path is the
    same. Run from an empty directory so no .env is read."""
    work = tmp_path_factory.mktemp("served")
    port = _free_port()
    env = {k: v for k, v in os.environ.items() if not k.startswith(("ANGEL_", "KITE_"))}
    env.update({
        "API_KEY": SECRET,
        "DATABASE_URL": f"postgresql+psycopg://quant:{DB_SECRET}@127.0.0.1:1/quantdesk",
        "REDIS_URL": f"redis://:{REDIS_SECRET}@127.0.0.1:1/0",
        "QD_TEST_LEAK_URL": f"https://upstream.example/v1?api_key={SECRET}&token={ENCODED}",
        "PYTHONPATH": os.pathsep.join([str(BACKEND), str(ROOT / "tests")]),
        "PYTHONDONTWRITEBYTECODE": "1",
    })
    out, err = work / "stdout.log", work / "stderr.log"
    with out.open("wb") as o, err.open("wb") as e:
        proc = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "redaction_harness:app", "--host", "127.0.0.1",
             "--port", str(port), "--lifespan", "off"],
            cwd=work, env=env, stdout=o, stderr=e)
    base = f"http://127.0.0.1:{port}"
    for _ in range(100):
        try:
            httpx.get(f"{base}/health", timeout=1)
            break
        except httpx.HTTPError:
            time.sleep(0.2)
    else:
        proc.kill()
        pytest.fail("the harness server did not start")
    yield base, port, out, err
    proc.terminate()
    proc.wait(timeout=30)


def _traffic(base: str, port: int) -> None:
    from websockets.sync.client import connect

    for name in NAMES:
        httpx.get(f"{base}/health?{name}={SECRET}&x=1", timeout=5)
    httpx.get(f"{base}/health?key={ENCODED}", timeout=5)
    httpx.get(f"{base}/health?k%65y={SECRET}", timeout=5)
    httpx.get(f"{base}/health?key=abc;{SECRET}&x=2", timeout=5)          # Codex: ; in value
    for mark in (")", "]", "}", "'", "!", "*", "$", "@", "%29"):          # Codex: punctuation
        httpx.get(f"{base}/health?token=prefix{mark}{SECRET}&x=3", timeout=5)
    httpx.get(f"{base}/health", headers={"X-API-Key": SECRET}, timeout=5)
    httpx.get(f"{base}/test-only/boom", timeout=5)
    ws = f"ws://127.0.0.1:{port}"
    for path in (f"/ws/signals?key={SECRET}",                  # the dashboard's own URL
                 f"/ws/signals?key={ENCODED}",
                 f"/ws/signals?key=abc;{SECRET}",
                 f"/ws/signals?token=prefix){SECRET}",
                 "/ws/signals?key=TEST_WRONG_KEY_DO_NOT_USE_7f3a",   # refused, still logged
                 f"/test-only/ws-boom?token={SECRET}"):
        try:
            with connect(ws + path, open_timeout=5, close_timeout=2) as conn:
                try:
                    conn.recv(timeout=2)
                except Exception:  # noqa: BLE001, S110 — the log is what is under test
                    pass
        except Exception:  # noqa: BLE001, S110
            pass
    time.sleep(1.0)                                             # let the logs flush


def test_http_and_websocket_secrets_never_reach_stdout_or_stderr(served):
    base, port, out, err = served
    _traffic(base, port)
    stdout, stderr = out.read_text(), err.read_text()
    _absent(stdout)
    _absent(stderr)
    # The lines were written — redacted, not suppressed.
    assert 'GET /health?key=***&x=1 HTTP/1.1" 200' in stdout, stdout[-2000:]
    assert '"WebSocket /ws/signals?key=***" [accepted]' in stderr, stderr[-2000:]
    assert "Exception in ASGI application" in stderr
    assert "upstream request failed" in stderr and "socket upstream failed" in stderr
