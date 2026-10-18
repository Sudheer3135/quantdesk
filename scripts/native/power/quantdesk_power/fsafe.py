"""Descriptor-safe filesystem access for the power controller.

Every path the controller trusts is reached from `/` one component at a time,
each opened with `O_DIRECTORY | O_NOFOLLOW` relative to the one before, so a
symbolic link anywhere on the way is refused, not only at the end. Files are
then opened only by name inside a held directory, never by path. Records are
published by writing a private scratch file and linking it to its final name
(`link` fails if the name exists), so a record is either absent or complete
and is never replaced.
"""
from __future__ import annotations

import contextlib
import os
import secrets
import stat
from dataclasses import dataclass

DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
# Search-only directory descriptors: they need search (x) permission and never read (r).
# Enough to stat the directory and to create, link or remove names relative to it; not
# enough to list it. Linux: O_PATH; macOS and others: O_SEARCH.
SEARCH_ONLY = getattr(os, "O_PATH", None) or getattr(os, "O_SEARCH", None)
SEARCH_FLAGS = (SEARCH_ONLY or 0) | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
FILE_READ_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC


class UnsafePath(OSError):
    """A path, file or directory is not what the controller may trust."""


@dataclass
class HeldDir:
    """An open directory and the identity of every directory on its way from `/`."""
    fd: int
    path: str
    identities: tuple[tuple[int, int], ...]

    def close(self) -> None:
        with contextlib.suppress(OSError):
            os.close(self.fd)


def _parts(path: str) -> list[str]:
    if not isinstance(path, str) or not path.startswith("/"):
        raise UnsafePath(f"{path!r} is not an absolute path")
    parts = [p for p in path.split("/") if p]
    if any(p in (".", "..") for p in parts):
        raise UnsafePath(f"{path!r} contains '.' or '..'")
    return parts


def check_dir(st: os.stat_result, what: str, owner: int | None, private: bool) -> None:
    if not stat.S_ISDIR(st.st_mode):
        raise UnsafePath(f"{what} is not a directory")
    if owner is not None and st.st_uid != owner:
        raise UnsafePath(f"{what} is owned by uid {st.st_uid}, not {owner}")
    if private and st.st_mode & 0o022:
        raise UnsafePath(f"{what} is writable by group or others")


def open_dir(path: str, *, owner: int | None = None, private: bool = True,
             create: bool = False, mode: int = 0o700, search_only: bool = False) -> HeldDir:
    """Open `path` from `/` without following any link.

    `owner` and `private` apply to the final directory. With `create`, a
    missing final component is created (relative to its held parent) with
    `mode`. Intermediate directories must exist. With `search_only`, every
    directory on the way is opened for search only, so the caller needs no
    read permission anywhere (the unprivileged requester: its request
    directory is mode 1730, its parent 0711).
    """
    if search_only and SEARCH_ONLY is None:
        raise UnsafePath("this platform has no search-only directory descriptors")
    flags = SEARCH_FLAGS if search_only else DIR_FLAGS
    if search_only and create:
        raise UnsafePath("a search-only walk never creates anything")
    parts = _parts(path)
    fd = os.open("/", flags)
    identities = []
    try:
        st = os.fstat(fd)
        identities.append((st.st_dev, st.st_ino))
        for index, name in enumerate(parts):
            last = index == len(parts) - 1
            try:
                nxt = os.open(name, flags, dir_fd=fd)
            except FileNotFoundError:
                if not (last and create):
                    raise
                os.mkdir(name, mode, dir_fd=fd)
                nxt = os.open(name, flags, dir_fd=fd)
            except OSError as exc:
                raise UnsafePath(f"{path}: component {name!r} cannot be opened safely "
                                 f"({exc.strerror})") from exc
            os.close(fd)
            fd = nxt
            st = os.fstat(fd)
            identities.append((st.st_dev, st.st_ino))
        check_dir(os.fstat(fd), path, owner, private)
        return HeldDir(fd, path, tuple(identities))
    except BaseException:
        os.close(fd)
        raise


def verify_dir(held: HeldDir) -> None:
    """Walk the path again; every directory on the way must still be the same one."""
    again = open_dir(held.path, private=False)
    try:
        if again.identities != held.identities:
            raise UnsafePath(f"{held.path} was replaced while in use")
    finally:
        again.close()


def subdir(parent: HeldDir, name: str, *, owner: int | None = None, create: bool = False,
           mode: int = 0o700, private: bool = True) -> HeldDir:
    """A child directory of a held directory, opened relative to it."""
    if not name or "/" in name or name in (".", ".."):
        raise UnsafePath(f"{name!r} is not a single path component")
    try:
        fd = os.open(name, DIR_FLAGS, dir_fd=parent.fd)
    except FileNotFoundError:
        if not create:
            raise
        os.mkdir(name, mode, dir_fd=parent.fd)
        fd = os.open(name, DIR_FLAGS, dir_fd=parent.fd)
    except OSError as exc:
        raise UnsafePath(f"{parent.path}/{name} cannot be opened safely ({exc.strerror})") \
            from exc
    try:
        st = os.fstat(fd)
        check_dir(st, f"{parent.path}/{name}", owner, private)
    except BaseException:
        os.close(fd)
        raise
    return HeldDir(fd, f"{parent.path}/{name}", parent.identities + ((st.st_dev, st.st_ino),))


def read_file(directory: HeldDir, name: str, *, max_bytes: int, owner: int | None = None,
              private: bool = False, single_link: bool = True) -> bytes:
    """The bytes of regular file `name` inside `directory`, within `max_bytes`."""
    if not name or "/" in name or name in (".", ".."):
        raise UnsafePath(f"{name!r} is not a single path component")
    try:
        fd = os.open(name, FILE_READ_FLAGS, dir_fd=directory.fd)
    except FileNotFoundError:
        raise
    except OSError as exc:
        raise UnsafePath(f"{directory.path}/{name} cannot be opened safely ({exc.strerror})") \
            from exc
    try:
        st = os.fstat(fd)
        what = f"{directory.path}/{name}"
        if not stat.S_ISREG(st.st_mode):
            raise UnsafePath(f"{what} is not a regular file")
        if owner is not None and st.st_uid != owner:
            raise UnsafePath(f"{what} is owned by uid {st.st_uid}, not {owner}")
        if private and st.st_mode & 0o077:
            raise UnsafePath(f"{what} is readable or writable by group or others")
        if single_link and st.st_nlink != 1:
            raise UnsafePath(f"{what} has {st.st_nlink} links")
        if st.st_size > max_bytes:
            raise UnsafePath(f"{what} is larger than {max_bytes} bytes")
        chunks, total = [], 0
        while True:
            chunk = os.read(fd, 65536)
            if not chunk:
                break
            total += len(chunk)
            if total > max_bytes:
                raise UnsafePath(f"{what} grew beyond {max_bytes} bytes while read")
            chunks.append(chunk)
        return b"".join(chunks)
    finally:
        os.close(fd)


def fsync_dir(directory: HeldDir) -> None:
    os.fsync(directory.fd)


def publish(directory: HeldDir, name: str, data: bytes, mode: int = 0o400) -> bool:
    """Create `name` in `directory` holding exactly `data`, or leave it absent.

    False if the name already exists (nothing is replaced). The file and the
    directory are synced before True is returned.
    """
    if not name or "/" in name or name.startswith("."):
        raise UnsafePath(f"{name!r} is not a publishable name")
    scratch = f".scratch-{secrets.token_hex(8)}"
    fd = os.open(scratch, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                 mode, dir_fd=directory.fd)
    try:
        try:
            view = memoryview(data)
            while view:
                written = os.write(fd, view)
                view = view[written:]
            os.fsync(fd)
        finally:
            os.close(fd)
        try:
            os.link(scratch, name, src_dir_fd=directory.fd, dst_dir_fd=directory.fd,
                    follow_symlinks=False)
        except FileExistsError:
            return False
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(scratch, dir_fd=directory.fd)
    fsync_dir(directory)
    return True


def names(directory: HeldDir) -> list[str]:
    """The entries of a held directory, sorted; scratch files are not entries."""
    return sorted(n for n in os.listdir(directory.fd) if not n.startswith(".scratch-"))
