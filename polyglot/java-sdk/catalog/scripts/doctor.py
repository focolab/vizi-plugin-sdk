#!/usr/bin/env python3
"""Check that this Java plugin is ready for vizi. Read-only; ends with one
ready / not-ready line and exits non-zero when not ready.

    python3 scripts/doctor.py

Run it with any Python 3.11 or later, after ``scripts/bootstrap.py``. It checks:

- the runtime: the lock entry for this platform, the bootstrap's record, the
  unpacked files' digest, and the runtime's own version;
- the jars: every one in ``lib/SHA256SUMS`` is present and matches;
- the manifest: ``vizi-plugin.json`` launches ``runtime/bin/java`` with the SDK's
  flags, and the jar it names is in ``lib/``;
- the plugin: the JVM starts with the flags, and the plugin's own ``--manifest``
  (written to a temporary folder) is the shipped ``vizi-plugin.json``. That
  proves the classpath, the SDK and the declarations together, and prints the
  SDK version.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

sys.dont_write_bytecode = True  # read-only: no __pycache__ in the plugin folder
sys.path.insert(0, str(Path(__file__).resolve().parent))
import bootstrap as b  # noqa: E402 -- the same paths, pins and checks the bootstrap uses

FAILED: list[str] = []


def check(name: str, fn) -> None:
    try:
        detail = fn()
        print(f"pass  {name}" + (f": {detail}" if detail else ""))
    except Exception as exc:  # noqa: BLE001 -- every failure is reported, none stops the rest
        FAILED.append(name)
        print(f"FAIL  {name}: {exc}")


def main() -> int:
    shipped: dict = {}

    def runtime():
        lock = json.loads(b.LOCK.read_text(encoding="utf-8"))
        here = b.current_platform()
        pin = lock["platforms"].get(here)
        if pin is None:
            raise RuntimeError(f"no runtime is pinned for {here}")
        record = json.loads(b.RECORD.read_text(encoding="utf-8"))
        for key in ("platform", "version", "url", "sha256", "size"):
            want = here if key == "platform" else lock["version"] if key == "version" else pin[key]
            if record.get(key) != want:
                raise RuntimeError(f"runtime/ was set up for {record.get(key)!r}, and the lock says {want!r}; run the bootstrap")
        if b.tree_sha256(b.RUNTIME) != record.get("tree_sha256"):
            raise RuntimeError("runtime/'s files changed after setup; delete runtime/ and run the bootstrap")
        release = dict(line.split("=", 1) for line in (b.RUNTIME / "release").read_text().splitlines() if "=" in line)
        java_version = release.get("JAVA_VERSION", "").strip('"')
        if java_version.split(".")[0] != lock["version"].split(".")[0]:
            raise RuntimeError(f"runtime/release says Java {java_version}, not {lock['version']}")
        return (f"{lock['runtime']} {lock['version']} for {here} (archive sha256 {pin['sha256'][:12]}…, "
                f"files unchanged), {release.get('IMPLEMENTOR', '').strip(chr(34))} Java {java_version}")

    def jars():
        return b.check_jars()

    def manifest():
        nonlocal shipped
        shipped = json.loads((b.ROOT / "vizi-plugin.json").read_text(encoding="utf-8"))
        if shipped.get("manifest_version") != 1:
            raise RuntimeError(f"manifest_version is {shipped.get('manifest_version')!r}, not 1")
        engines = shipped.get("engines", {}).get("vizi", "")
        if engines.split(".")[0] != "1":
            raise RuntimeError(f"engines.vizi {engines!r} is not protocol 1.x")
        command = shipped.get("entry", {}).get("command", [])
        if command[:1] != ["runtime/bin/java"]:
            raise RuntimeError(f"entry.command starts {command[:1]}, not runtime/bin/java")
        missing = [flag for flag in b.JVM_FLAGS if flag not in command]
        if missing:
            raise RuntimeError(f"entry.command lacks {' '.join(missing)}")
        if "-jar" not in command or command.index("-jar") + 1 >= len(command):
            raise RuntimeError("entry.command names no jar (-jar lib/<plugin>.jar)")
        jar = b.ROOT / command[command.index("-jar") + 1]
        if not jar.is_file():
            raise RuntimeError(f"entry.command's jar {jar.relative_to(b.ROOT)} is not there")
        return f"{shipped['id']} {shipped['version']}, engines.vizi {engines}, min_vizi {shipped.get('min_vizi')}"

    def jvm():
        return b.java_starts()

    def plugin():
        command = shipped["entry"]["command"]
        with tempfile.TemporaryDirectory() as out:
            done = subprocess.run([str(b.java_exe(b.RUNTIME)), *command[1:], f"--manifest={out}"], cwd=b.ROOT,
                                  capture_output=True, text=True, timeout=300)
            if done.returncode != 0:
                raise RuntimeError(f"--manifest failed: {(done.stderr or done.stdout).strip()[-300:]}")
            built = json.loads((Path(out) / "vizi-plugin.json").read_text(encoding="utf-8"))
        if built != shipped:
            raise RuntimeError("the plugin's --manifest is not the shipped vizi-plugin.json")
        return f"its --manifest is the shipped vizi-plugin.json; vizi Java SDK {built.get('sdk_version')}"

    check("runtime", runtime)
    check("jars", jars)
    check("manifest", manifest)
    check("JVM with the SDK's flags", jvm)
    if shipped:
        check("plugin", plugin)
    print("doctor: ready" if not FAILED else f"doctor: NOT ready ({', '.join(FAILED)})")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
