#!/usr/bin/env python3
"""Write ``locks/java-runtime.json``: the Java runtime a release's bootstrap fetches,
pinned per platform by URL, size and SHA-256.

    python3 tools/lock_runtime.py                  # the newest Temurin 25 JRE
    python3 tools/lock_runtime.py --release jdk-25.0.5+8

A developer runs it, never the bootstrap: regenerating the lock is a dependency
update, committed like any other. It reads Eclipse Adoptium's API
(api.adoptium.net) and checks every pin against the ``.sha256.txt`` file
published beside each archive, so the API and the release page agree.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LOCK = ROOT / "locks" / "java-runtime.json"
API = "https://api.adoptium.net/v3"
#: The catalog's platforms, and how Adoptium names each one.
PLATFORMS = {
    "linux-x86_64": ("linux", "x64"),
    "macos-arm64": ("mac", "aarch64"),
    "windows-x86_64": ("windows", "x64"),
}
FEATURE = 25


def get(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": "vizi-plugin-lock-runtime"})
    with urllib.request.urlopen(request, timeout=60) as response:
        return response.read()


def asset(os_name: str, arch: str, release: str | None) -> dict:
    query = urllib.parse.urlencode({"image_type": "jre", "os": os_name, "architecture": arch, "vendor": "eclipse"})
    if release:
        named = json.loads(get(f"{API}/assets/release_name/eclipse/{urllib.parse.quote(release)}?{query}&project=jdk"))
        found = [{"binary": b, "version": named["version_data"]} for b in named["binaries"]]
    else:
        found = json.loads(get(f"{API}/assets/latest/{FEATURE}/hotspot?{query}"))
    binaries = [a for a in found if a["binary"]["image_type"] == "jre" and a["binary"]["os"] == os_name
                and a["binary"]["architecture"] == arch]
    if len(binaries) != 1:
        sys.exit(f"lock_runtime: expected one JRE for {os_name} {arch}, found {len(binaries)}")
    return binaries[0]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--release", help="an Adoptium release name, such as jdk-25.0.5+8 (default: the newest)")
    args = ap.parse_args()
    platforms, versions = {}, set()
    for token, (os_name, arch) in PLATFORMS.items():
        found = asset(os_name, arch, args.release)
        package = found["binary"]["package"]
        published = get(package["checksum_link"]).decode().split()[0]
        if published != package["checksum"]:
            sys.exit(f"lock_runtime: {package['name']}: the API's SHA-256 is not the published one")
        name = package["name"]
        platforms[token] = {
            "url": package["link"],
            "size": package["size"],
            "sha256": package["checksum"],
            "archive": "zip" if name.endswith(".zip") else "tar.gz",
        }
        versions.add(found["version"]["openjdk_version"])
        print(f"{token}: {name}, {package['size'] / 1e6:.1f} MB, sha256 {package['checksum'][:16]}…")
    if len(versions) != 1:
        sys.exit(f"lock_runtime: the platforms' builds differ: {sorted(versions)}")
    lock = {
        "runtime": "Eclipse Temurin JRE",
        "version": versions.pop().removesuffix("-LTS"),
        "license": "GPL-2.0 with the Classpath Exception",
        "platforms": platforms,
    }
    LOCK.parent.mkdir(exist_ok=True)
    LOCK.write_text(json.dumps(lock, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {LOCK.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
