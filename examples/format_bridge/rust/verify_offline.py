#!/usr/bin/env python3
"""Cross-check CLI exit codes, all fixture pairs, and host-free writer output.

Run with the repository's .venv/bin/python so the reference comparator's numpy
dependency is available.
"""
from itertools import product
from pathlib import Path
import subprocess
import sys
import tempfile

HERE = Path(__file__).resolve().parent
FIXTURES = HERE.parent / "fixtures"
BINARY = HERE / "format-bridge"
REFERENCE = HERE.parent / "python" / "crate_tool.py"
CALIBRATION = FIXTURES / "vizi-crate-cal.bin"


def run(command, *args):
    return subprocess.run([*map(str, command), *map(str, args)], text=True,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def verdict(result):
    return result.returncode, result.stdout.split(":", 1)[0].strip()


def main():
    fixtures = sorted(FIXTURES.glob("*.crate"))
    for a, b in product(fixtures, repeat=2):
        args = ["compare", a, b, "--calibration", CALIBRATION]
        rust = run([BINARY], *args)
        python = run([sys.executable, REFERENCE], *args)
        assert verdict(rust) == verdict(python), (a.name, b.name, rust, python)
    print(f"ok: Rust/Python verdicts and exit codes agree on {len(fixtures)**2} fixture pairs")
    for name in ["unfinalized.crate", "sample-calibrated.crate"]:
        result = run([BINARY], "compare", FIXTURES / name, FIXTURES / name)
        assert result.returncode == 2, result
    assert run([BINARY], "compare").returncode == 1
    assert run([BINARY], "recreate", "--order", "invalid").returncode == 1
    assert run([BINARY], "check", FIXTURES / "badcrc.crate").returncode == 3
    print("ok: usage, unopenable/undecodable inputs, and bad-CRC check exit codes")
    output = HERE / "test-output"
    output.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(dir=output) as directory:
        directory = Path(directory)
        expected = (HERE.parent / "interop" / "python.crate").read_bytes()
        for order in ["ascending", "reverse", "scrambled"]:
            targets = [directory / f"{order}-{i}.crate" for i in range(2)]
            for target in targets:
                result = run([BINARY], "recreate", FIXTURES / "sample.crate", target,
                             "--order", order)
                assert result.returncode == 0, result
                result = run([sys.executable, REFERENCE], "compare", FIXTURES / "sample.crate", target)
                assert result.returncode == 0 and result.stdout.startswith(("semantically equal", "byte-equal")), result
            assert targets[0].read_bytes() == targets[1].read_bytes()
            if order == "ascending":
                assert targets[0].read_bytes() == expected
            again = directory / f"{order}-again.crate"
            assert run([BINARY], "recreate", targets[0], again).returncode == 0
            assert again.read_bytes() == expected
        for name in ["nospacing.crate", "sample-calibrated.crate"]:
            target = directory / name
            result = run([BINARY], "recreate", FIXTURES / name, target,
                         "--calibration", CALIBRATION)
            assert result.returncode == 0, result
            for command in [[BINARY], [sys.executable, REFERENCE]]:
                result = run(command, "compare", FIXTURES / name, target,
                             "--calibration", CALIBRATION)
                assert result.returncode == 0 and result.stdout.startswith("semantically equal"), result
    print("ok: deterministic ascending/reverse/scrambled writes, canonical re-export, calibration, unknown spacing")
    print("offline cross-check passed")


if __name__ == "__main__":
    main()
