"""Runs in a fresh interpreter with every unix-only socket symbol hidden, so
that importing and driving the transport proves the Windows path touches none
of them.  Invoked as a subprocess by test_doorbell.WindowsShapeTests.
"""

import socket
import struct
import sys
import time

UNIX_ONLY = [
    "AF_UNIX",
    "SOCK_SEQPACKET",
    "SCM_RIGHTS",
    "CMSG_SPACE",
    "CMSG_LEN",
    "MSG_CTRUNC",
    "MSG_CMSG_CLOEXEC",
    "MSG_NOSIGNAL",
    "MSG_DONTWAIT",
]
for _name in [n for n in UNIX_ONLY if hasattr(socket, n)]:
    delattr(socket, _name)


def tcp_pair():
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    client = socket.create_connection(listener.getsockname())
    server, _ = listener.accept()
    listener.close()
    return client, server


socket.socketpair = lambda *a, **k: tcp_pair()  # the Windows emulation's shape

assert "vizi_plugin_sdk.doorbell" not in sys.modules
try:
    from vizi_plugin_sdk import doorbell as db  # noqa: E402  (must follow the hiding)
except ModuleNotFoundError:
    # A tree where the same package lives under another name, as the
    # conftest beside this file arranges.
    from vizi.plugins.sdk import doorbell as db  # noqa: E402


def _forbidden(self):
    raise AssertionError("a stream transport must never call recvmsg")


db.DoorbellTransport._receive_message = _forbidden  # recvmsg does not exist on Windows

got = []
plugin, host = tcp_pair()
t = db.DoorbellTransport(plugin, "stream", got.append, handup_deadline=2.0)
t.handshake_complete(db.PROTOCOL_MAJOR, db.PROTOCOL_MINOR)
t.start()

name = b"Local\\vizi-0123456789abcdef0123456789abcdef"
grant = db._M_HEAD.pack(9, 65536, db.PERM_PLUGIN_READ, 0, len(name)) + name + b"\0"
host.sendall(db.encode_record(db.OP_MEMORY, 1, grant + bytes(100 - len(grant))))

t.tool_activated(1)
t.epoch_acknowledged(0, 1)
event = db._E_HEAD.pack(1, 0, 1, 0, 1, 0, 0, 1, 1, db.PHASE_FIRE, 0, 0, 0, 1, 0, db.KIND_VOXEL, 0)
host.sendall(
    db.encode_record(
        db.OP_INPUT, 2, event + b"\0\0" + struct.pack("<fff", 1, 2, 3) + bytes(32) + bytes(12)
    )
)
host.sendall(db.encode_record(db.OP_NOTIFY, 3, db.encode_notification_payload(9, [5])))

assert t.send_notification(9, [1, 2]) is db.SendResult.SENT
back = host.recv(200)
assert len(back) == 112, len(back)
assert struct.unpack_from("<I", back, 108)[0] == db.crc32(back[:108])

deadline = time.monotonic() + 5
while len(got) < 3 and time.monotonic() < deadline:
    time.sleep(0.01)

assert len(got) == 3, got
assert all(f.disposition is db.Disposition.DECODED for f in got), got
assert got[0].record.name == name.decode()
assert got[0].descriptor is None, "a stream grant carries no descriptor"
assert got[1].record.position == db.Voxel(1.0, 2.0, 3.0)
assert got[2].record.regions() == (5,)

host.close()
t.join(3.0)
assert isinstance(t.terminal, db.Teardown) and t.terminal.reason == "peer_closed", t.terminal
print("OK")
