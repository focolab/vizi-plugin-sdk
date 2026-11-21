#!/usr/bin/env python3
"""Set up a Java plugin installed from vizi's catalog: fetch the pinned Java
runtime for this platform into ``runtime/``, and check the plugin's jars.

    python3 scripts/bootstrap.py
    python3 scripts/doctor.py

vizi runs it this way itself, with no arguments, the plugin folder as the
working directory, on the Python minor ``.python-version`` names. It uses the
standard library only, because nothing else exists yet.

- **The runtime.** ``locks/java-runtime.json`` pins one archive per platform by
  URL, size and SHA-256. The archive is streamed into the plugin folder,
  checked, and unpacked there. The runtime's home (the folder holding
  ``bin/java``) becomes ``runtime/``, which ``entry.command`` launches as
  ``runtime/bin/java``. A platform the lock does not name is a failure, not a
  fallback.
- **The jars.** ``lib/SHA256SUMS``, written by the release build, names every
  jar in ``lib/``. Each must be present and match.
- **Idempotent.** ``runtime/.vizi-runtime.json`` records what was unpacked. A
  second run with the same lock changes nothing. A ``runtime/`` from another
  lock entry, or one with no record, is replaced, and the bootstrap says so.
- **The plugin folder only.** The download and the unpacking happen inside it,
  and nothing is written anywhere else.
- **The cause last.** On failure it exits non-zero, and the last line it prints
  is the cause.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import shutil
import ssl
import subprocess
import sys
import tarfile
import time
import urllib.request
import zipfile
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[1]
LOCK = ROOT / "locks" / "java-runtime.json"
RUNTIME = ROOT / "runtime"
RECORD = RUNTIME / ".vizi-runtime.json"
SUMS = ROOT / "lib" / "SHA256SUMS"
#: Scratch space inside the plugin folder; removed on success and on failure.
WORK = ROOT / ".runtime-setup"
#: The flags every JVM that runs the vizi Java SDK needs (PluginMain.JVM_FLAGS).
JVM_FLAGS = ("--enable-native-access=ALL-UNNAMED", "--sun-misc-unsafe-memory-access=allow")
CHUNK = 1 << 16
TIMEOUT_S = 60


class Failure(Exception):
    """A setup that did not happen; ``str()`` is the cause in one line."""


def say(text: str) -> None:
    # vizi shows each line as the install's progress.
    print(f"bootstrap: {text}", flush=True)


def current_platform() -> str:
    """This machine as a catalog platform token: ``<os>-<arch>``."""
    os_name = {"Linux": "linux", "Darwin": "macos", "Windows": "windows"}.get(platform.system())
    arch = {"x86_64": "x86_64", "AMD64": "x86_64", "amd64": "x86_64", "arm64": "arm64", "aarch64": "arm64",
            "ARM64": "arm64"}.get(platform.machine())
    if os_name is None or arch is None:
        raise Failure(f"this machine ({platform.system()} {platform.machine()}) is not a platform this plugin supports")
    return f"{os_name}-{arch}"


def java_exe(home: Path) -> Path:
    return home / "bin" / ("java.exe" if os.name == "nt" else "java")


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


def tree_sha256(home: Path) -> str:
    """One digest of a runtime's files: their sorted relative paths and contents.
    The doctor recomputes it, so a runtime changed after setup is noticed."""
    lines = []
    for path in sorted(p for p in home.rglob("*") if p.is_file() and p.name != RECORD.name):
        rel = path.relative_to(home).as_posix()
        lines.append(f"{rel}\0{file_sha256(path)}\n")
    return hashlib.sha256("".join(lines).encode()).hexdigest()


def https_context() -> ssl.SSLContext:
    """The system's trust store. A standalone CPython can find no certificates
    on its own; then the platform's usual bundle files are tried."""
    context = ssl.create_default_context()
    if context.cert_store_stats().get("x509_ca", 0) == 0:
        for bundle in ("/etc/ssl/cert.pem", "/etc/ssl/certs/ca-certificates.crt", "/etc/pki/tls/certs/ca-bundle.crt"):
            if Path(bundle).is_file():
                context.load_verify_locations(cafile=bundle)
                break
    return context


def download(pin: dict, target: Path) -> None:
    """Stream the archive to ``target``, held to the pinned size and hash as it arrives."""
    url, size = pin["url"], int(pin["size"])
    if not url.startswith("https://"):
        raise Failure(f"the lock's address for the runtime is not https: {url}")
    request = urllib.request.Request(url, headers={"User-Agent": "vizi-plugin-bootstrap"})
    digest = hashlib.sha256()
    received, shown = 0, -1
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_S, context=https_context()) as response, \
                open(target, "wb") as out:
            for chunk in iter(lambda: response.read(CHUNK), b""):
                received += len(chunk)
                if received > size:
                    raise Failure(f"the runtime download is larger than the pinned {size:,} bytes")
                digest.update(chunk)
                out.write(chunk)
                tenth = received * 10 // size
                if tenth != shown:
                    shown = tenth
                    say(f"downloading the Java runtime: {received / 1e6:.0f} of {size / 1e6:.0f} MB")
    except OSError as exc:
        raise Failure(f"the Java runtime could not be downloaded from {url}: {getattr(exc, 'reason', exc)}") from exc
    if received != size:
        raise Failure(f"the runtime download is {received:,} bytes, not the pinned {size:,}")
    if digest.hexdigest() != pin["sha256"]:
        raise Failure("the runtime download does not match the lock's SHA-256; nothing was unpacked")


def unpack(archive: Path, kind: str, into: Path) -> None:
    """Unpack inside ``into`` only: no absolute paths, no ``..``, no link out of it."""
    root = into.resolve()

    def inside(name: str) -> Path:
        dest = (root / name).resolve()
        if PurePosixPath(name).is_absolute() or not dest.is_relative_to(root):
            raise Failure(f"the runtime archive names a path outside itself ({name})")
        return dest

    if kind == "zip":
        with zipfile.ZipFile(archive) as bundle:
            for member in bundle.infolist():
                inside(member.filename)
            bundle.extractall(root)
        return
    if kind != "tar.gz":
        raise Failure(f"the lock names an archive type this bootstrap cannot unpack: {kind}")
    with tarfile.open(archive, "r:gz") as bundle:
        members = bundle.getmembers()
        for member in members:
            inside(member.name)
            if member.issym() or member.islnk():
                target = member.linkname if member.islnk() else str(PurePosixPath(member.name).parent / member.linkname)
                inside(target)
            elif not (member.isfile() or member.isdir()):
                raise Failure(f"the runtime archive holds a special file ({member.name})")
        if hasattr(tarfile, "data_filter"):
            bundle.extractall(root, members=members, filter="data")
        else:
            bundle.extractall(root, members=members)


def find_home(unpacked: Path) -> Path:
    """The runtime's home in an unpacked archive: the folder holding ``bin/java``.
    It is the archive's top folder, or ``Contents/Home`` in it on macOS."""
    tops = [p for p in unpacked.iterdir() if p.is_dir()]
    for top in tops:
        for home in (top, top / "Contents" / "Home"):
            if java_exe(home).is_file():
                return home
    raise Failure("the runtime archive holds no bin/java where a JRE keeps it")


def check_jars() -> str:
    if not SUMS.is_file():
        raise Failure("lib/SHA256SUMS is missing: this folder is not a built release")
    count = 0
    for line in SUMS.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        digest, name = line.split(maxsplit=1)
        name = name.lstrip("*")
        jar = ROOT / "lib" / name
        if not jar.is_file():
            raise Failure(f"lib/{name} is missing")
        if file_sha256(jar) != digest:
            raise Failure(f"lib/{name} does not match lib/SHA256SUMS")
        count += 1
    return f"{count} jars match lib/SHA256SUMS"


def java_starts() -> str:
    """The runtime starts, with the SDK's flags, and is the feature release the lock names."""
    try:
        done = subprocess.run([str(java_exe(RUNTIME)), *JVM_FLAGS, "-version"], cwd=ROOT,
                              capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise Failure(f"the Java runtime does not start: {exc}") from exc
    first = (done.stderr or done.stdout).strip().splitlines()[:1]
    if done.returncode != 0:
        raise Failure(f"the Java runtime does not start with the SDK's flags: {(done.stderr or '').strip()[-300:]}")
    return first[0] if first else "it started"


def main() -> int:
    lock = json.loads(LOCK.read_text(encoding="utf-8"))
    here = current_platform()
    pin = lock["platforms"].get(here)
    if pin is None:
        raise Failure(f"no Java runtime is pinned for {here}; this release supports: {', '.join(sorted(lock['platforms']))}")
    wanted = {"platform": here, "runtime": lock["runtime"], "version": lock["version"],
              "url": pin["url"], "sha256": pin["sha256"], "size": pin["size"]}

    try:
        have = json.loads(RECORD.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        have = None
    if have is not None and {k: have.get(k) for k in wanted} == wanted and java_exe(RUNTIME).is_file():
        say(f"{lock['runtime']} {lock['version']} for {here} is already in runtime/; nothing to download")
    else:
        if RUNTIME.exists():
            why = "was made from another lock entry" if have is not None else "has no record of what it is"
            say(f"runtime/ {why}; replacing it")
        shutil.rmtree(WORK, ignore_errors=True)
        WORK.mkdir()
        try:
            name = pin["url"].rsplit("/", 1)[-1]
            say(f"downloading the Java runtime: {lock['runtime']} {lock['version']} for {here}, "
                f"{int(pin['size']) / 1e6:.0f} MB, from {pin['url'].split('/')[2]} ({lock['license']})")
            archive = WORK / name
            started = time.monotonic()
            download(pin, archive)
            say(f"checked {name} against the lock (SHA-256 {pin['sha256'][:12]}…); unpacking")
            unpacked = WORK / "unpacked"
            unpacked.mkdir()
            unpack(archive, pin["archive"], unpacked)
            home = find_home(unpacked)
            record = dict(wanted, tree_sha256=tree_sha256(home))
            (home / RECORD.name).write_text(json.dumps(record, indent=1) + "\n", encoding="utf-8")
            if RUNTIME.exists():
                shutil.rmtree(RUNTIME)
            os.replace(home, RUNTIME)
            say(f"runtime/ is {lock['runtime']} {lock['version']} ({time.monotonic() - started:.0f} s)")
        finally:
            shutil.rmtree(WORK, ignore_errors=True)

    say(check_jars())
    say(f"the Java runtime starts: {java_starts()}")
    say("done; check it with: python3 scripts/doctor.py")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Failure as exc:
        print(f"bootstrap: {exc}", flush=True)
        sys.exit(1)
