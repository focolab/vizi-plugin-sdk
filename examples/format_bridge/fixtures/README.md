# Crate format fixtures

The interop anchor for [`../SPEC.md`](../SPEC.md). Bytes, not prose: two
implementations that disagree about the format produce files that fail to open,
and these are what they fail against.

Regenerate with the pinned recipe:

```bash
python3 examples/format_bridge/fixtures/make_fixtures.py
```

Pure standard library on purpose — an implementor in any language must be able
to read `make_fixtures.py` and reproduce every byte, and a dependency they
cannot install is a fixture they cannot check.

## Contents

| File | Bytes | SHA-256 |
|---|---|---|
| `sample.crate` | 2162 | `5a60f7f496051d0ccccfe07a6fcbf8a12c543521b0b918a76f03333a17440c5f` |
| `sample-calibrated.crate` | 2162 | `b3ef9c3c2b7407247ae57396c17195180f2d3b76e953c9bb6bbb61724493ac1c` |
| `unfinalized.crate` | 1986 | `53af8200be28208b029ac56066047687c3ade705e42511193e6696d8c4ea60a5` |
| `badcrc.crate` | 2162 | `619bd5550eb1637e16884ca92a18a6be053d86e95eae853fc5a3e3d6299c8c46` |
| `nospacing.crate` | 1996 | `f387e5bb908c6d8e7bc8f83b44e91c756f131642bd63c5942d13fa48d43561b7` |
| `spacing-conflict.crate` | 2180 | `1552534d38447c0bd9063540178a635ee9092570646bf23a8cd0869031e2ba07` |
| `vizi-crate-cal.bin` | 32 | `20845d7cc64ec17ab899a993c1138352828823e55ebc1959f257a28e528671d3` |

**If a regenerated digest differs from the one above, the format changed and
`SPEC.md` is now wrong.** Fix one or the other before committing; the manifest
in `SPEC.md` declares `vizi-crate-cal.bin`'s size and digest to the host, which
verifies both and refuses the asset on a mismatch.

## What each one is for

- **`sample.crate`** — the reference crate. `T,C,Z,Y,X = 3,2,4,6,5`, u16,
  uncalibrated, an `ome-xml` metadata document, and blocks laid out in
  **reverse** canonical order so that a reader which assumes sequential layout
  reads the wrong volume every time. Voxel values are
  `10000*t + 1000*c + 100*z + 10*y + x`, so a wrong volume or a transposed axis
  is legible in the decimal digits of any voxel.

  Voxel size is `(z, y, x) = (2.0, 0.325, 0.325)` µm with a 1.5 s time
  increment — deliberately anisotropic, at a ratio ordinary confocal data
  actually has. The header fields and the document's `PhysicalSize*` attributes
  **agree by construction**.
- **`sample-calibrated.crate`** — the same logical volumes stored as biased
  sensor counts with the `CALIBRATED` flag set. Decoding it with the
  calibration table must reproduce `sample.crate` exactly.
- **`unfinalized.crate`** — header, metadata and blocks, with no index and no
  footer: what an aborted export leaves on disk. Must be rejected as
  *unfinished*, not as corrupt, and not by crashing on a short read.
- **`badcrc.crate`** — `sample.crate` with one bit flipped in block `i = 2`,
  `(t=1, c=0)`, and the index CRC left describing the original bytes. Reading
  that one block must fail; **every other block must still read cleanly.**
- **`nospacing.crate`** — zero spacing in the header and no `PhysicalSize`
  attributes in the document. The don't-panic path: it must open, display and
  export exactly like any other crate, reporting `(unknown)`. A plugin that
  errors here, or that substitutes `1.0`, has failed the fixture.
- **`spacing-conflict.crate`** — the header says `(2.0, 0.325, 0.325)` µm and
  1.5 s; the document says `(1.0, 1.0, 1.0)` µm and 0.5 s, expressing Z as
  `1000 nm` and `t` as `500 ms`. **The header wins**, and the reader never
  parses the document to find out. One file covers both the precedence rule and
  the unit conversion.
- **`vizi-crate-cal.bin`** — the manifest asset: offsets 7 and 13 for channels
  0 and 1. Provision it through Plugin Manager → **Import…**

## Adding a fixture

Extend `make_fixtures.py`, rerun it, and update both tables — the one above and
the one in `SPEC.md`. A fixture whose digest is recorded in only one place is a
fixture that will drift.
