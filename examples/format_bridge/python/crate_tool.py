#!/usr/bin/env python3
"""Host-free tools over the crate format: the semantic comparator, a writer,
and the whole-file check.

    python3 crate_tool.py compare A.crate B.crate [--calibration TABLE]
    python3 crate_tool.py recreate IN.crate OUT.crate [--order ascending|reverse|scrambled] [--calibration TABLE]
    python3 crate_tool.py check FILE.crate

`compare` prints which tier the two crates match at and exits 0 once both
opened, 2 when either could not be opened or decoded, 1 for a usage error.
`recreate` reads one crate through this implementation's reader and writes
it through its writer with no host in the loop — `../interop/python.crate`
is its output on `../fixtures/sample.crate`.
"""

from __future__ import annotations

import argparse
import random
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from crate.compare import compare  # noqa: E402
from crate.format import FLAG_DERIVED, Calibration, Crate, CrateError, CrateWriter, check_file  # noqa: E402


def _calibration(path: str | None) -> Calibration | None:
    return Calibration.load(Path(path)) if path else None


def cmd_compare(args) -> int:
    try:
        verdict = compare(Path(args.a), Path(args.b), _calibration(args.calibration))
    except (CrateError, OSError) as exc:
        print(f"cannot compare: {exc}", file=sys.stderr)
        return 2
    print(verdict)
    return 0


def recreate(source: Path, target: Path, order: str, calibration: Calibration | None) -> None:
    with Crate.open(source) as crate:
        h = crate.header
        writer = CrateWriter(
            target, h.shape_tczyx, h.np_dtype.newbyteorder("="),
            crate.form, crate.text_bytes if crate.text_utf8 else b"",
            h.spacing_zyx, h.time_increment,
            # A recreation is the same crate, not an export of it: DERIVED,
            # the provenance record and the names carry over; CALIBRATED does
            # not, since the blocks are written decoded.
            flags=h.flags & FLAG_DERIVED,
            provenance=crate.provenance_bytes,
            names=crate.channel_names() or (),
        )
        slots = [(t, c) for t in range(h.t_count) for c in range(h.c_count)]
        if order == "reverse":
            slots.reverse()
        elif order == "scrambled":
            random.Random(0x5EED).shuffle(slots)
        writer.begin()
        try:
            for t, c in slots:
                writer.write_block(t, c, crate.read_block(t, c, verify=True, calibration=calibration))
            writer.finish()
        except BaseException:
            writer.abort()
            raise


def cmd_recreate(args) -> int:
    try:
        recreate(Path(args.source), Path(args.target), args.order, _calibration(args.calibration))
    except (CrateError, OSError) as exc:
        print(f"cannot recreate: {exc}", file=sys.stderr)
        return 2
    print(f"wrote {args.target} ({args.order} block order)")
    return 0


def cmd_check(args) -> int:
    path = Path(args.file)
    try:
        result = check_file(path)
    except (CrateError, OSError) as exc:
        print(f"{path.name}: {exc}")
        return 2
    print(result.summary(path.name))
    return 0 if result.ok else 3


class _Parser(argparse.ArgumentParser):
    """SPEC.md pins the exit codes: 1 is a usage error (argparse's own
    default, 2, is reserved for a crate that would not open)."""

    def error(self, message: str) -> None:  # type: ignore[override]
        self.print_usage(sys.stderr)
        print(f"{self.prog}: error: {message}", file=sys.stderr)
        sys.exit(1)


def main(argv=None) -> int:
    parser = _Parser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("compare", help="which tier two crates match at")
    p.add_argument("a")
    p.add_argument("b")
    p.add_argument("--calibration", help="vizi-crate-cal.bin, needed to decode a calibrated crate")
    p.set_defaults(fn=cmd_compare)
    p = sub.add_parser("recreate", help="read one crate, write another")
    p.add_argument("source")
    p.add_argument("target")
    p.add_argument("--order", choices=("ascending", "reverse", "scrambled"), default="ascending")
    p.add_argument("--calibration")
    p.set_defaults(fn=cmd_recreate)
    p = sub.add_parser("check", help="validate every block of a crate")
    p.add_argument("file")
    p.set_defaults(fn=cmd_check)
    args = parser.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
