"""scripts/native/env.sh: which PostgreSQL it owns, and which database it means.

Two things a Linux test VM needs from the lifecycle scripts, and the Mac must
not lose:

  * PostgreSQL by platform. The Mac keeps Homebrew's cluster exactly as
    before; Linux gets a cluster this user owns under ~/.quantdesk, never the
    distribution's `postgres`-owned one.
  * An explicit scratch target. `migrate.sh` sources env.sh, which always
    replaced DATABASE_URL — so a scratch database named the usual way was
    silently swapped for the desk's own. A target is now named with
    QUANTDESK_DATABASE_URL, wins when given, and migrate.sh refuses one that
    is really the desk's database.

The platform is imitated with a stand-in `uname` on PATH: these run on the
Mac and test the scripts' logic, not Linux itself. Nothing here connects to
the desk's database, and its URL is compared, never printed.
"""
import os
import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy import make_url

ROOT = Path(__file__).resolve().parents[1]
ENV_SH = ROOT / "scripts" / "native" / "env.sh"
MIGRATE_SH = ROOT / "scripts" / "migrate.sh"

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="bash scripts")


def shell(script, *, env=None, uname=None, tmp=None):
    run_env = {k: v for k, v in os.environ.items()
               if not k.startswith(("QUANTDESK_", "DATABASE_URL", "REDIS_URL"))}
    if uname:
        bin_dir = Path(tmp) / "fake-bin"
        bin_dir.mkdir(exist_ok=True)
        stub = bin_dir / "uname"
        stub.write_text(f"#!/bin/sh\necho {uname}\n")
        stub.chmod(0o755)
        run_env["PATH"] = f"{bin_dir}:{run_env['PATH']}"
    run_env.update(env or {})
    return subprocess.run(["bash", "-c", script], env=run_env, capture_output=True,
                          text=True, timeout=60)


def native_url(tmp_path, env_sh=ENV_SH):
    """The desk's own target as env.sh defines it — held, never shown."""
    out = shell(f"source '{env_sh}'; printf %s \"$NATIVE_DATABASE_URL\"", tmp=tmp_path)
    assert out.returncode == 0 and out.stdout
    return out.stdout


def scripts_tree(tmp_path):
    """migrate.sh and env.sh copied into a scratch tree whose "desk" database
    is on port 1, where nothing listens.

    So even a broken guard cannot reach the real database: whatever these
    tests do falls through to a refused connection on a dead port. The rest
    of env.sh is the real file, byte for byte.
    """
    tree = tmp_path / "tree"
    (tree / "scripts" / "native").mkdir(parents=True)
    lines = []
    for line in ENV_SH.read_text().splitlines(keepends=True):
        if line.startswith("export DATABASE_URL="):
            assert ":${PG_PORT}/" in line
            line = line.replace(":${PG_PORT}/", ":1/")
        lines.append(line)
    (tree / "scripts" / "native" / "env.sh").write_text("".join(lines))
    (tree / "scripts" / "migrate.sh").write_text(MIGRATE_SH.read_text())
    (tree / ".venv").symlink_to(ROOT / ".venv")
    (tree / "backend").symlink_to(ROOT / "backend")
    return tree


# ---- PostgreSQL by platform --------------------------------------------------------

def test_the_mac_keeps_homebrew_postgres(tmp_path):
    out = shell(f"source '{ENV_SH}'; echo \"$PG_BIN|$PG_DATA\"",
                uname="Darwin", tmp=tmp_path)
    assert out.stdout.strip() == ("/opt/homebrew/opt/postgresql@16/bin|"
                                  "/opt/homebrew/var/postgresql@16")


def test_linux_uses_a_user_owned_cluster_not_homebrew(tmp_path):
    home = tmp_path / "home"
    out = shell(f"source '{ENV_SH}'; echo \"$PG_BIN|$PG_DATA\"",
                uname="Linux", tmp=tmp_path, env={"HOME": str(home)})
    assert out.stdout.strip() == (f"/usr/lib/postgresql/16/bin|"
                                  f"{home}/.quantdesk/postgresql@16")
    assert "homebrew" not in out.stdout


@pytest.mark.parametrize("platform", ["Darwin", "Linux"])
def test_an_explicit_postgres_location_wins_on_either_platform(tmp_path, platform):
    out = shell(f"source '{ENV_SH}'; echo \"$PG_BIN|$PG_DATA\"", uname=platform,
                tmp=tmp_path, env={"QUANTDESK_PG_BIN": "/x/bin",
                                   "QUANTDESK_PG_DATA": "/x/data"})
    assert out.stdout.strip() == "/x/bin|/x/data"


def test_the_port_and_lifecycle_settings_do_not_depend_on_platform(tmp_path):
    probe = (f"source '{ENV_SH}'; echo \"$PG_PORT|$REDIS_PORT|$API_PORT|$WEB_PORT|"
             f"$RUN_DIR|$API_STOP_FALLBACK|$API_APP\"")
    home = {"HOME": str(tmp_path)}
    mac = shell(probe, uname="Darwin", tmp=tmp_path, env=home).stdout
    linux = shell(probe, uname="Linux", tmp=tmp_path, env=home).stdout
    assert mac == linux and mac.startswith("5433|6379|8000|5173|")


# ---- which database --------------------------------------------------------------------

def test_a_plain_database_url_in_the_shell_never_wins(tmp_path):
    out = shell(f"source '{ENV_SH}'; "
                "[ \"$DATABASE_URL\" = \"$NATIVE_DATABASE_URL\" ] && echo native; "
                "[ \"$REDIS_URL\" != 'redis://decoy:1/0' ] && echo redis-native",
                tmp=tmp_path, env={"DATABASE_URL": "sqlite:///decoy.db",
                                   "REDIS_URL": "redis://decoy:1/0"})
    assert out.stdout.split() == ["native", "redis-native"]


def test_an_explicit_scratch_target_wins_and_the_native_one_is_kept(tmp_path):
    # Compared inside the shell, so not even a failure diff can show the
    # desk's own URL.
    out = shell(f"source '{ENV_SH}'; "
                "[ \"$DATABASE_URL\" = \"$QUANTDESK_DATABASE_URL\" ] && echo db-target; "
                "[ \"$REDIS_URL\" = \"$QUANTDESK_REDIS_URL\" ] && echo redis-target; "
                "[ \"$NATIVE_DATABASE_URL\" != \"$DATABASE_URL\" ] && echo native-kept",
                tmp=tmp_path,
                env={"QUANTDESK_DATABASE_URL": f"sqlite:///{tmp_path}/scratch.db",
                     "QUANTDESK_REDIS_URL": "redis://127.0.0.1:6390/0"})
    assert out.stdout.split() == ["db-target", "redis-target", "native-kept"]


def test_no_override_means_exactly_the_previous_defaults(tmp_path):
    url = make_url(native_url(tmp_path))
    assert (url.get_backend_name(), url.host, url.port, url.database) == (
        "postgresql", "localhost", 5433, "quantdesk")


# ---- migrate.sh -----------------------------------------------------------------------

def _variants_of_the_desk(tmp_path, env_sh):
    url = make_url(native_url(tmp_path, env_sh))
    assert url.port == 1, "the scratch tree's desk must be unreachable"
    for host in ("localhost", "127.0.0.1"):
        yield url.set(host=host).render_as_string(hide_password=False)


def test_migrate_refuses_an_override_that_is_the_desks_database(tmp_path):
    tree = scripts_tree(tmp_path)
    env_sh = tree / "scripts" / "native" / "env.sh"
    password = make_url(native_url(tmp_path, env_sh)).password
    for desk in _variants_of_the_desk(tmp_path, env_sh):
        out = shell(f"bash '{tree}/scripts/migrate.sh'", tmp=tmp_path,
                    env={"QUANTDESK_DATABASE_URL": desk})
        assert out.returncode == 1, out.stdout + out.stderr
        assert "Refusing QUANTDESK_DATABASE_URL" in out.stdout
        assert "postgresql://" in out.stdout and "/quantdesk" in out.stdout
        assert "schema" not in (out.stdout + out.stderr).lower(), "never reached app.migrate"
        if password:
            # As a credential: the database name itself may contain the
            # same letters, and `describe()` shows that name.
            assert f":{password}@" not in out.stdout + out.stderr, "never shown"
            assert "@" not in out.stdout, "no userinfo at all"


def test_migrate_refuses_an_unreadable_override(tmp_path):
    tree = scripts_tree(tmp_path)
    out = shell(f"bash '{tree}/scripts/migrate.sh'", tmp=tmp_path,
                env={"QUANTDESK_DATABASE_URL": "not a url"})
    assert out.returncode == 1 and "Refusing QUANTDESK_DATABASE_URL" in out.stdout


def test_migrate_dry_run_goes_to_the_named_scratch_database(tmp_path):
    tree = scripts_tree(tmp_path)
    scratch = tmp_path / "scratch.db"
    out = shell(f"bash '{tree}/scripts/migrate.sh'", tmp=tmp_path,
                env={"QUANTDESK_DATABASE_URL": f"sqlite:///{scratch}"})
    text = out.stdout + out.stderr
    assert f"database: sqlite:///{scratch} (explicit QUANTDESK_DATABASE_URL)" in text
    assert f"sqlite:///{scratch} has no alembic_version" in text, text
    assert "postgresql://" not in text, text


def test_the_scratch_tree_differs_from_the_real_scripts_only_in_the_dead_port(tmp_path):
    tree = scripts_tree(tmp_path)
    assert (tree / "scripts" / "migrate.sh").read_text() == MIGRATE_SH.read_text()
    real = ENV_SH.read_text().splitlines()
    copy = (tree / "scripts" / "native" / "env.sh").read_text().splitlines()
    changed = [i for i, (a, b) in enumerate(zip(real, copy, strict=True)) if a != b]
    assert len(real) == len(copy) and len(changed) == 1
    assert real[changed[0]].startswith("export DATABASE_URL=")


# ---- the supervised (systemd) host -------------------------------------------------

SYSTEMD = ROOT / "scripts" / "native" / "systemd"
RUN_API = ROOT / "scripts" / "native" / "run_api.sh"


def _fake_systemctl(tmp_path, enabled):
    bin_dir = tmp_path / "fake-systemctl"
    bin_dir.mkdir(exist_ok=True)
    stub = bin_dir / "systemctl"
    stub.write_text(f"#!/bin/sh\nexit {0 if enabled else 1}\n")
    stub.chmod(0o755)
    return {"PATH": f"{bin_dir}:{os.environ['PATH']}"}


def test_start_and_stop_refuse_where_systemd_supervises_the_desk(tmp_path):
    out = shell(f"source '{ENV_SH}'; refuse_if_systemd_managed; echo carried-on",
                tmp=tmp_path, env=_fake_systemctl(tmp_path, enabled=True))
    assert out.returncode == 1 and "carried-on" not in out.stdout
    assert "sudo systemctl" in out.stdout


@pytest.mark.parametrize("enabled", [False, None])
def test_without_the_unit_the_scripts_carry_on_as_before(tmp_path, enabled):
    # None: no systemctl at all, as on the Mac.
    env = _fake_systemctl(tmp_path, enabled) if enabled is not None else {}
    out = shell(f"source '{ENV_SH}'; refuse_if_systemd_managed; echo carried-on",
                tmp=tmp_path, env=env)
    assert out.returncode == 0 and out.stdout.split() == ["carried-on"]


@pytest.mark.parametrize("script", ["start.sh", "stop.sh"])
def test_the_refusal_comes_before_anything_is_touched(script):
    text = (ROOT / "scripts" / script).read_text()
    assert text.index("refuse_if_systemd_managed") < text.index("lifecycle_lock ")


def test_run_api_refuses_a_second_api_before_starting_anything(tmp_path):
    """uvicorn runs the lifespan — scheduler, Angel login, paper engine —
    before binding, so a second copy must be refused before uvicorn runs."""
    import socket
    holder = socket.socket()
    holder.bind(("127.0.0.1", 0))
    holder.listen()
    port = holder.getsockname()[1]
    tree = tmp_path / "tree"
    (tree / "scripts" / "native").mkdir(parents=True)
    # The desk's database and Redis on port 1, where nothing listens, as in
    # scripts_tree(): even a broken refusal cannot reach the real ones.
    env_sh = (ENV_SH.read_text().replace("API_PORT=8000", f"API_PORT={port}")
              .replace(":${PG_PORT}/", ":1/").replace(":${REDIS_PORT}/", ":1/"))
    assert f"API_PORT={port}" in env_sh and env_sh.count(":1/") == 2
    (tree / "scripts" / "native" / "env.sh").write_text(env_sh)
    (tree / "scripts" / "native" / "run_api.sh").write_text(RUN_API.read_text())
    (tree / ".venv").symlink_to(ROOT / ".venv")
    (tree / "backend").symlink_to(ROOT / "backend")
    try:
        out = shell(f"HOME='{tmp_path}' bash '{tree}/scripts/native/run_api.sh'", tmp=tmp_path)
    finally:
        holder.close()
    assert out.returncode == 1, out.stdout + out.stderr
    assert f"refusing to start: :{port} is already held" in out.stdout
    assert "schema" not in out.stdout.lower(), "refused before the schema check"


def _unit(name):
    return (SYSTEMD / name).read_text()


def test_the_api_unit_runs_one_process_and_drains_before_any_kill(tmp_path):
    api = _unit("quantdesk-api.service")
    code = [ln for ln in (api + RUN_API.read_text()).splitlines()
            if not ln.lstrip().startswith("#")]
    assert not any("--workers" in ln for ln in code)
    assert "KillSignal=SIGTERM" in api and "KillMode=mixed" in api
    stop = int(api.split("TimeoutStopSec=")[1].split()[0])
    allowed = int(api.split("QUANTDESK_SUPERVISOR_STOP_SECONDS=")[1].split()[0])
    assert stop == allowed
    # The shutdown policy's default deadline (10 + 120 + 15) fits inside it.
    out = subprocess.run([sys.executable, "-m", "app.shutdown_policy"],
                         cwd=tmp_path, env={**os.environ, "PYTHONPATH": str(ROOT / "backend")},
                         capture_output=True, text=True, timeout=60)
    deadline = int(out.stdout.split("deadline=")[1].split()[0])
    assert deadline <= stop
    assert "Requires=quantdesk-postgres.service quantdesk-redis.service" in api


def test_the_units_keep_the_ports_and_loopback_env_sh_names(tmp_path):
    probe = f"source '{ENV_SH}'; echo \"$PG_PORT $REDIS_PORT $API_PORT $WEB_PORT\""
    pg, redis, api, web = shell(probe, uname="Linux", tmp=tmp_path,
                                env={"HOME": "/home/azureuser"}).stdout.split()
    data = shell(f"source '{ENV_SH}'; echo \"$PG_DATA\"", uname="Linux", tmp=tmp_path,
                 env={"HOME": "/home/azureuser"}).stdout.strip()
    assert f"-D {data}" in _unit("quantdesk-postgres.service")
    assert f"--port {redis} --bind 127.0.0.1" in _unit("quantdesk-redis.service")
    assert '--save ""' in _unit("quantdesk-redis.service")
    assert (pg, api, web) == ("5433", "8000", "5173")
    assert "--host 127.0.0.1" in RUN_API.read_text()
    assert "--host 127.0.0.1" in (ROOT / "scripts" / "native" / "run_web.sh").read_text()


@pytest.mark.parametrize("unit", ["quantdesk-api.service", "quantdesk-web.service"])
def test_each_unit_hands_its_log_to_the_user_logrotate_runs_as(unit):
    """systemd creates an append: log as root; logrotate's `su azureuser`
    could then never truncate it, and the log would grow without bound."""
    text = _unit(unit)
    log = text.split("StandardOutput=append:")[1].split()[0]
    assert f"ExecStartPre=+/usr/bin/chown azureuser:azureuser {log}" in text
    rotated = (ROOT / "scripts" / "native" / "logrotate" / "quantdesk").read_text()
    assert log in rotated and "su azureuser azureuser" in rotated


def test_logind_keeps_the_database_users_shared_memory():
    """PostgreSQL runs as a login user; logind's RemoveIPC=yes deletes its
    shared memory when that user's last session ends."""
    conf = (ROOT / "scripts" / "native" / "logind" / "quantdesk-removeipc.conf").read_text()
    assert "[Login]" in conf and "\nRemoveIPC=no\n" in conf
    assert "User=azureuser" in _unit("quantdesk-postgres.service")
