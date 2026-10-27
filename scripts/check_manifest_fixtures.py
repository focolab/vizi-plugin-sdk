"""Run the repository's manifest fixtures through the reference parser.

``polyglot/manifest_fixtures/*.json`` hold manifest cases the Go, Rust and
Java parsers' tests also read.  This checks that the reference SDK's
``parse_manifest`` gives the outcome each case states, so the ports are held
to what the reference does rather than to a second reading of it.

Cases the reference does not yet give are listed in ``PENDING_VIZI``: they
are reported, not failed, and a pending case that now passes is reported so
it can leave the list.

    python -I -B scripts/check_manifest_fixtures.py
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from vizi_plugin_sdk import manifest as reference  # noqa: E402


#: (file, case name) -> what the reference parser does not give yet.  An
#: entry leaves this list when the parser gives the case.
PENDING_VIZI: dict[tuple[str, str], str] = {}


def failure(path: Path, case: dict, host: str) -> str | None:
    """Why the reference does not give the case's stated outcome, or None."""
    name, expected = case["name"], case["expected"]
    try:
        reference.parse_manifest(case["json"])
    except reference.EnginesIncompatible as e:
        outcome, message = "engines_incompatible", str(e)
    except reference.ManifestError as e:
        outcome, message = "invalid", str(e)
    else:
        outcome, message = "accepted", ""
    if outcome != expected["kind"]:
        return f"{path.name}: {name}: {outcome} ({message!r}), expected {expected['kind']}"
    if outcome == "engines_incompatible" and message != expected["message"]:
        return f"{path.name}: {name}: {message!r}, expected {expected['message']!r}"
    if outcome == "invalid" and expected["message"] not in message:
        return f"{path.name}: {name}: {message!r} does not contain {expected['message']!r}"
    return None


def main() -> int:
    # The reference logs ignored names; the outcome is what is checked here.
    logging.disable(logging.WARNING)
    paths = sorted((ROOT / "polyglot" / "manifest_fixtures").glob("*.json"))
    if not paths:
        print("no manifest fixtures found", file=sys.stderr)
        return 1
    host = f"{reference.HOST_API_MAJOR}.{reference.HOST_API_MINOR}"
    failures, pending, cleared, cases = [], [], [], 0
    seen: set[tuple[str, str]] = set()
    for path in paths:
        document = json.loads(path.read_text(encoding="utf-8"))
        if document["protocol"] != host:
            failures.append(f"{path.name}: written for protocol {document['protocol']}, the reference speaks {host}")
        for case in document["cases"]:
            cases += 1
            key = (path.name, case["name"])
            seen.add(key)
            problem = failure(path, case, host)
            if key in PENDING_VIZI:
                (pending if problem else cleared).append(f"{path.name}: {case['name']}: {PENDING_VIZI[key]}")
            elif problem:
                failures.append(problem)
    failures += [f"pending case {f}: {n!r} is not a fixture" for f, n in sorted(set(PENDING_VIZI) - seen)]
    for line in pending:
        print(f"pending vizi: {line}")
    for line in cleared:
        print(f"pending vizi, now passes (remove it from PENDING_VIZI): {line}")
    for line in failures:
        print(line, file=sys.stderr)
    print(f"{cases} manifest fixture cases in {len(paths)} files; {len(failures)} failed, {len(pending)} pending vizi")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
