"""File open events from fanotify, through ctypes.

The group is created without FAN_REPORT_FID and the mark is FAN_MARK_MOUNT:
btrfs subvolumes reject FID marks with EXDEV, which is why fatrace sees nothing
on them. /usr, /etc and /opt all live on the root mount, so that is the only
mark needed, and events from /home or /mnt never arrive.
"""

import ctypes
import errno
import os
import select
import struct

FAN_OPEN = 0x20
FAN_CLOEXEC = 0x1
FAN_NONBLOCK = 0x2
FAN_MARK_ADD = 0x1
FAN_MARK_MOUNT = 0x10
FAN_NOFD = -1
AT_FDCWD = -100
METADATA_VERSION = 3

_METADATA = struct.Struct("=IBBHQii")

# Every event in a read holds an open file descriptor. 16 KiB is at most 682
# events, which stays under the usual soft limit of 1024; past that limit the
# kernel would drop events.
READ_SIZE = 16384

Event = tuple[int, bytes | None]


def parse_events(buffer: bytes, resolve: bool = True) -> tuple[list[Event], int]:
    """Turn a read buffer into (pid, path) events and an overflow count.

    Closes every file descriptor in the buffer. Path is None when `resolve` is
    false or the descriptor cannot be resolved.
    """
    events: list[Event] = []
    overflows = 0
    offset = 0
    while offset + _METADATA.size <= len(buffer):
        length, version, _, _, _, fd, pid = _METADATA.unpack_from(buffer, offset)
        if version != METADATA_VERSION:
            raise RuntimeError(f"unsupported fanotify metadata version {version}")
        if length < _METADATA.size:
            raise RuntimeError(f"malformed fanotify event length {length}")
        offset += length

        if fd == FAN_NOFD:
            overflows += 1
            continue
        try:
            path = None
            if resolve:
                try:
                    path = os.readlink(b"/proc/self/fd/%d" % fd)
                except OSError:
                    pass
            events.append((pid, path))
        finally:
            os.close(fd)
    return events, overflows


class Watcher:
    def __init__(self, mount: bytes = b"/"):
        libc = ctypes.CDLL(None, use_errno=True)
        libc.fanotify_init.argtypes = [ctypes.c_uint, ctypes.c_uint]
        libc.fanotify_init.restype = ctypes.c_int
        libc.fanotify_mark.argtypes = [
            ctypes.c_int,
            ctypes.c_uint,
            ctypes.c_uint64,
            ctypes.c_int,
            ctypes.c_char_p,
        ]
        libc.fanotify_mark.restype = ctypes.c_int

        self._fd = libc.fanotify_init(
            FAN_CLOEXEC | FAN_NONBLOCK, os.O_RDONLY | os.O_LARGEFILE | os.O_CLOEXEC
        )
        if self._fd < 0:
            e = ctypes.get_errno()
            raise OSError(e, f"fanotify_init: {os.strerror(e)}")

        if (
            libc.fanotify_mark(
                self._fd, FAN_MARK_ADD | FAN_MARK_MOUNT, FAN_OPEN, AT_FDCWD, mount
            )
            < 0
        ):
            e = ctypes.get_errno()
            os.close(self._fd)
            raise OSError(e, f"fanotify_mark {mount!r}: {os.strerror(e)}")

    def wait(self, timeout: float) -> bool:
        ready, _, _ = select.select([self._fd], [], [], timeout)
        return bool(ready)

    def read(self, resolve: bool = True) -> tuple[list[Event], int]:
        try:
            buffer = os.read(self._fd, READ_SIZE)
        except OSError as e:
            if e.errno in (errno.EAGAIN, errno.EINTR):
                return [], 0
            raise
        return parse_events(buffer, resolve)

    def close(self) -> None:
        os.close(self._fd)
