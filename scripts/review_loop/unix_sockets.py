"""Every unix socket in this network namespace, as the kernel lists it.

From sock_diag over netlink, byte for byte, with each socket's owner, type and
state: what ``agy_sandbox_check`` judges agy's sandbox by.
"""

from __future__ import annotations

import socket
import struct
from typing import TYPE_CHECKING, NamedTuple

if TYPE_CHECKING:
    from collections.abc import Iterator


#: sock_diag, the kernel's socket listing over netlink (linux/sock_diag.h,
#: linux/unix_diag.h): a dump of every AF_UNIX socket, each with its name and owner.
_NETLINK_SOCK_DIAG = 4
_SOCK_DIAG_BY_FAMILY = 20
_NLM_F_REQUEST, _NLM_F_DUMP, _NLM_F_DUMP_INTR = 0x1, 0x300, 0x10
_NLMSG_ERROR, _NLMSG_DONE = 2, 3
_UDIAG_SHOW_NAME, _UDIAG_SHOW_UID = 0x01, 0x40
_UNIX_DIAG_NAME, _UNIX_DIAG_UID = 0, 7
_NLMSG_HDR = struct.Struct("=IHHII")
_UNIX_DIAG_REQ = struct.Struct("=BBHIIIII")
_UNIX_DIAG_MSG = struct.Struct("=BBBBIII")
_RTATTR = struct.Struct("=HH")
#: unix_diag_msg's type and state that take a connection: a stream or seqpacket socket
#: in TCP_LISTEN, or any datagram one.
SOCK_DGRAM, TCP_LISTEN = 2, 10


class Sock(NamedTuple):
    """A bound unix socket: its name, and its owner (None where the kernel, before
    5.3, does not say)."""

    #: A path as it was bound (``b"/run/..."``), or an abstract name with its leading
    #: NUL (``b"\\0/tmp/.X11-unix/X0"``).
    name: bytes
    uid: int | None = None
    #: Whether it takes connections or datagrams from others: a listening stream
    #: socket, or a datagram one. A client's connected socket has a name too.
    accepts: bool = True
    #: SOCK_STREAM, SOCK_DGRAM or SOCK_SEQPACKET.
    kind: int = socket.SOCK_STREAM


def unix_sockets() -> list[Sock]:
    """Every bound unix socket in this network namespace, byte for byte, and its owner.

    From the kernel over netlink, not /proc/net/unix: that file prints each name raw on
    a line of its own, so a name with a line break in it reads as two sockets, neither
    of them the real one, and a name that is not UTF-8 reads as some other name.

    Raises OSError when the kernel will not list them, or the list comes back
    interrupted or malformed; a short list would pass every check that reads it.
    """
    request = _UNIX_DIAG_REQ.pack(
        socket.AF_UNIX,
        0,
        0,
        0xFFFFFFFF,
        0,
        _UDIAG_SHOW_NAME | _UDIAG_SHOW_UID,
        0xFFFFFFFF,
        0xFFFFFFFF,
    )
    header = _NLMSG_HDR.pack(
        _NLMSG_HDR.size + len(request),
        _SOCK_DIAG_BY_FAMILY,
        _NLM_F_REQUEST | _NLM_F_DUMP,
        1,
        0,
    )
    found: list[Sock] = []
    with socket.socket(socket.AF_NETLINK, socket.SOCK_RAW, _NETLINK_SOCK_DIAG) as nl:
        nl.settimeout(10)
        nl.sendall(header + request)
        while True:
            data = nl.recv(1 << 20)
            if not data:
                raise OSError("the kernel's socket list ended early")
            for kind, flags, body in _netlink_messages(data):
                if flags & _NLM_F_DUMP_INTR:
                    # The table changed while it was listed: a socket may be missing.
                    raise OSError("the kernel's socket list was interrupted")
                if kind == _NLMSG_DONE:
                    _check_done(body)
                    return found
                if kind == _NLMSG_ERROR:
                    raise OSError("the kernel would not list its sockets (sock_diag)")
                sock = _diag_socket(body)
                if sock.name:
                    found.append(sock)


def _netlink_messages(data: bytes) -> Iterator[tuple[int, int, bytes]]:
    """Each message in one netlink read: its type, its flags and its body."""
    offset = 0
    while offset < len(data):
        if offset + _NLMSG_HDR.size > len(data):
            raise OSError("a truncated message in the kernel's socket list")
        length, kind, flags, _, _ = _NLMSG_HDR.unpack_from(data, offset)
        if length < _NLMSG_HDR.size or offset + length > len(data):
            raise OSError("a malformed message in the kernel's socket list")
        yield kind, flags, data[offset + _NLMSG_HDR.size : offset + length]
        offset += (length + 3) & ~3


def _check_done(body: bytes) -> None:
    """NLMSG_DONE carries the dump's status: anything but 0 is a list cut short."""
    if len(body) < 4 or struct.unpack_from("=i", body)[0] != 0:
        raise OSError("the kernel's socket list did not complete")


def _diag_socket(body: bytes) -> Sock:
    """One socket's unix_diag_msg: its name (empty if it has none), owner, and
    whether it accepts anything from others."""
    if len(body) < _UNIX_DIAG_MSG.size:
        raise OSError("a truncated socket in the kernel's socket list")
    _, sock_type, state, _, _, _, _ = _UNIX_DIAG_MSG.unpack_from(body)
    accepts = sock_type == SOCK_DGRAM or state == TCP_LISTEN
    name, uid = b"", None
    offset = _UNIX_DIAG_MSG.size
    while offset < len(body):
        if offset + _RTATTR.size > len(body):
            raise OSError("a truncated attribute in the kernel's socket list")
        length, attr_type = _RTATTR.unpack_from(body, offset)
        if length < _RTATTR.size or offset + length > len(body):
            raise OSError("a malformed attribute in the kernel's socket list")
        value = body[offset + _RTATTR.size : offset + length]
        if attr_type == _UNIX_DIAG_NAME:
            # A path ends at its first NUL (a binder may count the terminator); an
            # abstract name is every byte after its leading one.
            name = value if value[:1] == b"\0" else value.split(b"\0", 1)[0]
        elif attr_type == _UNIX_DIAG_UID and len(value) >= 4:
            uid = struct.unpack_from("=I", value)[0]
        offset += (length + 3) & ~3
    return Sock(name, uid, accepts, sock_type)
