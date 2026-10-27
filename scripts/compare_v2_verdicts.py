#!/usr/bin/env python3
"""Compare the doorbell v2 verdicts of every implementation that produced them.

    uv run scripts/compare_v2_verdicts.py corpus.json go.json rust.json java.json ...
    uv run scripts/compare_v2_verdicts.py ... --out four_way.json

Each argument is a corpus or a verdicts file: an object with ``scenarios``,
each scenario naming itself and carrying one array per implementation that
replayed it.  Columns are merged by scenario name and every pair is compared.

``--out`` writes the merged corpus back out.  Handing that to each SDK's own
replayer is what makes its test compare against all three others rather than
only against whichever column it was first given.

**This tool grades no implementation against another.** It reports where the
columns differ and returns non-zero only for a difference the specification
settles.  Which differences it does not settle is data, not judgement: the
``unsettled`` list the corpus carries, from ``polyglot/unsettled.json``.
"""

from __future__ import annotations

import json
import sys
from itertools import combinations

# Keys that describe a scenario rather than grade it.
STRUCTURE = {"name", "lifecycle", "records", "fds_per_record", "terminal"}


def canonical(v: dict) -> tuple:
    """The comparable part of a verdict, with the labels the documents leave
    to each implementation folded together.  A disposition the specification
    only names in prose is not wire visible, so comparing spellings would
    report a divergence that does not exist."""
    kind = v["type"]
    if kind == "frame":
        disposition = v["disposition"]
        if disposition == "unknown_op":
            disposition = "unknown_opcode"
        return ("frame", disposition, v.get("where") or "",
                bool(v.get("grant_failure")), v.get("implicitly_closed") or 0)
    if kind == "cancellation":
        return ("cancellation", v["interaction"])
    return ("fault", v["class"])


def show(verdict: tuple) -> str:
    if verdict[0] == "frame":
        _, disposition, where, grant, closed = verdict
        extra = "".join(filter(None, [
            f" where={where}" if where else "",
            " grant_ack" if grant else "",
            f" closed={closed}" if closed else "",
        ]))
        return f"{disposition}{extra}"
    return f"{verdict[0]}({verdict[1]})"


def merge(paths: list[str]) -> tuple[dict[str, dict[str, list]], list, list[str]]:
    """Fold every file's columns into one table keyed by scenario name."""
    table: dict[str, dict[str, list]] = {}
    order: list[str] = []
    unsettled: list = []
    for path in paths:
        with open(path, encoding="utf-8") as fh:
            blob = json.load(fh)
        unsettled = unsettled or blob.get("unsettled", [])
        for scenario in blob["scenarios"]:
            name = scenario["name"]
            if name not in table:
                table[name] = {}
                order.append(name)
            for key, value in scenario.items():
                if key in STRUCTURE:
                    continue
                column = [canonical(v) for v in value]
                if key in table[name] and table[name][key] != column:
                    raise SystemExit(f"{path}: {name}: two different {key!r} columns")
                table[name][key] = column
    return table, unsettled, order


def columns_of(paths: list[str], name: str) -> dict[str, list]:
    """Every implementation's raw verdicts for one scenario, as written."""
    found: dict[str, list] = {}
    for path in paths:
        with open(path, encoding="utf-8") as fh:
            for scenario in json.load(fh)["scenarios"]:
                if scenario["name"] != name:
                    continue
                for key, value in scenario.items():
                    if key not in STRUCTURE:
                        found[key] = value
    return found


def subsequences(column: list) -> dict[str, list]:
    """Each verdict kind on its own, in order."""
    kinds: dict[str, list] = {}
    for verdict in column:
        kinds.setdefault(verdict[0], []).append(verdict)
    return kinds


def differences(mode: str, a: list, b: list) -> list[str]:
    """What separates two columns under this scenario's comparison mode."""
    if mode == "interleaving":
        # The documents fix each kind's own order and not the interleaving
        # between kinds, so compare the kinds separately.
        left, right = subsequences(a), subsequences(b)
        trouble = []
        for kind in sorted(set(left) | set(right)):
            one, two = left.get(kind, []), right.get(kind, [])
            if one != two:
                trouble.append(f"{kind}: {' '.join(map(show, one))} | {' '.join(map(show, two))}")
        return trouble
    trouble = []
    if len(a) != len(b):
        trouble.append(f"verdict count {len(a)} vs {len(b)}")
    for i, (one, two) in enumerate(zip(a, b)):
        if one != two:
            trouble.append(f"{i}: {show(one)} vs {show(two)}")
    for i in range(min(len(a), len(b)), max(len(a), len(b))):
        only = a[i] if len(a) > len(b) else b[i]
        trouble.append(f"{i}: only one side has {show(only)}")
    return trouble


def main(argv: list[str]) -> int:
    # --require-all: a scenario some column did not replay is a FAILURE, not a
    # note.  Without it a replayer that skips (Go's does when no SEQPACKET pair
    # is available) writes an empty column and the comparison passes.  Off by default so a manual
    # comparison of a subset still works.
    require_all = "--require-all" in argv
    argv = [a for a in argv if a != "--require-all"]
    out = None
    if "--out" in argv:
        at = argv.index("--out")
        out = argv[at + 1]
        argv = argv[:at] + argv[at + 2:]
    if not argv:
        print(__doc__)
        return 2
    table, unsettled, order = merge(argv)
    if out:
        with open(argv[0], encoding="utf-8") as fh:
            merged = json.load(fh)
        for scenario in merged["scenarios"]:
            for who, column in columns_of(argv[1:], scenario["name"]).items():
                scenario[who] = column
        merged["unsettled"] = unsettled
        with open(out, encoding="utf-8", mode="w") as fh:
            json.dump(merged, fh, indent=1)
        print(f"wrote {out}\n")
    open_questions = {entry["scenario"]: entry for entry in unsettled}

    implementations = sorted({key for columns in table.values() for key in columns})
    if len(implementations) < 2:
        # Folding one implementation's column into a --scenarios-only corpus is
        # a legitimate use; there is simply nothing yet to compare it against.
        if out:
            print(f"only {implementations[0] if implementations else 'no'} column(s); nothing to compare yet")
            return 0
        raise SystemExit(f"need at least two columns to compare, found {implementations}")
    print(f"comparing {', '.join(implementations)} over {len(order)} scenarios\n")

    settled_failures = 0
    unreplayed = 0
    reported = 0
    for name in order:
        columns = table[name]
        entry = open_questions.get(name)
        mode = (entry or {}).get("compare", "strict")
        missing = [i for i in implementations if i not in columns]
        pairs = [(a, b) for a, b in combinations(sorted(columns), 2)
                 if differences(mode, columns[a], columns[b])]
        counts = {len(c) for c in columns.values()}
        status = "agree" if not pairs else "DIFFER"
        note = f"  [{mode}]" if mode != "strict" else ""
        print(f"{status:>6}  {name:<28} {max(counts)} verdicts, {len(columns)} columns{note}")
        if missing:
            print(f"          not replayed by: {', '.join(missing)}")
            unreplayed += len(missing)
        for a, b in pairs:
            for line in differences(mode, columns[a], columns[b]):
                print(f"          {a} vs {b}: {line}")
            if mode == "report":
                reported += 1
            else:
                settled_failures += 1

    if open_questions:
        print("\nOpen specification questions this comparison does not decide:")
        for entry in unsettled:
            print(f"\n  {entry['scenario']}  ({entry['spec']}, compare={entry.get('compare', 'strict')})")
            print(f"    {entry['question']}")
            for reading, who in entry["split"].items():
                print(f"      {', '.join(who)}: {reading}")
            if entry.get("cost"):
                print(f"    {entry['cost']}")

    print()
    if require_all and unreplayed:
        print(f"{unreplayed} scenario column(s) not replayed, and --require-all is set")
        return 1
    if settled_failures:
        print(f"{settled_failures} divergence(s) on facts the documents settle")
        return 1
    print(f"no divergence on any fact the documents settle"
          + (f"; {reported} reported under an open question" if reported else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
