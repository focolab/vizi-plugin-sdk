#!/usr/bin/env python3
"""Put two SDKs on opposite ends of one live doorbell socket.

    uv run scripts/head_to_head.py [--only python,go,rust] [--skip-build]

The cross-check corpus has every implementation grading the same recording.
This does not: it opens one `AF_UNIX SOCK_SEQPACKET` socketpair, hands one end
to a peer built from one SDK and the other end to a peer built from another,
and has the first send `D` hints its own encoder produced while the second
decodes them with its own receiver.  A disagreement is a wire divergence.

Every ordered pair runs, the same-SDK pairs included as the control that shows
the harness itself agrees.  Each peer prints one canonical line per hint, so
the comparison is between two SDKs and not against a stored expectation.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# segment/pane/epoch/regions, "-" for an absent pane. Chosen to cover a
# multi-word mask, a bare hint with no pane, the largest field values, and pane
# zero with an epoch — the case a "both zero or both non-zero" rule would lose.
HINTS = [
    "9/1/2/3,64",
    "1/-/-/0",
    "244837814094590/255/7/0,7,8,511",
    "5/0/1/100",
]


def expected() -> list[str]:
    """What a receiver must print, derived from the hints so it cannot drift."""
    lines = []
    for spec in HINTS:
        segment, pane, epoch, regions = spec.split("/")
        lines.append(f"hint segment={segment} pane={pane} epoch={epoch} regions={regions}")
    return lines


def build(names: set[str], skip: bool) -> dict[str, list[str]]:
    """The command that launches each SDK's peer, building what needs building."""
    peers: dict[str, list[str]] = {}
    if "python" in names:
        peers["python"] = [sys.executable, str(ROOT / "scripts" / "doorbell_peer.py")]
    if "go" in names:
        binary = ROOT / "polyglot" / "go-sdk" / "doorbell-peer"
        if not skip:
            run(["go", "build", "-o", str(binary), "./cmd/doorbell-peer"], ROOT / "polyglot" / "go-sdk")
        peers["go"] = [str(binary)]
    if "rust" in names:
        crate = ROOT / "polyglot" / "rust-sdk"
        if not skip:
            run(["cargo", "build", "--example", "doorbell_peer"], crate)
        peers["rust"] = [str(crate / "target" / "debug" / "examples" / "doorbell_peer")]
    return peers


def run(command: list[str], cwd: Path) -> None:
    print(f"  building: {' '.join(command)}", file=sys.stderr)
    subprocess.run(command, cwd=cwd, check=True, stdout=subprocess.DEVNULL)


def one_pair(sender: list[str], receiver: list[str]) -> tuple[list[str], str]:
    """Run both peers over one socketpair and return what the receiver decoded."""
    left, right = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    try:
        left.set_inheritable(True)
        right.set_inheritable(True)
        # The receiver starts first so a fast sender never outruns it, though
        # the socket queue would hold the records either way.
        listening = subprocess.Popen(
            receiver + ["receive"], pass_fds=(right.fileno(),), text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env={**os.environ, "VIZI_DOORBELL_FD": str(right.fileno())})
        sending = subprocess.Popen(
            sender + ["send", *HINTS], pass_fds=(left.fileno(),), text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env={**os.environ, "VIZI_DOORBELL_FD": str(left.fileno())})
    finally:
        left.close()
        right.close()
    decoded, listener_errors = listening.communicate(timeout=60)
    sent, sender_errors = sending.communicate(timeout=60)
    trouble = ""
    if sending.returncode:
        trouble = f"sender exited {sending.returncode}: {sender_errors.strip()}"
    elif listening.returncode:
        trouble = f"receiver exited {listening.returncode}: {listener_errors.strip()}"
    elif sent.strip():
        trouble = f"sender reported: {sent.strip()}"
    return [line for line in decoded.splitlines() if line], trouble


def main(argv: list[str]) -> int:
    names = {"python", "go", "rust"}
    if "--only" in argv:
        names = set(argv[argv.index("--only") + 1].split(","))
    peers = build(names, "--skip-build" in argv)
    want = expected()
    order = [name for name in ("python", "go", "rust") if name in peers]
    print(f"\n{len(HINTS)} hints, {len(order)}x{len(order)} ordered pairs "
          f"over a real SOCK_SEQPACKET socketpair\n")

    failures = 0
    for sender in order:
        for receiver in order:
            decoded, trouble = one_pair(peers[sender], peers[receiver])
            arrow = f"{sender} -> {receiver}"
            if trouble:
                print(f"  ERROR  {arrow:<20} {trouble}")
                failures += 1
            elif decoded == want:
                print(f"  ok     {arrow:<20} {len(decoded)} hints decoded byte-identically")
            else:
                failures += 1
                print(f"  DIFFER {arrow:<20}")
                for i in range(max(len(want), len(decoded))):
                    mine = decoded[i] if i < len(decoded) else "(nothing)"
                    theirs = want[i] if i < len(want) else "(nothing)"
                    if mine != theirs:
                        print(f"           sent     {theirs}")
                        print(f"           decoded  {mine}")
    print()
    if failures:
        print(f"{failures} pair(s) disagreed on the wire")
        return 1
    print(f"every pair agreed on all {len(want)} hints")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
