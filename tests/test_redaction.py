"""No database credential reaches a diagnostic (Pass 2E-A.1).

Codex's reproduction: `schema_check.check()` printed
`...?password=supersecret` for an unreachable database, because
`render_as_string(hide_password=True)` hides only the authority password.
Diagnostics are now built from `redact.describe()` (scheme, host, port,
database) plus a redacted driver error.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND))

from app import migration_guard, schema_check  # noqa: E402
from app.redact import SECRET_NAMES, describe, error_text, redact  # noqa: E402

SECRET = "supersecret"
DEAD = "127.0.0.1:1"             # nothing listens on port 1: refused at once

URLS = {
    "authority": f"postgresql+psycopg://quant:{SECRET}@{DEAD}/quantdesk",
    "query (Codex)": f"postgresql+psycopg://quant@{DEAD}/quantdesk?password={SECRET}",
    "both": f"postgresql+psycopg://quant:{SECRET}@{DEAD}/quantdesk?password={SECRET}x",
    "postgres scheme": f"postgres://quant:{SECRET}@{DEAD}/quantdesk",
    "postgresql scheme": f"postgresql://quant:{SECRET}@{DEAD}/quantdesk",
}


@pytest.mark.parametrize("name", URLS)
def test_an_unreachable_database_reports_no_secret(name):
    status = schema_check.check(URLS[name], connect_timeout=2)
    assert status.state == schema_check.UNREACHABLE
    assert SECRET not in status.message, status.message


def test_the_codex_reproduction_keeps_host_port_and_database():
    status = schema_check.check(URLS["query (Codex)"], connect_timeout=2)
    assert "127.0.0.1:1/quantdesk" in status.message
    assert "password=" not in status.message and SECRET not in status.message


def test_a_url_encoded_password_is_redacted_in_every_form():
    raw = "p@ss/w0rd!sup3r"
    url = "postgresql+psycopg://quant:p%40ss%2Fw0rd%21sup3r@127.0.0.1:1/quantdesk"
    for text in (f"dsn {url}", f"auth failed for {raw}", "p%40ss%2Fw0rd%21sup3r",
                 "p%40ss%2Fw0rd%21sup3r".replace("%40", "@")):
        out = redact(text, url)
        assert raw not in out and "p%40ss%2Fw0rd%21sup3r" not in out and "w0rd" not in out, out
    status = schema_check.check(url, connect_timeout=2)
    assert "w0rd" not in status.message


@pytest.mark.parametrize("name", SECRET_NAMES)
def test_every_credential_parameter_name_is_redacted(name):
    for text in (f"postgresql://h/db?sslmode=require&{name}={SECRET}&x=1",
                 f"host=h {name}={SECRET} dbname=d",
                 f"host=h {name}='{SECRET} with space' dbname=d",
                 json.dumps({name: SECRET}),
                 f"{name.upper()}: {SECRET}"):
        assert SECRET not in redact(text), (name, text, redact(text))


def test_an_exception_carrying_the_full_dsn_is_redacted():
    url = URLS["both"]
    from sqlalchemy.exc import OperationalError

    class Driver(Exception):
        pass

    inner = Driver(f'connection failed: invalid dsn "{url}" (host=127.0.0.1 '
                   f"password={SECRET} dbname=quantdesk)")
    wrapped = OperationalError("SELECT 1", {}, inner)
    for exc in (inner, wrapped, RuntimeError(f"boom {url}")):
        assert SECRET not in error_text(exc, url)
        assert SECRET not in error_text(exc)          # patterns alone suffice


def test_describe_never_includes_credentials_or_query():
    for url in URLS.values():
        out = describe(url)
        assert SECRET not in out and "?" not in out and "quant:" not in out
    assert describe("not a url at all ::") == "<unparseable database URL>"
    assert SECRET not in describe(f"garbage://{SECRET}@@@::")


def test_the_writer_lease_and_migration_lock_report_no_secret():
    for url in URLS.values():
        with pytest.raises(migration_guard.CoordinationError) as lease:
            migration_guard.writer_lease("test", url).acquire()
        assert SECRET not in str(lease.value)
        if migration_guard.coordinated(url):
            with pytest.raises(migration_guard.CoordinationError) as lock:
                with migration_guard.MigrationLock(url):
                    pass
            assert SECRET not in str(lock.value)


@pytest.mark.parametrize("module,args", [("app.schema_check", []),
                                         ("app.migrate", []),
                                         ("app.migrate", ["--apply"])])
@pytest.mark.parametrize("name", ["authority", "query (Codex)", "both"])
def test_command_output_for_an_unreachable_database_has_no_secret(module, args, name):
    env = {**os.environ, "DATABASE_URL": URLS[name], "PYTHONDONTWRITEBYTECODE": "1"}
    run = subprocess.run([sys.executable, "-m", module, *args], cwd=BACKEND, env=env,
                         capture_output=True, text=True, timeout=60)
    assert run.returncode != 0
    assert SECRET not in run.stdout + run.stderr, run.stdout + run.stderr
