"""The public HTTPS front door exposes the dashboard and nothing else.

`scripts/native/caddy/Caddyfile` puts one authenticated HTTPS site in front of
the API that already runs on loopback. These tests hold it to what it claims:

  - the owner signs in before anything is served — files, API, websocket;
  - only the GET requests the dashboard actually makes are forwarded (GET,
    not read-only: `/signals/live` may record a holdout exposure in the API);
  - every route that writes, imports, migrates or runs research is absent;
  - the API key is added by the proxy and never seen by, or sent from, the
    browser, and neither it nor the owner's password reaches the access log;
  - a websocket opened from another site is refused.

The first group reads the files and the application's route table. The second
runs the real Caddyfile, unmodified, in front of a throwaway upstream on
loopback — never the desk's API, and nothing here starts a scheduler or a
feed. It needs a `caddy` binary (PATH, or QUANTDESK_CADDY_BIN) of the release
named in scripts/native/caddy/CADDY_VERSION. A development machine without one
skips that group; CI sets QUANTDESK_REQUIRE_CADDY=1, and there a missing
binary fails every test in it.
"""
import base64
import contextlib
import functools
import hashlib
import json
import os
import re
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from urllib.parse import unquote, urlsplit

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

CADDY_DIR = ROOT / "scripts" / "native" / "caddy"
CADDYFILE = CADDY_DIR / "Caddyfile"
FRONTEND_SRC = ROOT / "frontend" / "src"
CADDY_VERSION = (CADDY_DIR / "CADDY_VERSION").read_text().strip()

STREAM = "/ws/signals"


def proxied_paths() -> set[str]:
    """Every path the Caddyfile forwards for the dashboard, without /api."""
    block = re.search(r"@dashboard \{(.*?)\n\t\t\}", CADDYFILE.read_text(), re.S).group(1)
    return {p.removeprefix("/api") for line in block.splitlines()
            if line.strip().startswith("path ") for p in line.split()[1:]}


def dashboard_reads() -> set[str]:
    """Every path the dashboard asks for: the getJSON() calls in its source."""
    found = set()
    for source in FRONTEND_SRC.iterdir():
        if source.suffix not in (".js", ".jsx") or ".test." in source.name:
            continue
        for path in re.findall(r"""getJSON\(\s*[`"']([^`"'?$]+)""", source.read_text()):
            found.add(path)
    return found


# ---- what the files say --------------------------------------------------

def test_the_proxy_forwards_exactly_what_the_dashboard_reads():
    """A route added to the dashboard and not here is a broken panel; a route
    here and not in the dashboard is exposure nobody asked for."""
    assert dashboard_reads(), "found no getJSON() calls — the pattern has drifted"
    assert proxied_paths() == dashboard_reads()


def test_every_forwarded_path_is_a_get_route_outside_the_write_key():
    """Each one exists, answers GET, and does not sit behind the write key.
    Nothing the proxy forwards may be a route `require_api_key` guards. That
    is all this shows: it does not show a route writes nothing."""
    from app.main import app
    from fastapi.routing import APIRoute

    routes: dict[str, APIRoute] = {}

    def collect(found):
        for route in found:
            if isinstance(route, APIRoute):
                if "GET" in route.methods:
                    routes[route.path] = route
            else:
                inner = (getattr(route, "routes", None)
                         or getattr(getattr(route, "original_router", None), "routes", None))
                if inner:
                    collect(inner)
    collect(app.routes)

    def dependencies(dependant):
        for sub in dependant.dependencies:
            yield getattr(sub.call, "__name__", "")
            yield from dependencies(sub)

    for path in sorted(proxied_paths()):
        assert path in routes, f"{path} is not a GET route in the API"
        assert "require_api_key" not in set(dependencies(routes[path].dependant)), path


def test_the_stream_is_the_only_websocket_and_the_key_is_added_per_request():
    text = CADDYFILE.read_text()
    assert f"path /api{STREAM}" in text
    # `{env.…}` is read per request. `{$…}` would be substituted when the file
    # is loaded and so written into the configuration Caddy holds and prints.
    assert "{env.QUANTDESK_API_KEY}" in text
    assert "{$QUANTDESK_API_KEY" not in text


def global_options(caddyfile: str) -> str:
    """The Caddyfile's global options block: the one block with no name,
    opened by a `{` alone at the start of a line and closed by the next `}`
    alone at the start of a line. Snippets — `(name) {` — and the site come
    with a name before the brace and are not it."""
    lines = caddyfile.splitlines()
    (opened,) = [i for i, line in enumerate(lines) if line == "{"]
    closed = next(i for i in range(opened + 1, len(lines)) if lines[i] == "}")
    return "\n".join(lines[opened + 1:closed])


def debug_is_on(caddyfile: str) -> bool:
    """Is Caddy's debug logging switched on by this Caddyfile: the `debug`
    global option — in the options block or in a snippet it imports — or a
    `level debug` on any log."""
    directives = [line.strip() for line in caddyfile.splitlines()
                  if line.strip() and not line.lstrip().startswith("#")]
    options = [line.strip() for line in global_options(caddyfile).splitlines()]
    imported = set()
    for name in re.findall(r"^\s*import\s+(\S+)", global_options(caddyfile), re.M):
        # An import whose name comes from a setting can bring in any snippet.
        pattern = re.escape(name).replace(re.escape("{$QUANTDESK_CERTIFICATES:production}"),
                                          r"[\w-]+")
        for snippet, body in re.findall(r"^\((\S+)\) \{\n(.*?)\n\}$", caddyfile, re.M | re.S):
            if re.fullmatch(pattern, snippet):
                imported.update(line.strip() for line in body.splitlines())
    return (any(re.fullmatch(r"debug\b.*", line) for line in [*options, *imported])
            or any(re.fullmatch(r"level\s+debug\b.*", line, re.I) for line in directives))


def test_debug_logging_is_off():
    """With Caddy's `debug` option on, its reverse proxy logs the address of
    each request it forwards — for the stream, the one that carries the key."""
    text = CADDYFILE.read_text()
    # The block being read is the global options, not a certificate snippet.
    options = global_options(text)
    assert "skip_install_trust" in options and "http_port" in options
    assert "import certificates-" in options and "acme_ca" not in options
    assert not debug_is_on(text)


@pytest.mark.parametrize("planted", [
    ("\tskip_install_trust\n", "\tskip_install_trust\n\tdebug\n"),          # the global option
    ("{\n\t# 80 and 443", "{\n\tdebug\n\t# 80 and 443"),                    # first in the block
    ("\tskip_install_trust\n", "\tskip_install_trust\n\tdebug # for now\n"),
    # In a certificate snippet, which the global block imports.
    ("(certificates-production) {\n", "(certificates-production) {\n\tdebug\n"),
    ("(certificates-staging) {\n", "(certificates-staging) {\n\tdebug\n"),
    # A debug level on the site's log.
    ("\tlog {\n", "\tlog {\n\t\tlevel DEBUG\n"),
])
def test_the_debug_check_sees_a_planted_debug(planted):
    old, new = planted
    text = CADDYFILE.read_text()
    assert text.count(old) == 1, old
    assert debug_is_on(text.replace(old, new))


def test_the_debug_check_is_not_fooled_by_the_word_elsewhere():
    """A comment that mentions debug, as the real file's does, is not debug
    being on; nor is the word inside the site block's own directives."""
    text = CADDYFILE.read_text()
    assert "debug" in text, "the real file warns about it in a comment"
    assert not debug_is_on(text.replace("\tskip_install_trust\n",
                                        "\tskip_install_trust\n\t# debug\n"))
    assert not debug_is_on(text.replace('respond "not found" 404', 'respond "debug" 404'))


def test_the_documents_say_what_was_decided_about_debug_updates_and_certificates():
    readme = " ".join((CADDY_DIR / "README.md").read_text().split())
    for said in (
        "prohibited with production credentials",
        "with made-up credentials",
        "turn debug off", "restrict access to the logs", "rotate the API key",
        "Rotating the key while debug is still on is not a mitigation",
        "The VM is not held there", "Record every upgrade",
        "check_caddy_updates.sh",
        "acme-staging-v02.api.letsencrypt.org", "One production certificate",
        "trades cross-CA fallback", "If production issuance fails: stop the go-live",
        "Do not switch CA", "Do not accept a certificate warning",
        "Do not force or repeat production issuance",
        "If Caddy is not the reviewed version", "There is no override",
        "Have the result reviewed again",
        "there is no `apt-mark hold` and no other pin",
        # /signals/live: served, not storable from here, and not read-only.
        "GET is not the same as read-only",
        "computes and serves the current signal",
        "It cannot store a signal row from here",
        "may commit methodology holdout-exposure metadata",
        "`note_exposure()` safeguard",
        "is not blocked, suppressed or changed by anything here",
        # Certificates: a pair each, and one directory to clear.
        "/var/lib/caddy/quantdesk/production", "/var/lib/caddy/quantdesk/staging-trial",
        "clear_staging_trial.sh", "searches for nothing and matches nothing by pattern",
        # install.sh: check, then promote; and how to go back.
        "Check — nothing on the machine is replaced", "Promote — only after every check",
        "If promotion fails", "web.previous", "Caddyfile.previous",
        # The update checker is evidence.
        "diagnostic evidence, not an authoritative evaluation",
        "UNKNOWN — MANUAL INTERPRETATION REQUIRED",
        "A first certificate does not guarantee later renewals",
        "an owner-controlled hostname is required",
        "A browser certificate warning is never an accepted state",
    ):
        assert said in readme, said
    checker = (CADDY_DIR / "check_caddy_updates.sh").read_text()
    assert "--dry-run" in checker
    # It looks; it does not change the update policy or install anything.
    commands = "\n".join(line for line in checker.splitlines()
                         if not line.lstrip().startswith(("#", "echo ")))
    assert not re.search(r"apt(-get)? +(install|upgrade)|\btee\b|apt-mark +(un)?hold|>\s*/etc"
                         r"|systemctl +\w*start", commands)


def test_no_secret_is_written_in_the_repository_files():
    example = (CADDY_DIR / "caddy.env.example").read_text()
    values = dict(line.split("=", 1) for line in example.splitlines()
                  if line and not line.startswith("#"))
    assert values["QUANTDESK_BASIC_HASH"].startswith("REPLACE_WITH")
    assert values["QUANTDESK_API_KEY"].startswith("REPLACE_WITH")
    # The Azure DNS label is not chosen yet; no hostname is invented here.
    assert values["QUANTDESK_HOSTNAME"].startswith("REPLACE_WITH")
    # A bcrypt hash anywhere here would be a real credential committed.
    for name in ("Caddyfile", "caddy.env.example", "install.sh", "install_caddy.sh",
                 "check_caddy_updates.sh", "certificate_authority.sh",
                 "clear_staging_trial.sh", "README.md"):
        assert "$2a$" not in (CADDY_DIR / name).read_text(), name
        assert "$2y$" not in (CADDY_DIR / name).read_text(), name


def test_the_public_build_is_same_origin_and_refuses_a_key():
    script = (ROOT / "scripts" / "native" / "build_web.sh").read_text()
    assert "VITE_API_URL=/api" in script
    assert "refusing to build: VITE_API_KEY is set" in script
    # The tunnel's dev server, which does pass the key, is still there: it is
    # what the rollback returns to.
    assert "VITE_API_KEY" in (ROOT / "scripts" / "native" / "run_web.sh").read_text()


def test_caddys_own_cpu_use_is_capped():
    """Containment of Caddy's processor use beside the scheduler and feed —
    not a rate limit, and no promise that the dashboard stays available."""
    unit = (CADDY_DIR / "caddy.service.d" / "quantdesk.conf").read_text()
    settings = dict(line.split("=", 1) for line in unit.splitlines()
                    if "=" in line and not line.startswith("#"))
    assert settings == {"EnvironmentFile": "/etc/quantdesk/caddy.env",
                        "CPUQuota": "50%", "CPUWeight": "20"}


def test_one_caddy_release_is_named_and_everything_installs_that_one():
    assert re.fullmatch(r"2\.\d+\.\d+", CADDY_VERSION), "a stable release, not a beta or rc"
    installer = (CADDY_DIR / "install_caddy.sh").read_text()
    assert 'CADDY_VERSION"' in installer and '"caddy=$VERSION"' in installer
    # Installed at that release, not held there: the VM may update it later.
    # And never moved back: nothing here downgrades a Caddy.
    for script in CADDY_DIR.glob("*.sh"):
        commands = "\n".join(line for line in script.read_text().splitlines()
                             if not line.lstrip().startswith(("#", "echo ")))
        assert not re.search(r"apt-mark +(un)?hold", commands), script.name
        assert "allow-downgrades" not in commands, script.name
    # Caddy's own repository and its stock build: no xcaddy, no plugin.
    assert "dl.cloudsmith.io/public/caddy/stable" in installer
    assert "xcaddy" not in installer and "add-package" not in installer
    assert "CADDY_VERSION" in (CADDY_DIR / "install.sh").read_text()
    assert "basic_auth {" in CADDYFILE.read_text()
    assert "basicauth" not in CADDYFILE.read_text()


def test_the_documented_hash_is_bcrypt_cost_14_and_prompts_for_the_password():
    command = "caddy hash-password --algorithm bcrypt --bcrypt-cost 14"
    readme = (CADDY_DIR / "README.md").read_text()
    assert command in readme
    # The password is typed at a prompt, never given as an argument.
    for name in ("README.md", "install.sh", "install_caddy.sh", "caddy.env.example"):
        assert "--plaintext" not in (CADDY_DIR / name).read_text(), name
    # install.sh refuses a hash of any other kind or cost.
    assert r"'^\$2[aby]\$14\$[./A-Za-z0-9]{53}$'" in (CADDY_DIR / "install.sh").read_text()


def test_ci_installs_that_release_and_cannot_skip_these_tests():
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text()
    install = workflow.index("scripts/native/caddy/install_caddy.sh")
    assert install < workflow.index("pytest tests"), "Caddy is installed before the tests run"
    assert 'QUANTDESK_REQUIRE_CADDY: "1"' in workflow


CADDY = os.environ.get("QUANTDESK_CADDY_BIN") or shutil.which("caddy")
# Set in CI: these are the security tests, and there "no caddy" is a failure.
CADDY_REQUIRED = os.environ.get("QUANTDESK_REQUIRE_CADDY") == "1"
needs_caddy = pytest.mark.skipif(
    not CADDY and not CADDY_REQUIRED,
    reason="no caddy binary (PATH or QUANTDESK_CADDY_BIN); set QUANTDESK_REQUIRE_CADDY=1 to fail")

USER, PASSWORD = "owner", "correct horse battery staple, for this test only"
KEY = "upstream-key-FOR-THIS-TEST-ONLY-0123456789"


# ---- the installers, run against a scratch machine --------------------------

# The right shape for a bcrypt hash of cost 14, with no password behind it.
SHAPED_HASH = "$2a$14$" + "N" * 53
GOOD_SETTINGS = {"QUANTDESK_HOSTNAME": "desk.example.com", "QUANTDESK_BASIC_USER": "owner",
                 "QUANTDESK_BASIC_HASH": SHAPED_HASH, "QUANTDESK_API_KEY": KEY}


# `sudo` for the scratch machine. Runs the command as the test's own user, after
# writing it down — and after checking that every path among its arguments is
# inside the scratch machine — or the system's temporary directory, where the
# installer keeps its working files (`mktemp`), which is never somewhere a site
# is deployed. One that is not is recorded and refused.
SUDO_STAND_IN = r'''
import os, re, sys
scratch, temporary, command = sys.argv[1], sys.argv[2].split(":"), sys.argv[3:]
# For a run with no install root — the installer as production runs it, naming
# /etc, /srv and /var/lib themselves — those paths are carried into the scratch
# machine's root here, the one place every write passes through.
carried = os.environ.get("SCRATCH_MACHINE_ROOT")
if carried:
    def carry(found):
        path = found.group(0)
        real = path in ("/var", "/var/lib") or path.startswith(("/etc/", "/srv/", "/var/lib/"))
        return carried + path if real else path
    command = [re.sub(r"(?<![\w.$}/-])/[\w./-]*", carry, word) for word in command]
with open(os.path.join(scratch, "calls"), "a") as log:
    log.write("sudo " + " ".join(command) + "\n")
for path in re.findall(r"(?<![\w.$}-])/[\w./-]*", " ".join(command[1:])):
    inside = path.startswith(scratch + "/") or any(path.startswith(t + "/") for t in temporary)
    if not inside and path not in ("/dev/stdin", "/dev/null"):
        with open(os.path.join(scratch, "escapes"), "a") as log:
            log.write(f"outside the scratch machine: {path!r} in: sudo {' '.join(command)}\n")
        sys.exit(97)
os.execvp(command[0], command)
'''

_temporary = Path(tempfile.gettempdir())
SYSTEM_TEMPORARY = ":".join(sorted({str(_temporary), str(_temporary.resolve())}))

# Where the site really lives on a machine that has it. No test may change these.
REAL_PATHS = ("/etc/caddy", "/etc/caddy/Caddyfile", "/etc/quantdesk", "/etc/quantdesk/caddy.env",
              "/srv/quantdesk", "/srv/quantdesk/web", "/var/lib/caddy", "/var/lib/caddy/quantdesk",
              "/etc/systemd/system/caddy.service.d",
              "/etc/systemd/system/caddy.service.d/quantdesk.conf")


def real_deployment_paths() -> dict:
    found = {}
    for path in map(Path, REAL_PATHS):
        try:
            status = path.lstat()
            found[str(path)] = (status.st_mtime_ns, status.st_size, status.st_ino)
        except OSError:
            found[str(path)] = None
    return found


class Machine:
    """A stand-in for the VM, in a temporary directory: a copy of the
    repository's layout to run the installers from, and a root of its own
    (`QUANTDESK_INSTALL_ROOT`) holding a site that is already being served —
    an old dashboard, an old Caddyfile, an old drop-in.

    `sudo` runs what it is given, as the test's user, and writes it down —
    unless an argument names a path outside the temporary directory, which it
    refuses and records (`escapes`): a write that slipped past the install
    root would be stopped there, not carried out on the machine running the
    tests. `systemctl`, `curl` and `chown` only write down what they were
    asked, so no real service is ever addressed.
    `caddy` is either the real binary under test or, where a test stops before
    Caddy is consulted, a script that reports a version and nothing more. The
    dashboard build is a stub. Nothing outside the temporary directory is
    touched, and no real service is involved."""

    def __init__(self, tmp_path: Path, *, caddy: str | None = f"v{CADDY_VERSION}",
                 settings: dict | str | None = GOOD_SETTINGS, build: str = "ok"):
        self.tmp, self.root = tmp_path, tmp_path / "root"
        self.calls, self.escapes = tmp_path / "calls", tmp_path / "escapes"
        scripts = tmp_path / "repo" / "scripts" / "native"
        shutil.copytree(CADDY_DIR, scripts / "caddy")
        self.dir = scripts / "caddy"
        (tmp_path / "repo" / ".env").write_text("API_KEY=synthetic-key-from-the-project-env\n")
        stub = {"ok": 'mkdir -p "$1/assets" && echo NEW > "$1/index.html"',
                "fails": "exit 1", "empty": 'mkdir -p "$1"'}[build]
        (scripts / "build_web.sh").write_text(f"#!/bin/sh\n{stub}\n")
        (scripts / "build_web.sh").chmod(0o755)

        self.bin = tmp_path / "bin"
        self.bin.mkdir()
        self.fail_mv = tmp_path / "fail-mv-of"
        (tmp_path / "sudo.py").write_text(SUDO_STAND_IN)
        stand_ins = {
            "sudo": f'exec "{sys.executable}" "{tmp_path / "sudo.py"}" "{tmp_path}" '
                    f'"{SYSTEM_TEMPORARY}" "$@"',
            # Writes down what it was asked. Asked whether a unit is active, it
            # says what `systemctl_says()` told it to: by default, inactive.
            "systemctl": f'echo "systemctl $*" >> \'{self.calls}\'\n'
                         f'[ "$1" = is-active ] || exit 0\n'
                         f'cat \'{tmp_path / "is-active.says"}\'\n'
                         f'exit "$(cat \'{tmp_path / "is-active.status"}\')"',
            "pgrep": "exit 1",          # no such process
            "curl": f'echo "curl $*" >> \'{self.calls}\'',
            "chown": "exit 0",          # not root here; ownership is not what is tested
            "id": "exit 0",
            "install": 'args=()\nwhile [ $# -gt 0 ]; do case "$1" in -o|-g) shift 2 ;; '
                       '*) args+=("$1"); shift ;; esac; done\nexec /usr/bin/install "${args[@]}"',
            "mv": f'if [ -e \'{self.fail_mv}\' ]; then case "$*" in *"$(cat \'{self.fail_mv}\')") '
                  'exit 1 ;; esac; fi\nexec /bin/mv "$@"',
        }
        if caddy == "real":
            (self.bin / "caddy").symlink_to(Path(CADDY).resolve())
        elif caddy is not None:
            stand_ins["caddy"] = (f'[ "$1" = version ] && echo \'{caddy} h1:stand-in=\' && exit 0\n'
                                  f'echo "caddy $*" >> \'{self.calls}\'\nexit 1')
        for name, body in stand_ins.items():
            (self.bin / name).write_text(f"#!/bin/bash\n{body}\n")
            (self.bin / name).chmod(0o755)

        for made in ("etc/caddy", "etc/systemd/system/caddy.service.d", "var/lib/caddy",
                     "srv/quantdesk/web"):
            (self.root / made).mkdir(parents=True)
        self.web = self.root / "srv/quantdesk/web"
        self.caddyfile = self.root / "etc/caddy/Caddyfile"
        self.dropin = self.root / "etc/systemd/system/caddy.service.d/quantdesk.conf"
        self.settings = self.root / "etc/quantdesk/caddy.env"
        (self.web / "index.html").write_text("OLD\n")
        self.caddyfile.write_text("# the Caddyfile being served\n")
        self.dropin.write_text("# the drop-in in use\n")
        if settings is not None:
            self.settings.parent.mkdir(parents=True)
            self.settings.write_text(settings if isinstance(settings, str) else
                                     "# settings\n" + "".join(f"{k}={v}\n"
                                                              for k, v in settings.items()))
        (tmp_path / "stage").mkdir()
        self.env = {"PATH": f"{self.bin}:/usr/bin:/bin", "HOME": str(tmp_path),
                    "TMPDIR": str(tmp_path / "stage"), "QUANTDESK_INSTALL_ROOT": str(self.root)}
        # Every command that could reach the real machine resolves to a stand-in.
        self.systemctl_says(3, "inactive")
        for name in ("sudo", "systemctl", "pgrep", "curl", "chown", "id", "install", "mv",
                     *(["caddy"] if caddy is not None else [])):
            assert shutil.which(name, path=self.env["PATH"]) == str(self.bin / name), name

    def state(self) -> dict:
        """Every file, directory and link under the machine's root."""
        return {str(p.relative_to(self.root)):
                ("link", os.readlink(p)) if p.is_symlink() else
                ("dir",) if p.is_dir() else ("file", p.read_bytes())
                for p in sorted(self.root.rglob("*"))}

    def run(self, script: str = "install.sh", *, may_escape: bool = False,
            as_production: bool = False, **env):
        """Run one of the scripts. `as_production` runs it the way the VM
        does, with QUANTDESK_INSTALL_ROOT unset: the paths it then names are
        the real ones, and the stand-in `sudo` carries them into this
        machine's root (and refuses any it does not)."""
        environment = {**self.env, **env}
        if as_production:
            del environment["QUANTDESK_INSTALL_ROOT"]
            environment["SCRATCH_MACHINE_ROOT"] = str(self.root)
        real = real_deployment_paths()
        ran = subprocess.run(["bash", str(self.dir / script)], env=environment,
                             capture_output=True, text=True)
        # Whatever the script did, it did it inside the temporary directory.
        assert real_deployment_paths() == real, "a real deployment path changed"
        assert may_escape or not self.escapes.exists(), self.escapes.read_text()
        if script == "install.sh" and not as_production:
            # With an install root set, systemd is not addressed at all — not
            # through sudo and not directly, not even to reload.
            assert self.services() == [], self.services()
        else:
            assert set(self.services()) <= {
                "sudo systemctl daemon-reload", "systemctl daemon-reload",
                "systemctl is-active caddy"}, self.services()
        return ran

    def systemctl_says(self, status: int, output: str):
        """What the stand-in answers to `systemctl is-active …`."""
        (self.tmp / "is-active.says").write_text(output + "\n" if output else "")
        (self.tmp / "is-active.status").write_text(str(status))

    def services(self) -> list[str]:
        """Every time `systemctl` was reached, by any route."""
        return [c for c in self.asked() if "systemctl" in c]

    def asked(self) -> list[str]:
        return self.calls.read_text().splitlines() if self.calls.exists() else []

    def changes_asked(self) -> list[str]:
        """What was asked for that could change the machine: everything
        except looking (`sudo test`, `sudo cat`)."""
        return [c for c in self.asked() if not c.startswith(("sudo test ", "sudo cat "))]

    def refused(self, expect: str = ""):
        """Run the site installer and hold it to having changed nothing."""
        before = self.state()
        ran = self.run()
        assert ran.returncode != 0, "it went ahead:\n" + ran.stdout
        assert "STOPPED" in ran.stdout and "Nothing was changed." in ran.stdout, ran.stdout
        assert expect in ran.stdout, ran.stdout
        assert self.state() == before, "a failed check left the machine changed"
        assert self.changes_asked() == [], self.changes_asked()
        return ran


OTHER_VERSIONS = ["v2.6.2", "v2.10.2", "v2.11.6", "v2.11.8", "v2.12.0", "v3.0.0",
                  "v2.11.7-beta.1", "v2.11.70", "2.11.7"]


@pytest.mark.parametrize("installed", OTHER_VERSIONS)
def test_any_other_caddy_stops_the_deployment_before_anything_is_touched(tmp_path, installed):
    """Older or newer, no exception — and first: not even the settings file
    is read, the site is not built, and nothing is written."""
    machine = Machine(tmp_path, caddy=installed)
    machine.refused("the reviewed version")
    assert machine.asked() == [], "the version gate was not the first thing"
    before = machine.state()
    again = machine.run("install_caddy.sh")
    assert again.returncode != 0 and "Nothing was changed" in again.stdout
    assert machine.asked() == [] and machine.state() == before


def test_with_no_caddy_at_all_the_site_installer_stops_too(tmp_path):
    machine = Machine(tmp_path, caddy=None)
    machine.refused("not installed")
    assert machine.asked() == []


def test_a_caddy_that_is_already_the_reviewed_one_is_left_alone(tmp_path):
    """install_caddy.sh does not reinstall it, and does not `systemctl disable
    --now` a Caddy that may be serving."""
    machine = Machine(tmp_path)
    again = machine.run("install_caddy.sh")
    assert again.returncode == 0 and "nothing to do" in again.stdout
    assert machine.asked() == []


@pytest.mark.parametrize("problem, settings", [
    ("placeholders", {**GOOD_SETTINGS, "QUANTDESK_HOSTNAME": "REPLACE_WITH_AZURE_DNS_HOSTNAME"}),
    ("placeholders", {**GOOD_SETTINGS, "QUANTDESK_BASIC_HASH": "REPLACE_WITH_BCRYPT_COST_14_HASH"}),
    ("placeholders", {**GOOD_SETTINGS, "QUANTDESK_API_KEY": "REPLACE_WITH_API_KEY"}),
    ("QUANTDESK_HOSTNAME", {k: v for k, v in GOOD_SETTINGS.items() if k != "QUANTDESK_HOSTNAME"}),
    ("QUANTDESK_HOSTNAME", {**GOOD_SETTINGS, "QUANTDESK_HOSTNAME": "localhost"}),
    ("QUANTDESK_HOSTNAME", {**GOOD_SETTINGS, "QUANTDESK_HOSTNAME": "203.0.113.10"}),
    ("QUANTDESK_HOSTNAME", {**GOOD_SETTINGS, "QUANTDESK_HOSTNAME": "desk.example.com:8443"}),
    ("QUANTDESK_HOSTNAME", {**GOOD_SETTINGS, "QUANTDESK_HOSTNAME": "a.example.com, b.example.com"}),
    ("QUANTDESK_BASIC_USER", {**GOOD_SETTINGS, "QUANTDESK_BASIC_USER": ""}),
    ("QUANTDESK_BASIC_HASH", {**GOOD_SETTINGS, "QUANTDESK_BASIC_HASH": ""}),
    ("QUANTDESK_BASIC_HASH", {**GOOD_SETTINGS, "QUANTDESK_BASIC_HASH": "$2a$10$" + "N" * 53}),
    ("QUANTDESK_BASIC_HASH", {**GOOD_SETTINGS, "QUANTDESK_BASIC_HASH": "$2a$14$" + "N" * 20}),
    ("QUANTDESK_BASIC_HASH", {**GOOD_SETTINGS, "QUANTDESK_BASIC_HASH": "a-password-not-a-hash"}),
    ("QUANTDESK_BASIC_HASH", {**GOOD_SETTINGS,
                              "QUANTDESK_BASIC_HASH": "$argon2id$v=19$m=47104,t=1,p=1$c2Fs$aGFz"}),
    ("QUANTDESK_API_KEY", {k: v for k, v in GOOD_SETTINGS.items() if k != "QUANTDESK_API_KEY"}),
    ("QUANTDESK_CERTIFICATES", {**GOOD_SETTINGS, "QUANTDESK_CERTIFICATES": "zerossl"}),
    ("QUANTDESK_CERTIFICATES", {**GOOD_SETTINGS, "QUANTDESK_CERTIFICATES": "Production"}),
    ("does not take", {**GOOD_SETTINGS,
                       "QUANTDESK_ACME_CA": "https://acme.zerossl.com/v2/DV90"}),
    ("does not take", {**GOOD_SETTINGS, "QUANTDESK_STORAGE_ROOT": "/var/lib/caddy/.local"}),
    ("does not take", {**GOOD_SETTINGS, "QUANTDESK_UPSTREAM": "127.0.0.1:5433"}),
    ("does not take", {**GOOD_SETTINGS, "QUANTDESK_HTTP_PORT": "8080"}),
    ("does not take", {**GOOD_SETTINGS, "QUANTDESK_HTTPS_PORT": "8443"}),
    ("does not take", {**GOOD_SETTINGS, "QUANTDESK_WEB_ROOT": "/home/azureuser"}),
    ("does not take", {**GOOD_SETTINGS, "QUANTDESK_ACCESS_LOG": "/tmp/access.log"}),
    ("does not take", {**GOOD_SETTINGS, "QUANTDESK_INSTALL_ROOT": "/tmp/elsewhere"}),
    ("does not take", {**GOOD_SETTINGS, "QUANTDESK_NOT_A_SETTING": "1"}),
    ("more than once", {**GOOD_SETTINGS, "QUANTDESK_CERTIFICATES": "staging"}
     and "".join(f"{k}={v}\n" for k, v in GOOD_SETTINGS.items())
     + "QUANTDESK_CERTIFICATES=staging\nQUANTDESK_CERTIFICATES=production\n"),
    ("does not take", {**GOOD_SETTINGS, "CADDY_ADMIN": "0.0.0.0:2019"}),
    ("more than once", "".join(f"{k}={v}\n" for k, v in GOOD_SETTINGS.items())
     + "QUANTDESK_HOSTNAME=other.example.com\n"),
])
def test_settings_that_are_not_complete_and_valid_change_nothing(tmp_path, problem, settings):
    Machine(tmp_path, settings=settings).refused(problem)


FIVE = {"QUANTDESK_HOSTNAME", "QUANTDESK_BASIC_USER", "QUANTDESK_BASIC_HASH",
        "QUANTDESK_API_KEY", "QUANTDESK_CERTIFICATES"}
# Read by the Caddyfile so that the tests can run it on loopback in a temporary
# directory. Each defaults to the fixed production value; none is a setting.
TEST_ONLY = {"QUANTDESK_HTTP_PORT", "QUANTDESK_HTTPS_PORT", "QUANTDESK_UPSTREAM",
             "QUANTDESK_WEB_ROOT", "QUANTDESK_ACCESS_LOG", "QUANTDESK_STORAGE_ROOT"}


def test_the_deployment_has_exactly_five_settings():
    installer = (CADDY_DIR / "install.sh").read_text()
    (accepted,) = re.findall(r"^    ((?:QUANTDESK_\w+\|?)+)\) ;;$", installer, re.M)
    assert set(accepted.split("|")) == FIVE
    template = (CADDY_DIR / "caddy.env.example").read_text()
    assert set(re.findall(r"^#?([A-Z][A-Z_]+)=", template, re.M)) == FIVE
    caddyfile = "\n".join(line for line in CADDYFILE.read_text().splitlines()
                          if not line.lstrip().startswith("#"))
    read = set(re.findall(r"\{(?:\$|env\.)(\w+)", caddyfile))
    assert read == FIVE | TEST_ONLY
    # The others keep their production values unless a test says otherwise.
    for name, fixed in (("HTTP_PORT", "80"), ("HTTPS_PORT", "443"),
                        ("UPSTREAM", "127.0.0.1:8000"), ("WEB_ROOT", "/srv/quantdesk/web"),
                        ("ACCESS_LOG", "/var/log/caddy/quantdesk-access.log"),
                        ("STORAGE_ROOT", "/var/lib/caddy/quantdesk")):
        used = re.findall(rf"\{{\$QUANTDESK_{name}(.*?)\}}", caddyfile)
        assert used and set(used) == {f":{fixed}"}, name
    readme = " ".join((CADDY_DIR / "README.md").read_text().split())
    assert "Settings: five, and only five" in readme
    assert "Nothing else is configurable for this deployment, on purpose" in readme
    for name in FIVE | TEST_ONLY:
        assert f"`{name}`" in readme, name


def test_the_install_root_is_documented_as_a_test_hook_and_promotion_as_not_atomic():
    readme = " ".join((CADDY_DIR / "README.md").read_text().split())
    for said in (
        "`QUANTDESK_INSTALL_ROOT` is a test hook", "In production it must be unset",
        "changes the filesystem root the installer uses",
        "installing under <directory>", "It does not prove isolation",
        "With the hook set the installer does not call `systemctl` at all",
        "prints that it skipped it",
        "With the hook unset, which is the only way production is installed",
        "absent from the environment altogether, which is not the same as set to nothing",
        '[ -z "${QUANTDESK_INSTALL_ROOT+set}" ] && echo unset',
        "None of those is ever taken to mean production",
        "No setting, argument or environment variable can point it anywhere else",
        "positively established that Caddy is stopped",
        "every directory above it is a real directory, not a link",
        "runs `systemctl daemon-reload` after a successful promotion",
        "Promotion is not fully atomic", "this is not a zero-downtime deployment",
        "the served directory is absent", "may get a transient 404",
        "accepted for an owner-only dashboard deployed in the approved after-market window",
    ):
        assert said in readme, said
    # And nothing in the directory claims the opposite.
    for path in [*CADDY_DIR.glob("*"), CADDY_DIR / "caddy.service.d" / "quantdesk.conf"]:
        if path.is_file():
            text = " ".join(path.read_text().lower().split())
            for claim in ("atomically", "is atomic", "zero downtime", "no downtime",
                          "without interruption"):
                assert claim not in text, f"{path.name}: {claim}"
            assert "zero-downtime" not in text.replace("not a zero-downtime", ""), path.name


def test_every_path_the_installer_touches_is_under_the_install_root():
    """Read from the script, command by command. The four destinations are
    built from $PREFIX and nothing else; every `sudo` command names only
    those, $PREFIX itself, the temporary directory, or the repository; and no
    absolute system path is written in the script outside those definitions."""
    lines = [line for line in (CADDY_DIR / "install.sh").read_text().splitlines()
             if not line.lstrip().startswith("#")]
    text = "\n".join(lines)
    # Absent is production; present is a scratch directory, or it is refused.
    absent = 'if [ -z "${QUANTDESK_INSTALL_ROOT+set}" ]; then\n  SCRATCH=no\n  PREFIX=""\nelse'
    assert absent in text
    assert "${QUANTDESK_INSTALL_ROOT:-" not in text, "that would read empty as absent"
    assert re.findall(r"^\s*PREFIX=(.*)$", text, re.M) == [
        '""', '"$QUANTDESK_INSTALL_ROOT"', '"$(cd "$PREFIX" && pwd -P)" || stop '
        '"QUANTDESK_INSTALL_ROOT could not be resolved."']
    assert '  echo "installing under $PREFIX"\nfi\n' in text
    for name, under in (("ENV_FILE", "/etc/quantdesk/caddy.env"),
                        ("WEB_ROOT", "/srv/quantdesk/web"),
                        ("CADDYFILE", "/etc/caddy/Caddyfile"),
                        ("DROPIN", "/etc/systemd/system/caddy.service.d/quantdesk.conf")):
        assert re.findall(rf"^{name}=(.*)$", text, re.M) == [f'"$PREFIX{under}"'], name
    # Literal system paths appear only behind $PREFIX — bar one line that
    # reads the drop-in's own text, and the message beside it.
    system = r"(?<!\$PREFIX)(?<![\w}])/(etc|srv|var|usr|opt|home|root|tmp)\b"
    literal = [line.strip() for line in lines if re.search(system, line)]
    assert literal == [
        "grep -q '^EnvironmentFile=/etc/quantdesk/caddy.env$' \"$STAGE/quantdesk.conf\" ||",
        'stop "the service drop-in does not read /etc/quantdesk/caddy.env"']
    allowed = r"\$(\{?(ENV_FILE|WEB_ROOT|CADDYFILE|DROPIN|PREFIX|STAGE|HERE|1|target|needed)\b|\()"
    for line in lines:
        if "sudo " not in line or line.strip().startswith("echo"):
            continue
        for variable in re.findall(r"\$\{?\w+|\$\(", line):
            assert re.match(allowed, variable), f"{variable} in: {line.strip()}"
    # What swap and undo are handed is those same three destinations.
    assert 'for target in "$WEB_ROOT" "$CADDYFILE" "$DROPIN"; do' in text
    # Not under the root, and said so in README.md: the temporary directory.
    assert 'STAGE="$(mktemp -d)"' in text


def test_the_scratch_machine_refuses_a_path_outside_itself(tmp_path):
    """The tripwire, shown working: an installer changed to name the real
    settings file is stopped at its first `sudo`, and the escape is recorded.
    Without this the tests above would prove less than they appear to."""
    machine = Machine(tmp_path)
    script = machine.dir / "install.sh"
    text = script.read_text()
    assert text.count('ENV_FILE="$PREFIX/etc/quantdesk/caddy.env"') == 1
    script.write_text(text.replace('ENV_FILE="$PREFIX/etc/quantdesk/caddy.env"',
                                   'ENV_FILE="/etc/quantdesk/caddy.env"'))
    ran = machine.run(may_escape=True)       # which also holds the real paths unchanged
    assert ran.returncode != 0
    escapes = machine.escapes.read_text()
    assert "'/etc/quantdesk/caddy.env'" in escapes and "sudo test -f" in escapes
    assert "sudo install" in escapes, "the write was refused as well as the look"
    assert (machine.web / "index.html").read_text() == "OLD\n"


@pytest.mark.parametrize("value, expect", [
    ("", "set but empty"), ("/", "the real root"), ("//", "the real root"),
    ("/.", "the real root"), ("/..", "the real root"), ("/tmp/../", "the real root"),
    ("scratch", "absolute path"), ("./root", "absolute path"), (" ", "absolute path"),
    ("/no/such/directory/anywhere", "existing directory"),
])
def test_an_install_root_that_is_not_a_scratch_directory_is_refused(tmp_path, value, expect):
    """Present but empty is not absent. Neither it, nor `/`, nor anything
    else that is not a scratch directory, is taken to mean production: the
    installer stops before it reads, writes or asks for anything."""
    machine = Machine(tmp_path, caddy="real" if CADDY else f"v{CADDY_VERSION}")
    before = machine.state()
    ran = machine.run(QUANTDESK_INSTALL_ROOT=value)
    assert ran.returncode != 0, "it went ahead:\n" + ran.stdout
    assert "STOPPED" in ran.stdout and "Nothing was changed." in ran.stdout
    assert expect in ran.stdout, ran.stdout
    assert "installing under" not in ran.stdout
    assert machine.asked() == [], "something was asked for before the refusal"
    assert machine.services() == [] and machine.state() == before
    assert not machine.escapes.exists()


def test_a_link_to_the_real_root_is_refused_as_an_install_root(tmp_path):
    machine = Machine(tmp_path)
    (tmp_path / "looks-like-scratch").symlink_to("/")
    ran = machine.run(QUANTDESK_INSTALL_ROOT=str(tmp_path / "looks-like-scratch"))
    assert ran.returncode != 0 and "the real root" in ran.stdout
    assert machine.asked() == []


def test_the_install_root_is_reported_when_it_is_set(tmp_path):
    machine = Machine(tmp_path, settings={**GOOD_SETTINGS, "QUANTDESK_HOSTNAME": "localhost"})
    ran = machine.refused()
    assert ran.stdout.splitlines()[0] == f"installing under {machine.root}"


def test_a_refusal_names_the_setting_and_never_its_value(tmp_path):
    secret = "a-password-pasted-where-the-hash-goes"
    ran = Machine(tmp_path, settings={**GOOD_SETTINGS,
                                      "QUANTDESK_BASIC_HASH": secret}).refused()
    assert secret not in ran.stdout + ran.stderr and KEY not in ran.stdout + ran.stderr


@pytest.mark.parametrize("build", ["fails", "empty"])
def test_a_dashboard_that_does_not_build_changes_nothing(tmp_path, build):
    Machine(tmp_path, build=build).refused("dashboard")


def test_the_first_run_only_creates_the_settings_file(tmp_path):
    """The one write that comes before the checks are complete: a new file,
    from the template, replacing nothing. The site being served is untouched."""
    machine = Machine(tmp_path, settings=None)
    before = machine.state()
    ran = machine.run()
    assert ran.returncode == 0 and "then run this again" in ran.stdout
    after = machine.state()
    created = set(after) - set(before)
    assert created == {"etc/quantdesk", "etc/quantdesk/caddy.env"}
    assert {k: after[k] for k in before} == before
    written = machine.settings.read_text()
    assert "QUANTDESK_HOSTNAME=REPLACE_WITH_AZURE_DNS_HOSTNAME" in written
    assert "QUANTDESK_API_KEY=synthetic-key-from-the-project-env" in written
    assert "synthetic-key" not in ran.stdout + ran.stderr
    # And run again as it stands, it is refused: the placeholders are still there.
    machine.calls.unlink()
    machine.refused("placeholders")


def test_the_installer_checks_in_order_and_only_then_writes():
    """Read from the script as well: the version gate comes before everything,
    and no command that writes under the machine's root comes before the line
    that begins the promotion — bar the first-run creation of the settings
    file, which ends the run."""
    text = (CADDY_DIR / "install.sh").read_text()
    check = "\n".join(line for line in text.split("# ---- 2. promote", 1)[0].splitlines()
                      if not line.lstrip().startswith("#"))
    gate = check.index('[ "$HAVE" = "$WANT" ] ||')
    assert gate < check.index("sudo ") and gate < check.index("build_web.sh")
    order = [check.index(mark) for mark in (
        '[ "$HAVE" = "$WANT" ] ||', 'SETTINGS="$(sudo cat', "QUANTDESK_HOSTNAME is not",
        "QUANTDESK_BASIC_HASH is not", "build_web.sh\" \"$STAGE/web\"", "caddy validate",
        "certificate_authority.sh\" --env-file", "id caddy")]
    assert order == sorted(order)
    first_run = check.index('if ! sudo test -f "$ENV_FILE"; then')
    bootstrap = check[first_run:check.index("\nfi\n", first_run)]
    assert bootstrap.rstrip().endswith("exit 0")
    rest = check.replace(bootstrap, "")
    writes = re.findall(r"^[^#\n]*sudo +(?!test |cat )\S+.*$", rest, re.M)
    assert writes == [], writes
    # Nothing in it starts, restarts, reloads, enables or stops a service. The
    # one thing it asks of systemd is to re-read its unit files, once, as the
    # last step — and only when no install root is set.
    assert set(re.findall(r"systemctl +\S+", text)) == {"systemctl daemon-reload"}
    commands = [line.strip() for line in text.splitlines()
                if "systemctl" in line and not line.lstrip().startswith(("#", "echo "))]
    assert commands == ["sudo systemctl daemon-reload"]
    assert ('if [ "$SCRATCH" = no ]; then\n  sudo systemctl daemon-reload\nelse\n  echo '
            '"           systemctl daemon-reload SKIPPED: QUANTDESK_INSTALL_ROOT is set') in text
    assert text.index("for target in \"$WEB_ROOT\"") < text.index("sudo systemctl daemon-reload")
    assert "sort -V" not in text and "NOTE:" not in text


@needs_caddy
class TestInstallWithTheRealCaddy:
    """From here the checks reach Caddy itself, so the binary under test is
    used: validation, the adapted configuration, and then promotion."""

    def edited(self, machine: Machine, old: str, new: str):
        path = machine.dir / "Caddyfile"
        text = path.read_text()
        assert text.count(old) == 1, old
        path.write_text(text.replace(old, new))

    def test_a_caddyfile_caddy_does_not_accept_changes_nothing(self, tmp_path):
        machine = Machine(tmp_path, caddy="real")
        self.edited(machine, "\tskip_install_trust\n", "\tskip_install_trust\n\tno_such_option\n")
        machine.refused("caddy validate")

    def test_validating_does_not_create_the_log_or_the_storage(self, tmp_path):
        """`caddy validate` creates the log file and the storage directory of
        the configuration it loads. Left pointed at the real paths that would
        be a root-owned log Caddy could not then write to."""
        machine = Machine(tmp_path, caddy="real")
        self.edited(machine, "acme_ca https://acme-v02.api.letsencrypt.org/directory",
                    "acme_ca https://acme.zerossl.com/v2/DV90")
        machine.refused("certificate authority")       # which is after validation
        assert not (machine.root / "var/lib/caddy/quantdesk").exists()
        assert not (machine.root / "var/log").exists()

    @pytest.mark.parametrize("old, new", [
        # Another CA; no CA named (Caddy's two defaults); production's CA kept
        # in the trial's directory; and in Caddy's default storage.
        ("acme_ca https://acme-v02.api.letsencrypt.org/directory",
         "acme_ca https://acme.zerossl.com/v2/DV90"),
        ("\tacme_ca https://acme-v02.api.letsencrypt.org/directory\n", ""),
        ("/var/lib/caddy/quantdesk}/production", "/var/lib/caddy/quantdesk}/staging-trial"),
        ("\tstorage file_system {$QUANTDESK_STORAGE_ROOT:/var/lib/caddy/quantdesk}/production\n",
         ""),
    ])
    def test_an_authority_or_storage_that_is_not_allowed_changes_nothing(self, tmp_path,
                                                                        old, new):
        machine = Machine(tmp_path, caddy="real")
        self.edited(machine, old, new)
        ran = machine.refused("certificate authority")
        assert "REFUSED" in ran.stdout

    def test_a_machine_that_is_not_ready_changes_nothing(self, tmp_path):
        machine = Machine(tmp_path, caddy="real")
        (machine.root / "var/lib/caddy").rmdir()
        machine.refused("var/lib/caddy does not exist")

    def test_when_every_check_passes_the_site_is_promoted_beside_what_it_replaced(self, tmp_path):
        machine = Machine(tmp_path, caddy="real")
        ran = machine.run()
        assert ran.returncode == 0, ran.stdout + ran.stderr
        assert "Let's Encrypt production only" in ran.stdout
        assert "/var/lib/caddy/quantdesk/production" in ran.stdout
        assert (machine.web / "index.html").read_text() == "NEW\n"
        assert machine.caddyfile.read_text() == CADDYFILE.read_text()
        assert machine.dropin.read_text() == (CADDY_DIR / "caddy.service.d"
                                              / "quantdesk.conf").read_text()
        # What was being served is kept, whole, beside each.
        assert (machine.web.with_name("web.previous") / "index.html").read_text() == "OLD\n"
        assert Path(f"{machine.caddyfile}.previous").read_text() == "# the Caddyfile being served\n"
        assert Path(f"{machine.dropin}.previous").read_text() == "# the drop-in in use\n"
        assert Path(f"{machine.caddyfile}.before-quantdesk").exists()
        assert not list(machine.root.rglob("*.new"))
        # Promotion came after every check. This is a run with an install root,
        # so systemd was not addressed — not even to reload — and it says so.
        output = ran.stdout
        assert output.index("ready") < output.index("promoting")
        assert machine.services() == []
        assert ("systemctl daemon-reload SKIPPED: QUANTDESK_INSTALL_ROOT is set"
                in output), output
        assert output.index("promoting") < output.index("daemon-reload SKIPPED")
        assert KEY not in output + ran.stderr and SHAPED_HASH not in output + ran.stderr
        # A second run keeps one generation: the first run's files become .previous.
        again = machine.run()
        assert again.returncode == 0
        assert (machine.web.with_name("web.previous") / "index.html").read_text() == "NEW\n"
        assert Path(f"{machine.caddyfile}.previous").read_text() == CADDYFILE.read_text()

    def test_all_five_settings_together_are_accepted(self, tmp_path):
        machine = Machine(tmp_path, caddy="real",
                          settings={**GOOD_SETTINGS, "QUANTDESK_CERTIFICATES": "production"})
        assert set(GOOD_SETTINGS) | {"QUANTDESK_CERTIFICATES"} == FIVE
        ran = machine.run()
        assert ran.returncode == 0, ran.stdout + ran.stderr
        assert "Let's Encrypt production only" in ran.stdout

    def test_everything_a_whole_installation_writes_lands_under_the_install_root(self, tmp_path):
        """A complete run, first the settings file and then the site: every
        `sudo` command passed the path check, what was created is under the
        root's /etc and /srv, and the certificate storage — which is Caddy's
        to create, not the installer's — was neither created nor touched."""
        machine = Machine(tmp_path, caddy="real", settings=None)
        assert machine.run().returncode == 0                    # creates the settings file
        machine.settings.write_text("".join(f"{k}={v}\n" for k, v in GOOD_SETTINGS.items()))
        before = set(machine.state())
        ran = machine.run()
        assert ran.returncode == 0, ran.stdout + ran.stderr
        assert ran.stdout.splitlines()[0] == f"installing under {machine.root}"
        created = set(machine.state()) - before
        assert created and all(path.startswith(("etc/caddy/", "srv/quantdesk/",
                                                "etc/systemd/system/caddy.service.d/"))
                               for path in created), created
        assert not (machine.root / "var/lib/caddy/quantdesk").exists()
        assert not machine.escapes.exists()

    def test_a_write_outside_the_install_root_would_be_caught_at_promotion(self, tmp_path):
        machine = Machine(tmp_path, caddy="real")
        script = machine.dir / "install.sh"
        script.write_text(script.read_text().replace('WEB_ROOT="$PREFIX/srv/quantdesk/web"',
                                                     'WEB_ROOT="/srv/quantdesk/web"'))
        ran = machine.run(may_escape=True)
        assert ran.returncode != 0
        assert "'/srv/quantdesk/web.new'" in machine.escapes.read_text()
        assert (machine.web / "index.html").read_text() == "OLD\n"

    def test_without_an_install_root_systemd_is_reloaded_once_after_promotion(self, tmp_path):
        """The installer as the VM runs it: QUANTDESK_INSTALL_ROOT unset. It
        names /etc and /srv themselves; the stand-in `sudo` carries those into
        the scratch machine, and the stand-in `systemctl` only writes down
        that it was called. Exactly one `daemon-reload`, after the last file
        is in place, and nothing else asked of systemd."""
        machine = Machine(tmp_path, caddy="real")
        ran = machine.run(as_production=True)
        assert ran.returncode == 0, ran.stdout + ran.stderr
        assert "installing under" not in ran.stdout and "SKIPPED" not in ran.stdout
        assert not machine.escapes.exists()
        # The same promotion, at the same places under the machine's root.
        assert (machine.web / "index.html").read_text() == "NEW\n"
        assert machine.caddyfile.read_text() == CADDYFILE.read_text()
        assert (machine.web.with_name("web.previous") / "index.html").read_text() == "OLD\n"
        assert machine.services() == ["sudo systemctl daemon-reload", "systemctl daemon-reload"]
        asked = machine.asked()
        last_move = max(i for i, call in enumerate(asked) if call.startswith("sudo mv "))
        assert asked.index("sudo systemctl daemon-reload") > last_move
        assert asked[last_move].endswith(f"{machine.dropin}.new {machine.dropin}")

    def test_without_an_install_root_a_failure_never_reaches_systemd(self, tmp_path):
        # A check that fails: nothing changed, systemd not addressed.
        refused = Machine(tmp_path / "a", caddy="real",
                          settings={**GOOD_SETTINGS, "QUANTDESK_HOSTNAME": "localhost"})
        before = refused.state()
        ran = refused.run(as_production=True)
        assert ran.returncode != 0 and "Nothing was changed." in ran.stdout
        assert refused.state() == before and refused.services() == []
        # A promotion that fails part-way: put back, systemd not addressed.
        failing = Machine(tmp_path / "b", caddy="real")
        failing.fail_mv.write_text(f"{failing.caddyfile}.new {failing.caddyfile}")
        ran = failing.run(as_production=True)
        assert ran.returncode != 0 and "Restored." in ran.stdout
        assert (failing.web / "index.html").read_text() == "OLD\n"
        assert failing.caddyfile.read_text() == "# the Caddyfile being served\n"
        assert failing.services() == []

    def test_the_staging_trial_is_promoted_as_a_trial_and_says_so(self, tmp_path):
        machine = Machine(tmp_path, caddy="real",
                          settings={**GOOD_SETTINGS, "QUANTDESK_CERTIFICATES": "staging"})
        ran = machine.run()
        assert ran.returncode == 0, ran.stdout + ran.stderr
        assert "STAGING only" in ran.stdout and "STAGING TRIAL" in ran.stdout
        assert "/var/lib/caddy/quantdesk/staging-trial" in ran.stdout

    @pytest.mark.parametrize("fails_on", ["web.new", "Caddyfile.new", "quantdesk.conf.new",
                                          "Caddyfile.previous"])
    def test_a_promotion_that_fails_part_way_puts_back_what_was_served(self, tmp_path, fails_on):
        machine = Machine(tmp_path, caddy="real")
        target = {"web.new": f"{machine.web}.new {machine.web}",
                  "Caddyfile.new": f"{machine.caddyfile}.new {machine.caddyfile}",
                  "quantdesk.conf.new": f"{machine.dropin}.new {machine.dropin}",
                  "Caddyfile.previous": f"{machine.caddyfile} {machine.caddyfile}.previous"}
        machine.fail_mv.write_text(target[fails_on])
        ran = machine.run()
        assert ran.returncode != 0 and "FAILED while putting" in ran.stdout
        assert "Restored. The site is as it was before this run." in ran.stdout
        assert (machine.web / "index.html").read_text() == "OLD\n"
        assert machine.caddyfile.read_text() == "# the Caddyfile being served\n"
        assert machine.dropin.read_text() == "# the drop-in in use\n"
        assert not list(machine.root.rglob("*.new"))
        assert machine.services() == [] and "daemon-reload" not in ran.stdout

    def test_a_first_deployment_that_fails_part_way_leaves_nothing_half_installed(self, tmp_path):
        machine = Machine(tmp_path, caddy="real")
        shutil.rmtree(machine.web)
        machine.dropin.unlink()
        machine.fail_mv.write_text(f"{machine.dropin}.new {machine.dropin}")
        ran = machine.run()
        assert ran.returncode != 0 and "Restored" in ran.stdout
        assert not machine.web.exists() and not machine.dropin.exists()
        assert machine.caddyfile.read_text() == "# the Caddyfile being served\n"


# ---- the trial's storage, and clearing it -----------------------------------

def test_staging_and_production_have_storage_directories_of_their_own():
    """Read from the Caddyfile's own lines: two pairs, each one CA and one
    directory."""
    written = {mode: (ca, root, kept) for mode, ca, root, kept in re.findall(
        r"\(certificates-(\w+)\) \{\n\tacme_ca (\S+)\n\tstorage file_system "
        r"\{\$QUANTDESK_STORAGE_ROOT:(\S+?)\}(/\S+)\n\}", CADDYFILE.read_text())}
    assert set(written) == {"production", "staging"}
    (ca_p, root_p, production), (ca_s, root_s, staging) = written["production"], written["staging"]
    assert ca_p == "https://acme-v02.api.letsencrypt.org/directory"
    assert ca_s == "https://acme-staging-v02.api.letsencrypt.org/directory"
    assert root_p == root_s == "/var/lib/caddy/quantdesk"
    assert (production, staging) == ("/production", "/staging-trial")
    # Neither is the other, inside the other, or Caddy's default storage.
    for one, other in ((production, staging), (staging, production)):
        assert not (root_p + one + "/").startswith(root_p + other + "/")
    assert ".local/share/caddy" not in CADDYFILE.read_text()
    # One switch picks a whole pair; there is no setting for a CA or a path alone.
    text = CADDYFILE.read_text()
    assert "import certificates-{$QUANTDESK_CERTIFICATES:production}" in text
    assert text.count("acme_ca ") == 2 and text.count("storage file_system ") == 2
    assert "QUANTDESK_ACME_CA" not in text


TRIAL = "var/lib/caddy/quantdesk/staging-trial"
PRODUCTION = "var/lib/caddy/quantdesk/production"


def test_clearing_the_trial_names_one_directory_and_searches_for_nothing():
    script = (CADDY_DIR / "clear_staging_trial.sh").read_text()
    commands = "\n".join(line for line in script.splitlines()
                         if not line.lstrip().startswith(("#", "echo ")))
    # The target is a constant, written in full. Nothing from the environment
    # or the command line goes into it.
    assert f"readonly TRIAL=/{TRIAL}\n" in commands
    assigned = re.findall(r"^\s*(?:readonly\s+)?TRIAL=.*$", commands, re.M)
    assert assigned == [f"readonly TRIAL=/{TRIAL}"]
    assert "QUANTDESK_" not in script and "STORAGE_ROOT" not in script
    assert not re.search(r"\$\{?[0-9@*#]", commands), "it takes no arguments"
    removals = re.findall(r"^.*\brm\b.*$", commands, re.M)
    assert removals == ['sudo rm -rf -- "$TRIAL" || stop "removing $TRIAL failed part-way; '
                        'look at what is left of it."']
    for forbidden in (r"\bfind\b", "-delete", r"\bxargs\b", r"\bproduction\b", r"\.local",
                      r"rm\b[^\n]*\*", r"systemctl +(start|stop|restart|reload|kill)"):
        assert not re.search(forbidden, commands), forbidden
    # Every level above it is checked, by name, for being a link.
    assert "for part in /var /var/lib /var/lib/caddy /var/lib/caddy/quantdesk; do" in commands
    # And the old sweep through Caddy's shared storage is gone from the procedure.
    for name in ("README.md", "install.sh", "install_caddy.sh"):
        text = (CADDY_DIR / name).read_text()
        assert "-delete" not in text and "acme-staging-v02*" not in text, name
        assert not re.search(r"\bfind\b[^\n]*(/var/lib/caddy|staging)", text), name


def stored(machine) -> Path:
    """What a VM that has run both the trial and production holds, in the
    scratch machine's own /var/lib/caddy/quantdesk — with a look-alike beside
    them that only a pattern would mistake for the trial."""
    root = machine.root / "var/lib/caddy/quantdesk"
    for kept in ("production/certificates/acme-v02.api.letsencrypt.org-directory/desk/desk.crt",
                 "production/acme/acme-v02.api.letsencrypt.org-directory/users/account.json",
                 "staging-trial/certificates/acme-staging-v02.api.letsencrypt.org-directory/d.crt",
                 "staging-trial/acme/acme-staging-v02.api.letsencrypt.org-directory/users/a.json",
                 "staging-trial-old/keep.me", "something-else/acme-staging-v02-lookalike/keep.me"):
        (root / kept).parent.mkdir(parents=True, exist_ok=True)
        (root / kept).write_text(kept)
    return root


def clear(machine, **env):
    """The cleanup as the VM runs it — it names /var/lib/caddy/... itself; the
    stand-in `sudo` carries that into the scratch machine, and refuses any
    other real path."""
    return machine.run("clear_staging_trial.sh", as_production=True, **env)


def everything_under(root: Path) -> set[str]:
    return {str(p.relative_to(root)) for p in root.rglob("*")}


def test_clearing_the_trial_removes_the_trial_and_nothing_beside_it(tmp_path):
    machine = Machine(tmp_path)
    root = stored(machine)
    before = everything_under(root)
    ran = clear(machine)
    assert ran.returncode == 0, ran.stdout + ran.stderr
    after = everything_under(root)
    assert before - after == {p for p in before if p.split("/")[0] == "staging-trial"}
    assert after == {p for p in before if p.split("/")[0] != "staging-trial"}
    assert (root / "production/certificates/acme-v02.api.letsencrypt.org-directory"
            / "desk/desk.crt").exists()
    assert (root / "staging-trial-old/keep.me").exists()
    assert f"removed /{TRIAL}" in ran.stdout
    # Caddy was only asked about, never told to do anything.
    assert machine.services() == ["systemctl is-active caddy"]
    # Run again with nothing to clear, it clears nothing.
    again = clear(machine)
    assert again.returncode == 0 and "nothing to clear" in again.stdout
    assert everything_under(root) == after


def test_no_setting_argument_or_variable_can_point_the_cleanup_elsewhere(tmp_path):
    machine = Machine(tmp_path)
    root = stored(machine)
    elsewhere = tmp_path / "elsewhere"
    for kept in ("staging-trial/would-be-removed", "production/keep"):
        (elsewhere / kept).parent.mkdir(parents=True)
        (elsewhere / kept).write_text(kept)
    there = everything_under(elsewhere)
    environment = {"QUANTDESK_STORAGE_ROOT": str(elsewhere), "TRIAL": str(elsewhere / "production"),
                   "ROOT": str(elsewhere), "QUANTDESK_INSTALL_ROOT_": str(elsewhere)}
    ran = subprocess.run(["bash", str(machine.dir / "clear_staging_trial.sh"),
                          str(elsewhere / "production"), "--root", str(elsewhere)],
                         env={**machine.env, **environment,
                              "SCRATCH_MACHINE_ROOT": str(machine.root)},
                         capture_output=True, text=True)
    assert ran.returncode == 0, ran.stdout + ran.stderr
    assert everything_under(elsewhere) == there, "the cleanup was redirected"
    assert not (root / "staging-trial").exists() and (root / "production").is_dir()
    assert not machine.escapes.exists()


# What `systemctl is-active caddy` may say, as (exit status, output). Only the
# first two mean stopped. `is-active` exits non-zero for every state but
# "active" and for every way of failing, so the status alone says nothing.
STOPPED = [(3, "inactive"), (3, "failed")]
NOT_ESTABLISHED = [
    (0, "active"), (0, "reloading"), (0, ""), (0, "inactive"),
    (3, "activating"), (3, "deactivating"), (3, "unknown"), (3, ""), (3, "inactive\nactive"),
    (4, "inactive"), (1, "inactive"),
    (1, "Failed to connect to bus: No such file or directory"),
    (1, "System has not been booted with systemd as init system (PID 1). Can't operate."),
    (1, "Failed to get properties: Access denied"),
    (127, "systemctl: command not found"), (126, "Permission denied"), (255, ""),
]


@pytest.mark.parametrize("status, said", STOPPED)
def test_the_trial_is_cleared_when_caddy_is_positively_stopped(tmp_path, status, said):
    machine = Machine(tmp_path)
    root = stored(machine)
    machine.systemctl_says(status, said)
    assert clear(machine).returncode == 0
    assert not (root / "staging-trial").exists() and (root / "production").is_dir()


@pytest.mark.parametrize("status, said", NOT_ESTABLISHED)
def test_anything_short_of_positively_stopped_removes_nothing(tmp_path, status, said):
    machine = Machine(tmp_path)
    root = stored(machine)
    before = everything_under(root)
    machine.systemctl_says(status, said)
    ran = clear(machine)
    assert ran.returncode != 0, f"it went ahead on {status}: {said!r}"
    assert "STOPPED" in ran.stdout and "Nothing was removed." in ran.stdout
    assert everything_under(root) == before
    assert not [c for c in machine.asked() if c.startswith("sudo rm")]


def test_a_missing_systemctl_removes_nothing(tmp_path):
    """Not a stand-in that fails, but no `systemctl` to be found at all: the
    shell's own "command not found" is not a statement that Caddy is stopped."""
    machine = Machine(tmp_path)
    root = stored(machine)
    before = everything_under(root)
    (machine.bin / "systemctl").unlink()
    only = tmp_path / "only"
    only.mkdir()
    for needed in ("bash", "sh", "cat", "test", "rm", "env"):
        (only / needed).symlink_to(shutil.which(needed))
    ran = subprocess.run([shutil.which("bash"), str(machine.dir / "clear_staging_trial.sh")],
                         env={**machine.env, "PATH": f"{machine.bin}:{only}",
                              "SCRATCH_MACHINE_ROOT": str(machine.root)},
                         capture_output=True, text=True)
    assert ran.returncode != 0 and "could not establish that Caddy is stopped" in ran.stdout
    assert everything_under(root) == before


def pgrep_exits(machine, status: int):
    (machine.bin / "pgrep").write_text(f"#!/bin/sh\nexit {status}\n")


@pytest.mark.parametrize("status, expect", [
    (0, "a caddy process is running"),
    (2, "pgrep -x caddy exited 2"), (3, "pgrep -x caddy exited 3"),
    (126, "pgrep -x caddy exited 126"), (127, "pgrep -x caddy exited 127"),
    (255, "pgrep -x caddy exited 255"), (64, "pgrep -x caddy exited 64"),
])
def test_a_caddy_process_or_a_failed_look_for_one_removes_nothing(tmp_path, status, expect):
    """systemd says inactive — that check passes — but a caddy is running
    outside it (0), or the look for one did not work (anything but 1). An
    error is not "no Caddy running"."""
    machine = Machine(tmp_path)
    root = stored(machine)
    before = everything_under(root)
    machine.systemctl_says(3, "inactive")
    pgrep_exits(machine, status)
    ran = clear(machine)
    assert ran.returncode != 0 and expect in ran.stdout, ran.stdout
    assert "STOPPED" in ran.stdout and "Nothing was removed." in ran.stdout
    assert everything_under(root) == before and (root / "staging-trial").is_dir()
    assert not [c for c in machine.asked() if c.startswith("sudo rm")]


def test_a_missing_pgrep_removes_nothing(tmp_path):
    """Not a stand-in that fails but no `pgrep` to be found at all, with
    systemd saying inactive: the shell's "command not found" is not a look."""
    machine = Machine(tmp_path)
    root = stored(machine)
    before = everything_under(root)
    (machine.bin / "pgrep").unlink()
    only = tmp_path / "only"
    only.mkdir()
    for needed in ("bash", "sh", "cat", "test", "rm", "env", "true"):
        (only / needed).symlink_to(shutil.which(needed))
    path = f"{machine.bin}:{only}"
    assert shutil.which("pgrep", path=path) is None and shutil.which("systemctl", path=path)
    ran = subprocess.run([shutil.which("bash"), str(machine.dir / "clear_staging_trial.sh")],
                         env={**machine.env, "PATH": path,
                              "SCRATCH_MACHINE_ROOT": str(machine.root)},
                         capture_output=True, text=True)
    assert ran.returncode != 0 and "pgrep -x caddy exited 127" in ran.stdout, ran.stdout
    assert "Nothing was removed." in ran.stdout
    assert everything_under(root) == before
    assert machine.services() == ["systemctl is-active caddy"]      # that check had passed


def test_no_caddy_process_is_not_enough_on_its_own(tmp_path):
    """pgrep answers 1 — no process — in every case here, and each time
    another safeguard fails: systemd does not say stopped; a level of the
    path is a link; the trial's own path is a link. Nothing is removed. With
    every safeguard passing, the same answer lets the cleanup through."""
    def machine_in(name):
        made = Machine(tmp_path / name)
        pgrep_exits(made, 1)
        return made, stored(made)

    for status, said in ((0, "active"), (3, "activating"), (1, "Failed to connect to bus"),
                         (127, "")):
        machine, root = machine_in(f"systemd-{status}-{said[:4]}")
        before = everything_under(root)
        machine.systemctl_says(status, said)
        assert clear(machine).returncode != 0
        assert everything_under(root) == before

    machine, root = machine_in("parent-link")
    (machine.root / "var/lib/caddy/quantdesk").rename(tmp_path / "moved")
    (machine.root / "var/lib/caddy/quantdesk").symlink_to(tmp_path / "moved")
    before = everything_under(tmp_path / "moved")
    ran = clear(machine)
    assert ran.returncode != 0 and "is a link" in ran.stdout
    assert everything_under(tmp_path / "moved") == before

    machine, root = machine_in("trial-link")
    shutil.rmtree(root / "staging-trial")
    (root / "staging-trial").symlink_to(root / "production")
    before = everything_under(root)
    assert clear(machine).returncode != 0
    assert everything_under(root) == before

    machine, root = machine_in("all-pass")
    ran = clear(machine)
    assert ran.returncode == 0, ran.stdout
    assert not (root / "staging-trial").exists() and (root / "production").is_dir()
    assert (root / "staging-trial-old/keep.me").exists()


def test_the_pgrep_guard_is_as_decided_and_its_limits_are_written_down():
    script = (CADDY_DIR / "clear_staging_trial.sh").read_text()
    guard = script[script.index("pgrep -x caddy >/dev/null 2>&1"):]
    guard = guard[:guard.index("esac") + 4]
    assert guard == ('pgrep -x caddy >/dev/null 2>&1\nFOUND=$?\ncase "$FOUND" in\n  1) ;;\n'
                     '  0) stop "a caddy process is running." ;;\n'
                     '  *) stop "could not check for a running caddy process '
                     '(pgrep -x caddy exited $FOUND)." ;;\nesac')
    assert script.count("pgrep") == sum(line.count("pgrep") for line in script.splitlines()
                                        if line.lstrip().startswith("#")) + 2
    # It comes before the removal, and nothing in the script manages Caddy or locks.
    assert script.index("esac", script.index("pgrep -x caddy >")) < script.index("sudo rm -rf")
    commands = "\n".join(line for line in script.splitlines()
                         if not line.lstrip().startswith(("#", "echo ")))
    assert not re.search(r"\b(flock|lockfile|kill|pkill|killall)\b", commands)
    assert not re.search(r"systemctl +(start|stop|restart|reload|mask|disable|enable)", commands)
    assert "They are not a lock" in script
    readme = " ".join((CADDY_DIR / "README.md").read_text().split())
    for said in ("exit 1, no process, is the only answer it proceeds on",
                 "a missing `pgrep` or any other exit status is a look that did not happen",
                 "are point-in-time checks, not a lock",
                 "Caddy must stay stopped for the whole cleanup",
                 "the script does not start, stop or restart Caddy",
                 "only as this step of the controlled after-market deployment procedure"):
        assert said in readme, said


@pytest.mark.parametrize("linked", ["var/lib/caddy/quantdesk/staging-trial",
                                    "var/lib/caddy/quantdesk", "var/lib/caddy", "var/lib", "var"])
def test_a_link_anywhere_on_the_way_down_removes_nothing(tmp_path, linked):
    """The trial's own name as a link to production's directory; and each
    directory above it as a link to another tree that has a `staging-trial`
    and a `production` of its own. Neither tree loses a file."""
    machine = Machine(tmp_path)
    root = stored(machine)
    decoy = tmp_path / "decoy"
    place = machine.root / linked
    if linked == TRIAL:
        shutil.rmtree(place)
        place.symlink_to(root / "production")
    else:
        # A whole copy of the level, moved aside, with a link left in its place.
        place.rename(decoy)
        place.symlink_to(decoy)
    assert (machine.root / TRIAL).exists() or linked == TRIAL
    watched = [machine.root / "var"] + ([decoy] if linked != TRIAL else [])
    before = [everything_under(w) for w in watched if w.exists()]
    ran = clear(machine)
    assert ran.returncode != 0 and "is a link, not a directory" in ran.stdout, ran.stdout
    assert "Nothing was removed." in ran.stdout
    assert [everything_under(w) for w in watched if w.exists()] == before
    assert not [c for c in machine.asked() if c.startswith("sudo rm")]


def test_a_trial_path_that_is_not_a_directory_removes_nothing(tmp_path):
    machine = Machine(tmp_path)
    root = stored(machine)
    shutil.rmtree(root / "staging-trial")
    (root / "staging-trial").write_text("a file where the directory should be")
    ran = clear(machine)
    assert ran.returncode != 0 and "is not a directory" in ran.stdout
    assert (root / "staging-trial").read_text() == "a file where the directory should be"


# ---- the update checker gathers evidence and reaches no verdict --------------

def update_checker(tmp_path: Path, *, origins: str, dry_run: str, dry_status: int = 0):
    """check_caddy_updates.sh with stand-ins for apt, systemd and
    unattended-upgrades that print what a machine in that state would."""
    stand_ins = tmp_path / "apt-bin"
    stand_ins.mkdir()
    asked = tmp_path / "asked"
    bodies = {
        "apt-cache": 'if [ "$2" = caddy ]; then printf "caddy:\\n  Installed: 2.11.7\\n  '
                     'Candidate: 2.11.8\\n"; else printf " 500 https://dl.cloudsmith.io/public/'
                     'caddy/stable/deb/debian any-version/main amd64 Packages\\n     release '
                     'o=cloudsmith/caddy/stable,a=any-version,n=any-version,l=source=none,'
                     'c=main,b=amd64\\n"; fi',
        "apt-config": f"cat <<'EOF'\n{origins}\nAPT::Periodic::Unattended-Upgrade \"1\";\nEOF",
        "apt-mark": "exit 0",
        "systemctl": "echo enabled",
        "sudo": 'exec "$@"',
        "unattended-upgrade": f"cat <<'EOF'\n{dry_run}\nEOF\nexit {dry_status}",
    }
    for name, body in bodies.items():
        (stand_ins / name).write_text(f"#!/bin/sh\necho \"{name} $*\" >> '{asked}'\n{body}\n")
        (stand_ins / name).chmod(0o755)
    ran = subprocess.run(["bash", str(CADDY_DIR / "check_caddy_updates.sh")],
                         env={"PATH": f"{stand_ins}:/usr/bin:/bin", "HOME": str(tmp_path)},
                         capture_output=True, text=True)
    return ran, asked.read_text().splitlines()


UBUNTU_ONLY = ('Unattended-Upgrade::Allowed-Origins:: "${distro_id}:${distro_codename}-security";')
POLICIES = {
    "the origin is not mentioned": UBUNTU_ONLY,
    "the origin is named literally":
        UBUNTU_ONLY + '\nUnattended-Upgrade::Origins-Pattern:: "origin=cloudsmith/caddy/stable";',
    "a wildcard that covers every origin":
        'Unattended-Upgrade::Origins-Pattern:: "origin=*";',
    "a wildcard that may or may not cover it":
        'Unattended-Upgrade::Origins-Pattern:: "o=cloudsmith/*,a=stable";',
    "named, and the package blacklisted":
        'Unattended-Upgrade::Origins-Pattern:: "origin=cloudsmith/caddy/stable";\n'
        'Unattended-Upgrade::Package-Blacklist:: "caddy";',
    "nothing configured": "",
}


@pytest.mark.parametrize("policy", POLICIES)
@pytest.mark.parametrize("dry_run", ["Allowed origins are: o=Ubuntu,a=noble-security",
                                     "Checking: caddy ([<Origin component:'main'>])\n"
                                     "Packages that will be upgraded: caddy"])
def test_the_update_checker_never_concludes_for_or_against(tmp_path, policy, dry_run):
    """Not `manual` because the origin's name is absent — a wildcard may cover
    it. Not `automatic` because it is present — a blacklist, a hold or a
    pattern's other fields may exclude it. It shows the evidence and says a
    person must read it."""
    ran, _ = update_checker(tmp_path, origins=POLICIES[policy], dry_run=dry_run)
    assert ran.returncode == 0
    verdict = ran.stdout.split("== verdict\n", 1)[1]
    assert verdict.startswith("UNKNOWN — MANUAL INTERPRETATION REQUIRED\n")
    without = ran.stdout.replace("MANUAL INTERPRETATION REQUIRED", "")
    for claim in ("MANUAL", "AUTOMATIC", "LIKELY", "will update", "will not update", "eligible"):
        assert claim not in without, claim
    # The evidence itself is all there, as the machine gave it.
    assert "Installed: 2.11.7" in ran.stdout and "Candidate: 2.11.8" in ran.stdout
    assert "o=cloudsmith/caddy/stable" in ran.stdout
    for line in POLICIES[policy].splitlines():
        assert line in ran.stdout
    assert dry_run.splitlines()[0] in ran.stdout


def test_a_dry_run_that_fails_is_shown_as_a_failure(tmp_path):
    ran, _ = update_checker(tmp_path, origins=UBUNTU_ONLY, dry_status=1,
                            dry_run="Traceback (most recent call last):\n"
                                    "apt_pkg.Error: E:Could not get lock /var/lib/dpkg/lock")
    assert ran.returncode == 0
    assert "the dry run FAILED (exit 1)" in ran.stdout
    assert "Could not get lock" in ran.stdout and "Traceback" in ran.stdout
    assert "did not mention Caddy" not in ran.stdout
    assert "UNKNOWN — MANUAL INTERPRETATION REQUIRED" in ran.stdout


def test_the_update_checker_only_looks(tmp_path):
    _, asked = update_checker(tmp_path, origins=UBUNTU_ONLY, dry_run="")
    allowed = ("apt-cache policy", "apt-config dump", "apt-mark showhold",
               "systemctl is-enabled", "systemctl is-active",
               "sudo unattended-upgrade --dry-run --debug",
               "unattended-upgrade --dry-run --debug")
    assert asked and all(call.startswith(allowed) for call in asked), asked


# ---- the Caddyfile, running ----------------------------------------------

def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


class Upstream:
    """Stands in for the API: records what reached it and answers anything.

    Speaks just enough HTTP to answer a GET with JSON and to complete a
    websocket handshake. It is not the desk and shares nothing with it.
    """

    def __init__(self):
        self.seen: list[dict] = []
        # How it answers: "ok"; "hangup" (closes without a word, as a dying
        # API would); "garbage" (not HTTP); "stall" (never answers); or a
        # status code, e.g. 403 — what the API sends a socket it refuses.
        self.mode: str | int = "ok"
        self.hold = 0.2             # seconds an accepted websocket stays open
        self.port = 0
        self.listen()

    def listen(self):
        """Start accepting — again on the same port after `close()`, which is
        how a test has the proxy find nothing listening there at all."""
        self.sock = socket.socket()
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", self.port))
        self.sock.listen(16)
        self.port = self.sock.getsockname()[1]
        threading.Thread(target=self._serve, args=(self.sock,), daemon=True).start()

    def _serve(self, sock):
        while True:
            try:
                conn, _ = sock.accept()
            except OSError:
                return
            threading.Thread(target=self._answer, args=(conn,), daemon=True).start()

    def _answer(self, conn):
        with conn:
            conn.settimeout(5)
            data = b""
            try:
                while b"\r\n\r\n" not in data:
                    chunk = conn.recv(4096)
                    if not chunk:
                        return
                    data += chunk
            except OSError:
                return
            head = data.split(b"\r\n\r\n")[0].decode("latin-1").split("\r\n")
            method, target, _ = head[0].split(" ", 2)
            headers = {k.strip().lower(): v.strip()
                       for k, v in (line.split(":", 1) for line in head[1:])}
            self.seen.append({"method": method, "target": target, "headers": headers})
            if self.mode == "hangup":
                return
            if self.mode == "garbage":
                conn.sendall(b"\x00\x01 this is not HTTP \xff\r\n\r\n")
                return
            if self.mode == "stall":
                with contextlib.suppress(OSError):
                    conn.recv(1)            # until the proxy gives up on it
                return
            if self.mode != "ok":
                body = b'{"detail":"refused"}'
                conn.sendall(f"HTTP/1.1 {self.mode} Refused\r\nContent-Type: application/json"
                             f"\r\nContent-Length: {len(body)}\r\nConnection: close\r\n\r\n"
                             .encode() + body)
                return
            if headers.get("upgrade", "").lower() == "websocket":
                accept = base64.b64encode(hashlib.sha1(
                    (headers["sec-websocket-key"]
                     + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()).decode()
                conn.sendall(("HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n"
                              f"Connection: Upgrade\r\nSec-WebSocket-Accept: {accept}\r\n\r\n")
                             .encode())
                conn.settimeout(self.hold)
                with contextlib.suppress(OSError):
                    conn.recv(1)
                return
            body = json.dumps({"upstream": target}).encode()
            conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                         + f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode()
                         + body)

    def targets(self) -> list[str]:
        return [r["target"] for r in self.seen]

    def close(self):
        self.sock.close()


class Site:
    def __init__(self, https_port: int, http_port: int, upstream: Upstream, log: Path):
        self.https_port, self.http_port = https_port, http_port
        self.upstream, self.log = upstream, log
        self.own_log = log.with_name("caddy.log")       # what Caddy itself prints
        self.environment: dict = {}
        self.origin = f"https://localhost:{https_port}"
        self.tls = ssl._create_unverified_context()     # Caddy's own test CA

    def logs(self, since: tuple[int, int] = (0, 0)) -> str:
        """Everything this Caddy has written, in its access log and its own —
        or only what it has added since `mark()` was taken."""
        files = zip((self.log, self.own_log), since, strict=True)
        return "\n".join(f.read_bytes()[start:].decode(errors="replace")
                         for f, start in files if f.exists())

    def mark(self) -> tuple[int, int]:
        return tuple(f.stat().st_size if f.exists() else 0 for f in (self.log, self.own_log))

    def connect(self):
        raw = socket.create_connection(("127.0.0.1", self.https_port), timeout=10)
        return self.tls.wrap_socket(raw, server_hostname="localhost")

    def upgrade(self) -> dict:
        """The signed-in owner's websocket handshake, from this site's page."""
        return {**self.auth(), "Origin": self.origin, "Upgrade": "websocket",
                "Connection": "Upgrade", "Sec-WebSocket-Version": "13",
                "Sec-WebSocket-Key": base64.b64encode(os.urandom(16)).decode()}

    def send(self, target: str, headers: dict):
        """Send a request and hand back the open connection, unread."""
        conn = self.connect()
        lines = [f"GET {target} HTTP/1.1", f"Host: localhost:{self.https_port}"]
        lines += [f"{k}: {v}" for k, v in headers.items()]
        conn.sendall(("\r\n".join(lines) + "\r\n\r\n").encode())
        return conn

    def auth(self, user=USER, password=PASSWORD) -> dict:
        return {"Authorization": "Basic "
                + base64.b64encode(f"{user}:{password}".encode()).decode()}

    def request(self, method: str, target: str, headers: dict | None = None,
                *, plain: bool = False) -> tuple[int, dict, bytes]:
        """One HTTP/1.1 request, read to the end of its headers and body."""
        port = self.http_port if plain else self.https_port
        raw = socket.create_connection(("127.0.0.1", port), timeout=10)
        conn = raw if plain else self.tls.wrap_socket(raw, server_hostname="localhost")
        with conn:
            lines = [f"{method} {target} HTTP/1.1", f"Host: localhost:{port}",
                     "Connection: close"]
            lines += [f"{k}: {v}" for k, v in (headers or {}).items()]
            conn.sendall(("\r\n".join(lines) + "\r\n\r\n").encode())
            data = b""
            try:
                while True:
                    chunk = conn.recv(65536)
                    if not chunk:
                        break
                    data += chunk
                    if data.startswith(b"HTTP/1.1 101") and b"\r\n\r\n" in data:
                        break
            except (TimeoutError, ssl.SSLError, OSError):
                pass
        head, _, body = data.partition(b"\r\n\r\n")
        first, *rest = head.decode("latin-1").split("\r\n")
        return (int(first.split()[1]),
                {k.strip().lower(): v.strip() for k, v in (line.split(":", 1) for line in rest)},
                body)

    def get(self, target: str, headers: dict | None = None):
        return self.request("GET", target, {**self.auth(), **(headers or {})})

    def handshake(self, headers: dict):
        return self.request("GET", f"/api{STREAM}", {
            "Upgrade": "websocket", "Connection": "Upgrade",
            "Sec-WebSocket-Version": "13",
            "Sec-WebSocket-Key": base64.b64encode(os.urandom(16)).decode(), **headers})


@functools.cache
def owner_hash() -> str:
    """The documented command, with the password on stdin as a prompt gives it."""
    hashed = subprocess.run([CADDY, "hash-password", "--algorithm", "bcrypt",
                             "--bcrypt-cost", "14"], input=PASSWORD + "\n",
                            capture_output=True, text=True, check=True).stdout.strip()
    assert re.match(r"\$2[aby]\$14\$", hashed), "not a bcrypt hash of cost 14"
    return hashed


@contextlib.contextmanager
def running(work: Path, caddyfile: Path = CADDYFILE, certificates: str | None = None):
    """A Caddy of its own on loopback ports, in front of a throwaway upstream,
    with made-up credentials. `caddyfile` is the repository's unless a test
    needs to show what a changed one would do."""
    if not CADDY:
        pytest.fail("QUANTDESK_REQUIRE_CADDY=1 and there is no caddy binary: "
                    "the proxy's security tests cannot be skipped here")
    web = work / "web"
    (web / "assets").mkdir(parents=True)
    (web / "index.html").write_text("<!doctype html><title>the dashboard</title>")
    (web / "assets" / "app.js").write_text("console.log('compiled')")
    upstream = Upstream()
    https_port, http_port, log = free_port(), free_port(), work / "access.log"
    env = {**os.environ, "HOME": str(work), "XDG_DATA_HOME": str(work / "data"),
           "XDG_CONFIG_HOME": str(work / "config"),
           "CADDY_ADMIN": f"127.0.0.1:{free_port()}",
           "QUANTDESK_HOSTNAME": f"localhost:{https_port}",
           "QUANTDESK_HTTPS_PORT": str(https_port), "QUANTDESK_HTTP_PORT": str(http_port),
           "QUANTDESK_BASIC_USER": USER, "QUANTDESK_BASIC_HASH": owner_hash(),
           "QUANTDESK_API_KEY": KEY, "QUANTDESK_UPSTREAM": f"127.0.0.1:{upstream.port}",
           "QUANTDESK_WEB_ROOT": str(web), "QUANTDESK_ACCESS_LOG": str(log),
           # Certificate storage, which on the VM is under /var/lib/caddy.
           "QUANTDESK_STORAGE_ROOT": str(work / "storage")}
    if certificates:
        env["QUANTDESK_CERTIFICATES"] = certificates
    site = Site(https_port, http_port, upstream, log)
    site.environment = env
    process = subprocess.Popen([CADDY, "run", "--adapter", "caddyfile", "--config",
                                str(caddyfile)], env=env, cwd=work,
                               stdout=site.own_log.open("wb"), stderr=subprocess.STDOUT)
    try:
        deadline = time.monotonic() + 30
        while True:
            try:
                if site.request("GET", "/")[0] == 401:
                    break
            except (OSError, ssl.SSLError, IndexError, ValueError):
                pass
            if process.poll() is not None or time.monotonic() > deadline:
                process.kill()
                pytest.fail("caddy did not come up:\n" + site.own_log.read_text()[-3000:])
            time.sleep(0.2)
        yield site
        # Stopped with the owner's stream still open, so that whatever Caddy
        # says about a connection it has to drop is in the log before the
        # caller reads it.
        upstream.mode, upstream.hold = "ok", 30
        open_stream = site.send(f"/api{STREAM}", site.upgrade())
        assert open_stream.recv(4096).startswith(b"HTTP/1.1 101")
    finally:
        process.terminate()
        try:
            process.wait(10)
        except subprocess.TimeoutExpired:
            process.kill()
        upstream.close()


@pytest.fixture(scope="module")
def site(tmp_path_factory):
    """The repository's Caddyfile, exactly as it would be installed — and so
    at Caddy's normal log level, with no `debug`."""
    with running(tmp_path_factory.mktemp("caddy")) as site:
        yield site
    # Everything it wrote, start to stop, across every test above.
    written = site.logs()
    assert "/api/market/price" in written and '"level":"debug"' not in written
    assert KEY not in written, "the API key is in a log written at the normal level"
    assert PASSWORD not in written
    assert site.auth()["Authorization"].split()[1] not in written


@needs_caddy
def test_the_binary_under_test_is_the_release_the_deployment_names():
    """Otherwise everything below describes some other Caddy."""
    if not CADDY:
        pytest.fail("QUANTDESK_REQUIRE_CADDY=1 and there is no caddy binary")
    found = subprocess.run([CADDY, "version"], capture_output=True, text=True,
                           check=True).stdout.split()[0]
    assert found == f"v{CADDY_VERSION}"
    # The stock build: every module it lists is one Caddy ships itself.
    modules = subprocess.run([CADDY, "list-modules", "--versions"], capture_output=True,
                             text=True, check=True).stdout
    assert "Standard modules:" in modules and "Non-standard modules" not in modules


@needs_caddy
def test_nothing_is_served_without_the_owners_sign_in(site):
    for target in ("/", "/index.html", "/assets/app.js", "/api/market/price",
                   "/api/journal", f"/api{STREAM}"):
        status, headers, body = site.request("GET", target)
        assert status == 401, target
        assert headers["www-authenticate"].lower().startswith("basic")
        assert b"dashboard" not in body
    assert site.request("GET", "/", site.auth(password="wrong"))[0] == 401
    assert site.request("GET", "/", site.auth(user="someone"))[0] == 401
    assert site.upstream.seen == []


@needs_caddy
def test_plain_http_is_sent_to_https(site):
    status, headers, _ = site.request("GET", "/", plain=True)
    assert status in (301, 308)
    assert headers["location"].startswith("https://localhost")


@needs_caddy
def test_the_signed_in_owner_gets_the_dashboard(site):
    status, headers, body = site.get("/")
    assert status == 200 and b"the dashboard" in body
    assert headers["cache-control"] == "no-cache"
    assert "strict-transport-security" in headers
    assert site.get("/assets/app.js")[2] == b"console.log('compiled')"
    # One page: an unknown path is the dashboard, not a 404 and not the API.
    assert b"the dashboard" in site.get("/anything/else")[2]


@needs_caddy
def test_dashboard_reads_reach_the_api_without_the_prefix_or_any_credential(site):
    site.upstream.seen.clear()
    for path in sorted(proxied_paths()):
        status, _, body = site.get(f"/api{path}")
        assert status == 200, path
        assert json.loads(body)["upstream"] == path
    status, _, body = site.get("/api/market/candles?symbol=NIFTY&interval=5m&days=2",
                               {"X-API-Key": "a key the caller brought"})
    assert json.loads(body)["upstream"] == "/market/candles?symbol=NIFTY&interval=5m&days=2"
    for seen in site.upstream.seen:
        assert seen["method"] == "GET"
        assert "authorization" not in seen["headers"], "the owner's sign-in was forwarded"
        assert "x-api-key" not in seen["headers"], "a caller's key was forwarded"
        assert KEY not in seen["target"]


@needs_caddy
@pytest.mark.parametrize("method, target", [
    ("POST", "/api/journal"), ("GET", "/api/journal"), ("GET", "/api/journal/stats"),
    ("POST", "/api/journal/1/close"), ("POST", "/api/v2/close"),
    ("POST", "/api/backtest/run"), ("POST", "/api/backtest/options"),
    ("POST", "/api/backtest/diagnose"), ("POST", "/api/backtest/stop-sweep"),
    ("POST", "/api/backtest/option-buying"), ("GET", "/api/backtest/option-buying/coverage"),
    ("POST", "/api/data/import/index"), ("POST", "/api/data/import/options"),
    ("POST", "/api/data/regimes/backfill"), ("POST", "/api/market/archive/backfill"),
    ("GET", "/api/data/readiness"), ("GET", "/api/data/methodology"),
    ("GET", "/api/data/datasets"), ("GET", "/api/data/regimes"),
    ("GET", "/api/market/structure"), ("GET", "/api/market/archive"),
    ("GET", "/api/signals/outcomes/by-regime"), ("GET", "/api/signals/outcomes/two-layer"),
    ("GET", "/api/v2/positions"), ("GET", "/api/v2/decisions"),
    ("GET", "/api/v2/decisions/1/evidence"), ("GET", "/api/v2/vix"),
    ("GET", "/api/health"), ("GET", "/api/health/broker"),
    ("GET", "/api/docs"), ("GET", "/api/redoc"), ("GET", "/api/openapi.json"), ("GET", "/api/"),
    # An allowed path with another verb, or with something after it.
    ("POST", "/api/market/price"), ("DELETE", "/api/signals/history"),
    ("GET", "/api/market/price/anything"), ("GET", "/api/market/price/../../journal"),
])
def test_everything_else_under_the_api_does_not_exist_here(site, method, target):
    site.upstream.seen.clear()
    status, _, _ = site.request(method, target, site.auth())
    assert status == 404
    assert site.upstream.seen == [], f"{method} {target} reached the API"


# The same request spelled every way a URL allows: percent-encoded letters and
# slashes, encoded and doubled dot segments, doubled slashes, other case,
# trailing junk. Caddy matches a decoded, cleaned path and the API routes on
# its own reading of what is forwarded; a difference between the two readings
# is how an allow-list is walked around.
SPELLINGS = [
    "/api/%6aournal", "/api/%6Aournal", "/%61pi/journal", "/api%2fjournal", "/api%2Fjournal",
    "/api//journal", "//api/journal", "/api/./journal", "/x/../api/journal",
    "/api/market/price/../../journal", "/api/market/price/..%2fjournal",
    "/api/market/price%2f..%2f..%2fjournal", "/api/market/price/%2e%2e/%2e%2e/journal",
    "/api/market/price/%2E%2E/journal", "/api/market/price%252f..%252fjournal",
    "/api/market/price\\..\\journal", "/api/market/price/..;/journal",
    "/api/market/price;/../journal", "/api/%2e%2e/journal", "/api/..%2fjournal",
    "/api/market/price%00", "/api/market/price%00/../journal", "/api/market/price%20",
    "/api/market/price%0a", "/api/market/price%3Fx", "/api/market/price%23", "/api/market/price/",
    "/api/v2/status/../close", "/api/v2/statu%73/../positions", "/api/v2/%2e%2e/journal",
    "/API/journal", "/api/JOURNAL", "/api/Journal/Stats", "/api/v2/Positions",
    "/api/docs", "/api/%64ocs", "/api/openapi%2ejson",
    # Spellings of an allowed read. These may be forwarded.
    "/api/market/pric%65", "/api%2fmarket%2fprice", "/api//market/price", "/api/market//price",
    "/api/market/price/.", "/api/journal/../market/price", "/API/MARKET/PRICE",
    "/api/Market/Price", "/api/v2/positions/../status",
]


@needs_caddy
@pytest.mark.parametrize("method", ["GET", "POST"])
@pytest.mark.parametrize("target", SPELLINGS)
def test_no_spelling_of_a_path_reaches_a_route_outside_the_allow_list(site, method, target):
    """Whatever the answer, what the API is sent — read the most generous way,
    decoded and without regard to case — is one of the sixteen reads, by GET."""
    site.upstream.seen.clear()
    status, _, _ = site.request(method, target, site.auth())
    for seen in site.upstream.seen:
        path = unquote(urlsplit(seen["target"]).path)
        assert seen["method"] == "GET", f"{method} {target} was forwarded as {seen['method']}"
        assert path.lower() in proxied_paths(), f"{target} reached the API as {seen['target']}"
        assert ".." not in path and "//" not in path and "\\" not in path
    if method != "GET":
        # 404 from the API block; 405 where the path cleans to one outside
        # /api and lands on the dashboard's files, which are read-only.
        assert site.upstream.seen == [] and status in (404, 405)


@needs_caddy
@pytest.mark.parametrize("target", [t for t in SPELLINGS if "journal" in t.lower()
                                    and "market/price" not in t.split("journal")[-1]])
def test_no_spelling_of_the_journal_is_forwarded(site, target):
    if target == "/api/journal/../market/price":
        pytest.skip("cleans to an allowed read")
    site.upstream.seen.clear()
    site.get(target)
    assert site.upstream.seen == []


@needs_caddy
def test_the_stream_is_not_reachable_by_another_spelling_without_its_checks(site):
    """Any spelling Caddy takes for the stream goes through the stream's own
    origin check, and is sent upstream as the one fixed path."""
    for target in ("/api/WS/signals", "/api/ws/signal%73", "/api//ws/signals",
                   "/api/ws/signals/", "/api/ws/./signals", "/api%2fws%2fsignals"):
        site.upstream.seen.clear()
        upgrade = {"Upgrade": "websocket", "Connection": "Upgrade",
                   "Sec-WebSocket-Version": "13",
                   "Sec-WebSocket-Key": base64.b64encode(os.urandom(16)).decode()}
        assert site.request("GET", target, upgrade)[0] == 401, target
        foreign = site.request("GET", target, {**site.auth(), **upgrade,
                                               "Origin": "https://evil.example"})[0]
        assert foreign in (403, 404), target
        assert site.upstream.seen == [], target
        site.request("GET", target, {**site.auth(), **upgrade, "Origin": site.origin})
        for seen in site.upstream.seen:
            assert seen["target"] == f"{STREAM}?key={KEY}", target


@needs_caddy
@pytest.mark.parametrize("query", [
    "persist=true", "persist=1", "persist=false", "persist", "persist=",
    "symbol=NIFTY&persist=1", "symbol=NIFTY&timeframe=5m&persist=true",
    "persist=true&symbol=NIFTY", "x=1&&persist=true", "&persist=true", "persist=true&persist=false",
    # Encoded, so that the name only appears once the query is decoded.
    "%70ersist=true", "%50ERSIST=true", "p%65rsist=true", "persis%74=1", "%70%65rsist=true",
    # Queries Caddy cannot parse, which the API then parses its own way.
    "%zz=1&persist=true", "persist=true&%zz", "a=%&persist=true", "%&persist=true",
    "%zz&%70ersist=true", "a=1;x&%70ersist=true", "a=1;persist=true", "a=1;x&persist=true",
    # Separators, doubled marks, other shapes and case.
    "?persist=true", "x=1%26persist=true", "persist%3Dtrue", "persist[]=true", "persist+=true",
    "+persist=true", "%20persist=true", "PERSIST=true", "Persist=true", "x=1%0apersist=true",
    "x=1#&persist=true",
])
def test_a_read_cannot_be_turned_into_a_stored_row(site, query):
    """`/signals/live?persist=true` stores a row. No spelling of that query is
    forwarded — nor, on this one route, any query the proxy and the API could
    read differently."""
    for path in ("/api/signals/live", "/api/signals/liv%65", "/api//signals/live",
                 "/API/signals/live", "/api/signals/live/.", "/api/x/../signals/live"):
        site.upstream.seen.clear()
        status, _, _ = site.get(f"{path}?{query}")
        assert status in (400, 403, 404), f"{path}?{query} answered {status}"
        assert site.upstream.seen == [], f"{path}?{query} reached the API"


@needs_caddy
def test_no_other_route_forwards_a_persist_parameter(site):
    for path in sorted(proxied_paths() - {"/signals/live"}):
        site.upstream.seen.clear()
        assert site.get(f"/api{path}?persist=true")[0] == 403, path
        assert site.upstream.seen == [], path


@needs_caddy
def test_the_dashboards_own_live_signal_request_still_goes_through(site):
    source = (FRONTEND_SRC / "App.jsx").read_text()
    (asked,) = set(re.findall(r"""getJSON\(\s*["'`](/signals/live[^"'`]*)""", source))
    site.upstream.seen.clear()
    assert site.get(f"/api{asked}")[0] == 200
    assert site.upstream.targets() == [asked]
    assert site.get("/api/signals/live")[0] == 200


@needs_caddy
def test_the_stream_needs_the_sign_in_and_this_sites_origin(site):
    site.upstream.seen.clear()
    assert site.handshake({"Origin": site.origin})[0] == 401
    assert site.handshake({**site.auth(), "Origin": "https://evil.example"})[0] == 403
    assert site.handshake({**site.auth(), "Origin": site.origin.replace("https", "http")})[0] == 403
    assert site.handshake(site.auth())[0] == 403                 # names no origin
    assert site.upstream.seen == []


@needs_caddy
def test_the_owners_stream_is_upgraded_and_the_proxy_adds_the_key(site):
    site.upstream.seen.clear()
    # A key in the browser's URL is replaced, not passed along.
    status, headers, _ = site.request("GET", f"/api{STREAM}?key=from-the-browser", {
        **site.auth(), "Origin": site.origin, "Upgrade": "websocket",
        "Connection": "Upgrade", "Sec-WebSocket-Version": "13",
        "Sec-WebSocket-Key": base64.b64encode(os.urandom(16)).decode()})
    assert status == 101 and headers["upgrade"].lower() == "websocket"
    (seen,) = site.upstream.seen
    assert seen["target"] == f"{STREAM}?key={KEY}"
    assert "authorization" not in seen["headers"]


@needs_caddy
def test_neither_the_password_nor_the_key_is_written_to_the_access_log(site):
    site.get("/api/market/price")
    site.handshake({**site.auth(), "Origin": site.origin})
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and not (site.log.exists() and site.log.stat().st_size):
        time.sleep(0.1)
    written = site.log.read_text()
    assert f"/api{STREAM}" in written and "/api/market/price" in written
    assert KEY not in written
    assert PASSWORD not in written
    assert site.auth()["Authorization"].split()[1] not in written


# Every way the API can fail the proxy. The key is in the address of the
# request the proxy could not complete — exactly what an error message tends
# to quote — so each is made to happen, for the stream and for a read, and
# then everything Caddy wrote and everything it sent back is searched.
FAILURES = ["hangup", "garbage", "stall", "nothing listening", 400, 401, 403, 404, 500, 503]


@needs_caddy
@pytest.mark.parametrize("failure", FAILURES)
def test_no_failure_of_the_api_puts_the_key_in_a_log_or_a_response(site, failure):
    upstream, before = site.upstream, site.mark()
    answers = []
    try:
        if failure == "nothing listening":
            upstream.close()
        else:
            upstream.mode = failure
        upstream.seen.clear()
        if failure == "stall":
            # The browser gives up first; the proxy is left holding a request
            # it then has to abandon and account for.
            for target, headers in ((f"/api{STREAM}", site.upgrade()),
                                    ("/api/market/price", site.auth())):
                waiting = site.send(target, headers)
                time.sleep(1)
                waiting.close()
        else:
            answers.append(site.request("GET", f"/api{STREAM}?key=from-the-browser",
                                        site.upgrade()))
            answers.append(site.get("/api/market/price"))
            answers.append(site.get("/api/signals/live?symbol=NIFTY&timeframe=5m"))
        if failure != "nothing listening":
            # The failure was of the real thing: the request that carries the key.
            assert f"{STREAM}?key={KEY}" in upstream.targets()
    finally:
        if failure == "nothing listening":
            upstream.listen()
        upstream.mode = "ok"
    time.sleep(0.5)

    for status, headers, body in answers:
        expected = failure if isinstance(failure, int) else 502
        assert status == expected, f"{failure}: answered {status}"
        assert KEY not in repr(headers) and KEY.encode() not in body
    written = site.logs(before)
    assert f"/api{STREAM}" in written, "the failure left nothing in the log to search"
    if not isinstance(failure, int) and failure != "stall":
        assert '"level":"error"' in written, f"{failure}: Caddy logged no error to search"
    assert KEY not in written, f"{failure}: the API key is in a log at the normal level"
    assert PASSWORD not in written
    assert site.auth()["Authorization"].split()[1] not in written


@needs_caddy
def test_what_the_proxy_refuses_itself_is_logged_without_the_key(site):
    """401, 403, 404 and 400 that never reach the API, each with a key the
    caller put in the address themselves: that one is theirs and is logged as
    sent; the real one has no reason to be there."""
    before = site.mark()
    assert site.request("GET", f"/api{STREAM}", {k: v for k, v in site.upgrade().items()
                                                 if k != "Authorization"})[0] == 401
    assert site.request("GET", f"/api{STREAM}", {**site.upgrade(),
                                                 "Origin": "https://evil.example"})[0] == 403
    assert site.get("/api/signals/live?persist=true")[0] == 403
    assert site.get("/api/journal")[0] == 404
    assert site.request("POST", "/api/v2/close", site.auth())[0] == 404
    assert site.get("/api/signals/live? persist=true")[0] == 400
    time.sleep(0.5)
    written = site.logs(before)
    assert "/api/journal" in written and KEY not in written and PASSWORD not in written


@needs_caddy
def test_a_stream_that_opens_and_closes_is_logged_without_the_key(site):
    before = site.mark()
    site.upstream.seen.clear()
    stream = site.send(f"/api{STREAM}", site.upgrade())
    assert stream.recv(4096).startswith(b"HTTP/1.1 101")
    stream.close()                                  # the browser tab closes
    assert site.handshake({**site.auth(), "Origin": site.origin})[0] == 101
    time.sleep(1)                                   # the API closes this one
    assert site.upstream.targets() == [f"{STREAM}?key={KEY}"] * 2
    written = site.logs(before)
    assert f"/api{STREAM}" in written and KEY not in written


@needs_caddy
def test_debug_logging_does_write_the_key_which_is_why_it_is_forbidden(tmp_path):
    """The hazard itself, kept as evidence. The same Caddyfile with `debug`
    switched on logs the address of each request it forwards, and the
    stream's carries the key — here a made-up one, on a proxy of its own,
    which is the only place debug logging may ever be used.

    If a later Caddy stops doing this the test fails, and the rule in
    README.md can be looked at again. Until then it stands."""
    changed = tmp_path / "Caddyfile"
    text = CADDYFILE.read_text()
    assert text.count("\tskip_install_trust\n") == 1
    changed.write_text(text.replace("\tskip_install_trust\n", "\tskip_install_trust\n\tdebug\n"))
    work = tmp_path / "work"
    work.mkdir()
    with running(work, changed) as debugging:
        assert debugging.handshake({**debugging.auth(), "Origin": debugging.origin})[0] == 101
        time.sleep(0.5)
    own = debugging.own_log.read_text()
    assert '"level":"debug"' in own
    assert f"key={KEY}" in own, "debug logging no longer shows the key — see the docstring"
    # Redacting the sign-in header says nothing about a key in an address.
    assert PASSWORD not in own and KEY not in debugging.log.read_text()


@needs_caddy
def test_the_key_is_not_in_the_configuration_caddy_holds(site):
    """Neither as adapted from the Caddyfile nor as the running server
    reports it on its local admin address."""
    adapted = subprocess.run([CADDY, "adapt", "--adapter", "caddyfile", "--config",
                              str(CADDYFILE)], env=site.environment, capture_output=True,
                             text=True, check=True)
    admin = socket.create_connection(tuple(site.environment["CADDY_ADMIN"].rsplit(":", 1)),
                                     timeout=10)
    with admin:
        admin.sendall(b"GET /config/ HTTP/1.1\r\nHost: " + site.environment["CADDY_ADMIN"].encode()
                      + b"\r\nConnection: close\r\n\r\n")
        held = b""
        while chunk := admin.recv(65536):
            held += chunk
    assert b"200 OK" in held and b"{env.QUANTDESK_API_KEY}" in held
    for text in (adapted.stdout, adapted.stderr, held.decode()):
        assert KEY not in text and PASSWORD not in text


# ---- which certificate authority Caddy would really use, and its storage -----

LE_PRODUCTION = "https://acme-v02.api.letsencrypt.org/directory"
LE_STAGING = "https://acme-staging-v02.api.letsencrypt.org/directory"
STORAGE = "/var/lib/caddy/quantdesk"


def adapted(tmp_path: Path, mode: str | None, hostname: str = "desk.example.com",
            caddyfile: Path = CADDYFILE):
    """The configuration Caddy builds from the Caddyfile and the settings: the
    issuers and the storage in it, its whole text, and what
    certificate_authority.sh says. `mode` is QUANTDESK_CERTIFICATES."""
    env = {"PATH": f"{Path(CADDY).parent}:/usr/bin:/bin", "HOME": str(tmp_path),
           "QUANTDESK_HOSTNAME": hostname, "QUANTDESK_BASIC_USER": USER,
           "QUANTDESK_BASIC_HASH": owner_hash(), "QUANTDESK_API_KEY": KEY}
    if mode is not None:
        env["QUANTDESK_CERTIFICATES"] = mode
    ran = subprocess.run([CADDY, "adapt", "--adapter", "caddyfile", "--config", str(caddyfile)],
                         env=env, capture_output=True, text=True)
    issuers = storage = None
    if ran.returncode == 0:
        config = json.loads(ran.stdout)
        policies = (config["apps"].get("tls", {}).get("automation", {}).get("policies", []))
        issuers = [[(i.get("module"), i.get("ca")) for i in p.get("issuers", [])]
                   for p in policies]
        storage = config.get("storage")

    def check(*arguments):
        # The script finds `caddy` on PATH; the binary under test may have any name.
        named = tmp_path / "caddy-bin"
        named.mkdir(exist_ok=True)
        if not (named / "caddy").exists():
            (named / "caddy").symlink_to(Path(CADDY).resolve())
        return subprocess.run(
            ["bash", str(CADDY_DIR / "certificate_authority.sh"), *arguments, str(caddyfile)],
            env={**env, "PATH": f"{named}:/usr/bin:/bin"}, capture_output=True, text=True)
    return issuers, storage, ran.stdout, check


@needs_caddy
def test_production_is_lets_encrypt_production_in_its_own_storage(tmp_path):
    for mode in (None, "production"):
        issuers, storage, text, check = adapted(tmp_path, mode)
        assert issuers == [[("acme", LE_PRODUCTION)]]
        assert storage == {"module": "file_system", "root": f"{STORAGE}/production"}
        assert "staging" not in text and "zerossl" not in text.lower() and "internal" not in text
        assert check().returncode == 0 and check("--require", "production").returncode == 0
        said = check().stdout
        assert "Let's Encrypt production only" in said and f"{STORAGE}/production" in said
        assert check("--require", "staging").returncode != 0


@needs_caddy
def test_staging_is_lets_encrypt_staging_in_its_own_storage_with_no_production(tmp_path):
    issuers, storage, text, check = adapted(tmp_path, "staging")
    assert issuers == [[("acme", LE_STAGING)]]
    assert storage == {"module": "file_system", "root": f"{STORAGE}/staging-trial"}
    # Neither production's directory of Let's Encrypt nor production's storage
    # is anywhere in the configuration Caddy would run.
    assert LE_PRODUCTION not in text and f"{STORAGE}/production" not in text
    assert "zerossl" not in text.lower()
    assert check("--require", "staging").returncode == 0
    said = check().stdout
    assert "STAGING only" in said and f"{STORAGE}/staging-trial" in said
    # A staging configuration can never pass as the go-live one.
    assert check("--require", "production").returncode != 0


@needs_caddy
@pytest.mark.parametrize("mode", ["zerossl", "Production", "STAGING", "staging-trial", "prod",
                                  "../production", "https://acme.zerossl.com/v2/DV90"])
def test_there_is_no_third_choice_of_authority(tmp_path, mode):
    """Any other value names a pair that does not exist: Caddy will not build
    a configuration from it at all, so it cannot start on one."""
    issuers, storage, _, check = adapted(tmp_path, mode)
    assert issuers is None and storage is None, "Caddy adapted a configuration for it"
    assert check().returncode != 0


@needs_caddy
@pytest.mark.parametrize("old, new", [
    # Another CA in production's place, and three near-misses of the real one.
    ("acme_ca https://acme-v02.api.letsencrypt.org/directory",
     "acme_ca https://acme.zerossl.com/v2/DV90"),
    ("acme_ca https://acme-v02.api.letsencrypt.org/directory",
     "acme_ca https://dv.acme-v02.api.pki.goog/directory"),
    ("acme_ca https://acme-v02.api.letsencrypt.org/directory",
     "acme_ca https://acme-v02.api.letsencrypt.org/directory/"),
    ("acme_ca https://acme-v02.api.letsencrypt.org/directory",
     "acme_ca http://acme-v02.api.letsencrypt.org/directory"),
    # No CA named: Caddy's own default pair, Let's Encrypt and then ZeroSSL.
    ("\tacme_ca https://acme-v02.api.letsencrypt.org/directory\n", ""),
    # Production's CA kept where the trial keeps its own; kept in Caddy's
    # default storage; kept somewhere else altogether.
    ("quantdesk}/production", "quantdesk}/staging-trial"),
    ("\tstorage file_system {$QUANTDESK_STORAGE_ROOT:/var/lib/caddy/quantdesk}/production\n", ""),
    ("quantdesk}/production", "quantdesk}/production/../staging-trial"),
    ("quantdesk}/production", "quantdesk}/../.local/share/caddy"),
])
def test_the_check_refuses_any_other_pairing_of_authority_and_storage(tmp_path, old, new):
    changed = tmp_path / "Caddyfile"
    text = CADDYFILE.read_text()
    assert text.count(old) == 1, old
    changed.write_text(text.replace(old, new))
    issuers, _, _, check = adapted(tmp_path, None, caddyfile=changed)
    assert issuers is not None, "the changed file should still adapt: the check must catch it"
    refused = check()
    assert refused.returncode != 0 and "REFUSED" in refused.stderr


@needs_caddy
def test_the_trial_cannot_be_given_productions_storage_or_the_reverse(tmp_path):
    """The staging pair with its directory swapped for production's is
    refused in staging mode, so a trial certificate has no way into the
    directory production reads."""
    changed = tmp_path / "Caddyfile"
    changed.write_text(CADDYFILE.read_text().replace("quantdesk}/staging-trial",
                                                     "quantdesk}/production"))
    issuers, storage, _, check = adapted(tmp_path, "staging", caddyfile=changed)
    assert issuers == [[("acme", LE_STAGING)]] and storage["root"] == f"{STORAGE}/production"
    assert check().returncode != 0 and check("--require", "staging").returncode != 0


@needs_caddy
def test_the_check_refuses_what_the_caddyfile_text_alone_would_not_show(tmp_path):
    """Caddy's internal CA for a name that is not public, though the Caddyfile
    still says Let's Encrypt."""
    for hostname in ("localhost", "10.0.0.4"):
        issuers, _, _, check = adapted(tmp_path, None, hostname)
        assert [] in issuers, "a policy that names no issuer"
        assert check().returncode != 0
    # From a settings file, read as systemd reads it, hash and all.
    settings = tmp_path / "caddy.env"
    settings.write_text(f"# settings\nQUANTDESK_HOSTNAME=desk.example.com\n"
                        f"QUANTDESK_BASIC_USER={USER}\nQUANTDESK_BASIC_HASH={owner_hash()}\n"
                        f"QUANTDESK_CERTIFICATES=staging\n")
    from_file = adapted(tmp_path, None)[3]("--env-file", str(settings))
    assert from_file.returncode == 0 and "STAGING only" in from_file.stdout
    assert owner_hash() not in from_file.stdout + from_file.stderr


@needs_caddy
def test_a_running_caddy_keeps_its_state_only_in_the_directory_of_its_mode(site, tmp_path):
    """Not the adapted configuration this time but a Caddy that has run: the
    module's site, in production mode, has written under `production` and
    nowhere else; one started in staging mode writes under `staging-trial`
    and leaves `production` alone. (On loopback the certificates are Caddy's
    own test ones — no CA is contacted — but they are kept by the same rule.)"""
    storage = Path(site.environment["QUANTDESK_STORAGE_ROOT"])
    assert [p.name for p in storage.iterdir()] == ["production"]
    assert any(p.is_file() for p in (storage / "production").rglob("*"))

    work = tmp_path / "trial"
    work.mkdir()
    with running(work, certificates="staging") as trial:
        assert trial.get("/")[0] == 200
    kept = Path(trial.environment["QUANTDESK_STORAGE_ROOT"])
    assert [p.name for p in kept.iterdir()] == ["staging-trial"]
    assert any(p.is_file() for p in (kept / "staging-trial").rglob("*"))
