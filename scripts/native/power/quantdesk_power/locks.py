"""The two locks a shutdown holds.

COORDINATION LOCK — one controller at a time.
    `coordination.lock` in the controller's root-only state directory. Every
    controller invocation (request handling, recovery, operator resolution)
    takes it without waiting and keeps it for its whole run; a second
    invocation finds it held and changes nothing.

OBSERVER LOCK — no observer run during the critical stages.
    The observer's own `lock` file, the one every observer assessment,
    correction and housekeeping round takes. The controller opens it
    descriptor-safely (the state directory reached from `/` without following
    a link, owned by the observer account and not group- or world-writable;
    the file opened by name inside it, never created, owned by the observer)
    and takes it without waiting. While the controller holds it, no observer
    run that takes the lock can start, scheduled or manual: assessments find it
    held and record that, exactly as when two observer runs meet. The
    descriptor is passed to the observer's evidence mode, which verifies it is
    that file and reads under it.

    It is opened READ-ONLY: `flock` needs no write access, and the
    controller's sandbox (ProtectSystem=strict, the observer's state not among
    its writable paths) keeps the observer's state read-only to it. The
    controller can open that one existing file and change nothing there.

    The observer's liveness heartbeat does not wait for the lock: a manual
    liveness run during a shutdown still sends its heartbeat (never retried,
    and nothing else), and skips its housekeeping. That is the observer's
    existing behaviour and is left as it is.
"""
from __future__ import annotations

import contextlib
import fcntl
import os
import stat
import time

from . import fsafe


class Busy(Exception):
    """The lock is held by someone else."""


def coordination_lock(state_dir: fsafe.HeldDir) -> int:
    """Take the coordination lock without waiting. Returns the descriptor to keep open."""
    fd = os.open("coordination.lock",
                 os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600,
                 dir_fd=state_dir.fd)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_uid != os.geteuid() or st.st_mode & 0o077:
            raise fsafe.UnsafePath("coordination.lock is not a private regular file")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise Busy("another power-controller run holds the coordination lock") from exc
        return fd
    except BaseException:
        os.close(fd)
        raise


class ObserverLock:
    """The observer's own lock, held by the controller."""

    def __init__(self, state_root: str, observer_uid: int):
        self.state_root, self.observer_uid = state_root, observer_uid
        self.fd: int | None = None
        self.identity: tuple[int, int] | None = None

    def _open(self) -> int:
        held = fsafe.open_dir(self.state_root, owner=self.observer_uid, private=True)
        try:
            fd = os.open("lock", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
                         dir_fd=held.fd)
            try:
                st = os.fstat(fd)
                if not stat.S_ISREG(st.st_mode) or st.st_uid != self.observer_uid \
                        or st.st_mode & 0o022 or st.st_nlink != 1:
                    raise fsafe.UnsafePath("the observer lock is not the observer's regular file")
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                fsafe.verify_dir(held)
                self.identity = (st.st_dev, st.st_ino)
                return fd
            except BaseException:
                os.close(fd)
                raise
        finally:
            held.close()

    def acquire(self, wait_seconds: float, sleep=time.sleep, monotonic=time.monotonic) -> None:
        """Take the lock, waiting at most `wait_seconds` for a running observer to finish."""
        deadline = monotonic() + wait_seconds
        while True:
            try:
                self.fd = self._open()
                return
            except BlockingIOError as exc:
                if monotonic() >= deadline:
                    raise Busy("an observer run held its lock for the whole wait") from exc
                sleep(2.0)

    def release(self) -> None:
        if self.fd is not None:
            with contextlib.suppress(OSError):
                os.close(self.fd)
            self.fd = None
