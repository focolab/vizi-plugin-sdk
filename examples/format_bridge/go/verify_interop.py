#!/usr/bin/env python3
"""Cross-check CLI verdicts against the reference using committed crate bytes."""
from itertools import product
from pathlib import Path
import subprocess
import sys
import tempfile

HERE = Path(__file__).resolve().parent
EXAMPLE = HERE.parent
GO = HERE / "format-bridge"
REFERENCE = EXAMPLE / "python" / "crate_tool.py"
TIERS = ("byte-equal", "semantically equal", "voxel-equal", "different")


def result(command):
    run = subprocess.run(command, capture_output=True, text=True)
    tier = next((x for x in TIERS if run.stdout.startswith(x)), None)
    return run.returncode, tier, run.stdout + run.stderr


def main():
    fixtures = sorted((EXAMPLE / "fixtures").glob("*.crate"))
    count = 0
    for calibration in ([], ["--calibration", str(EXAMPLE / "fixtures" / "vizi-crate-cal.bin")]):
        for a, b in product(fixtures, repeat=2):
            args = ["compare", str(a), str(b), *calibration]
            go = result([str(GO), *args])
            py = result([sys.executable, str(REFERENCE), *args])
            assert go[:2] == py[:2], (a, b, calibration, go, py)
            count += 1
    print(f"{count} fixture comparisons agree with the reference (with/without calibration)")
    with tempfile.TemporaryDirectory(prefix="go-crate-interop-") as tmp:
        output = str(Path(tmp) / "go.crate")
        subprocess.run([str(GO), "recreate", str(EXAMPLE / "fixtures" / "sample.crate"), output], check=True)
        for peer in sorted((EXAMPLE / "interop").glob("*.crate")):
            for command in ([str(GO)], [sys.executable, str(REFERENCE)]):
                r = result([*command, "compare", output, str(peer)])
                assert r[0] == 0 and r[1] in TIERS[:2], (peer, r)
            print(f"both comparators accept {peer.name}")
    for args in (["compare"], ["compare", "a", "b", "--bogus"], ["unknown"]):
        assert result([str(GO), *args])[0] == 1
    print("interop and CLI exit checks passed")


if __name__ == "__main__":
    main()
