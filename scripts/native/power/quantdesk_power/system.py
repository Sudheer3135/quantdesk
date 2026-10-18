"""Bounded commands and systemd control.

Every command has a timeout. On expiry the whole process group is killed and
`CommandTimeout` is raised; nothing waits unbounded. Commands are argument
lists, never shell strings. Switching to another user happens only when the
controller runs as root, and drops every supplementary group.
"""
from __future__ import annotations

import os
import pwd
import signal
import subprocess
from dataclasses import dataclass

OUTPUT_LIMIT = 4 * 1024 * 1024


class CommandFailed(Exception):
    pass


class CommandTimeout(CommandFailed):
    pass


@dataclass
class Result:
    argv: list[str]
    returncode: int
    stdout: str
    stderr: str


def _demote(user: str | None):
    if user is None:
        return None
    entry = pwd.getpwnam(user)
    if os.geteuid() != 0:
        if os.geteuid() == entry.pw_uid:
            return None
        raise CommandFailed(f"cannot run as {user}: the controller is not root")

    def demote():
        os.setgroups([])
        os.setgid(entry.pw_gid)
        os.setuid(entry.pw_uid)
    return demote


class Runner:
    """Runs a program with a timeout, optionally as another user.

    `allowed` is the complete set of programs this runner may start; the
    controller builds it from its configuration.
    """

    def __init__(self, allowed: frozenset[str] | None = None):
        self.allowed = allowed

    def run(self, argv: list[str], timeout: float, *, user: str | None = None,
            env: dict | None = None, pass_fds: tuple[int, ...] = (),
            input_text: str | None = None) -> Result:
        if not argv or not os.path.isabs(argv[0]):
            raise CommandFailed("commands must name an absolute program path")
        if self.allowed is not None and argv[0] not in self.allowed:
            raise CommandFailed(f"{argv[0]} is not an allowed program")
        proc = subprocess.Popen(  # noqa: S603 - argument list, no shell
            argv, stdin=subprocess.PIPE if input_text is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, pass_fds=pass_fds,
            start_new_session=True, preexec_fn=_demote(user), close_fds=True, text=True)
        try:
            out, err = proc.communicate(input=input_text, timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            proc.communicate()
            raise CommandTimeout(f"{argv[0]} did not finish within {timeout:.0f} s") from exc
        if len(out) > OUTPUT_LIMIT or len(err) > OUTPUT_LIMIT:
            raise CommandFailed(f"{argv[0]} produced more output than allowed")
        return Result(list(argv), proc.returncode, out, err)


SHOW_PROPERTIES = ("ActiveState", "SubState", "LoadState", "UnitFileState", "Result",
                   "ExecMainStatus", "ExecMainCode", "MainPID",
                   "ExecMainStartTimestampMonotonic")


class Systemd:
    """The few systemctl operations the controller performs."""

    def __init__(self, runner: Runner, systemctl: str = "/usr/bin/systemctl",
                 show_timeout: float = 15.0):
        self.runner, self.systemctl, self.show_timeout = runner, systemctl, show_timeout

    def show(self, unit: str, timeout: float | None = None) -> dict:
        """The unit's properties. `timeout`, when given, can only shorten the wait: the
        call waits at most min(show_timeout, timeout) seconds, and a non-positive budget
        is refused without running anything."""
        argv = [self.systemctl, "show", unit, "--no-pager"]
        for prop in SHOW_PROPERTIES:
            argv += ["-p", prop]
        wait = self.show_timeout
        if timeout is not None:
            if timeout <= 0:
                raise CommandTimeout(f"no time left to read {unit}")
            wait = min(wait, timeout)
        result = self.runner.run(argv, wait)
        if result.returncode != 0:
            raise CommandFailed(f"systemctl show {unit} failed: {result.stderr.strip()[:200]}")
        props = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
        missing = [p for p in ("ActiveState", "LoadState") if p not in props]
        if missing:
            raise CommandFailed(f"systemctl show {unit} lacks {', '.join(missing)}")
        if props["LoadState"] != "loaded":
            raise CommandFailed(f"{unit} is not loaded ({props['LoadState']})")
        return props

    def stop(self, unit: str, timeout: float) -> None:
        result = self.runner.run([self.systemctl, "stop", unit], timeout)
        if result.returncode != 0:
            raise CommandFailed(f"systemctl stop {unit} failed: {result.stderr.strip()[:200]}")

    def start(self, unit: str, timeout: float) -> None:
        result = self.runner.run([self.systemctl, "start", unit], timeout)
        if result.returncode != 0:
            raise CommandFailed(f"systemctl start {unit} failed: {result.stderr.strip()[:200]}")


def active(props: dict) -> bool:
    return props.get("ActiveState") in ("active", "activating", "reloading")


def cleanly_stopped(props: dict) -> bool:
    """Stopped by request with a clean exit: inactive, Result=success, status 0."""
    return (props.get("ActiveState") == "inactive" and props.get("Result") == "success"
            and props.get("ExecMainStatus", "0") in ("0", ""))
