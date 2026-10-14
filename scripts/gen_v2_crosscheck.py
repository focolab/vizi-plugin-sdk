#!/usr/bin/env python3
"""Generate a v2 doorbell cross-check corpus, optionally with Python's verdicts.

The corpus is scenarios of doorbell records plus the lifecycle inputs that
precede them, and one verdict column per implementation that has replayed them.
Each implementation replays a scenario, records what it decided about every
frame, and compares itself against every other column present.  Where two
disagree the finding is against the specification: no implementation is the
oracle (see polyglot/CROSSCHECK.md), which is why ``--scenarios-only`` exists —
the scenarios are fixture bytes, and the first column need not be this one.

Only specification-defined facts are compared — the frame disposition, whether
a violation rides the grant acknowledgement or ContractViolation, and the
`where` names E §2 fixes.  Diagnostic spellings and cancellation reason strings
are each implementation's own and are deliberately not compared.

The corpus also carries ``polyglot/unsettled.json`` verbatim, so every replayer
treats a question the documents leave open the same way.

    uv run scripts/gen_v2_crosscheck.py <out.json> [--scenarios-only]
"""

from __future__ import annotations

import json
import socket
import struct
import sys
import time
from pathlib import Path

from vizi_plugin_sdk import doorbell as db

# --- record builders -------------------------------------------------------

E_HEAD = struct.Struct("<IIIIIIIIHBBBBBBBBH")  # [42..44) is `action`


def e_payload(**kw) -> bytes:
    f = dict(
        gen=1, t=0, interaction=1, seq=0, epoch=1, target_id=0, target_part=0,
        tool=1, channels=1, phase=db.PHASE_PRESS, view_id=0, level=0, mods=0,
        click_count=1, target_kind=0, payload_kind=db.KIND_NONE, position_flags=0,
        action=0,
    )
    f.update({k: v for k, v in kw.items() if k not in ("position", "pad2")})
    head = E_HEAD.pack(*[f[k] for k in (
        "gen", "t", "interaction", "seq", "epoch", "target_id", "target_part",
        "tool", "channels", "phase", "view_id", "level", "mods", "click_count",
        "target_kind", "payload_kind", "position_flags", "action")])
    pos = kw.get("position", b"")
    # The position union is [44..92), 48 bytes, and the fixed padding [92..100).
    return head + pos + bytes(48 - len(pos)) + kw.get("pad2", bytes(8))


def m_payload(segment_id=7, size=4096, perms=db.PERM_PLUGIN_READ, pad=0, name=b"") -> bytes:
    body = db._M_HEAD.pack(segment_id, size, perms, pad, len(name)) + name + b"\0"
    return (body + bytes(100))[:100]


def rec(op: int, payload: bytes, rid: int) -> str:
    return db.encode_record(op, rid, payload).hex()


VOXEL = struct.pack("<fff", 1.5, 2.5, 3.5)
RAY_OK = struct.pack("<ffffffff", 0, 0, 0, 10, 0, 0, 0.25, 1.5)
RAY_DEGENERATE = struct.pack("<ffffffff", 3, 4, 5, 3, 4, 5, 0.0, 0.0)
RAY_NAN = struct.pack("<ffffffff", float("nan"), 0, 0, 10, 0, 0, 0.5, 0)
RAY_FRAC = struct.pack("<ffffffff", 0, 0, 0, 10, 0, 0, 1.5, 0)
BACK_HALF = struct.pack("<f", 0.5)


def scenarios() -> list[dict]:
    """Each scenario is replayed on a fresh transport."""
    out: list[dict] = []

    # 1. D — every transport-checkable content case.
    out.append({
        "name": "notification_content",
        "lifecycle": [],
        "records": [
            rec(db.OP_NOTIFY, db.encode_notification_payload(5, [0, 7, 8, 511]), 1),
            rec(db.OP_NOTIFY, db.encode_notification_payload(5, 0), 2),               # empty mask
            rec(db.OP_NOTIFY, bytes(8) + bytes(64) + struct.pack("<IB", 3, 0) + bytes(23), 3),  # zero id
            rec(db.OP_NOTIFY, db.encode_notification_payload(9, [1], 4, 2), 4),        # pane-associated
            rec(db.OP_NOTIFY, db.encode_notification_payload(9, [1]) [:76] + b"\x05" + bytes(23), 5),  # view without epoch
        ],
    })

    # 2. M — the record-local grant checks, unix layout.
    out.append({
        "name": "memory_grants",
        "lifecycle": [],
        "fds_per_record": 1,
        "records": [
            rec(db.OP_MEMORY, m_payload(), 1),
            rec(db.OP_MEMORY, m_payload(segment_id=0), 2),
            rec(db.OP_MEMORY, m_payload(size=31), 3),
            rec(db.OP_MEMORY, m_payload(perms=db.PERM_PLUGIN_WRITE), 4),
            rec(db.OP_MEMORY, m_payload(perms=db.PERM_PLUGIN_READ | 0x10), 5),
            rec(db.OP_MEMORY, m_payload(name=b"ignored-on-unix"), 6),
            rec(db.OP_MEMORY, m_payload(perms=db.PERM_PLUGIN_READ | db.PERM_HOST_WRITE, size=1 << 20), 7),
        ],
    })

    # 3. E — a whole gesture plus every ignore/reject row reachable without a fault.
    def ev(**kw):
        kw.setdefault("epoch", 5)  # the epoch this scenario installs
        return e_payload(**kw)

    press = ev(interaction=1, phase=db.PHASE_PRESS)
    out.append({
        "name": "input_algorithm",
        "lifecycle": [["tool_activated", 1], ["tool_activated", 3], ["epoch_acknowledged", 0, 5]],
        "records": [
            rec(db.OP_INPUT, press, 1),                                                     # accepted PRESS
            rec(db.OP_INPUT, press, 1),                                                     # byte-identical retry
            rec(db.OP_INPUT, ev(interaction=1, phase=db.PHASE_PRESS, click_count=2), 1),  # changed live opening
            rec(db.OP_INPUT, ev(interaction=1, seq=1, phase=db.PHASE_DRAG), 2),
            rec(db.OP_INPUT, ev(interaction=1, seq=3, phase=db.PHASE_DRAG), 3),      # seq gap
            rec(db.OP_INPUT, ev(interaction=1, seq=3, phase=db.PHASE_DRAG), 4),      # duplicate seq
            rec(db.OP_INPUT, ev(interaction=1, seq=4, phase=db.PHASE_DRAG, gen=2), 5),  # interaction mismatch
            rec(db.OP_INPUT, ev(interaction=2, phase=db.PHASE_PRESS), 6),            # opens, implicit close
            rec(db.OP_INPUT, ev(interaction=3, seq=5, phase=db.PHASE_PRESS), 7),     # seq_on_open
            rec(db.OP_INPUT, ev(interaction=0, phase=db.PHASE_FIRE), 8),             # zero_id
            rec(db.OP_INPUT, ev(interaction=4, tool=0, phase=db.PHASE_FIRE), 9),     # zero_id
            rec(db.OP_INPUT, ev(interaction=4, gen=0, phase=db.PHASE_FIRE), 10),     # zero_id
            rec(db.OP_INPUT, ev(interaction=4, epoch=0, phase=db.PHASE_FIRE), 11),   # zero_id
            rec(db.OP_INPUT, ev(interaction=4, phase=db.PHASE_FIRE, payload_kind=db.KIND_VOXEL,
                                       position=VOXEL + b"\1"), 12),                        # position_tail
            rec(db.OP_INPUT, ev(interaction=4, phase=db.PHASE_FIRE, payload_kind=db.KIND_RAY,
                                       position=RAY_NAN), 13),                              # nonfinite
            rec(db.OP_INPUT, ev(interaction=4, phase=db.PHASE_FIRE, payload_kind=db.KIND_RAY,
                                       position=RAY_FRAC), 14),                             # entry_frac range
            rec(db.OP_INPUT, ev(interaction=4, phase=db.PHASE_FIRE, payload_kind=db.KIND_RAY,
                                       position=RAY_DEGENERATE), 15),                       # camera == far
            rec(db.OP_INPUT, ev(interaction=4, phase=db.PHASE_FIRE, payload_kind=db.KIND_PICKED,
                                       position=VOXEL, position_flags=db.FLAG_MISS), 16),   # miss_with_picked
            rec(db.OP_INPUT, ev(interaction=4, phase=db.PHASE_FIRE, target_kind=2, target_id=0), 17),
            rec(db.OP_INPUT, ev(interaction=4, phase=db.PHASE_FIRE, target_kind=0, target_part=3), 18),
            rec(db.OP_INPUT, ev(interaction=4, phase=db.PHASE_FIRE, epoch=4), 19),    # stale epoch
            rec(db.OP_INPUT, ev(interaction=4, phase=db.PHASE_FIRE, epoch=6), 20),    # future epoch
            rec(db.OP_INPUT, ev(interaction=4, phase=db.PHASE_FIRE, tool=9), 21),     # unknown tool
            rec(db.OP_INPUT, ev(interaction=4, phase=db.PHASE_FIRE, tool=2), 22),     # gap tool = retired
            rec(db.OP_INPUT, ev(interaction=9, seq=1, phase=db.PHASE_DRAG), 23),      # unknown interaction
            rec(db.OP_INPUT, ev(interaction=4, phase=db.PHASE_FIRE, phase_unknown=None) if False
                else ev(interaction=4, phase=7), 24),                                 # unknown phase
            rec(db.OP_INPUT, ev(interaction=4, phase=db.PHASE_FIRE, payload_kind=99,
                                       position=b"\7" * 48), 25),                            # unknown payload kind
            rec(db.OP_INPUT, ev(interaction=5, phase=db.PHASE_FIRE, target_kind=99,
                                       target_id=0, target_part=7), 26),                     # unknown target kind
            rec(db.OP_INPUT, ev(interaction=6, phase=db.PHASE_FIRE, mods=0x81,
                                       position_flags=0x80), 27),                            # reserved bits
            rec(db.OP_INPUT, ev(interaction=7, phase=db.PHASE_PRESS, payload_kind=db.KIND_RAY_AND_PICKED,
                                       position=RAY_OK + VOXEL + BACK_HALF, position_flags=db.FLAG_ROLL_REF_Y), 28),
            rec(db.OP_INPUT, ev(interaction=7, seq=1, phase=db.PHASE_RELEASE), 29),
            rec(db.OP_INPUT, ev(interaction=7, seq=2, phase=db.PHASE_CANCEL), 30),    # late terminal
        ],
    })

    # 3b. `back_frac`: a 48-byte RAY_AND_PICKED (on a DRAG
    # too: whether a record picks is the host's opinion), NaN a point pick and
    # accepted, anything outside [0, 1] or infinite `nonfinite_position`,
    # and a stray byte at payload 88 on a shorter kind the record's own
    # `position_tail`, not the fatal padding.
    def picked(back: bytes) -> bytes:
        return RAY_OK + VOXEL + back

    out.append({
        "name": "input_back_frac",
        "lifecycle": [["tool_activated", 1], ["epoch_acknowledged", 0, 5]],
        "records": [
            rec(db.OP_INPUT, ev(interaction=1, phase=db.PHASE_PRESS, payload_kind=db.KIND_RAY_AND_PICKED,
                                position=picked(BACK_HALF)), 1),                             # 48 bytes round trip
            # The pick policy is off the wire: any phase may carry a pick.
            rec(db.OP_INPUT, ev(interaction=1, seq=1, phase=db.PHASE_DRAG, payload_kind=db.KIND_RAY_AND_PICKED,
                                position=picked(BACK_HALF)), 2),                             # a DRAG with a pick
            rec(db.OP_INPUT, ev(interaction=1, seq=2, phase=db.PHASE_RELEASE), 3),
            rec(db.OP_INPUT, ev(interaction=2, phase=db.PHASE_FIRE, payload_kind=db.KIND_RAY_AND_PICKED,
                                position=picked(struct.pack("<f", float("nan")))), 4),      # point pick: accepted
            rec(db.OP_INPUT, ev(interaction=3, phase=db.PHASE_FIRE, payload_kind=db.KIND_RAY_AND_PICKED,
                                position=picked(struct.pack("<f", 1.25))), 5),               # nonfinite_position
            rec(db.OP_INPUT, ev(interaction=4, phase=db.PHASE_FIRE, payload_kind=db.KIND_RAY_AND_PICKED,
                                position=picked(struct.pack("<f", -0.5))), 6),               # nonfinite_position
            rec(db.OP_INPUT, ev(interaction=5, phase=db.PHASE_FIRE, payload_kind=db.KIND_RAY_AND_PICKED,
                                position=picked(struct.pack("<f", float("inf")))), 7),       # nonfinite_position
            rec(db.OP_INPUT, ev(interaction=6, phase=db.PHASE_FIRE, payload_kind=db.KIND_VOXEL,
                                position=VOXEL + bytes(32) + b"\1"), 8),                    # byte 88: position_tail
            rec(db.OP_INPUT, ev(interaction=7, phase=db.PHASE_FIRE, payload_kind=db.KIND_RAY_AND_PICKED,
                                position=picked(struct.pack("<f", 1.0))), 9),                # [0, 1] inclusive
        ],
    })

    # 4. Unknown opcode, and the id gap / regression diagnostics around it.
    out.append({
        "name": "unknown_opcode_and_ids",
        "lifecycle": [],
        "records": [
            rec(db.OP_NOTIFY, db.encode_notification_payload(5, [1]), 1),
            rec(0x5A, bytes(100), 2),
            rec(db.OP_NOTIFY, db.encode_notification_payload(5, [2]), 4),   # gap
            rec(db.OP_NOTIFY, db.encode_notification_payload(5, [3]), 3),   # regression
            rec(db.OP_NOTIFY, db.encode_notification_payload(5, [4]), 5),
        ],
    })

    # 5. Framing faults, one per scenario: each is terminal.
    bad_crc = bytearray(db.encode_record(db.OP_NOTIFY, 1, db.encode_notification_payload(5, [1])))
    bad_crc[40] ^= 1
    body_ver = bytearray(db._HEADER.pack(db.OP_NOTIFY, 3, 0, 0, 1) + db.encode_notification_payload(5, [1]))
    body_flags = bytearray(db._HEADER.pack(db.OP_NOTIFY, 2, 1, 0, 1) + db.encode_notification_payload(5, [1]))
    body_res = bytearray(db._HEADER.pack(db.OP_NOTIFY, 2, 0, 9, 1) + db.encode_notification_payload(5, [1]))
    body_ff = bytearray(db._HEADER.pack(0xFF, 2, 0, 0, 1) + bytes(100))
    for label, body in (("bad_ver", body_ver), ("bad_flags", body_flags),
                        ("bad_reserved", body_res), ("reserved_op", body_ff)):
        out.append({"name": f"fault_{label}", "lifecycle": [], "terminal": True,
                    "records": [(bytes(body) + struct.pack("<I", db.crc32(bytes(body)))).hex()]})
    out.append({"name": "fault_bad_crc", "lifecycle": [], "terminal": True, "records": [bytes(bad_crc).hex()]})
    out.append({"name": "fault_bad_padding_e", "lifecycle": [["tool_activated", 1], ["epoch_acknowledged", 0, 1]],
                "terminal": True,
                "records": [rec(db.OP_INPUT, e_payload(pad2=b"\1" + bytes(7)), 1)]})
    # [42..44) is `action`, not padding.  A FIRE naming an action lands
    # inside the open gesture and touches its state not at all -- three accepted
    # records, and a receiver that faults on a non-zero byte at 42 is wrong.
    out.append({"name": "action_fire_inside_open_gesture",
                "lifecycle": [["tool_activated", 1], ["epoch_acknowledged", 0, 1]],
                "records": [
                    rec(db.OP_INPUT, e_payload(interaction=1, seq=0, phase=db.PHASE_PRESS), 1),
                    rec(db.OP_INPUT, e_payload(interaction=2, seq=0, phase=db.PHASE_FIRE, action=1), 2),
                    rec(db.OP_INPUT, e_payload(interaction=1, seq=1, phase=db.PHASE_DRAG), 3),
                    rec(db.OP_INPUT, e_payload(interaction=1, seq=2, phase=db.PHASE_RELEASE), 4),
                ]})
    out.append({"name": "fault_bad_padding_d", "lifecycle": [], "terminal": True,
                "records": [rec(db.OP_NOTIFY, db.encode_notification_payload(5, [1])[:99] + b"\1", 1)]})
    return out


# --- Python's verdicts -----------------------------------------------------


def normalise(outcome) -> dict | None:
    if isinstance(outcome, db.Frame):
        where = outcome.violation
        return {
            "type": "frame",
            "disposition": outcome.disposition.value,
            "where": None if outcome.grant_failure else where,
            "grant_failure": outcome.grant_failure,
            "implicitly_closed": outcome.implicitly_closed or 0,
        }
    if isinstance(outcome, db.Cancellation):
        return {"type": "cancellation", "interaction": outcome.interaction}
    if isinstance(outcome, (db.Fault,)):
        return {"type": "fault", "class": outcome.failure_class}
    return None  # teardown is scenario shutdown, not a verdict


def run(scn: dict) -> list[dict]:
    plugin_end, host_end = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    collected: list = []
    t = db.DoorbellTransport(plugin_end, "seqpacket", collected.append, handup_deadline=1.0)
    t.start()
    try:
        for call in scn["lifecycle"]:
            getattr(t, call[0])(*call[1:])
        pipes = []
        for hexed in scn["records"]:
            data = bytes.fromhex(hexed)
            if scn.get("fds_per_record"):
                r, w = socket.socketpair(socket.AF_UNIX, socket.SOCK_DGRAM)
                pipes.append((r, w))
                host_end.sendmsg([data], [(socket.SOL_SOCKET, socket.SCM_RIGHTS,
                                           struct.pack("i", w.fileno()))])
            else:
                host_end.send(data)
            time.sleep(0.01)
        time.sleep(0.15)
        for r, w in pipes:
            r.close()
            w.close()
    finally:
        t.stop()
        t.join(2.0)
        host_end.close()
    return [v for v in (normalise(o) for o in collected) if v is not None]


UNSETTLED = Path(__file__).resolve().parent.parent / "polyglot" / "unsettled.json"


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    graded = "--scenarios-only" not in sys.argv[1:]
    corpus = {"scenarios": [], "unsettled": json.loads(UNSETTLED.read_text("utf-8"))["unsettled"]}
    for scn in scenarios():
        entry = dict(scn)
        if graded:
            entry["python"] = run(scn)
        corpus["scenarios"].append(entry)
    path = args[0] if args else "crosscheck_corpus.json"
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(corpus, fh, indent=1)
    total = sum(len(s.get("python", ())) for s in corpus["scenarios"])
    graded_note = f", {total} verdicts" if graded else " (no verdicts)"
    print(f"wrote {path}: {len(corpus['scenarios'])} scenarios{graded_note}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
