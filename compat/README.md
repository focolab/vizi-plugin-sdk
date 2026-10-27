# The 1.0 protocol baseline

`vizi_plugin-1.0.binpb` is the protocol's `FileDescriptorSet` (`roi.proto` and
`vizi_plugin.proto`) as the Python SDK compiles it. It is the contract a
plugin built against protocol 1.0 relies on, in the form a machine can compare.

## The check

`.github/workflows/compat.yml` runs `buf breaking` on every change to `proto/`
or the compiled stubs, against this file, with the rules in `buf.yaml`: buf's
`WIRE` category plus `SERVICE_NO_DELETE` and `RPC_NO_DELETE`. It fails when a
change would break a plugin built against 1.0 on a later 1.x host:

- a field or an enum value deleted without reserving its number;
- a field whose type or cardinality changes in a way the wire cannot carry;
- a field moved into or out of a oneof;
- a package change;
- a service or an RPC deleted, or an RPC whose request, response or
  streaming changes.

An added field, enum value, message or RPC passes. Renaming a field passes
too: the wire carries numbers, not names, although a rename is still a source
change for anyone generating code from `proto/`.

**Why buf rather than a hand-written descriptor diff:** the rules above are
the ones protobuf's own compatibility guidance gives, and buf implements and
maintains each of them, oneofs and cardinality included. A descriptor diff
written here would be a second implementation of those rules with no one else
testing it. The binary is pinned by version and SHA-256 in the workflow, so a
buf release cannot change what the check means.

To run it locally:

```bash
buf breaking . --against compat/vizi_plugin-1.0.binpb --config compat/buf.yaml
```

## Producing the baseline

```bash
python compat/make_descriptor_set.py compat/vizi_plugin-1.0.binpb
```

It reads the compiled modules in `vizi_plugin_sdk/_proto/`, which
`scripts/regen-stubs.sh --check` keeps equal to `proto/`. Source comments are
not in a compiled module and are not part of the contract.

## The baseline is frozen

The workflow also checks the file against the SHA-256 pinned in it, so the
baseline itself cannot change unnoticed: a later minor never edits it. A
later minor that adds to the protocol passes the check because additions are
allowed. A major version gets a new baseline file beside this one; this one
is not replaced.
