#!/usr/bin/env python3
"""Build the release zip of a Java plugin: ``dist/<plugin id>-<version>.zip``.

    ./mill --no-daemon releaseTree                       # lib/: plugin.jar and every dependency jar
    python3 tools/make_release.py --tree out/releaseTree.dest

The zip holds one folder named for the plugin id, with what a fresh machine needs
and nothing else:

- from git (``HEAD``): ``vizi-plugin.json``, ``.python-version``, ``scripts/bootstrap.py``,
  ``scripts/doctor.py``, ``locks/java-runtime.json``, ``README.md``, ``LICENSE``, ``NOTICE``
  and ``licenses/``;
- from the build tree: ``lib/plugin.jar``, every dependency jar exactly as resolved, and
  ``lib/SHA256SUMS``, which this script rewrites.

**The same commit gives the same bytes on any machine.** Dependency jars are copied
unchanged. ``plugin.jar`` is rewritten with its entries in a fixed order, the earliest
timestamp a zip can record, and no compression, because a build tool stamps the time and
compressors differ between machines. The zip is written the same way. Its SHA-256 is what
the catalog pins, and anyone can check a release by rebuilding it.

With ``--java`` (default: ``$JAVA_HOME/bin/java``), the plugin's own ``--manifest`` is run
and must equal the committed ``vizi-plugin.json``.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FROM_GIT = ("vizi-plugin.json", ".python-version", "scripts/bootstrap.py", "scripts/doctor.py",
            "locks/java-runtime.json", "README.md", "LICENSE", "NOTICE", "licenses/")
#: The earliest time a zip can record; no file's own time reaches the archive.
EPOCH = (1980, 1, 1, 0, 0, 0)
FILE_MODE = 0o100644


def _git(*args: str) -> bytes:
    return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, check=True).stdout


def _entry(name: str) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(name, date_time=EPOCH)
    info.compress_type = zipfile.ZIP_STORED
    info.create_system = 3  # the host OS field: otherwise Windows and Unix differ
    info.external_attr = FILE_MODE << 16
    return info


def normalized_jar(data: bytes) -> bytes:
    """The jar's entries, manifest first and the rest sorted, at a fixed time, stored."""
    with zipfile.ZipFile(io.BytesIO(data)) as jar:
        names = [i.filename for i in jar.infolist() if not i.is_dir()]
        first = [n for n in ("META-INF/MANIFEST.MF",) if n in names]
        out = io.BytesIO()
        with zipfile.ZipFile(out, "w", zipfile.ZIP_STORED) as new:
            for name in first + sorted(n for n in names if n not in first):
                new.writestr(_entry(name), jar.read(name))
    return out.getvalue()


def pack(out: Path, top: str, files: dict[str, bytes]) -> None:
    with zipfile.ZipFile(out, "w", zipfile.ZIP_STORED) as zf:
        for name in sorted(files):
            zf.writestr(_entry(f"{top}/{name}"), files[name])


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--tree", type=Path, required=True, help="the build's release tree, holding lib/")
    ap.add_argument("--java", help="a java to run the plugin's --manifest with (default: $JAVA_HOME/bin/java)")
    args = ap.parse_args()

    dirty = _git("status", "--porcelain", "--", *FROM_GIT).decode()
    if dirty.strip():
        sys.exit(f"uncommitted changes in shipped files:\n{dirty}")
    names = [n for n in _git("ls-tree", "-r", "--name-only", "-z", "HEAD", "--", *FROM_GIT).decode().split("\0") if n]
    files = {name: _git("cat-file", "blob", f"HEAD:{name}") for name in names}
    for required in ("vizi-plugin.json", ".python-version", "scripts/bootstrap.py", "locks/java-runtime.json"):
        if required not in files:
            sys.exit(f"{required} is not committed")

    lib = args.tree / "lib"
    jars = sorted(p for p in lib.glob("*.jar"))
    if not (lib / "plugin.jar").is_file():
        sys.exit(f"{lib}/plugin.jar is missing: build the release tree first")
    for jar in jars:
        data = jar.read_bytes()
        files[f"lib/{jar.name}"] = normalized_jar(data) if jar.name == "plugin.jar" else data
    files["lib/SHA256SUMS"] = "".join(
        f"{hashlib.sha256(files[f'lib/{j.name}']).hexdigest()}  {j.name}\n" for j in jars).encode()

    manifest = json.loads(files["vizi-plugin.json"])
    java = args.java or (str(Path(os.environ["JAVA_HOME"]) / "bin" / "java") if os.environ.get("JAVA_HOME") else None)
    if java:
        command = manifest["entry"]["command"]
        with tempfile.TemporaryDirectory() as tmp:
            staged = Path(tmp) / "plugin"
            for name, data in files.items():
                (staged / name).parent.mkdir(parents=True, exist_ok=True)
                (staged / name).write_bytes(data)
            out = Path(tmp) / "manifest"
            subprocess.run([java, *command[1:], f"--manifest={out}"], cwd=staged, check=True, capture_output=True)
            if json.loads((out / "vizi-plugin.json").read_text(encoding="utf-8")) != manifest:
                sys.exit("vizi-plugin.json is not what the plugin declares: run its --manifest and commit the result")

    out = ROOT / "dist" / f"{manifest['id']}-{manifest['version']}.zip"
    out.parent.mkdir(exist_ok=True)
    pack(out, manifest["id"], files)
    print(f"wrote {out} ({len(files)} files, {out.stat().st_size / 1e6:.1f} MB)")
    print(f"sha256 {hashlib.sha256(out.read_bytes()).hexdigest()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
